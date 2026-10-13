"""Chat widgets for ask-with-options and plan-first (johan_plan.py).

Kept out of johan_tui.py so the app file only gains the glue.

  Choices    selectable options. Number keys pick, ↑/↓ + enter pick the
             highlighted one, a click picks; typing anything else jumps to
             the prompt with that character, so a free answer still works.
  Checklist  plan steps with live marks, driven by johan_plan.Progress.
  PlanCard   a Checklist plus Choices(go ahead / cancel) until approved.
"""

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

import johan_plan as jp

STYLE = {jp.PENDING: "$text-muted", jp.ACTIVE: "$accent", jp.DONE: "$success",
         jp.FAILED: "$error", jp.SKIPPED: "$text-muted"}


def _esc(s):
    return Text(str(s)).markup


class Choices(Widget, can_focus=True):
    """Options he can pick. Posts Choices.Picked; never acts by itself."""

    DEFAULT_CSS = """
    Choices { height: auto; margin: 0 0 0 1; padding: 0 1; }
    Choices > .choice { height: auto; }
    Choices:focus { border-left: outer $accent; }
    Choices.used { display: none; }
    """
    BINDINGS = [Binding("up", "move(-1)", "prev", show=False),
                Binding("down", "move(1)", "next", show=False),
                Binding("enter", "pick", "pick")]

    class Picked(Message):
        def __init__(self, choices, index, text):
            super().__init__()
            self.choices, self.index, self.text = choices, index, text

    def __init__(self, options, keys=None, hint=None, **kw):
        super().__init__(**kw)
        self.options = list(options)
        self.keys = dict(keys or {})           # extra letter shortcuts, e.g. y/n
        self.hint = hint
        self.index = 0
        self.done = False
        self._lines = []

    def compose(self):
        for i, _ in enumerate(self.options):
            line = Static("", classes="choice")
            line.choice_index = i
            self._lines.append(line)
            yield line
        yield Static(f"[$text-muted]{_esc(self.hint or 'number or ↑/↓ + enter to pick · or type your own answer')}[/]",
                     classes="choice-hint")

    def on_mount(self):
        self._paint()

    def _paint(self):
        for i, (line, opt) in enumerate(zip(self._lines, self.options)):
            here = i == self.index and not self.done
            mark = "[b $accent]›[/]" if here else " "
            label = f"[b]{_esc(opt)}[/]" if here else _esc(opt)
            line.update(f"{mark} [$accent]{i + 1}[/] {label}")

    def action_move(self, step):
        if self.options and not self.done:
            self.index = (self.index + step) % len(self.options)
            self._paint()

    def action_pick(self):
        self.pick(self.index)

    def pick(self, i):
        if self.done or not 0 <= i < len(self.options):
            return
        self.done = True
        self.index = i
        self.post_message(self.Picked(self, i, self.options[i]))

    def retire(self):
        """Answered (or superseded): stop taking keys, keep nothing on screen."""
        self.done = True
        self.add_class("used")

    def on_click(self, event):
        w = event.widget
        while w is not None and w is not self:
            if hasattr(w, "choice_index"):
                return self.pick(w.choice_index)
            w = w.parent

    def on_key(self, event):
        ch = event.character or ""
        if self.done:
            return
        if ch.isdigit() and 1 <= int(ch) <= len(self.options):
            event.stop()
            return self.pick(int(ch) - 1)
        if ch.lower() in self.keys:
            event.stop()
            return self.pick(self.keys[ch.lower()])
        if ch and ch.isprintable() and event.key not in ("enter", "up", "down"):
            # A free answer: hand the keystroke to the prompt.
            event.stop()
            try:
                box = self.app.query_one("#prompt")
            except Exception:
                return
            box.focus()

            def type_it():
                # after the focus event: Input selects all on focus, and a
                # selection would swallow this first character
                box.cursor_position = len(box.value)
                box.insert_text_at_cursor(ch)
            box.call_after_refresh(type_it)


class Checklist(Static):
    DEFAULT_CSS = "Checklist { height: auto; margin: 0 0 0 1; padding: 0 1; }"

    def __init__(self, progress, **kw):
        super().__init__("", **kw)
        self.progress = progress

    def on_mount(self):
        self.paint()

    def paint(self):
        rows = []
        for s, line in zip(self.progress.steps, self.progress.lines()):
            st = STYLE[s["state"]]
            mark, _, text = line.partition(" ")
            body = f"[{st}]{_esc(text)}[/]" if s["state"] in (jp.SKIPPED, jp.PENDING) else _esc(text)
            rows.append(f"[{st}]{mark}[/] {body}")
        self.update("\n".join(rows))


class PlanCard(Vertical):
    """The plan, waiting for a yes; then the live checklist of the run."""

    DEFAULT_CSS = """
    PlanCard { height: auto; margin: 1 0 0 1; padding: 0 1; border-left: outer $warning; }
    PlanCard > .plan-title { height: auto; }
    """

    def __init__(self, steps, **kw):
        super().__init__(**kw)
        self.progress = jp.Progress(steps)
        self.title = Static("", classes="plan-title")
        self.checklist = Checklist(self.progress)
        self.choices = Choices(["Go ahead", "Cancel"], keys={"y": 0, "n": 1},
                               hint="enter/y go · n cancel · or type changes to the plan")
        self._title("[b $warning]plan[/] [$text-muted]· nothing runs until you say go[/]")

    def compose(self):
        yield self.title
        yield self.checklist
        yield self.choices

    def _title(self, markup):
        self.title.update(markup)

    def start(self):
        """Approved: the choices go, the checklist starts ticking."""
        self.choices.retire()
        self._title("[b $accent]plan[/] [$text-muted]· running[/]")
        self.checklist.paint()

    def tick(self, name, failed=False):
        self.progress.on_tool(name, failed)
        self.checklist.paint()

    def finish(self, answered=True, stopped=False):
        self.progress.finish(answered=answered and not stopped)
        done, total = self.progress.counts()
        word = "stopped" if stopped else "done"
        self._title(f"[b $success]plan[/] [$text-muted]· {word} · {done}/{total} steps[/]")
        self.checklist.paint()

    def cancelled(self):
        self.choices.retire()
        for s in self.progress.steps:
            s["state"] = jp.SKIPPED
        self._title("[b $text-muted]plan · cancelled — nothing ran[/]")
        self.checklist.paint()

    def superseded(self):
        self.choices.retire()
        self._title("[$text-muted]plan · replaced by the edited one below[/]")
        self.checklist.display = False
