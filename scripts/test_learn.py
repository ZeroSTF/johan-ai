"""Tests for johan_learn — corrections become behaviour rules.

Model and database are stubbed: nothing here calls Ollama, touches the
memories table or writes under brain/.

    python test_learn.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import johan_learn as jl            # noqa: E402

results = {}


def check(name, cond):
    results[name] = bool(cond)


def prefilter():
    fire = [
        "stop ending every answer with a question",
        "don't use bullet points when we're chatting",
        "dont use emojis",
        "don’t bring up jobs unless I ask",           # phone apostrophe
        "I told you, use celsius",
        "I already told you not to do that",
        "you keep apologizing",
        "from now on answer in french",
        "never run find on / again",
        "no, not like that, keep it short",
        "that's not what I asked",
        "how many times do I have to say this",
        "quit lecturing me",
        "I want you to always check first",
        "always answer in two lines",
    ]
    quiet = [
        "why don't my headphones work",               # "don't" + not a behaviour verb
        "stop the qdrant container",                   # a task, not a habit
        "what is the stop button for",
        "my cat knocked my coffee over",
        "give me 2 jobs to apply to",
        "how do I install htop",
        "notice anything weird in the logs?",          # "no" only as a whole word
        "nonetheless, show me the queue",
        "the doctor told me to rest",                  # "told" but not "told you"
        "hi",
        "",
        "a" * 700,                                     # pasted logs, not a correction
    ]
    for m in fire:
        check(f"pre-filter fires: {m[:40]!r}", jl.might_be_correction(m))
    for m in quiet:
        check(f"pre-filter quiet: {m[:40]!r}", not jl.might_be_correction(m))


def cleaning():
    r = jl.clean("don't end answers with a question", "it annoys him.")
    check("clean: capitalised, full stop, why without trailing dot",
          r == {"rule": "Don't end answers with a question.", "why": "it annoys him"})
    check("clean: empty rule rejected", jl.clean("", "x") is None)
    check("clean: too-long rule rejected", jl.clean("x " * 100, "y") is None)
    check("as_text includes the why",
          jl.as_text({"rule": "Use celsius.", "why": "he lives in Tunisia"})
          == "Use celsius. (why: he lives in Tunisia)")


def block():
    rows = [{"memory_id": i, "text": f"Rule number {i} is here. (why: reason {i})"}
            for i in range(30)]
    b = jl.rules_block(rows)
    lines = [l for l in b.splitlines() if l.startswith("- ")]
    check("block: at most MAX_RULES rules", 0 < len(lines) <= jl.MAX_RULES)
    check("block: bounded by MAX_BLOCK_CHARS",
          sum(len(l) + 1 for l in lines) <= jl.MAX_BLOCK_CHARS)
    check("block: newest first (the list order is kept)", lines[0].startswith("- Rule number 0 "))
    long_rows = [{"memory_id": i, "text": "x" * 250} for i in range(5)]
    check("block: long rules cut by chars, not count",
          len([l for l in jl.rules_block(long_rows).splitlines() if l.startswith("- ")]) == 2)
    check("block: nothing stored -> empty string", jl.rules_block([]) == "")


def hook():
    """after_turn with the decision, the writer and storage stubbed."""
    calls = {"decide": 0, "write": 0, "save": []}
    real = (jl.is_correction, jl.write_rule, jl.save)

    def fake_decide(prev, msg):
        calls["decide"] += 1
        calls["prev"] = prev
        yes = "bullet" in msg or "question" in msg
        return SimpleNamespace(choice="yes" if yes else "no", confident=True,
                               p=0.9)

    def fake_write(prev, msg):
        calls["write"] += 1
        return {"rule": "Don't use bullet points.", "why": "he finds them noisy"}

    def fake_save(rule):
        calls["save"].append(rule)
        return {"memory_id": 77, "text": jl.as_text(rule), "duplicate": False}

    jl.is_correction, jl.write_rule, jl.save = fake_decide, fake_write, fake_save
    try:
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "list my tasks"},
                {"role": "assistant", "content": "- a\n- b"},
                {"role": "user", "content": "don't use bullet points"}]
        out = jl.after_turn(msgs, len(msgs), msgs[-1]["content"])
        check("hook: a correction is saved and returned",
              out and out[0]["memory_id"] == 77 and len(calls["save"]) == 1)
        check("hook: the decision sees Johan's previous answer", calls["prev"] == "- a\n- b")

        before = dict(decide=calls["decide"], write=calls["write"])
        out = jl.after_turn(msgs, len(msgs), "what's the weather in tunis")
        check("hook: no pre-filter hit -> no model call at all",
              out == [] and calls["decide"] == before["decide"])

        out = jl.after_turn(msgs, len(msgs), "stop the qdrant container please now")
        check("hook: pre-filter quiet on a task -> nothing", out == [])

        out = jl.after_turn(msgs, len(msgs), "no, that's wrong, it's Canberra")
        check("hook: decision says 'no' -> nothing saved, no rule written",
              out == [] and calls["write"] == before["write"])

        jl.is_correction = lambda p, m: SimpleNamespace(choice="yes", confident=False, p=0.55)
        out = jl.after_turn(msgs, len(msgs), "don't use bullet points")
        check("hook: an UNconfident yes saves nothing", out == [])

        def boom(p, m):
            raise RuntimeError("ollama down")
        jl.is_correction = boom
        check("hook: errors never escape", jl.after_turn(msgs, len(msgs), "don't use bullets") == [])
    finally:
        jl.is_correction, jl.write_rule, jl.save = real


def injection():
    """prime_with_memories puts rules in their own block and keeps them out
    of the facts list."""
    import johan_assistant as ja
    real = (ja.jt.list_memories, jl.rules, ja.jt.scratchpad_read)
    ja.jt.list_memories = lambda limit=15: {"memories": [
        {"memory_id": 1, "kind": "feedback", "text": "Don't use bullet points. (why: noisy)"},
        {"memory_id": 2, "kind": "preference", "text": "prefers PostgreSQL"}]}
    jl.rules = lambda limit=jl.MAX_RULES: [
        {"memory_id": 1, "text": "Don't use bullet points. (why: noisy)"}]
    ja.jt.scratchpad_read = lambda: ""
    real_root = ja.ROOT
    ja.ROOT = Path("/nonexistent-johan-test-root")      # no recipes.md read
    try:
        p = ja.prime_with_memories("SYS")
    finally:
        ja.jt.list_memories, jl.rules, ja.jt.scratchpad_read = real
        ja.ROOT = real_root
    check("inject: rules block present", "How he wants you to behave" in p
          and "- Don't use bullet points. (why: noisy)" in p)
    check("inject: rule not repeated in the facts list", p.count("bullet points") == 1)
    check("inject: facts still listed", "(preference) prefers PostgreSQL" in p)
    check("inject: the block comes after the system prompt", p.startswith("SYS"))


def engine_hook():
    """run_turn exposes what was learned in LAST_LEARNED, and a turn that
    learns nothing leaves it empty."""
    import johan_assistant as ja
    real = (ja.run_turn_loop, ja._learn.after_turn, ja.VERIFY_ANSWERS,
            ja._maybe_research, ja._skills.inject)
    ja.run_turn_loop = lambda messages, **kw: "ok"
    ja.VERIFY_ANSWERS = False
    ja._maybe_research = lambda *a, **k: None
    ja._skills.inject = lambda *a, **k: None
    try:
        ja._learn.after_turn = lambda m, s, q: [{"memory_id": 5, "text": "R.", "duplicate": False}]
        ja.run_turn([{"role": "user", "content": "don't do that"}])
        check("engine: LAST_LEARNED carries the saved rule", ja.LAST_LEARNED
              and ja.LAST_LEARNED[0]["memory_id"] == 5)
        ja._learn.after_turn = lambda m, s, q: []
        ja.run_turn([{"role": "user", "content": "what time is it"}])
        check("engine: LAST_LEARNED reset every turn", ja.LAST_LEARNED == [])
        ja._learn.after_turn = lambda m, s, q: [{"memory_id": 9, "text": "R.", "duplicate": False}]
        ja.run_turn([{"role": "user", "content": "don't do that"}], should_stop=lambda: True)
        check("engine: an interrupted turn learns nothing", ja.LAST_LEARNED == [])
    finally:
        (ja.run_turn_loop, ja._learn.after_turn, ja.VERIFY_ANSWERS,
         ja._maybe_research, ja._skills.inject) = real


def main():
    prefilter()
    cleaning()
    block()
    hook()
    injection()
    engine_hook()
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    bad = [k for k, v in results.items() if not v]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
