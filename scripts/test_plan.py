"""Ask-with-options, plan-first and the checklist — model stubbed, no GPU,
no DB writes, nothing written under ~/johan.

    python test_plan.py
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja      # noqa: E402
import johan_plan as jp           # noqa: E402
import johan_steps                # noqa: E402

FAILURES = []


def check(label, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f": got {got!r}, want {want!r}"))
    if not ok:
        FAILURES.append(label)


# ------------------------------------------------------------------ ask tool
check("ask: list options kept", jp.ask("Which one?", ["a", "b"])["options"], ["a", "b"])
check("ask: string options split, numbering stripped",
      jp.ask("Which?", "1. Spotify\n2. VLC\n3. mpv")["options"], ["Spotify", "VLC", "mpv"])
check("ask: duplicates dropped", jp.ask("Q?", ["Tea", "tea", "Coffee"])["options"], ["Tea", "Coffee"])
check("ask: capped at 4", len(jp.ask("Q?", list("abcdef"))["options"]), 4)
check("ask: one option is an error, not a question", "error" in jp.ask("Q?", ["only"]))
check("ask: no question is an error", "error" in jp.ask("", ["a", "b"]))
res = jp.ask("Which file?", ["notes.txt", "todo.md"])
check("ask: rendered with numbered options", jp.render_ask(res),
      "Which file?\n\n1. notes.txt\n2. todo.md")


def fake_call(script):
    """call_model stub returning scripted messages, recording each call."""
    calls = []

    def call(messages, on_delta=None, no_tools=False, should_stop=None, tools=None):
        calls.append(list(messages))
        return dict(script[min(len(calls) - 1, len(script) - 1)])
    return call, calls


def tool_call(name, **args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args}}]}


real = (ja.call_model, ja.log_tool_call, johan_steps._verify_and_fix, ja._maybe_research)
verified = []
ja.log_tool_call = lambda *a, **k: None                 # no DB writes from tests
ja._maybe_research = lambda *a, **k: None
johan_steps._verify_and_fix = lambda m, s, a, st=None: verified.append(a) or a
try:
    # the ask tool ends the turn after ONE model call
    call, calls = fake_call([tool_call("ask", question="Which wallpaper?",
                                       options=["A photo of mine", "Generate one"]),
                             {"role": "assistant", "content": "SHOULD NOT BE REACHED"}])
    ja.call_model = call
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "change my wallpaper"}]
    seen_tools = []
    out = ja.run_turn(msgs, on_tool=lambda n, a, r: seen_tools.append((n, r)))
    check("ask ends the turn: one model call", len(calls), 1)
    check("ask ends the turn: reply is the rendered question", out,
          "Which wallpaper?\n\n1. A photo of mine\n2. Generate one")
    check("ask ends the turn: history ends with that question", msgs[-1],
          {"role": "assistant", "content": out})
    check("ask: UI callback gets the options", seen_tools[0][1]["options"],
          ["A photo of mine", "Generate one"])
    check("ask: the verifier never rewrites a question", verified, [])
    check("ended_by_ask reads it back from history", jp.ended_by_ask(msgs, 2)["question"],
          "Which wallpaper?")

    # an ask with bad args is an error to the model, and the loop goes on
    call, calls = fake_call([tool_call("ask", question="Which?", options=["only one"]),
                             {"role": "assistant", "content": "Done."}])
    ja.call_model = call
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}]
    out = ja.run_turn(msgs)
    check("invalid ask does not end the turn", (out, len(calls)), ("Done.", 2))
    check("…and a normal answer is not taken for an ask", jp.ended_by_ask(msgs, 2), None)

    # prose question that offers a choice -> re-asked through the tool
    call, calls = fake_call([{"role": "assistant",
                              "content": "Which player do you want, Spotify or VLC?"},
                             tool_call("ask", question="Which player?",
                                       options=["Spotify", "VLC"])])
    ja.call_model = call
    decided = []
    jd = sys.modules.get("johan_decide") or __import__("johan_decide")
    real_decide = jd.decide
    jd.decide = lambda *a, **k: decided.append(a) or SimpleNamespace(choice="yes", confident=True)
    try:
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "play some music"}]
        seen_tools = []
        out = ja.run_turn(msgs, on_tool=lambda n, a, r: seen_tools.append(n))
        check("prose choice question re-asked via the tool", out,
              "Which player?\n\n1. Spotify\n2. VLC")
        check("…one calibrated check, two model calls", (len(decided), len(calls)), (1, 2))
        after = next(i for i, m in enumerate(msgs) if m.get("content") == "play some music") + 1
        # (skills may insert a note BEFORE his message; look after it, not at a fixed index)
        check("…the prose draft is replaced in history", [m.get("content") for m in msgs[after:]],
              ["", json.dumps({"asked": True, "question": "Which player?",
                               "options": ["Spotify", "VLC"]}), out])
        check("…the UI hears about the ask", seen_tools, ["ask"])

        # a plain answer pays nothing: no decide, no extra call
        decided.clear()
        call, calls = fake_call([{"role": "assistant", "content": "It is 15:04."}])
        ja.call_model = call
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "time?"}]
        ja.run_turn(msgs)
        check("plain answer: no clarify check, one call", (len(decided), len(calls)), (0, 1))

        # an offer at the end is not a choice question (gate is code, free)
        check("gate: offer without a choice word", jp.looks_like_choice_question(
            "Il est 15h. Tu veux que je te fasse quelque chose d'autre ?"), False)
        check("gate: 'or' question", jp.looks_like_choice_question(
            "Found 3 configs.\n\nDo you want autoexec or the HUD one?"), True)
        check("gate: question then numbered list", jp.looks_like_choice_question(
            "Which style?\n\n1. Retro\n2. Minimal"), True)
        check("gate: 'or' inside a word is not 'or'", jp.looks_like_choice_question(
            "Is the colour right?"), False)
        check("gate: no question mark", jp.looks_like_choice_question(
            "Pick spotify or vlc."), False)

        # the check says no -> prose stays, verifier runs as before
        jd.decide = lambda *a, **k: SimpleNamespace(choice="no", confident=True)
        call, calls = fake_call([tool_call("run_command", command="ls"),
                                 {"role": "assistant", "content": "Want the big one or the small one?"}])
        ja.call_model = call
        real_rc = ja.DISPATCH["run_command"]
        ja.DISPATCH["run_command"] = lambda **k: {"output": "a b"}
        verified.clear()
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}]
        out = ja.run_turn(msgs)
        ja.DISPATCH["run_command"] = real_rc
        check("check says no: prose kept and verified", (out, len(verified)),
              ("Want the big one or the small one?", 1))
    finally:
        jd.decide = real_decide
finally:
    ja.call_model, ja.log_tool_call, johan_steps._verify_and_fix, ja._maybe_research = real

# ------------------------------------------------------------------ plan trigger
PRE = [
    # (message, worth asking?, why)
    ("what's my disk usage", False, "a question"),
    ("install htop", False, "one action — staged via F4 already, no plan"),
    ("in one sentence, what is the difference between TCP and UDP?", False, "question"),
    ("how do I set up docker?", False, "asks HOW: explain, don't plan"),
    ("install docker and add me to the docker group", True, "two changes"),
    ("set up a python dev environment with pyenv", True, "inherently several steps"),
    ("clean up my downloads folder", True, "inherently several steps"),
    ("draft CVs for the top 3 jobs, then compare them", True, "change + look, sequenced"),
    ("check my disk and list the docker containers", False, "only lookups — no approval needed"),
    ("open setup.py and read it", False, "'setup' inside 'setup.py' is a file, not a verb"),
    ("my startup folder has a reinstallation guide", False, "'start'/'install' inside longer words"),
    ("the organizer and the installer are both open", False, "'organize' in 'organizer', 'install' in 'installer'"),
    ("hi", False, "trivial"),
    ("/draw a castle and a dragon", False, "slash commands never plan"),
    ("can you uninstall vlc, then install mpv?", True, "polite request is still a request"),
    ("can you explain how to set up docker compose?", False, "polite HOW question: explain"),
    ("tidy up this paragraph: we set up the server and migrated the db", False,
     "writing about a setup is not doing one"),
    ("could you set up nginx for me", True, "polite request to DO it"),
]
for msg, want, why in PRE:
    check(f"prefilter({msg!r}) — {why}", jp.prefilter(msg), want)

asked = []
def stub_decide(choice, confident=True):
    def d(*a, **k):
        asked.append(a)
        return SimpleNamespace(choice=choice, confident=confident)
    return d
check("needs_plan: prefilter no -> no decision at all",
      (jp.needs_plan("what's my disk usage", decide=stub_decide("yes")), len(asked)), (False, 0))
check("needs_plan: confident yes", jp.needs_plan("install docker and add me to the group",
                                                 decide=stub_decide("yes")), True)
check("needs_plan: unconfident yes is no",
      jp.needs_plan("install docker and add me to the group",
                    decide=stub_decide("yes", confident=False)), False)
def boom(*a, **k):
    raise RuntimeError("ollama down")
check("needs_plan: broken decision layer = no plan, no crash",
      jp.needs_plan("set up docker", decide=boom), False)
check("needs_plan: exactly one decision", len(asked), 2)

# ------------------------------------------------------------------ plan call
cfg = SimpleNamespace(TOOLS=[{"function": {"name": "run_command"}}], MODEL="m",
                      OLLAMA_CHAT="x", KEEP_ALIVE="1m", NUM_CTX=1)
plan_json = json.dumps({"steps": [{"do": "Check if docker is installed", "tool": "run_command"},
                                  {"do": "Stage the install", "tool": "propose_action"},
                                  {"do": "Tell him what to approve", "tool": "none"}]})
msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "install docker and set it up"}]
sent = []
steps = jp.make_plan(msgs, cfg=cfg, call=lambda m, c: sent.append(m) or plan_json)
check("make_plan: steps parsed", [s["tool"] for s in steps], ["run_command", "propose_action", "none"])
check("make_plan: ONE call", len(sent), 1)
check("make_plan: appended, not rebuilt (cheap path)", msgs[:2] == sent[0][:2] and len(msgs), 4)
check("make_plan: the exact generated text is kept", msgs[-1]["content"], plan_json)
check("make_plan: internal prompt hidden from replay", msgs[-2]["content"].startswith(jp.INTERNAL), True)
check("is_plan_message", jp.is_plan_message(msgs[-1]), True)
check("is_plan_message: ordinary answer", jp.is_plan_message({"role": "assistant", "content": "{x}"}), False)
check("render_plan", jp.render_plan(steps).splitlines()[1], "2. Stage the install")
n = len(msgs)
check("make_plan: one-step plan -> no plan",
      jp.make_plan(msgs, cfg=cfg, call=lambda m, c: json.dumps({"steps": [{"do": "x", "tool": "none"}]})), [])
check("make_plan: garbage -> no plan", jp.make_plan(msgs, cfg=cfg, call=lambda m, c: "not json"), [])
check("make_plan: failure leaves history untouched", len(msgs), n)
check("make_plan: call raising -> no plan", jp.make_plan(msgs, cfg=cfg, call=lambda m, c: boom()), [])
jp.make_plan(msgs, changes="use podman instead", cfg=cfg, call=lambda m, c: sent.append(m) or plan_json)
check("re-plan carries his changes", "use podman instead" in sent[-1][-1]["content"], True)
jp.approve(msgs)
check("approve appends the go message", msgs[-1]["content"], jp.GO_PROMPT)
jp.cancel(msgs)
check("cancel is recorded, so 'ok' later isn't approval", msgs[-1]["content"], jp.CANCELLED)
schema = jp.plan_schema(["run_command", "ask"])
check("plan schema: tool is an enum of real tools + none",
      schema["properties"]["steps"]["items"]["properties"]["tool"]["enum"],
      ["ask", "run_command", "none"])

# ------------------------------------------------------------------ checklist
S = [{"do": "look", "tool": "run_command"}, {"do": "stage", "tool": "propose_action"},
     {"do": "say", "tool": "none"}]
p = jp.Progress(S)
check("checklist: nothing ticked before anything runs", [s["state"] for s in p.steps],
      ["pending", "pending", "pending"])
p.on_tool("run_command")
check("checklist: first call activates step 1", p.steps[0]["state"], "active")
p.on_tool("run_command", failed=True)
p.on_tool("propose_action")
check("checklist: reaching step 2 finishes step 1", [s["state"] for s in p.steps][:2], ["done", "active"])
p.finish(answered=True)
check("checklist: answer finishes the rest", [s["state"] for s in p.steps], ["done", "done", "done"])
check("checklist: counts", p.counts(), (3, 3))
check("checklist: text", p.text().splitlines()[0], "Plan 3/3")

p = jp.Progress(S)
p.on_tool("propose_action")
p.finish(answered=True)
check("checklist: a step with no call is skipped, not ticked", p.steps[0]["state"], "skipped")

p = jp.Progress(S)
p.on_tool("run_command", failed=True)
p.on_tool("web_search")            # tool no step expects: extra work on the current step
p.finish(answered=False)
check("checklist: unexpected tool counts on the current step", p.steps[0]["ok"], 1)
check("checklist: stopped turn leaves the rest skipped", [s["state"] for s in p.steps],
      ["done", "skipped", "skipped"])

p = jp.Progress(S)
p.on_tool("run_command", failed=True)
p.on_tool("propose_action")
check("checklist: a step whose calls all failed is marked failed", p.steps[0]["state"], "failed")
check("checklist: marks", p.lines()[0].split()[0], jp.MARKS["failed"])

p = jp.Progress([{"do": "a", "tool": "run_command"}, {"do": "b", "tool": "run_command"},
                 {"do": "c", "tool": "run_command"}])
p.on_tool("run_command")
p.on_tool("run_command")
check("checklist: same tool again after a success = next step",
      [s["state"] for s in p.steps], ["done", "active", "pending"])
p = jp.Progress([{"do": "a", "tool": "run_command"}, {"do": "b", "tool": "run_command"}])
p.on_tool("run_command", failed=True)
p.on_tool("run_command", failed=True)
p.on_tool("run_command")
check("checklist: retries after a failure stay on the step",
      [s["state"] for s in p.steps], ["active", "pending"])
check("checklist: …counted there", (p.steps[0]["ok"], p.steps[0]["failed"]), (1, 2))

print(f"\n{'all plan/ask checks pass' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'}")
sys.exit(1 if FAILURES else 0)
