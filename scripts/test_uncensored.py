"""/uncensored swaps the whole engine to the uncensored model, and back.

Checked: every kind of model call (chat, decide, plan) uses the selected
model — a mix would reload models every few seconds on a 6 GB card; a new
chat starts normal; a missing model is refused with a clear note; the status
line says "uncensored". Model calls are stubbed — no GPU, nothing under brain/.

    python test_uncensored.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja        # noqa: E402
import johan_decide as jd           # noqa: E402
import johan_interrupt as ji        # noqa: E402
import johan_plan as jp             # noqa: E402
import johan_telegram as tg         # noqa: E402
import johan_tui as tui             # noqa: E402

results = {}


def check(label, ok):
    results[label] = bool(ok)


def engine_checks():
    seen = []

    def fake_post(url, payload, timeout=180):
        seen.append(payload["model"])
        return {"message": {"role": "assistant", "content": '{"steps": []}'},
                "logprobs": [{"top_logprobs": [{"token": "A", "logprob": -0.1}]}]}
    real = ji.ollama_post
    ji.ollama_post = fake_post
    try:
        ja.use_model(ja.UNCENSORED_MODEL)
        ja.call_model([{"role": "user", "content": "x"}])
        try:
            jd.decide("q?", [("a", "first"), ("b", "second")], log=False, calibrate=False)
        except TypeError:
            jd.decide("q?", [("a", "first"), ("b", "second")], log=False)
        jp._plan_call([{"role": "user", "content": "x"}], ja)
        check("chat, decide and plan all use the uncensored model",
              seen and all(m == ja.UNCENSORED_MODEL for m in seen))
        ja.use_model(ja.NORMAL_MODEL)
        seen.clear()
        ja.call_model([{"role": "user", "content": "x"}])
        check("/normal puts every call back on the normal model", seen == [ja.NORMAL_MODEL])
    finally:
        ji.ollama_post = real
        ja.use_model(ja.NORMAL_MODEL)


async def tui_checks():
    saved = {}

    def stub(obj, name, val):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)
    warmed = []
    stub(tui.JohanApp, "warm_up", lambda self: warmed.append(ja.MODEL))
    stub(tui.Sidebar, "refresh_state", lambda self, messages=None: None)
    stub(ja, "save_session", lambda m: None)
    stub(ja, "log_session", lambda m: None)
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(140, 45)) as pilot:
            box = app.query_one("#prompt")

            async def say(text):
                box.focus()
                box.value = text
                await pilot.press("enter")
                await pilot.pause()

            stub(ja, "model_installed", lambda name: False)
            await say("/uncensored")
            check("not installed: refused, model unchanged", ja.MODEL == ja.NORMAL_MODEL)
            setattr(ja, "model_installed", lambda name: True)
            await say("/uncensored")
            check("/uncensored switches the engine", ja.is_uncensored())
            check("…and loads it right away", warmed[-1:] == [ja.UNCENSORED_MODEL])
            status = str(app.query_one("#statusr").render())
            check("the status line says uncensored", "uncensored" in status)
            await say("/normal")
            check("/normal switches back", ja.MODEL == ja.NORMAL_MODEL)
            await say("/uncensored")
            await pilot.press("ctrl+n")
            await pilot.pause()
            check("a new chat starts normal", ja.MODEL == ja.NORMAL_MODEL)
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)
        ja.use_model(ja.NORMAL_MODEL)


def telegram_checks():
    class FakeBot(tg.Bot):
        def __init__(self):
            super().__init__("fake", {1})
            self.sent = []

        def call(self, method, http_timeout=40, files=None, **params):
            self.sent.append(params.get("text", ""))
            return {"ok": True, "result": {"message_id": 1}}
    real = ja.model_installed
    ja.model_installed = lambda name: True
    try:
        bot = FakeBot()
        tg.handle_update(bot, {"message": {"chat": {"id": 1}, "text": "/uncensored", "from": {}}})
        check("telegram /uncensored switches", ja.is_uncensored())
        tg.handle_update(bot, {"message": {"chat": {"id": 1}, "text": "/new", "from": {}}})
        check("telegram /new starts normal", not ja.is_uncensored())
        tg.handle_update(bot, {"message": {"chat": {"id": 9}, "text": "/uncensored", "from": {}}})
        check("a stranger can't switch it", not ja.is_uncensored())
    finally:
        ja.model_installed = real
        ja.use_model(ja.NORMAL_MODEL)


engine_checks()
asyncio.run(tui_checks())
telegram_checks()
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
