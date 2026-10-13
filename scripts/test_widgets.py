"""Headless Textual pilot check of the ask options and the plan checklist.

Model, planner and session saving are stubbed: no GPU, nothing written
under ~/johan (save_session/log_session are replaced), no Ollama warm-up.

    python test_widgets.py
"""
import asyncio
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from textual.app import App           # noqa: E402
from textual.widgets import Input     # noqa: E402

import johan_assistant as ja          # noqa: E402
import johan_plan as jp               # noqa: E402
import johan_tui as tui               # noqa: E402
from johan_widgets import Choices, PlanCard  # noqa: E402

FAILURES = []


def check(label, got, want=True):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f": got {got!r}, want {want!r}"))
    if not ok:
        FAILURES.append(label)


# ----------------------------------------------------------- widgets alone

class Harness(App):
    def __init__(self):
        super().__init__()
        self.picked = []

    def compose(self):
        yield Choices(["Spotify", "VLC", "mpv"], id="ch")
        yield PlanCard([{"do": "Check docker", "tool": "run_command"},
                        {"do": "Stage the install", "tool": "propose_action"},
                        {"do": "Tell him", "tool": "none"}], id="plan")
        yield Input(id="prompt")

    def on_choices_picked(self, event):
        self.picked.append((event.choices.id or "plan", event.index, event.text))


async def widgets_alone():
    app = Harness()
    async with app.run_test() as pilot:
        ch = app.query_one("#ch", Choices)
        ch.focus()
        await pilot.press("down", "down", "up", "enter")
        check("choices: arrows + enter pick the highlighted option", app.picked[-1],
              ("ch", 1, "VLC"))
        await pilot.press("3")
        check("choices: answered choices take no more keys", len(app.picked), 1)

        ch.done = False
        await pilot.press("3")
        check("choices: number key picks", app.picked[-1], ("ch", 2, "mpv"))

        ch.done = False
        ch.focus()
        await pilot.press("h", "i")
        box = app.query_one("#prompt", Input)
        check("choices: typing goes to the prompt (free answer)", (box.value, box.has_focus),
              ("hi", True))

        card = app.query_one("#plan", PlanCard)
        card.choices.focus()
        await pilot.press("y")
        check("plan: y = go ahead", app.picked[-1], ("plan", 0, "Go ahead"))
        card.start()
        await pilot.pause()
        check("plan: choices gone once approved", card.choices.has_class("used"))
        card.tick("run_command")
        card.tick("propose_action")
        await pilot.pause()
        lines = str(card.checklist.render()).splitlines()
        check("checklist: step 1 ticked as step 2 starts", (lines[0][0], lines[1][0]), ("✓", "▸"))
        card.finish(answered=True)
        await pilot.pause()
        lines = str(card.checklist.render()).splitlines()
        check("checklist: all ticked at the end", [ln[0] for ln in lines], ["✓", "✓", "✓"])
        check("checklist: title says done", "3/3" in str(card.title.render()))


# ----------------------------------------------------------- inside the app

async def in_the_app():
    calls = {"turns": [], "plans": []}
    stubs = {}

    def stub(obj, name, value):
        stubs[(obj, name)] = getattr(obj, name)
        setattr(obj, name, value)

    def fake_turn(messages, on_tool=None, on_delta=None, should_stop=None, **k):
        q = messages[-1]["content"]
        calls["turns"].append(q)
        if q == "change my wallpaper":
            res = jp.ask("Which wallpaper?", ["A photo of mine", "Generate one"])
            on_tool("ask", {}, res)
            messages.append({"role": "assistant", "content": jp.render_ask(res)})
            return jp.render_ask(res)
        if q == jp.GO_PROMPT:
            on_tool("run_command", {"command": "which docker"}, {"output": ""})
            on_tool("propose_action", {"commands": "apt install docker.io"}, {"staged": 1})
            messages.append({"role": "assistant", "content": "Staged — approve in F4."})
            return "Staged — approve in F4."
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    def fake_plan(messages, changes=None, **k):
        calls["plans"].append(changes)
        steps = [{"do": "Check docker", "tool": "run_command"},
                 {"do": "Stage the install", "tool": "propose_action"}]
        messages.extend([{"role": "user", "content": jp.PLAN_PROMPT},
                         {"role": "assistant", "content": json.dumps({"steps": steps})}])
        return steps

    stub(ja, "run_turn", fake_turn)
    stub(ja, "save_session", lambda m: None)          # never touch brain/
    stub(ja, "log_session", lambda m: None)
    stub(ja, "prime_with_memories", lambda s, **k: s)
    stub(jp, "needs_plan", lambda text, **k: "docker" in text)
    stub(jp, "make_plan", fake_plan)
    stub(tui.JohanApp, "warm_up", lambda self: None)
    stub(tui.Sidebar, "refresh_state", lambda self, messages=None: None)
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(140, 45)) as pilot:
            def shot(name):
                # JOHAN_SHOTS=<dir> saves SVG screenshots to look at
                if os.environ.get("JOHAN_SHOTS"):
                    app.save_screenshot(f"{name}.svg", path=os.environ["JOHAN_SHOTS"])

            async def say(text):
                box = app.query_one("#prompt", Input)
                box.focus()
                box.value = text
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.pause()

            await say("change my wallpaper")
            ch = app.pending
            check("app: ask shows selectable choices", isinstance(ch, Choices)
                  and ch.options == ["A photo of mine", "Generate one"])
            shot("ask")
            check("app: choices have focus", ch is not None and ch.has_focus)
            check("app: the turn shows only the question (options are the choices)",
                  app.turn.answer.source, "Which wallpaper?")
            await pilot.press("2")
            await app.workers.wait_for_complete()
            await pilot.pause()
            check("app: the pick is sent as his next message", calls["turns"][-1], "Generate one")
            check("app: …and recorded as a user message",
                  [m["content"] for m in app.messages if m["role"] == "user"][-1], "Generate one")

            n = len(calls["turns"])
            await say("install docker and add me to the docker group")
            card = app.pending
            shot("plan")
            check("app: big request shows a plan, nothing ran",
                  isinstance(card, PlanCard) and len(calls["turns"]) == n)
            await say("use podman instead")
            check("app: typed text re-plans with his changes", calls["plans"][-1],
                  "use podman instead")
            check("app: the old plan card is superseded", card.choices.has_class("used"))
            card = app.pending
            card.choices.focus()
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.pause()
            check("app: y runs the approved plan", calls["turns"][-1], jp.GO_PROMPT)
            shot("checklist")
            check("app: checklist ticked off from the tool calls",
                  [s["state"] for s in card.progress.steps], ["done", "done"])
            check("app: nothing pending after the run", app.pending, None)

            n = len(calls["turns"])
            await say("install docker and add me to the docker group")
            await pilot.press("escape")
            await pilot.pause()
            check("app: esc on a waiting plan cancels it", (app.pending, len(calls["turns"])),
                  (None, n))
            check("app: …recorded in the conversation", app.messages[-1]["content"], jp.CANCELLED)

            await say("what's my disk usage")
            check("app: normal question never plans", (calls["turns"][-1], len(calls["plans"])),
                  ("what's my disk usage", 3))
    finally:
        for (obj, name), value in stubs.items():
            setattr(obj, name, value)


asyncio.run(widgets_alone())
asyncio.run(in_the_app())
print(f"\n{'all widget checks pass' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'}")
sys.exit(1 if FAILURES else 0)
