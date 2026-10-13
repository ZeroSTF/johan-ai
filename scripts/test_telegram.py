"""Offline test of the Telegram bridge — no token, no network.

A FakeBot records every Bot API call the bridge would make. Proves, before
a phone is ever paired, that the allowlist holds (a stranger gets one refusal
and NO model or tool call), pairing mode leaks nothing, commands work, and a
chat message flows through today's engine. Needs Ollama for the chat case.

    python test_telegram.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja        # noqa: E402
import johan_telegram as tg          # noqa: E402

ME, STRANGER = 1111, 9999


class FakeBot(tg.Bot):
    def __init__(self, allowed):
        super().__init__("fake-token", allowed)
        self.calls = []

    def call(self, method, http_timeout=40, files=None, **params):
        self.calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(self.calls), "username": "fakebot"}}

    def texts(self):
        return [p.get("text", "") for m, p in self.calls if m in ("sendMessage", "editMessageText")]


def update(chat, text):
    return {"message": {"chat": {"id": chat}, "text": text, "from": {"username": "x"}}}


def draw_checks():
    """/draw with the image engine and Ollama stubbed — no GPU, no ComfyUI.
    Threads run inline so each result is in before the next assertion."""
    import threading
    import johan_images as ji
    results, drawn = {}, []
    real = (ji.generate_image, tg.ollama_up, threading.Thread, tg.Bot.send_photo)

    class Inline:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    def fake_generate(prompt, **kw):
        drawn.append((prompt, kw))
        return {"path": "/tmp/fake.png", "seed": 7}
    ji.generate_image = fake_generate
    threading.Thread = Inline
    tg.Bot.send_photo = lambda self, chat, path, caption="": self.calls.append(
        ("sendPhoto", {"caption": caption})) or True
    try:
        results["parse: shape and strength, either order"] = (
            tg.parse_draw("landscape 0.4 a red fox in snow")
            == ("landscape", "anime", 0.4, "a red fox in snow")
            and tg.parse_draw("0.4 landscape a red fox")[:3] == ("landscape", "anime", 0.4))
        results["parse: strength, and a number in the text stays text"] = (
            tg.parse_draw("0.4 at night")[2:] == (0.4, "at night")
            and tg.parse_draw("3 cats on a sofa") == (None, "anime", None, "3 cats on a sofa"))

        tg.ollama_up = lambda: False
        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "/draw a castle"))
        results["/draw refused while paused (GameMode)"] = (
            not drawn and any("paused" in t for t in bot.texts()))

        tg.ollama_up = lambda: True
        bot = FakeBot({ME})
        tg.handle_update(bot, update(STRANGER, "/draw a castle"))
        results["/draw from a stranger draws nothing"] = not drawn

        tg.handle_update(bot, update(ME, "/draw landscape a castle at dusk"))
        results["/draw draws, with style and size"] = drawn[-1:] == [(
            "a castle at dusk", {"size": "landscape", "reference": None,
                                 "strength": None, "style": "anime"})]
        results["…and sends the picture"] = any(m == "sendPhoto" for m, _ in bot.calls)
        results["…and frees the slot"] = not bot.drawing

        bot.drawing = True
        n = len(drawn)
        tg.handle_update(bot, update(ME, "/draw another"))
        results["one drawing at a time"] = len(drawn) == n and any(
            "Still drawing" in t for t in bot.texts())
        bot.drawing = False

        bot.pending_ref[ME] = "/tmp/his-photo.jpg"
        tg.handle_update(bot, update(ME, "/draw"))
        results["bare /draw keeps his picture"] = bot.pending_ref.get(ME) == "/tmp/his-photo.jpg"
        tg.handle_update(bot, update(ME, "/draw 0.3 at night"))
        results["/draw after a photo redraws it"] = drawn[-1][1]["reference"] == \
            "/tmp/his-photo.jpg" and drawn[-1][1]["strength"] == 0.3 and ME not in bot.pending_ref

        ji.generate_image = lambda prompt, **kw: {"error": "engine exploded"}
        tg.handle_update(bot, update(ME, "/draw a tree"))
        results["engine error reported, slot freed"] = (
            any("engine exploded" in t for t in bot.texts()) and not bot.drawing)
    finally:
        ji.generate_image, tg.ollama_up, threading.Thread, tg.Bot.send_photo = real
    return results


def file_command_checks():
    """commands/*.md over Telegram, against a temp commands dir with the
    engine stubbed: the expanded prompt reaches the model, a built-in name
    still runs the built-in, and a stranger reaches nothing."""
    import tempfile
    import johan_commands as jc
    tmp = Path(tempfile.mkdtemp())
    (tmp / "tldr.md").write_text("---\ndescription: summarise a page\nargs: <url>\n---\n"
                                 "Read $ARGS and summarise it.\n")
    (tmp / "jobs.md").write_text("HIJACKED jobs prompt\n")         # clashes with /jobs
    sent, results = [], {}
    real = (jc.COMMANDS_DIR, ja.run_turn, tg.ollama_up, ja.prime_with_memories, aq_latest())

    def fake_run_turn(messages, **kw):
        sent.append(messages[-1]["content"])
        return "done"
    jc.COMMANDS_DIR = tmp
    ja.run_turn, tg.ollama_up = fake_run_turn, (lambda: True)
    ja.prime_with_memories = lambda s, *a, **k: s
    tg.aq.latest_queue = lambda: None
    try:
        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "/tldr https://example.org/post"))
        results["file command sends its expanded prompt"] = \
            sent == ["Read https://example.org/post and summarise it."]
        results["…as a normal chat turn in his history"] = (
            bot.histories[ME][-1]["content"] == "Read https://example.org/post and summarise it.")

        sent.clear()
        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "/tldr"))
        results["file command without its args shows usage"] = (
            not sent and any("usage: /tldr <url>" in t for t in bot.texts()))

        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "/jobs"))
        results["a built-in beats a file with its name"] = (
            not sent and any("No queue" in t for t in bot.texts()))

        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "/help"))
        helptext = "\n".join(bot.texts())
        results["/help lists file commands"] = "/tldr <url>" in helptext and "HIJACKED" not in helptext

        bot = FakeBot({ME})
        tg.handle_update(bot, update(STRANGER, "/tldr https://example.org"))
        results["stranger's file command reaches no model"] = not sent and any(
            "private" in t for t in bot.texts())
    finally:
        jc.COMMANDS_DIR, ja.run_turn, tg.ollama_up, ja.prime_with_memories, tg.aq.latest_queue = real
        for p in tmp.iterdir():
            p.unlink()
        tmp.rmdir()
    return results


def aq_latest():
    return tg.aq.latest_queue


def press(chat, data, from_id=None, message_id=1):
    return {"callback_query": {"id": "cb1", "data": data,
                               "from": {"id": from_id or chat, "username": "x"},
                               "message": {"message_id": message_id, "chat": {"id": chat}}}}


def markups(bot):
    """Every inline keyboard the bridge sent, as (labels, callback_data)."""
    out = []
    for m, p in bot.calls:
        kb = (p.get("reply_markup") or {}).get("inline_keyboard")
        if kb:
            out.append(([b["text"] for row in kb for b in row],
                        [b["callback_data"] for row in kb for b in row]))
    return out


def button_checks():
    """Ask-with-options and plan approval as inline buttons, engine and
    planner stubbed — no model. The allowlist must hold for button presses
    exactly as for messages."""
    import johan_plan as jp
    results, turns = {}, []
    real = (ja.run_turn, tg.ollama_up, jp.needs_plan, jp.make_plan)

    def fake_turn(history, on_tool=None, on_delta=None, **k):
        turns.append(history[-1]["content"])
        if history[-1]["content"] == "change my wallpaper":
            res = jp.ask("Which wallpaper?", ["A photo of mine", "Generate one"])
            on_tool("ask", {}, res)
            history.append({"role": "assistant", "content": jp.render_ask(res)})
            return jp.render_ask(res)
        if history[-1]["content"] == jp.GO_PROMPT:
            on_tool("run_command", {}, {"output": "docker not installed"})
            on_tool("propose_action", {}, {"staged": 1})
            return "Staged the install — approve it in F4."
        return "ok"
    plans = []

    def fake_make_plan(history, changes=None, **k):
        plans.append(changes)
        history.extend([{"role": "user", "content": jp.PLAN_PROMPT},
                        {"role": "assistant", "content": '{"steps": []}'}])
        return [{"do": "Check docker", "tool": "run_command"},
                {"do": "Stage the install", "tool": "propose_action"}]
    ja.run_turn, tg.ollama_up, jp.make_plan = fake_turn, (lambda: True), fake_make_plan
    jp.needs_plan = lambda text, **k: False
    try:
        # --- ask -> buttons -> press -> his next message
        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "change my wallpaper"))
        kbs = markups(bot)
        results["ask: options sent as inline buttons"] = bool(kbs) and kbs[-1][0] == [
            "A photo of mine", "Generate one"]
        data = kbs[-1][1][1] if kbs else "x:1"

        n = len(turns)
        tg.handle_update(bot, press(STRANGER, data))
        results["stranger's button press refused"] = any(
            m == "answerCallbackQuery" and "private" in p.get("text", "") for m, p in bot.calls)
        results["stranger's press reached no model"] = len(turns) == n
        results["…and the question is still his"] = ME in bot.pending

        pairing = FakeBot(set())
        pairing.pending[STRANGER] = {"kind": "ask", "id": "a", "labels": ["x", "y"],
                                     "text": "q", "message_id": 1}
        tg.handle_update(pairing, press(STRANGER, "a:0"))
        results["pairing mode: button refused too"] = (len(turns) == n
                                                       and STRANGER in pairing.pending)

        tg.handle_update(bot, press(ME, data))
        results["his press sends the option as his message"] = turns[-1] == "Generate one"
        results["…and the buttons are gone"] = ME not in bot.pending

        tg.handle_update(bot, press(ME, data))
        results["stale press says expired, runs nothing"] = turns[-1] == "Generate one" and any(
            "expired" in p.get("text", "") for m, p in bot.calls if m == "answerCallbackQuery")

        # --- typed answer instead of a button
        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "change my wallpaper"))
        tg.handle_update(bot, update(ME, "use the one in ~/Pictures/cat.png"))
        results["typed answer works and drops the buttons"] = (
            turns[-1] == "use the one in ~/Pictures/cat.png" and ME not in bot.pending
            and any(m == "editMessageReplyMarkup" for m, _ in bot.calls))

        # --- plan -> go -> checklist
        jp.needs_plan = lambda text, **k: True
        bot = FakeBot({ME})
        n = len(turns)
        tg.handle_update(bot, update(ME, "install docker and add me to the docker group"))
        kbs = markups(bot)
        results["plan: shown with go/cancel, nothing ran"] = (
            bool(kbs) and kbs[-1][0] == tg.PLAN_LABELS and len(turns) == n)
        go = kbs[-1][1][0] if kbs else "x:0"
        tg.handle_update(bot, press(STRANGER, go))
        results["plan: stranger's go runs nothing"] = len(turns) == n
        tg.handle_update(bot, press(ME, go))
        results["plan: go runs the approved plan"] = turns[-1] == jp.GO_PROMPT
        edits = [p.get("text", "") for m, p in bot.calls if m == "editMessageText"]
        results["checklist: final message ticks both steps"] = bool(edits) and (
            "Plan 2/2" in edits[-1] and edits[-1].count("✓") == 2 and "F4" in edits[-1])

        # --- plan -> typed changes -> re-plan; then cancel
        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "install docker and add me to the docker group"))
        tg.handle_update(bot, update(ME, "use podman instead"))
        results["plan: typed text re-plans with his changes"] = plans[-1] == "use podman instead"
        n = len(turns)
        cancel = markups(bot)[-1][1][1]
        tg.handle_update(bot, press(ME, cancel))
        results["plan: cancel runs nothing and says so"] = len(turns) == n and any(
            jp.CANCELLED in t for t in bot.texts())
        results["…and is recorded in history"] = bot.history(ME)[-1]["content"] == jp.CANCELLED

        # --- edits are throttled (Telegram rate-limits them)
        def chatty(history, on_tool=None, on_delta=None, **k):
            for _ in range(50):
                on_delta("tok ")
            return "done"
        ja.run_turn = chatty
        bot = FakeBot({ME})
        bot.history(ME).append({"role": "user", "content": "x"})
        tg.run_chat_turn(bot, ME, progress=jp.Progress([{"do": "a", "tool": "none"},
                                                        {"do": "b", "tool": "none"}]))
        edits = [p for m, p in bot.calls if m == "editMessageText"]
        results["50 streamed tokens -> throttled edits (<= 2 + final)"] = len(edits) <= 3
    finally:
        ja.run_turn, tg.ollama_up, jp.needs_plan, jp.make_plan = real
    return results


def main():
    results = {}
    engine_calls = []
    real_run_turn = ja.run_turn

    def spy(*a, **k):
        engine_calls.append(1)
        return real_run_turn(*a, **k)
    ja.run_turn = spy
    try:
        bot = FakeBot({ME})
        tg.handle_update(bot, update(STRANGER, "what are my memories? list my files"))
        results["stranger refused"] = any("private" in t for t in bot.texts())
        results["stranger reached NO model or tool"] = not engine_calls
        results["stranger saw none of his data"] = not any("Riadh" in t or "riadh" in t for t in bot.texts())

        bot = FakeBot(set())
        tg.handle_update(bot, update(STRANGER, "hi"))
        results["pairing mode shows only the chat id"] = any(str(STRANGER) in t for t in bot.texts()) and not engine_calls

        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "/help"))
        results["/help answers"] = any("/jobs" in t for t in bot.texts())
        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "/nosuchcommand"))
        results["unknown command handled"] = any("No command" in t for t in bot.texts())

        bot = FakeBot({ME})
        tg.handle_update(bot, update(ME, "hi"))
        results["chat message goes through the engine"] = bool(engine_calls)
        results["…and a reply is sent"] = any(t.strip() and "Something broke" not in t for t in bot.texts())
    finally:
        ja.run_turn = real_run_turn

    results.update(draw_checks())
    results.update(file_command_checks())
    results.update(button_checks())

    saved = {k: os.environ.pop(k, None) for k in ("DISPLAY", "WAYLAND_DISPLAY")}
    try:
        r = ja.DISPATCH["autofill_application"](142)
        results["autofill refuses without a screen"] = "PC's screen" in r.get("error", "")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    for k, ok in results.items():
        print(f"{'ok  ' if ok else 'FAIL'}  {k}")
    bad = [k for k, ok in results.items() if not ok]
    print("\nall telegram checks pass" if not bad else f"\n{len(bad)} FAILURE(S)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
