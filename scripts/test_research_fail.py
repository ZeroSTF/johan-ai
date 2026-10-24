"""Forced research that fails or is fed a long message. No GPU, no network.

Asked for business ideas in one long message, Johan sent all 470 characters
to the search engine (DuckDuckGo crashed on it, 0 results) and then ended the
turn with the error text written for the model: "Say so rather than answering
from memory." Both are pinned here.

    python test_research_fail.py
"""
import os
# These test the 4B (Ollama) path and the 4B-only crutches a smart model skips;
# config/default-model now says qwen36.
os.environ["JOHAN_MODEL"] = "johan-agent"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja        # noqa: E402
import johan_decide as jd           # noqa: E402
import johan_research as jr         # noqa: E402

results = {}


def check(label, ok):
    results[label] = bool(ok)


LONG = ("im a software engineer, its october 2026 and the job market here in tunisia is dead, "
        "so i want to build an app or platform that makes money through premium or ads. help me "
        "by proposing ideas, feel free to think about it deeply and do research")

real_gen = jr._generate
jr._generate = lambda prompt, should_stop=None: "Profitable app ideas for a solo developer in 2026?"
try:
    check("a long message is boiled down to a short search question",
          jr.short_question(LONG) == "Profitable app ideas for a solo developer in 2026?")
    check("a short one is searched as he wrote it", jr.short_question("lisa the painful controls")
          == "lisa the painful controls")
    jr._generate = lambda prompt, should_stop=None: ""
    check("the model writing nothing still gives a short query", len(jr.short_question(LONG))
          <= jr.LONG_QUESTION)
finally:
    jr._generate = real_gen


class D:
    def __init__(self, choice):
        self.choice, self.confident = choice, True


seen = []


def fake_model(messages, **k):
    seen.append([m.get("content", "") for m in messages if m["role"] == "system"])
    return {"role": "assistant", "content": "Here are five ideas…"}


real = (jd.decide, ja._research.deep_research, ja.call_model, ja.log_tool_call,
        ja.VERIFY_ANSWERS, ja._skills.inject, ja._plan.reask_as_tool)
jd.decide = lambda q, opts, ctx, **k: D("yes" if opts is jd.RESEARCH_OPTS else "report")
ja._research.deep_research = lambda q, **k: {"error": "research found nothing readable", "seconds": 9}
ja.call_model = fake_model
ja.log_tool_call = lambda *a, **k: None
ja.VERIFY_ANSWERS = False
ja._skills.inject = lambda *a, **k: None
ja._plan.reask_as_tool = lambda *a, **k: None
try:
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": LONG}]
    ans = ja.run_turn(msgs)
    check("failed research: the model still answers", ans == "Here are five ideas…")
    check("…told research failed and to say what is its own knowledge",
          any("Deep research failed" in s for s in seen[0]))
    check("…the model's instruction never reaches him", "Say so" not in ans)
    check("…and the one-turn note is not kept in history",
          not any("Deep research failed" in m.get("content", "") for m in msgs))
    asked = []
    ja._research.deep_research = lambda q, **k: asked.append(q) or {"error": "nothing readable"}
    ja.run_turn([{"role": "system", "content": "s"},
                 {"role": "user", "content": "how does async work in rust"},
                 {"role": "assistant", "content": "futures and executors…"},
                 {"role": "user", "content": "dig into that more thoroughly"}])
    check("a research follow-up researches what 'that' was", asked and "rust" in asked[0])
    ja._research.deep_research = lambda q, **k: {"error": "stopped — you pressed esc"}
    seen.clear()
    ans = ja.run_turn([{"role": "system", "content": "s"}, {"role": "user", "content": LONG}])
    check("esc during research still stops (no model call)", not seen and "stopped" in ans)
finally:
    (jd.decide, ja._research.deep_research, ja.call_model, ja.log_tool_call,
     ja.VERIFY_ANSWERS, ja._skills.inject, ja._plan.reask_as_tool) = real

import johan_steps as jst        # noqa: E402
turn = [{"role": "user", "content": "what skills do you have?"},
        {"role": "tool", "content": '{"error": "unknown tool scratchpad_clear"}'}]
check("a failed call is not evidence (a good self-description was 'corrected')",
      jst._evidence(turn, 0) == "")
turn.append({"role": "tool", "content": '{"count": 0}'})
check("…a real result still is", jst._evidence(turn, 0) == '{"count": 0}')
check("the prompt never names tools that no longer exist",
      "scratchpad_clear" not in ja.prime_with_memories(ja.SYSTEM_PROMPT)
      and "scratchpad_write" not in ja.prime_with_memories(ja.SYSTEM_PROMPT))

# A bare "yes" to an offer is a go-ahead, not small talk (the gate stripped all tools).
offer = [{"role": "assistant", "content": "I can install htop for you — want me to?"}]
check("'yes' after an offer keeps the tools", not ja._is_trivial("yes", offer))
check("'ok' after a statement is still small talk", ja._is_trivial("ok", [{"role": "assistant",
                                                                          "content": "Done."}]))
check("'hi' is always small talk", ja._is_trivial("hi", offer))
check("the plan layer's internal go-ahead is not his message",
      ja._last_user_message([{"role": "user", "content": "back up my Documents"},
                             {"role": "user", "content": "[Johan, internal] Plan approved. Do it now"}])
      == "back up my Documents")

# Repeat guard: reads go stale after a change; a failed web call gets one retry.
calls = []
real_d = dict(ja.DISPATCH)
ja.DISPATCH.update(run_command=lambda **a: calls.append("read") or {"output": "prefer-light"},
                   propose_action=lambda **a: calls.append("change") or {"ran": True},
                   web_search=lambda **a: calls.append("web") or {"error": "engines refusing"})
real_log = ja.log_tool_call
ja.log_tool_call = lambda *a, **k: None
try:
    seen = {}
    c = lambda n, **a: {"function": {"name": n, "arguments": a}}
    ja.execute_tool(c("run_command", command="gsettings get x"), seen, 0, "q")
    ja.execute_tool(c("propose_action", commands="gsettings set x"), seen, 0, "q")
    ja.execute_tool(c("run_command", command="gsettings get x"), seen, 0, "q")
    check("after a change the same read really runs again", calls == ["read", "change", "read"])
    calls.clear()
    for _ in range(3):
        ja.execute_tool(c("web_search", query="x"), seen, 0, "q")
    check("a failed web search gets ONE retry, not endless", calls == ["web", "web"])
    calls.clear()
    ja.execute_tool(c("run_command", command="ls a"), seen, 0, "q")
    ja.execute_tool(c("run_command", command="ls a"), seen, 0, "q")
    check("an identical read with no change between is still refused", calls == ["read"])
finally:
    ja.DISPATCH.clear(); ja.DISPATCH.update(real_d); ja.log_tool_call = real_log

for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
