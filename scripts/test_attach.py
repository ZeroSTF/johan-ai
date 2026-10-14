"""Attached pictures: paste, drag-drop, file picker, and what the model gets.

Pilot-driven against the real app with the clipboard (xclip), the file
picker (zenity) and Ollama's HTTP stubbed — the stub records the exact
payload call_model sends, so the base64 pictures and the trimming of old
ones are checked on the wire, not on a helper. brain/uploads is a temp dir.
Then ONE real-model check: a generated picture is attached and the answer
must read what's in it (needs Ollama; skipped with --no-model).

    python test_attach.py [--no-model]
"""
import asyncio
import base64
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from textual import events                  # noqa: E402

import johan_assistant as ja        # noqa: E402
import johan_attach as at           # noqa: E402
import johan_plan as jp             # noqa: E402
import johan_tui as tui             # noqa: E402
# His REAL pending changes must never surface in a test (a stray enter on a
# waiting card could approve one): no 'waiting from earlier' cards here.
tui.JohanApp._show_backlog = lambda self, intro=True: None

results = {}


def check(label, ok):
    results[label] = bool(ok)


def picture(path, size=(1600, 900), text="BLUE 7342"):
    im = Image.new("RGB", size, (30, 90, 160))
    d = ImageDraw.Draw(im)
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size[1] // 6)
    except OSError:
        f = ImageFont.load_default()
    d.text((size[0] // 12, size[1] // 5), text, fill=(255, 220, 0), font=f)
    r = size[1] // 5
    d.ellipse((size[0] - 3 * r, size[1] - 2 * r - 20, size[0] - r, size[1] - 20), fill=(220, 30, 30))
    im.save(path)
    return Path(path)


def unit(tmp):
    a = picture(tmp / "a b.png")
    b = picture(tmp / "b.jpg", (300, 200))
    # what WezTerm pastes when files are dragged in
    check("drop: a quoted path", at.dropped_paths(f"'{a}'") == [str(a)])
    check("drop: backslash-escaped spaces", at.dropped_paths(str(a).replace(" ", "\\ ")) == [str(a)])
    check("drop: file:// URI", at.dropped_paths("file://" + str(a).replace(" ", "%20")) == [str(a)])
    check("drop: two files", at.dropped_paths(f"'{a}' {b}") == [str(a), str(b)])
    check("drop: a sentence naming a picture is NOT swallowed",
          at.dropped_paths(f"what is {b}") == [])
    check("drop: a non-picture is not attached", at.dropped_paths(str(Path(__file__))) == [])
    m = at.user_message("redraw this", [str(b)])
    check("message: his words + a note with the path and size",
          m["content"].startswith("redraw this") and str(b) in m["content"] and "300×200" in m["content"]
          and m["image_paths"] == [str(b)])
    check("shown text drops the note", at.shown_text(m["content"]) == "redraw this")
    check("a picture alone asks what's in it", at.user_message("", [str(b)])["content"]
          .startswith(at.DEFAULT_QUESTION))
    raw = base64.b64decode(at.encode(a))
    with Image.open(io.BytesIO(raw)) as im:
        check(f"sent downscaled to ≤{at.MAX_SIDE}px ({im.size[0]}×{im.size[1]})", max(im.size) <= at.MAX_SIDE)
    msgs = [{"role": "system", "content": "s"},
            at.user_message("one", [str(b)]), {"role": "assistant", "content": "x"},
            at.user_message("two", [str(a)]), {"role": "assistant", "content": "y"},
            {"role": "user", "content": "three"}]
    before = json.dumps(msgs)
    out = at.ollama_messages(msgs)
    check("old pictures (beyond 2 user turns) are dropped from what's sent",
          "images" not in out[1] and "image_paths" not in out[1] and str(b) in out[1]["content"])
    check("recent pictures go as base64 images", len(out[3].get("images", [])) == 1
          and "image_paths" not in out[3])
    check("the stored history is never changed", json.dumps(msgs) == before)
    gone = at.user_message("gone", [str(tmp / "deleted.png")])
    check("a deleted picture doesn't crash, it's said",
          "gone" in at.ollama_messages([gone])[0]["content"]
          and "images" not in at.ollama_messages([gone])[0])


class FakeOllama:
    """Stands in for johan_interrupt.ollama_lines: records each payload,
    answers 'ok' (streamed or not)."""
    def __init__(self):
        self.payloads = []

    def __call__(self, url, payload, idle_timeout=180):
        self.payloads.append(payload)
        yield {"message": {"role": "assistant", "content": "ok"}, "done": True}


async def pilot(tmp):
    uploads = tmp / "uploads"
    clip = picture(tmp / "clip-src.png", (800, 450))
    dropped = picture(tmp / "dragged in.png", (640, 480))
    picked = picture(tmp / "picked.jpg", (500, 500))
    fake = FakeOllama()
    clipboard = {"image": None, "text": None}
    saved = {}

    def stub(obj, name, val):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)

    def fake_clip_image(run=None):
        if clipboard["image"] is None:
            return None
        uploads.mkdir(parents=True, exist_ok=True)
        out = uploads / f"clip-{len(list(uploads.iterdir()))}.png"
        shutil.copy(clipboard["image"], out)
        return out
    stub(at, "UPLOADS", uploads)
    stub(at, "clipboard_image", fake_clip_image)
    stub(at, "clipboard_text", lambda run=None: clipboard["text"])
    stub(at, "pick_file", lambda run=None: str(picked))
    stub(ja.ji, "ollama_lines", fake)
    stub(ja, "VERIFY_ANSWERS", False)
    stub(ja, "save_session", lambda m: None)
    stub(ja, "log_session", lambda m: None)
    stub(ja, "log_tool_call", lambda *a, **k: None)
    stub(ja, "prime_with_memories", lambda s, **k: s)
    stub(ja._skills, "inject", lambda messages, q: None)
    stub(ja._learn, "after_turn", lambda *a, **k: [])
    stub(ja, "_maybe_research", lambda *a, **k: None)
    stub(jp, "prefilter", lambda text: False)
    stub(jp, "reask_as_tool", lambda *a, **k: None)
    stub(tui.JohanApp, "warm_up", lambda self: None)
    stub(tui.Sidebar, "refresh_state", lambda self, messages=None: None)
    drawn = []

    def fake_draw(self, prompt, size, seed, reference=None, strength=None, style="anime", keep=None):
        drawn.append({"prompt": prompt, "reference": reference})
        self.call_later(self._finish)
    stub(tui.JohanApp, "draw_worker", fake_draw)
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(140, 45)) as pilot:
            box = app.query_one("#prompt")
            row = app.query_one("#attachments")

            async def settle():
                for _ in range(200):
                    await pilot.pause(0.05)
                    if not app.busy:
                        break
                await pilot.pause()

            # 1. ctrl+v with a picture on the clipboard
            clipboard.update(image=clip, text=None)
            box.focus()
            await pilot.press("ctrl+v")
            await pilot.pause()
            check("ctrl+v with a picture: attached (saved in uploads)",
                  len(app.attachments) == 1 and Path(app.attachments[0]).parent == uploads)
            check("…a chip shows above the prompt, with its size",
                  row.has_class("has") and "800×450" in str(row.query("Button").first().label))
            check("…and nothing was typed", box.value == "")

            # 2. ctrl+v with text: an ordinary paste, once
            clipboard.update(image=None, text="hello there")
            await pilot.press("ctrl+v")
            await pilot.pause()
            check("ctrl+v with text: pasted once, no attachment",
                  box.value == "hello there" and len(app.attachments) == 1)
            box.value = ""

            # 3. a file dragged onto the terminal = a paste of its path
            box.post_message(events.Paste(f"'{dropped}'"))
            await pilot.pause()
            check("drag-drop: the path becomes an attachment, not text",
                  len(app.attachments) == 2 and box.value == ""
                  and Path(app.attachments[1]).parent == uploads)
            check("…copied (the original can be deleted later)",
                  Path(app.attachments[1]).is_file() and Path(app.attachments[1]) != dropped)

            # 4. removing: backspace on an empty prompt, and a click
            await pilot.press("backspace")
            await pilot.pause()
            check("backspace on an empty prompt removes the last picture", len(app.attachments) == 1)
            await pilot.click("#attachments Button")
            await pilot.pause()
            check("clicking a chip removes it", app.attachments == [] and not row.has_class("has"))

            # 5. ctrl+f: the file picker
            await pilot.press("ctrl+f")
            for _ in range(40):
                await pilot.pause(0.05)
                if app.attachments:
                    break
            check("ctrl+f: the picked file is attached", len(app.attachments) == 1)

            # 6. send: pixels to the model, paths in the history
            box.value = "what is in this?"
            await pilot.press("enter")
            await settle()
            sent = fake.payloads[-1]["messages"]
            last = [m for m in sent if m["role"] == "user"][-1]
            check("sent: the user message carries the picture as base64 images",
                  len(last.get("images", [])) == 1 and "image_paths" not in last)
            check("…with the path note, so the model can redraw it",
                  "generate_image" in last["content"] and "uploads" in last["content"])
            stored = [m for m in app.messages if m["role"] == "user"][-1]
            check("history keeps the PATH, never the base64",
                  stored.get("image_paths") and "images" not in stored
                  and len(json.dumps(app.messages)) < 20000)
            check("chips cleared after sending", app.attachments == [] and not row.has_class("has"))
            check("his bubble shows the picture", len(app.query(tui.AttachedPics)) == 1)

            # 7. a picture with no text
            clipboard.update(image=clip, text=None)
            await pilot.press("ctrl+v")
            await pilot.pause()
            box.value = ""
            await pilot.press("enter")
            await settle()
            stored = [m for m in app.messages if m["role"] == "user"][-1]
            check("a picture alone is sent as \"what's in this picture?\"",
                  stored["content"].startswith(at.DEFAULT_QUESTION) and stored.get("image_paths"))

            # 8. a third turn: the first picture is no longer sent
            box.value = "thanks"
            await pilot.press("enter")
            await settle()
            users = [m for m in fake.payloads[-1]["messages"] if m["role"] == "user"]
            check("3rd turn: the oldest picture is dropped, the last one still sent",
                  "images" not in users[-3] and len(users[-2].get("images", [])) == 1
                  and all("image_paths" not in m for m in users))

            # 9. /draw with an attached picture redraws it
            clipboard.update(image=clip, text=None)
            await pilot.press("ctrl+v")
            await pilot.pause()
            app._submit("/draw 1girl, night")
            await settle()
            check("/draw uses the attached picture as the reference",
                  drawn and drawn[-1]["reference"] and Path(drawn[-1]["reference"]).parent == uploads
                  and drawn[-1]["prompt"] == "1girl, night" and app.attachments == [])

            # 10. reopening the chat shows the picture, not the path note
            app._replay(app.messages)
            await pilot.pause()
            texts = " ".join(str(w.render()) for w in app.query(tui.UserMsg))
            check("replay: no path note in his bubbles, pictures shown",
                  "generate_image" not in texts and len(app.query(tui.AttachedPics)) == 2)

            # 11. rewind puts the question AND the picture back
            idx = max(i for i, m in enumerate(app.messages) if m.get("image_paths"))
            app._rewind_chat(idx)
            await pilot.pause()
            check("rewind: the question comes back without the note, the picture re-attached",
                  box.value == at.DEFAULT_QUESTION and len(app.attachments) == 1
                  and row.has_class("has"))
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)


def real_model(tmp):
    """The real model reads an attached picture through the real engine."""
    pic = picture(tmp / "probe.png", (900, 500))
    msgs = [{"role": "system", "content": ja.prime_with_memories(ja.SYSTEM_PROMPT)},
            at.user_message("What text is written in this picture, and what colour is the circle?",
                            [str(pic)])]
    real = (ja.save_session, ja.log_session)
    ja.save_session = ja.log_session = lambda m: None
    ja.EVAL_RUN = "attach-test"
    try:
        ans = ja.run_turn(msgs) or ""
    finally:
        ja.save_session, ja.log_session = real
    check(f"real model reads the attached picture: {ans[:90]!r}",
          "7342" in ans.replace(",", "").replace(" ", "") and "red" in ans.lower())


tmp = Path(tempfile.mkdtemp())
try:
    unit(tmp)
    asyncio.run(pilot(tmp))
    if "--no-model" not in sys.argv:
        real_model(tmp)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
