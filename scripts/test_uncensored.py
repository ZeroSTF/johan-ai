"""/uncensored swaps the whole engine to the uncensored model, and back.

Checked: every kind of model call (chat, decide, plan) uses the selected
model — a mix would reload models every few seconds on a 6 GB card; a new
chat keeps the selected model; a missing model is refused with a clear note; the status
line says "uncensored". Model calls are stubbed — no GPU, nothing under brain/.

    python test_uncensored.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fake_llm                     # noqa: E402  (first: no real model in this test)
import johan_assistant as ja        # noqa: E402
import johan_decide as jd           # noqa: E402
import johan_interrupt as ji        # noqa: E402
import johan_plan as jp             # noqa: E402
import johan_tui as tui             # noqa: E402
# His REAL pending changes must never surface in a test (a stray enter on a
# waiting card could approve one): no 'waiting from earlier' cards here.
tui.JohanApp._show_backlog = lambda self, intro=True: None

results = {}


def check(label, ok):
    results[label] = bool(ok)


def engine_checks():
    seen = []

    def fake_post(payload):
        seen.append(payload["model"])
        return {"message": {"role": "assistant", "content": '{"steps": []}'},
                "logprobs": [{"top_logprobs": [{"token": "A", "logprob": -0.1}]}]}
    fake_llm.install(fake_post)
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
        fake_llm.uninstall()
        ja.use_model(ja.NORMAL_MODEL)


async def tui_checks():
    saved = {}

    def stub(obj, name, val):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)
    warmed = []
    stub(tui.JohanApp, "warm_up", lambda self: warmed.append(ja.MODEL))
    stub(tui.JohanApp, "_warm_up_model", lambda self, model: warmed.append(model))
    stub(tui.Sidebar, "refresh_state", lambda self, messages=None: None)
    stub(ja, "release_model", lambda model=None: True)
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
            await pilot.press("ctrl+n")
            await pilot.pause()
            check("a new chat keeps uncensored mode", ja.MODEL == ja.UNCENSORED_MODEL)
            await say("/normal")
            check("/normal switches back", ja.MODEL == ja.NORMAL_MODEL)
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)
        ja.use_model(ja.NORMAL_MODEL)



engine_checks()
asyncio.run(tui_checks())
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
