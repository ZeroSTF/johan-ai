"""Reopening a chat (--continue, F2, /resume): answers and pictures come back.

Every replayed answer used to render EMPTY — set_text() ran before the
Markdown was mounted, and Markdown drops an update made that early — and
drawn / found pictures never came back at all. Drives the real app with a
made-up conversation; pictures are temp files; nothing under brain/.

    python test_replay.py
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import johan_tui as tui        # noqa: E402
tui.JohanApp._show_backlog = lambda self, intro=True: None
tui.JohanApp.warm_up = lambda self: None
tui.Sidebar.refresh_state = lambda self, messages=None: None

results = {}


def check(label, ok):
    results[label] = bool(ok)


def call(name):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": {}}}]}


async def main():
    tmp = Path(tempfile.mkdtemp())
    drawn, web1, web2 = tmp / "d.png", tmp / "w1.jpg", tmp / "w2.jpg"
    for p in (drawn, web1, web2):
        Image.new("RGB", (64, 64), "red").save(p)
    drawn.with_suffix(".json").write_text(json.dumps({"prompt": "red panda", "seconds": 30}))
    web1.with_suffix(".json").write_text(json.dumps({"title": "Red panda", "source": "https://www.zoo.org/p"}))
    pics = [{"path": str(web1)}, {"path": str(tmp / "deleted.jpg")}, {"path": str(web2)}]
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "draw a red panda"}, call("generate_image"),
            {"role": "tool", "content": json.dumps({"path": str(drawn)})},
            {"role": "assistant", "content": "FIRST answer"},
            {"role": "user", "content": "show me red pandas"}, call("find_images"),
            {"role": "tool", "content": json.dumps({"pictures": pics})},
            {"role": "assistant", "content": "SECOND answer"},
            {"role": "user", "content": "again"}, call("find_images"),
            {"role": "tool", "content": "cut short {\"pictures\": [{\"path\": \"/tm"},
            {"role": "assistant", "content": "THIRD answer"}]
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause(0.3)
            app._replay(msgs)
            await pilot.pause(1.0)
            answers = [t.answer.source for t in app.query(tui.Turn)]
            check("every replayed answer shows its text (they were all empty)",
                  answers == ["FIRST answer", "SECOND answer", "THIRD answer"])
            cards = app.query(tui.ImageCard)
            check("a drawing comes back as its card, details from its sidecar",
                  len(cards) == 1 and cards[0].result.get("prompt") == "red panda")
            web = app.query(tui.WebPicsCard)
            check("found pictures come back; a deleted one is skipped",
                  len(web) == 1 and [p["path"] for p in web[0].pics] == [str(web1), str(web2)])
            check("…with the title and site from the sidecar",
                  web[0].pics[0]["title"] == "Red panda" and web[0].pics[0]["site"] == "zoo.org")
            order = [type(w).__name__ for w in app.query_one("#chat").children]
            check("cards sit below their answer, as they did live",
                  order == ["UserMsg", "Turn", "ImageCard", "UserMsg", "Turn", "WebPicsCard",
                            "UserMsg", "Turn"])
            check("a cut-short result doesn't crash or invent a card", len(web) == 1)
            check("ctrl+o can open the replayed pictures", str(drawn) in app.openables)
        # F2: arrows then enter reopens — the list ate enter, so it only
        # worked after clicking the preview pane.
        import johan_assistant as ja
        real_list, real_load = ja.list_sessions, tui.JohanApp._load_session
        ja.list_sessions = lambda limit=50: [
            {"id": f"2026-01-0{i}-120000", "when": "x", "messages": 2, "first": f"chat {i}",
             "preview": []} for i in range(1, 4)]
        opened = []
        tui.JohanApp._load_session = lambda self, sid: opened.append(sid)
        try:
            app = tui.JohanApp()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause(0.3)
                await pilot.press("f2")
                await pilot.pause(0.3)
                await pilot.press("down", "enter")
                await pilot.pause(0.3)
                check("F2, ↓, enter reopens that chat (list focused)",
                      opened == ["2026-01-02-120000"])
        finally:
            ja.list_sessions, tui.JohanApp._load_session = real_list, real_load
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main())
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
