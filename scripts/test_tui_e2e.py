"""End-to-end test of the interface: the real app, the real model, real keys.

The pilot tests in this project so far checked screens render. This one uses
Johan the way Riadh does — types questions, waits for streamed answers, runs
tools, stops an answer with esc, recalls history with ↑, switches tabs, draws
a picture and deletes the chat it's in — and asserts on what ends up on screen.

Privacy and side effects, by construction:
  - never opens History (F2) or Images (F5): those show his chats and pictures
  - the sidebar's tasks panel is blanked (it shows his task text)
  - never asks about him, so stored memories aren't recited onto the screen
  - deletes only the chats it created, and restores the `--continue`
    pointer files byte-for-byte
  - tool calls are tagged eval_run='tui-e2e', separable from real usage

    python test_tui_e2e.py            # ~3-4 min, uses the GPU
    python test_tui_e2e.py --no-draw  # skip the image step (~40 s of GPU)

Screenshots (SVG) go to logs/tui-e2e/.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja      # noqa: E402
import johan_tui as tui           # noqa: E402
from textual.widgets import DataTable, Input, Markdown, TabbedContent   # noqa: E402

SHOTS = ja.ROOT / "logs/tui-e2e"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))


async def wait_until(pilot, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await pilot.pause(0.25)
    return cond()


def answer_text(turn):
    return turn.answer.source or ""


async def ask(app, pilot, text, timeout=150):
    box = app.query_one("#prompt", Input)
    box.value = text
    await pilot.press("enter")
    await wait_until(pilot, lambda: app.busy, 5)
    started = time.monotonic()
    done = await wait_until(pilot, lambda: not app.busy, timeout)
    return done, time.monotonic() - started, app.turn


async def run(draw=True):
    SHOTS.mkdir(parents=True, exist_ok=True)
    tui.Sidebar._tasks = lambda self: "[$text-muted](hidden during test)[/]"
    ja.EVAL_RUN = "tui-e2e"
    created = {ja.SESSION_ID}
    app = tui.JohanApp()

    async with app.run_test(size=(150, 46)) as pilot:
        shot = lambda name: app.save_screenshot(f"{name}.svg", str(SHOTS))   # noqa: E731

        print("startup")
        check("welcome card on an empty chat", app.query(tui.Welcome))
        check("prompt has focus", app.query_one("#prompt", Input).has_focus)
        warm = await wait_until(pilot, lambda: "ready" in str(app.query_one("#status").render()), 60)
        check("model warms up and status says ready", warm)
        shot("01-home")

        print("plain question (no tools)")
        ok, secs, turn = await ask(app, pilot, "In one sentence: what is the difference between TCP and UDP?")
        text = answer_text(turn)
        check("answer finishes", ok, f"{secs:.0f}s")
        check("answer is on screen", len(text) > 30, repr(text[:80]))
        check("no tool steps on a plain question", not turn.steps, str(turn.steps))
        check("meta line shows timing", "johan ·" in str(turn.meta.render()))
        check("welcome card is gone once chatting", not app.query(tui.Welcome))
        print(f"        {secs:.1f}s · {text[:90]!r}")

        print("tool question")
        ok, secs, turn = await ask(app, pilot, "what is 1847 * 23?")
        text = answer_text(turn)
        check("tool answer finishes", ok, f"{secs:.0f}s")
        check("calculate tool shown as a step", any(n == "calculate" for n, _ in turn.steps), str(turn.steps))
        check("correct result on screen", "42481" in text.replace(",", ""), repr(text[:80]))
        check("activity line summarises the steps", "step" in str(turn.activity.title))
        print(f"        {secs:.1f}s · steps {turn.steps}")
        shot("02-chat")

        print("esc stops a long answer")
        box = app.query_one("#prompt", Input)
        box.value = "Write a 600-word essay about the history of the Mediterranean sea."
        await pilot.press("enter")
        t_ask = time.monotonic()
        await wait_until(pilot, lambda: len(app._stream_buf) > 40 or time.monotonic() - t_ask > 8, 60)
        t0 = time.monotonic()
        await pilot.press("escape")
        stopped = await wait_until(pilot, lambda: not app.busy, 15)
        check("esc stops generation", stopped, f"{time.monotonic() - t0:.1f}s")
        check("stopped answer is marked", "stopped" in str(app.turn.meta.render()))
        print(f"        stopped {time.monotonic() - t0:.1f}s after esc, "
              f"{len(answer_text(app.turn).split())} words kept")

        print("input conveniences")
        app.query_one("#prompt", Input).focus()
        await pilot.press("up")
        check("↑ recalls the last question",
              app.query_one("#prompt", Input).value.startswith("Write a 600-word essay"))
        app.query_one("#prompt", Input).value = ""
        app.query_one("#prompt", Input).value = "/nosuchcommand"
        await pilot.press("enter")
        await pilot.pause(0.3)
        notes = [str(n.render()) for n in app.query(tui.Note)]
        check("unknown slash command explains itself", any("no command" in n for n in notes))

        print("help, palette, sidebar, theme")
        await pilot.press("f1")
        await pilot.pause(0.3)
        check("F1 opens help", isinstance(app.screen, tui.HelpScreen))
        shot("03-help")
        await pilot.press("escape")
        await pilot.pause(0.3)
        check("esc closes help", not isinstance(app.screen, tui.HelpScreen))
        await pilot.press("ctrl+p")
        await pilot.pause(0.5)
        check("ctrl+p opens the command palette", "CommandPalette" in type(app.screen).__name__)
        await pilot.press("escape")
        await pilot.pause(0.3)
        await pilot.press("ctrl+b")
        await pilot.pause(0.2)
        check("ctrl+b hides the sidebar", app.query_one(tui.Sidebar).has_class("hidden"))
        await pilot.press("ctrl+b")
        theme = app.theme
        await pilot.press("ctrl+t")
        await pilot.pause(0.2)
        check("ctrl+t changes theme", app.theme != theme)
        await pilot.press("ctrl+t", "ctrl+t", "ctrl+t")          # cycle back round
        app.theme = theme

        print("jobs tab")
        await pilot.press("f3")
        await pilot.pause(0.5)
        table = app.query_one("#jtable", DataTable)
        q = tui.aq.latest_queue() or {}
        check("F3 shows the jobs tab", app.query_one("#main", TabbedContent).active == "tab-jobs")
        check("table lists every queued job", table.row_count == len(q.get("jobs", [])),
              f"{table.row_count} rows vs {len(q.get('jobs', []))}")
        check("jobs table has focus (keys work at once)", table.has_focus)
        await pilot.press("down")
        await pilot.pause(0.3)
        detail = str(app.query_one("#jdetail").render())
        second = (q.get("jobs") or [{}, {}])[1].get("title", "")[:20] if len(q.get("jobs", [])) > 1 else ""
        check("moving the cursor updates the details", second and second in detail.replace("\n", " "),
              repr(detail[:80]))
        shot("04-jobs")
        await pilot.press("up")
        app.query_one("#main", TabbedContent).active = "tab-chat"
        app.query_one("#prompt", Input).focus()

        if draw:
            print("draw, then delete the chat it's in (the old crash)")
            app.query_one("#prompt", Input).value = "/draw square red apple on a wooden table"
            await pilot.press("enter")
            done = await wait_until(pilot, lambda: not app.busy, 240)
            cards = list(app.query(tui.ImageCard))
            check("drawing finishes", done)
            check("picture appears inline", bool(cards))
            path = Path(cards[-1].result["path"]) if cards else None
            check("image file saved", path and path.exists())
            shot("05-draw")
            app.query_one("#prompt", Input).value = "/delete"
            await pilot.press("enter")
            await pilot.pause(1.0)
            check("bare /delete clears the screen to a fresh chat", app.query(tui.Welcome))
            check("its picture was deleted with it", path is not None and not path.exists())
            await pilot.pause(1.0)                       # survive a few repaints
            check("no crash after deleting a chat with a picture", app.is_running)
            created.add(ja.SESSION_ID)

        print("new chat")
        before = ja.SESSION_ID
        await pilot.press("ctrl+n")
        await pilot.pause(0.5)
        created.add(ja.SESSION_ID)
        check("ctrl+n starts a new session id", ja.SESSION_ID != before)
        check("ctrl+n shows the welcome card", app.query(tui.Welcome))
        check("ctrl+n resets the conversation", len(app.messages) == 1)
        shot("06-new")
        await pilot.press("ctrl+q")
    return created


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-draw", action="store_true")
    args = ap.parse_args()
    ptr = {f: (f.read_bytes() if f.exists() else None) for f in (ja.SESSION_FILE, ja.LAST_ID_FILE)}
    created = set()
    t0 = time.monotonic()
    try:
        created = asyncio.run(run(draw=not args.no_draw))
    finally:
        for sid in created:
            try:
                ja.delete_session(sid)
            except Exception:
                pass
        for f, data in ptr.items():
            if data is None:
                f.unlink(missing_ok=True)
            else:
                f.write_bytes(data)
        ja.release_model()
        try:
            import johan_images as ji
            ji.stop_server()
        except Exception:
            pass
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed in {time.monotonic() - t0:.0f}s · "
          f"cleaned up {len(created)} test chat(s) · screenshots in {SHOTS}")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL {name}  {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
