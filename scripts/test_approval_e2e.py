"""End to end with the REAL model: Johan asks inline, he approves, it runs.

"make the top bar clock show seconds" → the model looks, proposes
`gsettings set … clock-show-seconds true` → the approval card appears in the
chat mid-turn → `1` → the setting really changes and is verified → the model
reports it in the same turn. Then `2`-less decline is exercised on a second
request. The original setting is restored afterwards no matter what.

Takes a minute or two; needs Ollama running.

    python test_approval_e2e.py
"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja        # noqa: E402
import johan_tui as tui             # noqa: E402
# His REAL pending changes must never surface in a test (a stray enter on a
# waiting card could approve one): no 'waiting from earlier' cards here.
tui.JohanApp._show_backlog = lambda self, intro=True: None

KEY = ["org.gnome.desktop.interface", "clock-show-seconds"]
results = {}


def check(label, ok, detail=""):
    results[label] = (bool(ok), detail)


def get():
    return subprocess.run(["gsettings", "get", *KEY], capture_output=True, text=True).stdout.strip()


async def main():
    original = get()
    subprocess.run(["gsettings", "set", *KEY, "false"], check=True)   # a change to make
    ja.EVAL_RUN = "approval-e2e"
    tui.Sidebar.refresh_state = lambda self, messages=None: None
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(150, 46)) as pilot:
            async def wait_for(cond, secs):
                end = time.monotonic() + secs
                while time.monotonic() < end:
                    if cond():
                        return True
                    await pilot.pause(0.25)
                return cond()

            await wait_for(lambda: not app.busy, 60)          # warm-up
            t0 = time.monotonic()
            app._submit("make the top bar clock show seconds")
            shown = await wait_for(lambda: app.approval is not None or not app.busy, 180)
            card = app.approval[0] if app.approval else None
            check("the real model asks inline (card in the chat)", card is not None,
                  f"{time.monotonic() - t0:.0f}s")
            if card:
                steps = [s.get("shown", "") for s in card.item.get("steps", [])]
                check("…proposing the clock-seconds setting",
                      any("clock-show-seconds" in s for s in steps), "; ".join(steps))
                check("…while nothing has changed yet", get() == "false")
                app.save_screenshot("approval-card.svg", "/home/zeros/johan/logs/tui-e2e")
                await pilot.press("1")
                await wait_for(lambda: not app.busy, 120)
                check("approved: the setting really changed", get() == "true", get())
                answer = app.turn.answer.source or ""
                check("Johan reports it in the same turn", any(w in answer.lower() for w in
                      ("second", "done", "enabled", "now", "on")), answer[:120])
                app.save_screenshot("approval-done.svg", "/home/zeros/johan/logs/tui-e2e")

            # decline path: ask to turn it off, say no — nothing may change
            subprocess.run(["gsettings", "set", *KEY, "true"], check=True)
            app._submit("now turn the clock seconds back off")
            await wait_for(lambda: app.approval is not None or not app.busy, 180)
            if app.approval:
                await pilot.press(str(len(app.approval[0].options)))   # last option = no
                await wait_for(lambda: not app.busy, 120)
                check("declined: nothing changed", get() == "true", get())
            else:
                check("declined: nothing changed", False, "no card the second time")
    finally:
        subprocess.run(["gsettings", "set", *KEY, original or "false"])
        check("his original setting is restored", get() == (original or "false"), get())


asyncio.run(main())
for k, (ok, d) in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}" + (f"  [{d}]" if d else ""))
bad = [k for k, (ok, _) in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
