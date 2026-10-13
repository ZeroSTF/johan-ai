"""Inline approval: Johan asks in the chat, the turn waits, then continues.

Drives the REAL engine (run_turn → propose_action / propose_edit → the
approval card) with a scripted model that asks for a change and then answers.
Real, non-root changes in a scratch folder under ~/.cache (deleted after);
the actions/edits/rules stores are temp files; nothing under brain/.

    python test_approval.py
"""
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_actions as jx          # noqa: E402
import johan_approval as jap        # noqa: E402
import johan_assistant as ja        # noqa: E402
import johan_plan as jp             # noqa: E402
import johan_tools as jt            # noqa: E402
import johan_tui as tui             # noqa: E402

results = {}
SCRATCH = Path.home() / ".cache/johan-approval-test"


def check(label, ok):
    results[label] = bool(ok)


def tool_call(name, **args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args}}]}


class Script:
    """A fake model: first asks for `call`, then answers with whatever the
    tool result said, so the test can see what reached the model."""
    def __init__(self):
        self.call = None
        self.seen = []

    def __call__(self, messages, on_delta=None, no_tools=False, should_stop=None, tools=None):
        if messages[-1].get("role") == "tool":
            self.seen.append(json.loads(messages[-1]["content"]))
            return {"role": "assistant", "content": "done: " + messages[-1]["content"][:60]}
        return dict(self.call)


async def main():
    tmp = Path(tempfile.mkdtemp())
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    saved = {}

    def stub(obj, name, val):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)
    model = Script()
    stub(ja, "call_model", model)
    stub(ja, "VERIFY_ANSWERS", False)
    stub(ja, "save_session", lambda m: None)
    stub(ja, "log_session", lambda m: None)
    stub(ja, "log_tool_call", lambda *a, **k: None)
    stub(ja, "prime_with_memories", lambda s, **k: s)
    stub(ja._skills, "inject", lambda messages, q: None)
    stub(ja._learn, "after_turn", lambda *a, **k: [])
    stub(ja, "_maybe_research", lambda *a, **k: None)
    stub(jp, "prefilter", lambda text: False)
    stub(jx, "PENDING", tmp / "pending.json")
    stub(jx, "LOG", tmp / "log.json")
    stub(jx, "ALLOW_RULES", tmp / "rules.json")
    stub(jt, "PENDING_EDITS", tmp / "edits.json")
    stub(jt, "EDIT_BACKUPS", tmp / "backups")
    stub(tui.JohanApp, "warm_up", lambda self: None)
    stub(tui.Sidebar, "refresh_state", lambda self, messages=None: None)
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(140, 45)) as pilot:
            async def ask(text, call):
                model.call = call
                # Submit directly: pilot.press() waits for the app to go idle,
                # and a turn waiting for approval is deliberately NOT idle.
                app._submit(text)
                for _ in range(100):
                    await pilot.pause(0.05)
                    if app.approval is not None:
                        return True
                return False

            async def settle():
                for _ in range(200):
                    await pilot.pause(0.05)
                    if not app.busy:
                        break
                await pilot.pause()

            # 1. yes: runs, is verified, and the model hears it in the same turn
            d1 = SCRATCH / "one"
            shown = await ask("make a folder", tool_call("propose_action", commands=f"mkdir {d1}", why="test"))
            check("the card appears mid-turn", shown and app.busy)
            check("…and nothing ran yet", not d1.exists())
            await pilot.press("1")
            await settle()
            check("1 runs it", d1.is_dir())
            r = model.seen[-1] if model.seen else {}
            check("the model gets the result in the same turn", r.get("ran") and r.get("ok"))
            check("…verified", r.get("verified"))
            check("the turn finished normally", not app.busy and "done:" in app.turn.answer.source)

            # 2. no
            d2 = SCRATCH / "two"
            await ask("make another", tool_call("propose_action", commands=f"mkdir {d2}"))
            await pilot.press("3")
            await settle()
            check("3 (no) runs nothing", not d2.exists())
            check("…and the model is told he said no", model.seen[-1].get("declined"))
            check("…nothing left pending", jx.list_actions() == [])

            # 3. typed answer = no + what instead
            await ask("make a third", tool_call("propose_action", commands=f"mkdir {SCRATCH / 'three'}"))
            box = app.query_one("#prompt")
            box.focus()
            box.value = "call it four instead"
            await pilot.press("enter")
            await settle()
            check("typing declines it", not (SCRATCH / "three").exists())
            check("…and passes what he said", model.seen[-1].get("he_said") == "call it four instead")

            # 4. typing that starts with y must NOT approve
            await ask("make a fifth", tool_call("propose_action", commands=f"mkdir {SCRATCH / 'five'}"))
            app.approval[0].choices.focus()
            await pilot.press("y", "e", "s")
            await pilot.pause()
            check("a typed 'yes…' doesn't approve by its first letter",
                  app.approval is not None and not (SCRATCH / "five").exists())
            app.query_one("#prompt").value = ""
            await pilot.press("escape")
            t0 = time.monotonic()
            await settle()
            check(f"esc while waiting: stops, nothing runs ({time.monotonic() - t0:.1f}s)",
                  not app.busy and not (SCRATCH / "five").exists())
            app.cancel.clear()

            # 5. always allow: runs and saves a rule
            d6 = SCRATCH / "six"
            await ask("make six", tool_call("propose_action", commands=f"mkdir {d6}"))
            check("'always allow' is offered when the change can be a rule",
                  len(app.approval[0].options) == 3)
            await pilot.press("2")
            await settle()
            check("2 runs it", d6.is_dir())
            check("…and saves an always-allow rule", len(jx.load_rules()) == 1)

            # 6. a code edit gets the same card
            repo = SCRATCH / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            f = repo / "app.py"
            f.write_text("x = 1\n")
            await ask("change x", tool_call("propose_edit", path=str(f), old_string="x = 1",
                                            new_string="x = 2", why="test"))
            check("an edit shows a card with the diff", app.approval is not None
                  and app.approval[0].kind == "edit")
            await pilot.press("1")
            await settle()
            check("1 applies the edit", f.read_text() == "x = 2\n")
            check("…and the model hears it", model.seen[-1].get("applied"))
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)

    # 7. with nobody to ask (evals, one-shot), changes are staged as before
    real = (jx.PENDING, jx.LOG)
    jx.PENDING, jx.LOG = tmp / "p2.json", tmp / "l2.json"
    try:
        with jap.approver(None):
            r = jx.propose_action(f"mkdir {SCRATCH / 'seven'}")
        check("no approver: staged, not run", r.get("proposed") and not (SCRATCH / "seven").exists())
    finally:
        jx.PENDING, jx.LOG = real
        shutil.rmtree(SCRATCH, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main())
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
