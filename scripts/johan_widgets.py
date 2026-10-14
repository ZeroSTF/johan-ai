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
from textual.content import Content
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

import johan_plan as jp

STYLE = {jp.PENDING: "$text-muted", jp.ACTIVE: "$accent", jp.DONE: "$success",
         jp.FAILED: "$error", jp.SKIPPED: "$text-muted"}


def mk(markup, **values):
    """Markup for OUR labels; model/user text only ever as a $variable —
    inserted as plain text, never parsed (see johan_tui.mk for why)."""
    return Content.from_markup(markup, **{k: str(v) for k, v in values.items()})


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
        yield Static(mk("[$text-muted]$h[/]", h=self.hint or "number or ↑/↓ + enter to pick · or type your own answer"),
                     classes="choice-hint")

    def on_mount(self):
        self._paint()

    def _paint(self):
        for i, (line, opt) in enumerate(zip(self._lines, self.options)):
            here = i == self.index and not self.done
            mark = "[b $accent]›[/]" if here else " "
            line.update(mk(f"{mark} [$accent]{i + 1}[/] " + ("[b]$o[/]" if here else "$o"), o=opt))

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
            body = f"[{st}]$t[/]" if s["state"] in (jp.SKIPPED, jp.PENDING) else "$t"
            rows.append(mk(f"[{st}]$m[/] {body}", m=mark, t=text))
        self.update(Content("\n").join(rows))


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


# ================================================================ the prompt
from textual.binding import Binding        # noqa: E402
from textual.widgets import Input, TextArea  # noqa: E402


class PromptArea(TextArea):
    """The chat prompt: multi-line, but it behaves like the old one-line Input.

    The Input it replaces is single-line by design: shift+enter did nothing
    and a pasted multi-line block was collapsed onto one line. This keeps the
    Input interface the app already uses everywhere (`value`,
    `cursor_position`, `placeholder`, an `Input.Submitted` message) so no
    call site had to change.

      enter                    send
      shift+enter / alt+enter  new line (shift+enter only if the terminal
      ctrl+j                   reports it — WezTerm sends a plain enter for
                               it unless its kitty keyboard mode is on;
                               ctrl+j always works)
      paste                    keeps the newlines
      ↑ / ↓                    move between lines; on the first / last line
                               they fall through to the app's history recall

    TextArea's own ctrl+z (undo) and ctrl+y (redo) would shadow the app's
    rewind and copy-last-answer while the prompt has focus, so they're
    rebound to the app's actions; esc still stops a running turn.
    """

    BINDINGS = [
        Binding("ctrl+z", "app.rewind", "rewind", show=False),
        Binding("ctrl+y", "app.copy_last", "copy", show=False),
        Binding("escape", "app.interrupt", "stop", show=False),
    ]
    NEWLINE_KEYS = ("shift+enter", "alt+enter", "ctrl+j")
    MAX_LINES = 8

    def __init__(self, placeholder="", id=None, suggester=None, **kw):
        super().__init__("", id=id, soft_wrap=True, show_line_numbers=False,
                         tab_behavior="focus", highlight_cursor_line=False,
                         compact=True, placeholder=placeholder, **kw)
        self.suggester = suggester
        self._suggestion = ""

    # --- the Input interface the app uses ---------------------------------
    @property
    def value(self):
        return self.text

    @value.setter
    def value(self, text):
        self.load_text(str(text or ""))
        self.cursor_position = len(self.text)

    def insert_text_at_cursor(self, text):
        """Input's API, still used by Choices to hand a typed letter to the
        prompt — without it, typing your own answer to options CRASHED the
        app once the prompt became a TextArea."""
        self.insert(str(text))

    @property
    def cursor_position(self):
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, index):
        index = max(0, min(int(index), len(self.text)))
        self.cursor_location = self.document.get_location_from_index(index)

    # --- keys ---------------------------------------------------------------
    async def _on_key(self, event):
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(Input.Submitted(self, self.text))
            return
        if event.key in self.NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        row = self.cursor_location[0]
        last = self.document.line_count - 1
        if (event.key == "up" and row == 0) or (event.key == "down" and row == last):
            return          # let it bubble: the app recalls earlier questions
        await super()._on_key(event)

    async def _on_paste(self, event):
        # Handled here, once. Left to bubble, the app forwarded the same
        # paste back to the focused prompt and the block landed twice.
        event.stop()
        event.prevent_default()
        # Files dragged onto the terminal arrive as a paste of their paths:
        # pictures become attachments instead of text.
        attach = getattr(self.app, "attach_paths", None)
        if attach is not None:
            import johan_attach
            paths = johan_attach.dropped_paths(event.text)
            if paths:
                attach(paths)
                return
        await super()._on_paste(event)

    def action_paste(self):
        """ctrl+v: the SYSTEM clipboard (a picture is attached, text pasted).
        TextArea's own ctrl+v only knew text copied inside this app."""
        paste = getattr(self.app, "paste_clipboard", None)
        if paste is None or not paste():
            super().action_paste()

    def action_delete_left(self):
        """Backspace on an empty prompt drops the last attached picture."""
        if not self.text and getattr(self.app, "attachments", None):
            self.app.remove_attachment(-1)
            return
        super().action_delete_left()

    def action_cursor_up(self, select=False):
        if self.cursor_location[0] == 0 and not select:
            return          # history recall handles it (the app's on_key)
        super().action_cursor_up(select)

    def action_cursor_down(self, select=False):
        if self.cursor_location[0] == self.document.line_count - 1 and not select:
            return
        super().action_cursor_down(select)

    # --- slash-command ghost suggestion ----------------------------------------
    def on_text_area_changed(self, event):
        lines = max(1, min(self.document.line_count, self.MAX_LINES))
        self.styles.height = lines
        text = self.text
        hint = ""
        if self.suggester is not None and text.startswith("/") and "\n" not in text:
            hint = self.suggester.suggest_sync(text) or ""
        self._suggestion = hint
        self.suggestion = hint[len(text):] if hint.lower().startswith(text.lower()) and \
            self.cursor_position == len(text) else ""


# ================================================================ approval
from textual.containers import Vertical as _Vertical  # noqa: E402


class ApprovalCard(_Vertical):
    """Johan wants to change something: shown inline, the turn waits.

    1 yes · 2 always allow this kind (only when every step can be a rule) ·
    last = no · typing says no and tells Johan what to do instead · esc stops
    the turn. After the decision the card stays, showing it."""

    DEFAULT_CSS = """
    ApprovalCard { height: auto; margin: 0 0 1 1; padding: 0 1;
                   border: round $warning 60%; }
    ApprovalCard > .ap-title { height: auto; }
    ApprovalCard > .ap-body { height: auto; max-height: 24; overflow-y: auto; }
    ApprovalCard > .ap-done { height: auto; }
    """
    MAX_DIFF_LINES = 40

    def __init__(self, kind, item, earlier=False, **kw):
        super().__init__(**kw)
        self.kind, self.item = kind, item
        # earlier=True: proposed while no one could answer (self-review, the
        # phone, one-shot `johan "…"`) and shown when the app opens.
        self.earlier = earlier
        if kind == "action":
            opts = ["Yes, run it"]
            if item.get("can_always"):
                opts.append("Yes, and always allow this kind")
            opts.append("No")
        else:
            opts = ["Yes, apply it", "No"]
        # Numbers only, no letter shortcuts: with "y" bound, starting to type
        # "yeah but use mpv instead" would APPROVE the change on its first key.
        # Any letter starts a free answer in the prompt (= no, do this instead).
        self.options = opts
        short = {"Yes, run it": "run", "Yes, apply it": "apply",
                 "Yes, and always allow this kind": "always allow", "No": "no"}
        nums = " · ".join(f"{i} {short[o]}" for i, o in enumerate(opts, 1))
        self.choices = Choices(opts, hint=f"{nums} · or type what you want instead · esc stops")
        if earlier:
            # A card from earlier is focused when Johan opens: an enter out of
            # habit must not approve something old — start on "No".
            self.choices.index = len(opts) - 1

    def compose(self):
        yield Static(self._title(), classes="ap-title")
        yield Static(self._body(), classes="ap-body")
        yield self.choices

    def _title(self):
        if self.kind == "action":
            root = " [b $error]needs your password[/]" if self.item.get("root") else ""
            head = ("⏵ Waiting from earlier: a change" if self.earlier else
                    "⏵ Johan wants to run a command" if self.item.get("raw") else
                    "⏵ Johan wants to change something")
            return mk(f"[b $warning]{head}" + root + "[/]  [$text-muted]$w[/]",
                      w=self.item.get("why") or "")
        verb = ("create" if self.item.get("new_file") else "rewrite") if self.item.get("mode") == "write" else "edit"
        head = f"⏵ Waiting from earlier: {verb}" if self.earlier else f"⏵ Johan wants to {verb}"
        return mk(f"[b $warning]{head}[/] [b]$p[/]  [$text-muted]$w[/]",
                  p=self.item.get("path", "?"), w=self.item.get("why") or "")

    def _body(self):
        lines = []
        if self.kind == "action":
            for i, st in enumerate(self.item.get("steps", []), 1):
                tag = "[$error]root[/] " if st.get("root") else ""
                lines.append(mk(f"{i}. {tag}$w", w=st.get("what", "")))
                lines.append(mk("   [$accent]$c[/]", c="$ " + str(st.get("shown", ""))))
                if st.get("undo"):
                    lines.append(mk("   [$text-muted]undo: $u[/]", u=" ".join(map(str, st["undo"]))))
                elif st.get("undo_note"):
                    lines.append(mk("   [$text-muted]undo: $u[/]", u=st["undo_note"]))
                else:
                    lines.append(Content.from_markup("   [$warning]no automatic undo[/]"))
        else:
            diff = (self.item.get("diff") or "").splitlines()
            for line in diff[:self.MAX_DIFF_LINES]:
                style = ("$text-muted" if line.startswith(("+++", "---")) else
                         "$accent" if line.startswith("@@") else
                         "$success" if line.startswith("+") else
                         "$error" if line.startswith("-") else "$text-muted")
                lines.append(mk(f"[{style}]$l[/]", l=line))
            if len(diff) > self.MAX_DIFF_LINES:
                lines.append(Content.from_markup(
                    f"[$text-muted]… {len(diff) - self.MAX_DIFF_LINES} more lines[/]"))
        return Content("\n").join(lines) if lines else Content("")

    def decided(self, text, ok=True):
        """Replace the options with the decision, so the chat keeps a record."""
        self.choices.retire()
        style = "$success" if ok else "$text-muted"
        self.mount(Static(mk(f"[{style}]→ $t[/]", t=text), classes="ap-done"))
