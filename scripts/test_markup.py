"""Anything a user or model can type must survive the TUI's markup parser.

F2 crashed with "MarkupError: auto closing tag ('[/]') has nothing to close":
the history preview escaped chat text with Rich's escaper and handed it to
Textual's parser, and the two disagree (an unterminated "[/" is plain text to
Rich, a closing tag to Textual). Every surface that shows his, the model's or
a posting's text is fed the nastiest strings here, through the real app.
Data sources are stubbed; nothing is read from or written to brain/.

    python test_markup.py
"""
import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_actions as jx          # noqa: E402
import johan_assistant as ja        # noqa: E402
import johan_plan as jp             # noqa: E402
import johan_tools as jt            # noqa: E402
import johan_tui as tui             # noqa: E402
import johan_widgets as jw          # noqa: E402

NASTY = [
    "a [/] b", "[/", "x [/:", "[b]bold[/b]", "[/b] closes nothing", "[text](/url)",
    "[$accent]not a style[/]", "\\[escaped] and \\", "emoji 😀🔥", "\x1b[31mred ansi\x1b[0m",
    "**Step 6 done.** [/] then `code [x]`", "$t $hm $variable", "[link=https://x]y[/link]",
]
ALL = " | ".join(NASTY)
results = {}


def check(label, fn):
    try:
        fn()
        results[label] = True
    except Exception as e:      # noqa: BLE001
        results[label] = False
        print(f"      {label}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=2)


async def main():
    tmp = Path(tempfile.mkdtemp())
    saved = {}

    def stub(obj, name, val):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)

    sessions = [{"id": "2026-10-13-161450", "when": "today 16:14", "messages": 9,
                 "first": s, "last": s} for s in NASTY]
    stub(ja, "list_sessions", lambda limit=50: sessions)
    stub(ja, "session_preview", lambda sid, limit=40:
         [("user" if i % 2 else "assistant", s) for i, s in enumerate(NASTY)])
    stub(ja, "save_session", lambda m: None)
    stub(ja, "log_session", lambda m: None)
    stub(jt, "list_tasks", lambda *a, **k: {"tasks": [{"id": i, "text": s} for i, s in enumerate(NASTY)],
                                             "count": len(NASTY)})
    stub(jx, "PENDING", tmp / "pending.json")
    stub(jx, "LOG", tmp / "log.json")
    stub(jt, "PENDING_EDITS", tmp / "edits.json")
    stub(tui, "_picture", lambda path: None)
    stub(tui.JohanApp, "warm_up", lambda self: None)
    (tmp / "pending.json").write_text(json.dumps([{
        "id": 1, "why": ALL, "created": "now", "root": True, "undo_of": None,
        "steps": [{"what": s, "shown": s, "root": True, "undo": [s, "x"], "argv": ["x"]}
                  for s in NASTY[:4]] + [{"what": "t", "shown": "t", "root": False,
                                          "undo": None, "undo_note": ALL, "argv": ["t"]}]}]))
    (tmp / "edits.json").write_text(json.dumps([{
        "id": 1, "path": ALL, "why": ALL, "created": "now",
        "diff": "\n".join(["--- a/" + NASTY[0], "+++ b/" + NASTY[1], "@@ " + NASTY[2]]
                          + [p + s for p in "+- " for s in NASTY])}]))
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(150, 46)) as pilot:
            async def settle():
                await pilot.pause()
                await pilot.pause()

            # --- F2: the screen that crashed -------------------------------
            def history():
                app.push_screen(tui.HistoryScreen())
            check("F2 history mounts over a session full of markup", history)
            await settle()
            scr = app.screen
            check("history preview of every entry", lambda: [scr._preview(i) for i in range(len(NASTY))])
            await settle()
            app.pop_screen()
            await settle()

            # --- chat surfaces ---------------------------------------------
            chat = app._chat()
            for s in NASTY:
                check(f"his message: {s!r}", lambda s=s: chat.add(tui.UserMsg(s)))
                check(f"note: {s!r}", lambda s=s: chat.add(tui.Note(s, "err")))
            turn = chat.add(tui.Turn())
            await settle()
            for s in NASTY:
                check(f"tool step: {s!r}", lambda s=s: turn.add_step(s, {"q": s}, {"error": s}))
            check("answer text", lambda: turn.set_text(ALL))
            check("image caption", lambda: chat.add(tui.ImageCard(
                {"path": "/nonexistent.png", "prompt": ALL, "width": 1, "height": 2,
                 "seed": 3, "seconds": 4})))
            check("toast", lambda: app._toast(ALL, ALL, True))
            check("status line with a junk tool name", lambda: (
                setattr(app, "busy_label", ALL), app._start_busy(ALL), app._tick()
                if hasattr(app, "_tick") else None))
            await settle()

            # --- sidebar tasks ---------------------------------------------
            check("sidebar tasks", lambda: app.query_one(tui.Sidebar).query_one("#stasks")
                  .update(app.query_one(tui.Sidebar)._tasks())
                  if app.query("#stasks") else app.query_one(tui.Sidebar)._tasks())

            # --- jobs details ----------------------------------------------
            pane = app.query_one(tui.JobsPane)
            pane.rows = [{"job_id": 1, "title": ALL, "company": NASTY[0], "location": NASTY[1],
                          "seniority": NASTY[2], "hiring_type": NASTY[3], "basis": ALL,
                          "eligibility_quote": ALL, "experience_note": ALL,
                          "gaps_in_title": NASTY[:3], "gaps_in_desc": NASTY, "matched_skills": NASTY,
                          "pdf": "/tmp/" + NASTY[0] + "/cv.pdf", "url": ALL}]
            check("jobs details pane", lambda: pane._detail(0))

            # --- changes: a staged change and an edit ----------------------
            edits = app.query_one(tui.EditsPane)
            check("changes pane loads", lambda: edits.load())
            await settle()
            check("change details", lambda: edits._show(0))
            check("edit diff", lambda: edits._show(1))
            jx.PENDING.write_text("[]")
            jt.PENDING_EDITS.write_text("[]")
            jx.LOG.write_text(json.dumps([{"id": 9, "ok": True, "why": ALL, "undo": []}]))
            check("changes empty state with recent changes", lambda: edits.load())

            # --- options and plan checklist --------------------------------
            check("multiple-choice options", lambda: chat.mount(jw.Choices(NASTY, hint=ALL)))
            card = jw.PlanCard([{"do": s, "tool": "run_command"} for s in NASTY[:6]])
            check("plan card + checklist", lambda: chat.mount(card))
            await settle()
            check("checklist ticking", lambda: (card.tick("run_command"), card.finish(True)))

            # Rendering is lazy: force a full repaint of everything mounted.
            check("full repaint of every screen element", lambda: app.refresh(layout=True))
            await settle()
            app.save_screenshot("markup.svg", path=str(tmp))
            results["screenshot renders (every widget painted)"] = (tmp / "markup.svg").exists()
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)


asyncio.run(main())
for k, ok in results.items():
    if not ok:
        print(f"FAIL  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
