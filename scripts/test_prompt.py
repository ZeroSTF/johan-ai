"""The prompt takes multi-line input and still behaves like the old one.

The Input it replaced was single-line by design: shift+enter did nothing and
a pasted multi-line block collapsed onto one line. Model stubbed; nothing
written under brain/.

    python test_prompt.py
"""
import asyncio
import sys
from pathlib import Path

from textual import events

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja        # noqa: E402
import johan_plan as jp             # noqa: E402
import johan_tui as tui             # noqa: E402

results = {}
BLOCK = 'function copy-johan() {\n  local extra_excludes=""\n  if [[ "$1" == "--small" ]]; then'


def check(label, ok):
    results[label] = bool(ok)


async def main():
    sent = []
    saved = {}

    def stub(obj, name, val):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)

    def fake_turn(messages, **kw):
        sent.append(messages[-1]["content"])
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"
    stub(ja, "run_turn", fake_turn)
    stub(ja, "save_session", lambda m: None)
    stub(ja, "log_session", lambda m: None)
    stub(ja, "prime_with_memories", lambda s, **k: s)
    stub(jp, "prefilter", lambda text: False)
    stub(tui.JohanApp, "warm_up", lambda self: None)
    stub(tui.Sidebar, "refresh_state", lambda self, messages=None: None)
    rewound = []
    stub(tui.JohanApp, "action_rewind", lambda self: rewound.append(1))
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(140, 45)) as pilot:
            box = app.query_one("#prompt")
            box.focus()
            await pilot.press(*"first")
            await pilot.press("shift+enter")
            await pilot.press(*"second")
            check("shift+enter inserts a newline", box.value == "first\nsecond")
            await pilot.press("ctrl+j")
            await pilot.press(*"third")
            check("ctrl+j inserts a newline", box.value == "first\nsecond\nthird")
            await pilot.pause()
            check("the box grows with its lines", box.styles.height.value >= 3)

            box.value = ""
            app.post_message(events.Paste(BLOCK))      # as the terminal delivers it
            await pilot.pause()
            await pilot.pause()
            check("a pasted multi-line block keeps its newlines", box.value == BLOCK)

            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            check("enter sends the whole multi-line message", sent[-1:] == [BLOCK])
            check("…and empties the box", box.value == "")

            await pilot.press("up")
            await pilot.pause()
            check("↑ on an empty box recalls the last question", box.value == BLOCK)
            box.value = "a\nb"
            box.cursor_position = 3          # on line 2
            await pilot.press("up")
            check("↑ inside a multi-line message moves the cursor, not history",
                  box.value == "a\nb" and box.cursor_location[0] == 0)

            box.value = ""
            await pilot.press(*"/dra")
            await pilot.pause()
            check("a slash command shows its completion", box.suggestion.startswith("w"))
            await pilot.press("tab")
            await pilot.pause()
            check("tab completes it", box.value.startswith("/draw"))

            box.value = "x"
            await pilot.press("ctrl+z")
            check("ctrl+z still rewinds (not the text box's undo)", rewound == [1])

            box.value = "/draw "
            check("setting value puts the cursor at the end",
                  box.cursor_position == len("/draw "))

            # Typing your own answer while options are shown: Choices hands the
            # letter to the prompt. That called an Input-only method and
            # crashed the app once the prompt became multi-line.
            from johan_widgets import Choices
            box.value = ""
            ch = Choices(["one", "two"])
            await app._chat().mount(ch)
            ch.focus()
            await pilot.pause()
            await pilot.press("h", "i")
            await pilot.pause()
            check("typing over options goes to the prompt (no crash)", box.value == "hi")
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)


asyncio.run(main())
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
