"""Johan's interface.

Four surfaces, because this app does four different jobs:

  CHAT    talk to the assistant. Answers stream; each turn's tool calls fold
          into one expandable "activity" line instead of spamming the log.
  JOBS    the apply queue as a table you act on: open, CV, fill, applied.
  IMAGES  everything Johan has drawn — preview, open, vary, reuse a prompt.
  CHANGES staged file edits AND machine changes (installs, settings,
          services, files): y to run, n to discard, u to undo the last.

Design notes worth keeping (each one cost real time once):
  - The model runs in worker threads. Ollama takes seconds and a frozen UI
    reads as broken.
  - Streaming is throttled to ~10 fps: Markdown.update() re-renders the whole
    block, so pushing every token makes the UI the bottleneck.
  - Results of background work (autofill, rebuild, review, drawing) ALSO
    show as toasts. A message posted only to Chat is invisible from the tab
    where the key was pressed — that is exactly how the first autofill
    failure looked like "filling…" then nothing.
  - Tab-specific keys live on the tab's own widgets, so `o` means "open
    posting" only where there is a posting, and typing in the prompt never
    triggers them.
  - Never name a method `_render()` on a widget; DataTable cells are Rich
    Text, never Textual `$theme` markup; terminals send ctrl+h as backspace.
  - Nothing here submits an application. `a` records that YOU did.

    johan             open this
    johan "question"  one-shot answer, no UI
    johan --continue  reopen the last conversation
"""

import dataclasses
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from textual.content import Content


# Untrusted text — his messages, model output, job postings, file paths,
# diffs — NEVER goes into a markup string, escaped or not. Rich's escaper
# (Text(x).markup) leaves an unterminated "[/" alone while Textual's parser
# reads it as a closing tag: "MarkupError: auto closing tag ('[/]') has
# nothing to close" on F2. Even textual.markup.escape crashed on 18% of fuzzed
# strings. Variables are the only safe channel: `mk("[b]$t[/b]", t=title)`
# inserts the value as plain text, never parsed (0 crashes in 150k cases).
def tool_word(name):
    """A tool name as shown in titles/status. Real names are plain words; a
    garbled model reply can invent anything, and titles are parsed as markup."""
    name = str(name)
    return VERBS.get(name) or (re.sub(r"[^\w.-]", "", name)[:40] or "a tool")


def mk(markup, **values):
    return Content.from_markup(markup, **values)


def join_lines(parts):
    """Lines of fixed markup (str) and/or Content → one Content."""
    return Content("\n").join(p if isinstance(p, Content) else Content.from_markup(p)
                              for p in parts)
from rich.text import Text

from textual import work
from textual.color import Color
from textual.theme import Theme
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import (Button, Collapsible, DataTable, Footer, Input,
                             Label, ListItem, ListView, Markdown, Static,
                             TabbedContent, TabPane)

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja      # noqa: E402
import johan_tools as jt          # noqa: E402
import johan_actions as jx        # noqa: E402
import johan_commands as jc       # noqa: E402
import apply_queue as aq          # noqa: E402
import johan_plan as jp           # noqa: E402
import johan_attach as _attach    # noqa: E402
import johan_interrupt as ji      # noqa: E402
from johan_widgets import ApprovalCard, Choices, PlanCard  # noqa: E402
import johan_approval as jap  # noqa: E402
import johan_widgets as jw  # noqa: E402

try:                               # real pictures in WezTerm/kitty/sixel terminals
    from textual_image.widget import Image as TermImage
except Exception:                  # noqa: BLE001
    TermImage = None


def _sixel_defs_first(encode):
    """textual-image defines each colour register the first time it's used,
    mid-image. WezTerm (20240203) draws those images with a bright green line
    on top and stray stripes; with every definition moved up front, before
    the first pixel, they're clean. Same bytes otherwise."""
    import re
    define = re.compile(r"(#\d+);2;\d+;\d+;\d+")

    def wrapped(*a, **k):
        s = encode(*a, **k)
        defs = "".join(m.group(0) for m in define.finditer(s))
        body = define.sub(r"\1", s)
        start = body.find("#", body.find("q"))
        return body if start < 0 else body[:start] + defs + body[start:]
    return wrapped


try:
    import textual_image.renderable.sixel as _rs
    import textual_image.widget.sixel as _ws
    _ws.image_to_sixels = _sixel_defs_first(_ws.image_to_sixels)
    _rs.image_to_sixels = _sixel_defs_first(_rs.image_to_sixels)
except Exception:                  # noqa: BLE001
    pass

ROOT = Path.home() / "johan"
THEMES = ["johan", "tokyo-night", "catppuccin-mocha", "nord", "gruvbox", "textual-light"]
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# The default look: a calm near-black that sits well on WezTerm's dark
# background, warm grey text, and ONE accent (clay) for what should catch
# the eye — the prompt caret, the active tab, Johan's bullet.
JOHAN_THEME = Theme(
    name="johan", dark=True,
    primary="#D7825E", secondary="#8FA8D8", accent="#D7825E",
    warning="#D8B272", error="#E07A7A", success="#8DBF7A",
    foreground="#CDD0D8", background="#131419", surface="#1A1C23", panel="#2A2D37",
    variables={"footer-key-foreground": "#D7825E", "input-cursor-background": "#D7825E",
               "border": "#3A3E4A", "border-blurred": "#2A2D37"},
)


# DataTable cells are Rich Text, so theme variables can't reach them; these
# match the johan theme instead of terminal-bright red/green/cyan.
SOFT = {"red": "#E07A7A", "yellow": "#D8B272", "green": "#8DBF7A", "blue": "#8FA8D8"}


def _fixed_muted(theme):
    """`$text-muted` is "auto 60%", which inside content markup resolves
    against no background and came out BRIGHT WHITE — every "muted" hint,
    meta line and footnote was as loud as the answer. Pin it (and
    text-disabled) to real blended colours, per theme."""
    try:
        bg = Color.parse(theme.background or ("#000000" if theme.dark else "#ffffff"))
        fg = Color.parse(theme.foreground or ("#ffffff" if theme.dark else "#000000"))
    except Exception:
        return theme
    return dataclasses.replace(theme, variables={
        **(theme.variables or {}),
        "text-muted": bg.blend(fg, 0.55).hex, "text-disabled": bg.blend(fg, 0.36).hex})

PROMPT_HINT = "ask Johan anything  ·  / for commands  ·  ctrl+v pastes a picture"

SLASH = ["/draw ", "/draw landscape ", "/draw square ", "/research ", "/review",
         "/refresh",
         "/retry", "/rewind", "/copy", "/new", "/history", "/jobs", "/images", "/changes", "/rules",
         "/help", "/theme", "/forget ", "/uncensored", "/normal", "/attach"]

# Every name the app handles itself. A commands/<name>.md file with one of
# these names is ignored: a built-in always wins.
BUILTIN_SLASH = {"new", "history", "sessions", "jobs", "images", "edits", "help",
                 "review", "retry", "copy", "theme", "refresh", "draw", "research",
                 "delete", "resume", "rewind", "undo", "rules", "forget", "changes", "edits",
                 "uncensored", "normal", "attach"}


def file_commands():
    """commands/*.md, minus anything shadowing a built-in. Re-read only when
    a file changed, so this is cheap enough to call per keystroke."""
    try:
        return jc.load(exclude=BUILTIN_SLASH)
    except Exception:
        return {}


class SlashSuggester(SuggestFromList):
    """Tab completion for built-ins AND file commands. The file list is
    looked up on every keystroke (mtime-cached), so a command dropped into
    commands/ completes without restarting Johan."""

    def __init__(self):
        super().__init__(SLASH, case_sensitive=False)
        self.cache = None                    # the candidate list changes; never cache

    async def get_suggestion(self, value):
        return self.suggest_sync(value)

    def suggest_sync(self, value):
        if not value.startswith("/"):
            return None
        low = value.lower()
        extra = [f"/{n} " if c.get("args") or jc.PLACEHOLDER.search(c["template"]) else f"/{n}"
                 for n, c in sorted(file_commands().items())]
        for cand in SLASH + extra:
            if cand.lower().startswith(low):
                return cand
        return None

# What each tool is DOING, in words — "searching the web" reads better than
# web_search(query=…), and the raw call is still one click away.
VERBS = {
    "propose_action": "asking to change something", "propose_edit": "asking to edit a file",
    "web_search": "searching the web", "fetch_page": "reading a page",
    "wikipedia": "checking Wikipedia", "latest_version": "checking versions",
    "run_command": "running a command",
    "system_status": "checking the machine", "project_files": "reading files", "files": "reading files",
    "memory": "checking memory",
    "search_jobs": "searching jobs", "get_job": "reading a job",
    "get_pipeline_stats": "counting jobs", "build_apply_queue": "building the queue",
    "draft_cv": "writing a CV", "staged_applications": "listing CVs",
    "applied": "checking applications",
    "search_bullets": "searching your record", "check_company_hiring": "checking a company",
    "calculate": "calculating", "current_datetime": "checking the date",
    "tasks": "updating tasks", "propose_edit": "drafting an edit",
    "run_python": "running Python", "deep_research": "researching",
    "autofill_application": "filling an application", "generate_image": "drawing",
    "search_conversations": "searching old chats", "scratchpad": "taking notes",
    "ask": "asking you",
}


def open_target(target):
    """Hand a URL or file to the desktop. Returns False if we can't."""
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if not opener:
        return False
    try:
        subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _searx_ok():
    import requests
    try:
        return requests.get("http://127.0.0.1:8888/", timeout=3).ok
    except Exception:
        return False


def _queue_age_days(q):
    try:
        return (datetime.now() - datetime.fromisoformat(str(q["generated"])[:10])).days
    except Exception:
        return None


def _hm():
    return datetime.now().strftime("%H:%M")


def _clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:max(1, n - 1)].rstrip() + "…"


# ================================================================ sidebar

class Panel(Static):
    def __init__(self, title, **kw):
        super().__init__("", **kw)
        self.border_title = title


class Sidebar(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("", id="brand")
        yield Panel("this chat", id="p-chat", classes="panel")
        yield Panel("jobs", id="p-jobs", classes="panel")
        yield Panel("tasks", id="p-tasks", classes="panel")
        yield Panel("machine", id="p-machine", classes="panel")

    def on_mount(self):
        self._clock()
        self.set_interval(20, self._clock)

    def _clock(self):
        """The header used to spend a whole row on a title and a clock."""
        self.query_one("#brand", Static).update(
            f"[b $accent]✻ johan[/]  [$text-muted]{datetime.now():%a %d %b · %H:%M}[/]")

    def refresh_state(self, messages=None):
        self.messages = messages if messages is not None else getattr(self, "messages", None)
        self._gather()

    @work(thread=True, exclusive=True, group="sidebar")
    def _gather(self):
        """Collect in a thread, paint on the UI thread. The machine panel runs
        nvidia-smi, docker and a web check — done on the UI thread, every
        answer ended with the interface frozen for up to a few seconds."""
        out = {}
        for fn, wid in ((self._chat, "#p-chat"), (self._jobs, "#p-jobs"),
                        (self._tasks, "#p-tasks"), (self._machine, "#p-machine")):
            try:
                out[wid] = fn()
            except Exception as e:
                out[wid] = f"[$error]{type(e).__name__}[/]"
        self.app.call_from_thread(self._paint, out)

    def _paint(self, out):
        for wid, text in out.items():
            try:
                self.query_one(wid, Panel).update(text)
            except Exception:
                pass

    @staticmethod
    def _bar(frac, colour, width=24):
        filled = max(0, min(width, round(width * frac)))
        return f"[{colour}]{'━' * filled}[/][$panel]{'━' * (width - filled)}[/]"

    @staticmethod
    def _row(label, value):
        """`label ........ value` on one line, so the panel reads as a list."""
        plain = lambda s: re.sub(r"\[[^\]]*\]", "", s)       # noqa: E731
        gap = max(1, 24 - len(plain(label)) - len(plain(value)))
        return f"{label}{' ' * gap}{value}"

    def _chat(self):
        # Overflow fails silently — old messages fall out and the assistant
        # starts contradicting you — so the context fill is always visible.
        u = ja.LAST_USAGE
        used, ctx = u["prompt_tokens"], u["num_ctx"] or 1
        pct = round(100 * used / ctx)
        colour = "$success" if pct < 60 else ("$warning" if pct < 85 else "$error")
        turns = sum(1 for m in (self.messages or []) if m.get("role") == "user")
        out = [self._bar(used / ctx, colour),
               self._row("[$text-muted]context[/]", f"[{colour}]{pct}%[/] [$text-muted]of {ctx // 1024}k[/]"),
               self._row("[$text-muted]messages[/]", str(turns))]
        if pct >= 60:
            out.append("[$warning]ctrl+n starts fresh[/]")
        return "\n".join(out)

    def _jobs(self):
        stats = ja.get_pipeline_stats()
        v = stats.get("by_verdict", {})
        applied = len(ja.applied_ids())
        q = aq.latest_queue()
        out = [self._row("[$text-muted]likely[/]", f"[$success]{v.get('likely', 0)}[/]"),
               self._row("[$text-muted]unclear[/]", f"[$warning]{v.get('unclear', 0)}[/]"),
               self._row("[$text-muted]CVs ready[/]", str(stats.get("cvs_ready", 0))),
               self._row("[$text-muted]applied[/]", f"[$success]{applied}[/]" if applied else "0")]
        age = _queue_age_days(q) if q else None
        if age is not None:
            out.append(self._row("[$text-muted]queue built[/]",
                                 f"[$warning]{age}d ago[/]" if age >= 7 else f"{age}d ago"))
        return "\n".join(out)

    def _tasks(self):
        t = jt.list_tasks()
        if not t["tasks"]:
            return "[$text-muted]nothing open[/]"
        # One line each, cut with an ellipsis: a wrapped task used to spill
        # "contra" onto a line of its own.
        rows = [mk("[$text-muted]○[/] $t", t=_clip(r["text"], 22)) for r in t["tasks"][:5]]
        if t["count"] > 5:
            rows.append(f"[$text-muted]  +{t['count'] - 5} more[/]")
        return join_lines(rows)

    def _machine(self):
        s = jt.system_status()
        gpu = s.get("gpu", {})
        out = []
        if "vram_free_mb" in gpu:
            total, used = gpu["vram_total_mb"], gpu["vram_used_mb"]
            frac = used / max(total, 1)
            out += [self._bar(frac, "$success" if frac < 0.85 else "$warning"),
                    self._row("[$text-muted]VRAM[/]", f"{used / 1024:.1f} / {total / 1024:.1f} GB")]
        loaded = s.get("models_loaded")
        model = loaded[0] if isinstance(loaded, list) and loaded else None
        out.append(self._row("[$text-muted]model[/]", _clip(model, 15) if model
                             else "[$text-muted]unloaded[/]"))
        disk = s.get("disk", {})
        if disk:
            out.append(self._row("[$text-muted]disk free[/]", f"{disk['free_gb']} GB"))
        dots = []
        for name, short in (("postgres", "db"), ("qdrant", "vec"), ("embed_server", "emb")):
            dots.append(f"[{'$success' if s.get(name) == 'up' else '$error'}]●[/] [$text-muted]{short}[/]")
        dots.append(f"[{'$success' if _searx_ok() else '$error'}]●[/] [$text-muted]web[/]")
        out.append(" ".join(dots))
        return "\n".join(out)


# ================================================================ chat widgets

class UserMsg(Static):
    """His message: a quiet shaded block with a caret, like the prompt it
    came from — set apart from Johan's answers by shape, not by colour."""

    def __init__(self, text):
        super().__init__(mk("[b $accent]›[/] $t  [$text-muted]$hm[/]", t=str(text), hm=_hm()),
                         classes="msg-user")


class AttachedPics(Horizontal):
    """The pictures he attached to a message, small, under his bubble."""

    def __init__(self, paths):
        super().__init__(classes="attached-pics")
        self.paths = [str(p) for p in paths]

    def compose(self) -> ComposeResult:
        for p in self.paths:
            img = _picture(p) if Path(p).is_file() else None
            if img is not None:
                img.styles.height = 8
                img.styles.width = "auto"
                yield img
            else:
                yield Static(mk("[$text-muted]📎 $n[/]", n=_attach.display_name(p)), classes="pic-name")


class Note(Static):
    """Muted one-liner: tips, state changes."""

    def __init__(self, text, kind="note"):
        style = {"note": "$text-muted", "err": "$error", "ok": "$success"}[kind]
        mark = {"note": "·", "err": "✗", "ok": "✓"}[kind]
        super().__init__(mk(f"[{style}]{mark} $t[/]", t=str(text)),
                         classes=f"note note-{kind}")


class Turn(Vertical):
    """One answer: a collapsible activity line, the streamed answer, a meta line.

    Tool calls used to be one log line each — a 6-hop answer buried itself
    under `▸ run_command(…)` noise. They now fold into a single line that
    says what Johan is doing, expandable for the raw calls."""

    def __init__(self):
        # Children exist from construction, not from compose(): replaying a
        # saved chat calls set_text() right after mount(), before compose ran.
        super().__init__()
        self.started = time.monotonic()
        self.steps = []
        self.pending_steps = []
        # Shown from the start, so there is something under the question
        # while Johan works — not just a spinner down in the status bar.
        self.activity = Collapsible(title="thinking…", collapsed=True, classes="activity",
                                    collapsed_symbol="▸", expanded_symbol="▾")
        self.answer = Markdown("", classes="answer")
        self.answer.display = False
        # Johan's answers hang off a bullet, Claude-Code style: the shape
        # alone says who is talking, so the text itself can stay plain.
        self.bullet = Static("⏺", classes="bullet")
        self.bullet.display = False
        self.body = Horizontal(self.bullet, self.answer, classes="answer-row")
        self.meta = Static("", classes="meta")

    def compose(self) -> ComposeResult:
        yield self.activity
        yield self.body
        yield self.meta

    def add_step(self, name, args, result):
        failed = isinstance(result, dict) and bool(result.get("error"))
        brief = ", ".join(f"{k}={str(v)[:60]}" for k, v in (args or {}).items())
        line = (mk("[$text-muted]⎿[/] [$error]✗ $n[/][$text-muted]($a)[/]  [$error]$e[/]",
                   n=str(name), a=brief, e=str(result.get("error"))[:90])
                if failed else mk("[$text-muted]⎿[/] [$success]✓[/] $n[$text-muted]($a)[/]",
                                  n=str(name), a=brief))
        self.steps.append((name, failed))
        self.activity.display = True
        self.activity.query_one(Collapsible.Contents).mount(Static(line, classes="step"))
        self.activity.title = f"{tool_word(name)}…  · {len(self.steps)} step{'s' * (len(self.steps) > 1)}"

    def set_text(self, text):
        if text:
            self.answer.display = True
            self.bullet.display = True
            self.answer.update(text)

    def finish(self, interrupted=False):
        secs = time.monotonic() - self.started
        if not self.steps:
            self.activity.display = False      # nothing to expand on a plain answer
        else:
            names = {}
            for n, _ in self.steps:
                names[n] = names.get(n, 0) + 1
            summary = ", ".join(f"{tool_word(n)}{f' ×{c}' if c > 1 else ''}"
                                for n, c in names.items())
            fails = sum(1 for _, f in self.steps if f)
            self.activity.title = (f"{len(self.steps)} step{'s' * (len(self.steps) > 1)} · "
                                   f"{summary}" + (f" · {fails} failed" if fails else ""))
        bits = [f"{secs:.1f}s"]
        if ja.LAST_CORRECTED:
            # The answer check rewrote what streamed in — say so, so text
            # doesn't change under him without a reason.
            bits.append("[$warning]corrected after a fact check[/]")
        if interrupted:
            bits.append("[$warning]stopped[/]")
        self.meta.update(f"[$text-disabled]johan · {_hm()} · {' · '.join(bits)}[/]")


def _picture(path):
    """A terminal picture built from pixels already in memory. Given a PATH,
    textual-image re-opens the file on every repaint, so deleting the file
    (a chat delete, F4 x) crashed the next redraw. None if it can't load."""
    if TermImage is None:
        return None
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as im:
            im.load()
            return TermImage(im.copy())
    except Exception:
        return None


_REF_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _quote_path(path):
    return f"'{path}'" if " " in path else path


def _split_reference(arg):
    """Peel a leading picture path (quoted, backslash-escaped or plain — what a
    terminal pastes when a file is dragged in) and an optional strength off a
    /draw argument. Returns (path or None, strength or None, rest)."""
    s = arg.strip()
    path, rest = None, s
    if s[:1] in ("'", '"'):
        end = s.find(s[0], 1)
        if end > 0:
            path, rest = s[1:end], s[end + 1:]
    else:
        i, buf = 0, []
        while i < len(s) and s[i] != " ":
            if s[i] == "\\" and i + 1 < len(s):
                i += 1
            buf.append(s[i])
            i += 1
        path, rest = "".join(buf), s[i:]
    if not path or not path.lower().endswith(_REF_EXT) \
            or not Path(path).expanduser().is_file():
        return None, None, s
    rest = rest.strip()
    strength = None
    head = rest.split(" ", 1)
    try:
        val = float(head[0])
        if 0 < val < 1:
            strength, rest = val, head[1] if len(head) > 1 else ""
    except ValueError:
        pass
    return str(Path(path).expanduser()), strength, rest.strip()


class ImageCard(Vertical):
    """A drawn picture, inline. Real pixels in WezTerm; a caption either way."""

    def __init__(self, result):
        super().__init__(classes="imagecard")
        self.result = result

    def compose(self) -> ComposeResult:
        r = self.result
        img = _picture(r["path"])
        if img is not None:
            img.styles.height = 26
            img.styles.width = "auto"
            yield img
        yield Static(mk("[$text-muted]$p\n$w×$h · seed $s · $sec · ctrl+o opens it · F4 gallery[/]",
                        p=(r.get("prompt") or "")[:160], w=str(r.get("width")),
                        h=str(r.get("height")), s=str(r.get("seed")), sec=f"{r.get('seconds')}s"),
                     classes="imagecap")

    def file_gone(self):
        """Its file was deleted: drop the picture before the next redraw
        reads a path that no longer exists (that crashed the whole app)."""
        if TermImage is not None:
            for img in self.query(TermImage):
                img.remove()
        self.query_one(".imagecap", Static).update("[$text-muted](image deleted)[/]")


class Welcome(Vertical):
    """The empty-chat home: what's going on, and things to try."""

    STARTERS = [
        ("give me 5 jobs to apply to", "give me 5 jobs to apply to"),
        ("draw something…", "/draw "),
        ("what's using my GPU?", "what's using my GPU right now?"),
        ("research something…", "/research "),
        ("what do you know about me?", "what do you know about me?"),
        ("review your mistakes", "/review"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[b $accent]✻[/] [b]Welcome back.[/]  [$text-muted]Johan runs entirely "
                     "on this machine.[/]", id="w-title")
        yield Static("", id="w-state")
        yield Static("[$text-muted]Try[/]", id="w-try")
        with Grid(id="w-grid"):
            for i, (label, _) in enumerate(self.STARTERS):
                yield Button(f"› {label}", id=f"start-{i}", classes="starter")
        yield Static("[$text-muted][$text]/[/] commands  ·  [$text]ctrl+p[/] everything  ·  "
                     "[$text]F1[/] help[/]", id="w-hint")

    def on_mount(self):
        self.refresh_state()

    def refresh_state(self):
        lines = []
        try:
            q = aq.latest_queue()
            n = len((q or {}).get("jobs", []))
            age = _queue_age_days(q) if q else None
            applied = len(ja.applied_ids())
            if n:
                stale = (f"  [$warning]· {age} days old — F3 then [b]r[/b] to rebuild[/]"
                         if age is not None and age >= 7 else "")
                lines.append(f"[$success]●[/] {n} jobs in your queue [$text-muted]· "
                             f"{applied} applied · F3[/]{stale}")
            else:
                lines.append("[$warning]●[/] no apply queue yet [$text-muted]— F3 then [b]r[/b][/]")
        except Exception:
            pass
        try:
            edits = jt.list_edits()["count"] + len(jx.list_actions())
            if edits:
                lines.append(f"[$warning]●[/] {edits} change{'s' * (edits > 1)} waiting for your OK "
                             f"[$text-muted]· shown below[/]")
        except Exception:
            pass
        try:
            import johan_images as ji
            imgs = len(ji.list_images(500))
            if imgs:
                lines.append(f"[$accent]●[/] {imgs} image{'s' * (imgs > 1)} drawn [$text-muted]· F4[/]")
        except Exception:
            pass
        self.query_one("#w-state", Static).update("\n".join(lines))


class ChatPane(VerticalScroll):
    def add(self, widget):
        self.mount(widget)
        self.scroll_end(animate=False)
        return widget


# ================================================================ modal screens

class HistoryScreen(ModalScreen):
    """Browse past conversations: filter, preview, reopen, delete."""

    BINDINGS = [Binding("escape", "close", "close"), Binding("enter", "reopen", "reopen"),
                Binding("d", "delete", "delete")]

    def __init__(self):
        super().__init__()
        self.sessions = ja.list_sessions()
        self.shown = list(self.sessions)
        self.pending_delete = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="hbox"):
            with Vertical(id="hleft"):
                yield Input(placeholder="filter…", id="hfilter")
                yield ListView(id="hlist")
            with Vertical(id="hright"):
                yield Label("↑↓ browse · enter reopen · d delete · esc close", id="hhint")
                yield VerticalScroll(Static("", id="hpreview"))

    def on_mount(self):
        self.query_one("#hbox").border_title = "chat history"
        self._fill()
        self.query_one("#hlist", ListView).focus()

    def _fill(self):
        lv = self.query_one("#hlist", ListView)
        lv.clear()
        for s in self.shown:
            lv.append(ListItem(Label(mk("[b]$w[/b]  [$text-muted]$n msg[/]\n[$text-muted]$f[/]",
                                        w=str(s["when"]), n=str(s["messages"]),
                                        f=str(s["first"] or "")[:40]))))
        if self.shown:
            lv.index = 0
            self._preview(0)
        else:
            self.query_one("#hpreview", Static).update("[$text-muted]nothing matches[/]")

    def _preview(self, idx):
        if not (0 <= idx < len(self.shown)):
            return
        s = self.shown[idx]
        lines = [mk("[b]$w[/b]  [$text-muted]$i[/]", w=str(s["when"]), i=str(s["id"])), ""]
        for role, text in ja.session_preview(s["id"], limit=16):
            who = "[$accent]you[/]" if role == "user" else "[$success]johan[/]"
            lines += [mk(f"{who}  $t", t=str(text or "").replace(chr(10), " ")[:200]), ""]
        self.query_one("#hpreview", Static).update(join_lines(lines))

    def on_list_view_highlighted(self, event):
        self.pending_delete = None
        lv = self.query_one("#hlist", ListView)
        if lv.index is not None:
            self._preview(lv.index)

    def on_input_changed(self, event):
        q = (event.value or "").lower().strip()
        self.shown = [s for s in self.sessions if not q or q in s["first"].lower()
                      or q in s["last"].lower() or q in s["when"].lower()]
        self._fill()

    def on_input_submitted(self, event):
        self.query_one("#hlist", ListView).focus()

    def action_close(self):
        self.dismiss(None)

    def action_reopen(self):
        lv = self.query_one("#hlist", ListView)
        if lv.index is not None and self.shown:
            self.dismiss(self.shown[lv.index]["id"])

    def action_delete(self):
        """Two-step, because deleting a conversation can't be undone."""
        lv = self.query_one("#hlist", ListView)
        if lv.index is None or not self.shown:
            return
        sid = self.shown[lv.index]["id"]
        hint = self.query_one("#hhint", Label)
        if self.pending_delete != sid:
            self.pending_delete = sid
            hint.update(f"[$warning]press d again to delete {sid} and the images drawn in it[/]")
            return
        ja.delete_session(sid)
        self.app.after_delete(sid)
        self.sessions = [s for s in self.sessions if s["id"] != sid]
        self.shown = [s for s in self.shown if s["id"] != sid]
        self.pending_delete = None
        hint.update("↑↓ browse · enter reopen · d delete · esc close")
        self._fill()


HELP_MD = """\
# Johan

### Ask anything
- `give me 5 jobs to apply to` — ranks them, writes verified CVs
- `what's using my GPU?` · `check my tf2 config` · `how much disk is free?`
- `research the best budget GPU for local AI properly` — a few minutes, cited
- `draw a girl with silver hair on a rooftop at night` — or `/draw` with tags
- `remember I prefer Postgres` · `what do you know about me?`
- `use python to …` — runs in a locked sandbox

### Commands — type `/`, then tab completes
- `/draw <tags>` — draw now (anime); put `landscape` or `square` first to change shape
- `/draw ~/pic.png <tags>` — redraw a picture; `/draw ~/pic.png 0.3 <tags>` changes
  it less, `0.9` more (default 0.6). Drag a file into the terminal to paste its path.
  In Images (F4), `i` starts a redraw of the selected one
- `/research <question>` — deep research, a few minutes, with sources
- `/retry` — ask the last question again · `/copy` — copy the last answer
- `/rewind` or `ctrl+z` — take back the last turn: drops it, reverts the changes and edits it
  made (one confirmation listing exactly what, and what can't be undone), and puts your
  question back in the box to edit
- `/review` — Johan reviews its own mistakes; its proposals show up here for your OK
- `/uncensored` — swap to the uncensored model for this chat (same tools); `/normal` switches back, a new chat starts normal
- `/refresh` — fetch new job postings, drop closed ones, rebuild the queue
  (~20 min in the background; `/refresh` again shows progress; `R` in Jobs)
- `/new` — fresh chat · `/history` — past chats

### Keys
- `ctrl+p` — every action, searchable
- `F2` history · `F3` jobs · `F4` images
- Ask for a change in chat — “install vlc”, “set super+v to copyq” — Johan asks right there:
  `1` run · `2` run and always allow that exact kind (when it can be) · last = no · or type what
  you want instead · `esc` stops. Root steps open your system's password dialog. It then
  checks the change took effect and carries on. `/rules` lists always-allowed kinds.
- Changes proposed while you weren't here (self-review, your phone, `johan "…"`) show up as the
  same cards when you open Johan. `/changes` lists recent changes; `/changes undo` undoes the last
- `esc` — stop the current answer
- Pictures: `ctrl+v` pastes one from the clipboard (a screenshot, "copy image"), `ctrl+f` or
  `/attach` picks a file, or drag it onto the window. Johan sees it; "redraw this as anime" or
  `/draw <tags>` redraws it. Backspace on an empty prompt (or a click) removes it
- `ctrl+o` — open the newest link, PDF or image · `ctrl+y` — copy the last answer
- `ctrl+b` — hide/show the sidebar · `ctrl+n` new chat · `ctrl+t` theme · `ctrl+q` quit

### In the tabs
- **Jobs** — `o` posting · `p` CV · `f` fill the application (you click Submit) · `a` mark applied · `r` rebuild
- **Images** — `o` open · `v` variation · `u` reuse the prompt · `x` delete
- **Edits** — `y` apply · `n` discard

*esc closes this*
"""


def help_markdown():
    """HELP_MD plus the commands that live in files, read fresh each time."""
    cmds = file_commands()
    if not cmds:
        extra = ("\n### Your commands\n- none yet — drop a `.md` file in `commands/` "
                 "(`$ARGS` marks where what you type goes) and it becomes `/name`\n")
    else:
        rows = "\n".join(f"- `{jc.usage(c)}` — {c.get('description') or 'no description'}"
                         for _, c in sorted(cmds.items()))
        extra = (f"\n### Your commands — from `commands/*.md`\n{rows}\n"
                 "- add your own: a `.md` file in `commands/`; `$ARGS` marks where "
                 "what you type goes\n")
    marker = "\n### Keys"
    return HELP_MD.replace(marker, extra + marker, 1) if marker in HELP_MD else HELP_MD + extra


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("f1", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="helpbox"):
            yield Markdown(help_markdown())

    def on_mount(self):
        self.query_one("#helpbox").border_title = "help"


class RewindScreen(ModalScreen):
    """One confirmation for a rewind that touches the machine or files:
    exactly what will be reverted, discarded, and what can't be undone."""

    DEFAULT_CSS = """
    RewindScreen { align: center middle; }
    #rwbox { width: 90; max-width: 95%; height: auto; max-height: 80%;
             background: $surface; border: round $warning; padding: 1 2; }
    """
    BINDINGS = [Binding("y", "go", "rewind"), Binding("enter", "go", "rewind"),
                Binding("n", "stop", "cancel"), Binding("escape", "stop", "cancel")]

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        out = ["[b]Rewind the last turn[/b] — the question goes back in the box.", ""]
        for key, head, style in (("revert", "Will be reverted:", "$success"),
                                 ("discard", "Waiting, will be discarded:", "$text-muted"),
                                 ("cannot", "Can't be undone (stays as it is):", "$error")):
            if self.plan.get(key):
                out.append(f"[{style}]{head}[/]")
                out += [mk("  • $x", x=str(x)) for x in self.plan[key]]
                out.append("")
        out.append("[$text-muted]y / enter rewind · n / esc cancel[/]")
        with VerticalScroll(id="rwbox"):
            yield Static(join_lines(out))

    def action_go(self):
        self.dismiss(True)

    def action_stop(self):
        self.dismiss(False)


# What a turn's tool calls may have written that rewind does NOT revert —
# said up front rather than silently left behind.
def _side_effects(calls):
    out = []
    for name, args in calls:
        a = args or {}
        if name == "memory" and (a.get("text") or a.get("forget_id")):
            out.append("a memory saved/forgotten (say “forget …” to fix it)")
        elif name == "tasks" and a.get("action") in ("add", "done"):
            out.append(f"task {a.get('action')}: {str(a.get('text') or a.get('task_id') or '')[:50]}")
        elif name == "applied" and a.get("job_id"):
            out.append(f"job {a.get('job_id')} marked applied (a in Jobs toggles it)")
        elif name == "scratchpad" and (a.get("text") or a.get("clear")):
            out.append("the scratchpad note")
        elif name == "generate_image":
            out.append("the image drawn (F4 → x deletes it)")
    return list(dict.fromkeys(out))


# ================================================================ jobs

class JobsPane(Vertical):
    """The apply queue as something you can act on."""

    BINDINGS = [
        Binding("o", "app.open_posting", "open posting"),
        Binding("p", "app.open_cv", "open CV"),
        Binding("f", "app.autofill", "fill application"),
        Binding("a", "app.toggle_applied", "mark applied"),
        Binding("r", "app.rebuild", "rebuild queue"),
        Binding("R", "app.refresh_jobs", "refresh postings"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="jhead")
        yield DataTable(id="jtable", cursor_type="row", zebra_stripes=False)
        yield VerticalScroll(Static("", id="jdetail"), id="jdetailbox")

    def on_mount(self):
        self.query_one("#jtable", DataTable).add_columns(" ", "company", "role", "fit", "cv", "flag")
        self.query_one("#jdetailbox").border_title = "details"
        self.rows = []
        self.load()

    def load(self, focus=False):
        q = aq.latest_queue()
        table = self.query_one("#jtable", DataTable)
        keep = table.cursor_row if table.row_count else 0
        table.clear()
        self.rows = (q or {}).get("jobs", [])
        applied = ja.applied_ids()
        if not self.rows:
            self.query_one("#jhead", Static).update("[$warning]no queue yet[/] — press [b]r[/b] to build one")
            self.query_one("#jdetail", Static).update(
                "[$text-muted]Press [b]r[/b] to build the queue, or ask in chat: "
                "“give me 5 jobs to apply to”.[/]")
            return
        age = _queue_age_days(q)
        n_app = sum(1 for r in self.rows if r["job_id"] in applied)
        stale = (f"  [$warning]· built {age} days ago — postings close; [b]r[/b] rebuilds[/]"
                 if age is not None and age >= 7 else
                 f"  [$text-muted]· built {q.get('generated', '?')}[/]")
        self.query_one("#jhead", Static).update(
            f"[b]{len(self.rows)}[/] jobs · [$success]{n_app} applied[/]{stale}")
        for i, r in enumerate(self.rows, 1):
            done = r["job_id"] in applied
            if r.get("gaps_in_title"):
                flag = Text(f"gap: {','.join(r['gaps_in_title'])}"[:22], style=SOFT["red"])
            elif r.get("experience_note"):
                flag = Text(r["experience_note"][:22], style=SOFT["yellow"])
            else:
                flag = Text("good fit", style=SOFT["green"])
            cv = Text("✓", style=SOFT["green"]) if r.get("pdf") else Text("—", style="dim")
            table.add_row(
                Text("✓", style="bold " + SOFT["green"]) if done else Text(str(i), style="dim"),
                Text(r["company"][:14], style="dim" if done else ""),
                Text(r["title"][:42], style="dim" if done else "bold"),
                Text(f"{r['fit_score']:g}", style=SOFT["blue"]), cv, flag, key=str(r["job_id"]))
        if focus:
            table.focus()
        if keep and keep < len(self.rows):
            table.move_cursor(row=keep)
        self._detail(min(keep, len(self.rows) - 1))

    def selected(self):
        table = self.query_one("#jtable", DataTable)
        if self.rows and table.cursor_row is not None and 0 <= table.cursor_row < len(self.rows):
            return self.rows[table.cursor_row]
        return None

    def on_data_table_row_highlighted(self, event):
        table = self.query_one("#jtable", DataTable)
        if table.cursor_row is not None:
            self._detail(table.cursor_row)

    def _detail(self, idx):
        if not (0 <= idx < len(self.rows)):
            return
        r = self.rows[idx]
        applied = r["job_id"] in ja.applied_ids()
        out = [mk("[b]$t[/b]", t=str(r["title"])),
               mk("[$accent]$c[/]  [$text-muted]$l · $s · $h[/]", c=str(r["company"]),
                  l=str(r.get("location") or "no location"), s=str(r.get("seniority") or "?"),
                  h=str(r.get("hiring_type") or "?")), ""]
        if applied:
            out += ["[$success]✓ you marked this applied[/]", ""]
        out.append(mk("[b]why eligible[/b]  $b", b=str(r.get("basis") or "unknown")))
        if r.get("eligibility_quote") and r["eligibility_quote"] != "not stated":
            out.append(mk("[$text-muted]“$q”[/]", q=str(r["eligibility_quote"])[:240]))
        out.append("")
        if r.get("experience_note"):
            out.append(mk("[$warning]⚠ experience: $n[/]", n=str(r["experience_note"])))
        if r.get("gaps_in_title"):
            out.append(mk("[$error]⚠ built around $g — you don't have that[/]",
                          g=", ".join(map(str, r["gaps_in_title"]))))
        if r.get("gaps_in_desc"):
            out.append(mk("[$text-muted]mentions: $g[/]", g=", ".join(map(str, r["gaps_in_desc"][:6]))))
        pdf = r.get("pdf")
        try:
            import johan_autofill as jf
            fillable = bool(jf.application_url(r.get("url", "")))
        except Exception:
            fillable = False
        out += ["", mk("[b]matched[/b]  [$text-muted]$m[/]",
                       m=", ".join(map(str, r.get("matched_skills", [])[:12])) or "none"),
                "", (mk("[b]cv[/b]    [$success]✓ built[/]  [$text-muted]$p[/]", p=Path(pdf).parent.name)
                     if pdf else "[b]cv[/b]    [$error]not built[/]"),
                f"[b]fill[/b]  " + ("[$success]f fills the form for you[/]" if fillable
                                    else "[$text-muted]this portal isn't supported — o opens it[/]"),
                mk("[b]link[/b]  [$text-muted]$u[/]", u=str(r["url"])[:76]), "",
                "[$text-muted]o posting · p CV · f fill · a applied · r rebuild[/]"]
        self.query_one("#jdetail", Static).update(join_lines(out))


# ================================================================ images

class ImagesPane(Vertical):
    """Everything Johan has drawn."""

    BINDINGS = [
        Binding("o", "app.open_image", "open"),
        Binding("v", "app.vary_image", "variation"),
        Binding("u", "app.reuse_prompt", "reuse prompt"),
        Binding("i", "app.redraw_image", "redraw this"),
        Binding("x", "app.delete_image", "delete"),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="ileft"):
                yield DataTable(id="itable", cursor_type="row", zebra_stripes=False)
            yield Vertical(id="ipreview")

    def on_mount(self):
        self.query_one("#itable", DataTable).add_columns("when", "size", "prompt")
        self.query_one("#ipreview").border_title = "preview"
        self.rows = []
        self.pending_delete = None
        self.load()

    def load(self, focus=False):
        try:
            import johan_images as ji
            self.rows = ji.list_images(200)
        except Exception:
            self.rows = []
        table = self.query_one("#itable", DataTable)
        keep = table.cursor_row if table.row_count else 0
        table.clear()
        prev = self.query_one("#ipreview")
        if not self.rows:
            prev.remove_children()
            prev.mount(Static("[$text-muted]Nothing drawn yet.\n\nIn chat: “draw a knight in a "
                              "neon city” — or [b]/draw[/b] followed by tags.[/]"))
            return
        for r in self.rows:
            when = (r.get("created") or "")[5:16].replace("T", " ")
            table.add_row(Text(when, style="dim"), Text(r.get("size", "?")[:4]),
                          Text((r.get("prompt") or "")[:60]), key=r["path"])
        if focus:
            table.focus()
        if keep and keep < len(self.rows):
            table.move_cursor(row=keep)
        self._show(min(keep, len(self.rows) - 1))

    def selected(self):
        table = self.query_one("#itable", DataTable)
        if self.rows and table.cursor_row is not None and 0 <= table.cursor_row < len(self.rows):
            return self.rows[table.cursor_row]
        return None

    def on_data_table_row_highlighted(self, event):
        self.pending_delete = None
        table = self.query_one("#itable", DataTable)
        if table.cursor_row is not None:
            self._show(table.cursor_row)

    def _show(self, idx):
        if not (0 <= idx < len(self.rows)):
            return
        r = self.rows[idx]
        prev = self.query_one("#ipreview")
        prev.remove_children()
        img = _picture(r["path"])
        if img is not None:
            img.styles.height = "1fr"
            img.styles.width = "auto"
            prev.mount(img)
        prev.mount(Static(mk("[$text-muted]$p\n$w×$h · seed $s · $sec\n\n"
                             "o open · v variation · u reuse prompt · x delete[/]",
                             p=r.get("prompt") or "", w=str(r.get("width")), h=str(r.get("height")),
                             s=str(r.get("seed")), sec=f"{r.get('seconds')}s"),
                          classes="imagecap"))


# ================================================================ edits

# ================================================================ command palette

class JohanCommands(Provider):
    """ctrl+p: every Johan action, searchable. Discoverability was the gap —
    half the features lived behind keys you had to already know."""

    COMMANDS = [
        ("New chat", "new_chat", "clear the conversation, keep memories"),
        ("Chat history", "history", "browse, reopen or delete past chats"),
        ("Jobs", "jobs_tab", "the apply queue"),
        ("Images", "images_tab", "everything Johan has drawn"),
        ("Recent changes", "changes", "what Johan changed lately; /changes undo undoes the last"),
        ("Undo last change", "undo_last_change", "asks to undo the last change Johan made"),
        ("Uncensored mode", "uncensored", "swap to the uncensored model for this chat"),
        ("Normal mode", "normal_model", "back to the normal model"),
        ("Always-allow rules", "list_rules", "changes that run without asking; /rules forget N"),
        ("Draw an image…", "start_draw", "puts /draw in the prompt"),
        ("Attach a picture…", "attach_file", "ctrl+f — pick a file; ctrl+v pastes one"),
        ("Research something…", "start_research", "deep research, a few minutes"),
        ("Review my mistakes", "review", "self-review; proposals go to Edits"),
        ("Refresh job postings", "refresh_jobs", "fetch new jobs, drop closed ones (~20 min)"),
        ("Rebuild the job queue", "rebuild_any", "rank jobs and write CVs"),
        ("Retry last question", "retry", "ask the last question again"),
        ("Rewind last turn", "rewind", "ctrl+z: drop it, revert what it changed, edit the question"),
        ("Copy last answer", "copy_last", "to the clipboard"),
        ("Open newest link / file", "open_last", "posting, PDF or image"),
        ("Toggle sidebar", "toggle_sidebar", "more room for the chat"),
        ("Next theme", "cycle_theme", "cycle colour themes"),
        ("Help", "help_me", "keys and examples"),
    ]

    async def discover(self) -> Hits:
        for name, action, help_ in self.COMMANDS:
            yield DiscoveryHit(name, self._run(action), help=help_)
        for name, cmd in sorted(file_commands().items()):
            yield DiscoveryHit(jc.usage(cmd), self._file(cmd), help=self._file_help(cmd))

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, action, help_ in self.COMMANDS:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), self._run(action), help=help_)
        for name, cmd in sorted(file_commands().items()):
            label = jc.usage(cmd)
            score = max(matcher.match(label), matcher.match(cmd.get("description") or ""))
            if score > 0:
                yield Hit(score, matcher.highlight(label), self._file(cmd),
                          help=self._file_help(cmd))

    @staticmethod
    def _file_help(cmd):
        return (cmd.get("description") or "your command") + f"  ·  commands/{cmd['name']}.md"

    def _file(self, cmd):
        """A file command needing args lands in the prompt for him to finish;
        one that doesn't runs straight away."""
        def go():
            if cmd.get("args") or jc.PLACEHOLDER.search(cmd["template"]):
                box = self.app.query_one("#prompt")
                box.value = f"/{cmd['name']} "
                box.cursor_position = len(box.value)
                box.focus()
            else:
                self.app._submit(f"/{cmd['name']}")
        return go

    def _run(self, action):
        # The action method directly: App.run_action is async, and a plain
        # lambda returning its coroutine would silently never run it.
        return lambda: getattr(self.app, f"action_{action}")()


# ================================================================ app

class JohanApp(App):
    COMMANDS = App.COMMANDS | {JohanCommands}

    CSS = """
    Screen { layout: vertical; background: $background; }
    * { scrollbar-size-vertical: 1; scrollbar-background: $background;
        scrollbar-background-hover: $background; scrollbar-background-active: $background;
        scrollbar-color: $panel; scrollbar-color-hover: $accent 50%;
        scrollbar-color-active: $accent; }
    #body { height: 1fr; }
    #main { width: 1fr; }

    /* ---- tabs: quiet labels, one accent underline under the active one */
    #main > ContentTabs { height: 2; }
    #main Tab { color: $text-muted; padding: 0 2; }
    #main Tab.-active { color: $text; text-style: bold; }
    #main Tab:hover { color: $text; }
    #main Underline > .underline--bar { color: $accent; background: $panel 40%; }
    TabPane { padding: 0; }

    /* ---- sidebar: a calm column of sections, not a stack of boxes */
    Sidebar { width: 30; background: $surface; padding: 0 1; }
    Sidebar.hidden { display: none; }
    #brand { height: 1; margin: 0 0 1 1; }
    .panel { border: none; border-top: solid $panel; padding: 0 1; margin: 0 0 1 0;
             height: auto; border-title-color: $text-muted; border-title-style: bold;
             border-title-align: left; }

    /* ---- chat */
    #chat { padding: 0 2 1 2; scrollbar-size-vertical: 1; }
    .msg-user { margin: 1 0 0 0; padding: 0 1; background: $surface; color: $text; }
    .attached-pics { height: auto; padding: 0 1; background: $surface; }
    .attached-pics > * { margin: 0 1 0 0; }
    #attachments { height: auto; margin: 0 1; display: none; }
    #attachments.has { display: block; }
    #attachments > Button { min-width: 0; height: 1; border: none; margin: 0 1 0 0;
                            padding: 0 1; background: $panel; color: $text; }
    Turn { height: auto; margin: 1 0 0 0; }
    .activity { border: none; padding: 0; margin: 0; background: transparent; }
    .activity > CollapsibleTitle { color: $text-muted; padding: 0 0 0 1; background: transparent; }
    .activity > CollapsibleTitle:hover { color: $text; background: transparent; }
    .activity > CollapsibleTitle:focus { color: $text; background: transparent; text-style: none; }
    .activity > Contents { padding: 0 0 0 3; }
    .step { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .answer-row { height: auto; }
    .bullet { width: 2; color: $accent; margin: 0 0 0 1; }
    .answer { width: 1fr; margin: 0; padding: 0 1 0 0; background: transparent; }
    .meta { margin: 0 0 0 3; height: auto; }
    .note { margin: 1 0 0 1; height: auto; }
    .imagecard { height: auto; margin: 1 0 0 3; padding: 0; }
    .imagecap { margin: 0; }

    /* ---- the home card */
    Welcome { height: auto; margin: 1 0 0 0; padding: 1 2; border: round $panel; }
    #w-title { margin-bottom: 1; }
    #w-state { margin-bottom: 1; height: auto; }
    #w-try { height: 1; }
    #w-grid { grid-size: 2; grid-gutter: 0 2; grid-rows: 1; height: auto; margin-bottom: 1; }
    .starter { width: 100%; min-width: 10; height: 1; border: none; padding: 0;
               background: transparent; color: $text; text-style: none;
               content-align: left middle; text-align: left; }
    .starter:hover { background: $surface; color: $accent; border: none; }
    .starter:focus { background: $surface; color: $accent; text-style: none; border: none; }
    #w-hint { height: auto; }

    /* ---- the prompt box and the status line under it */
    #promptbox { height: auto; max-height: 10; margin: 0 1; border: round $panel; background: $background; }
    #promptbox:focus-within { border: round $accent 70%; }
    #caret { width: 3; color: $accent; text-style: bold; padding: 0 0 0 1; }
    #prompt { border: none; background: transparent; padding: 0 1 0 0; height: 1; max-height: 8; }
    #prompt:focus { border: none; background-tint: transparent; }
    #prompt > .input--placeholder { color: $text-disabled; }
    #statusbar { height: 1; padding: 0 2; }
    #status { width: 1fr; color: $text-muted; }
    #status.busy { color: $accent; }
    #statusr { width: auto; color: $text-disabled; }

    Footer { background: $background; }
    Footer FooterKey { background: $background; }
    Footer FooterKey .footer-key--key { color: $accent; background: transparent; }
    Footer FooterKey .footer-key--description { color: $text-muted; }

    /* ---- tables (jobs, images, changes) */
    DataTable { background: transparent; scrollbar-size-vertical: 1; }
    DataTable > .datatable--header { background: transparent; color: $text-muted;
                                     text-style: bold; }
    DataTable > .datatable--cursor { background: $accent 22%; color: $text; text-style: none; }
    DataTable:blur > .datatable--cursor { background: $panel; }
    DataTable > .datatable--hover { background: $panel 50%; }
    #jhead { height: auto; padding: 0 1 1 1; }
    #jtable, #etable { height: 1fr; }
    #jdetailbox { height: 45%; border: round $panel; padding: 0 1; margin: 0 1 0 0;
                  border-title-color: $text-muted; border-title-style: bold; }
    #ediffbox { height: 55%; border: round $panel; padding: 0 1; margin: 0 1 0 0;
                border-title-color: $text-muted; border-title-style: bold; }
    #ileft { width: 45%; }
    #itable { height: 1fr; }
    #ipreview { width: 55%; border: round $panel; padding: 0 1;
                border-title-color: $text-muted; border-title-style: bold; }

    /* ---- modals */
    HistoryScreen, HelpScreen { align: center middle; background: $background 60%; }
    #hbox { width: 88%; height: 82%; background: $surface; border: round $panel;
            border-title-color: $accent; border-title-style: bold; }
    #hleft { width: 42%; border-right: solid $panel; }
    #hright { width: 58%; padding: 0 1; }
    #hfilter { border: none; background: $background; }
    #hhint { color: $text-muted; padding: 0 0 1 0; }
    #helpbox { width: 88; max-width: 96%; height: 88%; background: $surface;
               border: round $panel; padding: 0 2;
               border-title-color: $accent; border-title-style: bold; }
    #helpbox Markdown { background: transparent; }
    #helpbox { scrollbar-size-vertical: 1; }

    /* ---- command palette: a centred card, not a full-width sheet */
    CommandPalette > Vertical { width: 90; max-width: 100%; margin-top: 2;
                                background: $surface; }
    CommandPalette #--input { border: none; border-bottom: solid $panel; }
    CommandPalette CommandList { background: $surface; border: none; }
    CommandPalette CommandList > .option-list--option-highlighted {
        background: $accent 22%; }
    """

    BINDINGS = [
        # The footer shows only what isn't visible elsewhere: F3-F4 are
        # printed on the tabs themselves, the rest is in F1 and ctrl+p.
        Binding("ctrl+q", "quit", "quit", show=False),
        Binding("f1", "help_me", "help"),
        Binding("f2", "history", "history"),
        Binding("f3", "jobs_tab", "jobs", show=False),
        Binding("f4", "images_tab", "images", show=False),
        Binding("escape", "interrupt", "stop", show=False),
        Binding("ctrl+n", "new_chat", "new chat"),
        Binding("ctrl+o", "open_last", "open", show=False),
        Binding("ctrl+y", "copy_last", "copy", show=False),
        Binding("ctrl+b", "toggle_sidebar", "sidebar", show=False),
        Binding("ctrl+r", "refresh", "refresh", show=False),
        Binding("ctrl+t", "cycle_theme", "theme", show=False),
        Binding("ctrl+z", "rewind", "rewind", show=False),
        Binding("ctrl+f", "attach_file", "attach", show=False),
    ]

    URL_RE = re.compile(r"https?://[^\s`\)\]<>\"']+")
    FILE_RE = re.compile(r"(/[^\s`\)\]<>\"']+\.(?:pdf|png))")

    def __init__(self, resume=False):
        super().__init__()
        self.resume = resume
        self.messages = None
        self.busy = False
        self.busy_label = ""
        self.busy_since = 0.0
        self.history = []
        self.history_pos = None
        self.openables = []
        self.theme_idx = 0
        self.cancel = threading.Event()
        self.turn = None
        self._stream_buf = ""
        self._stream_last = 0.0
        self._spin = 0
        self._ticker = None
        self.turn_ids = {}       # index of the user message → jx turn id (for rewind)
        self.turn_tools = {}     # turn id → [(tool, args)] it called
        self.pending = None        # Choices or PlanCard waiting for his pick
        self.approval = None       # (ApprovalCard, Event, box) while a turn waits for his OK
        self.backlog_card = None   # an ApprovalCard for a change waiting from earlier
        self.plan_card = None      # the approved plan whose checklist is ticking
        self._asked = None
        self.attachments = []      # pictures (paths in brain/uploads) for the next message

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with TabbedContent(id="main", initial="tab-chat"):
                with TabPane("Chat", id="tab-chat"):
                    yield ChatPane(id="chat")
                with TabPane("Jobs [$text-disabled]F3[/]", id="tab-jobs"):
                    yield JobsPane(id="jobs")
                with TabPane("Images [$text-disabled]F4[/]", id="tab-images"):
                    yield ImagesPane(id="images")
            yield Sidebar()
        yield Horizontal(id="attachments")
        with Horizontal(id="promptbox"):
            yield Static("›", id="caret")
            yield jw.PromptArea(placeholder=PROMPT_HINT, id="prompt", suggester=SlashSuggester())
        with Horizontal(id="statusbar"):
            yield Static("", id="status")
            yield Static("", id="statusr")
        yield Footer()

    def get_system_commands(self, screen):
        """Textual's own palette entries minus the developer ones (Keys,
        Maximize, Screenshot) that crowded Johan's actions off the screen,
        and Quit, which sat highlighted first — one stray enter from closing
        the app (ctrl+q still quits)."""
        for cmd in super().get_system_commands(screen):
            if cmd.title not in ("Keys", "Maximize", "Minimize", "Screenshot", "Quit"):
                yield cmd

    # ------------------------------------------------------------ lifecycle

    def on_mount(self):
        self.title = "johan"
        self.register_theme(_fixed_muted(JOHAN_THEME))
        for name in THEMES[1:]:
            if name in self.available_themes:
                self.register_theme(_fixed_muted(self.available_themes[name]))
        self.theme = THEMES[0]
        if self.resume:
            self.messages = ja.load_session()
            try:
                ja.use_session(ja.LAST_ID_FILE.read_text().strip())
            except OSError:
                pass
        if self.messages:
            self.messages[0] = {"role": "system", "content": ja.prime_with_memories(ja.SYSTEM_PROMPT)}
            self._replay(self.messages)
            self._note("resumed your last conversation")
        else:
            self._fresh()
        self._set_status("starting the model…")
        self._refresh_sidebar()
        self.query_one("#prompt").focus()
        self.warm_up()
        self.set_timer(0.5, self._show_backlog)    # changes waiting from earlier

    def _fresh(self):
        self.messages = [{"role": "system", "content": ja.prime_with_memories(ja.SYSTEM_PROMPT)}]
        self.pending = self.plan_card = None
        chat = self.query_one("#chat", ChatPane)
        chat.remove_children()
        chat.add(Welcome())

    def _replay(self, messages):
        chat = self.query_one("#chat", ChatPane)
        chat.remove_children()
        for m in messages:
            if m.get("role") == "user" and not str(m.get("content", "")).startswith("[Johan, internal]"):
                chat.add(UserMsg(_attach.shown_text(m["content"])))
                if m.get("image_paths"):
                    chat.add(AttachedPics(m["image_paths"]))
            elif m.get("role") == "assistant" and m.get("content"):
                t = chat.add(Turn())
                t.set_text(jp.render_plan(jp.parse_plan(m["content"]))
                           if jp.is_plan_message(m) else m["content"])
                t.activity.display = False
                t.meta.update("")
                self._remember_openables(m["content"])

    @work(thread=True, exclusive=True, group="warm")
    def warm_up(self):
        """Load the model AND read Johan's ~5,100-token system prompt + tool
        schemas while he's still looking at the screen.

        This model reads a fresh prompt at only ~230 tok/s on this card, so
        that prefix alone cost ~22 s on the first question of every chat. The
        old warm-up sent a bare "hi", which loaded the weights and nothing
        else. Sending the real prefix leaves a context checkpoint that a new
        question restores: measured 31 s → 3-4 s to the first answer. The
        payload must match call_model's (same system text, tools, num_ctx)."""
        import requests
        msgs = list(self.messages or [])[:1] or [
            {"role": "system", "content": ja.prime_with_memories(ja.SYSTEM_PROMPT)}]
        try:
            requests.post("http://127.0.0.1:11434/api/chat", timeout=180, json={
                "model": ja.MODEL, "messages": msgs + [{"role": "user", "content": "hi"}],
                "tools": ja.TOOLS, "think": False, "stream": False,
                "keep_alive": ja.KEEP_ALIVE,
                "options": {"temperature": 0.3, "num_ctx": ja.NUM_CTX, "num_predict": 1}})
            self.call_from_thread(self._set_status, None)
            self.call_from_thread(self._refresh_sidebar)
        except Exception:
            self.call_from_thread(self._set_status,
                                  "[$error]Ollama unreachable — is Johan paused (GameMode)?[/]")

    def _refresh_sidebar(self):
        self.query_one(Sidebar).refresh_state(self.messages)
        self._status_right()

    # ------------------------------------------------------------ status bar

    def _status_right(self):
        """Model and context fill, always under the prompt — the sidebar
        can be hidden, and overflow is the failure you never see coming."""
        try:
            u = ja.LAST_USAGE
            used, ctx = u.get("prompt_tokens", 0), u.get("num_ctx") or 1
            pct = round(100 * used / ctx)
            colour = "$text-disabled" if pct < 60 else ("$warning" if pct < 85 else "$error")
            label = "[b $warning]uncensored[/]" if ja.is_uncensored() else "$m"
            self.query_one("#statusr", Static).update(
                mk(f"{label}  ·  [{colour}]$u / $c context ($p%)[/]", m=ja.MODEL,
                   u=f"{used / 1000:.1f}k", c=f"{ctx // 1024}k", p=str(pct)))
        except Exception:
            pass

    def _set_status(self, text=None):
        bar = self.query_one("#status", Static)
        if self.busy:
            return
        bar.remove_class("busy")
        bar.update(text or "[$success]●[/] ready  [$text-disabled]·  esc stops  ·  "
                           "ctrl+p everything[/]")

    def _start_busy(self, label):
        self.busy = True
        self.busy_label = label
        self.busy_since = time.monotonic()
        self.query_one("#status", Static).add_class("busy")
        self.query_one("#prompt").placeholder = "working… (esc to stop)"
        if self._ticker is None:
            self._ticker = self.set_interval(0.15, self._tick)
        self._tick()

    def _tick(self):
        if not self.busy:
            return
        self._spin = (self._spin + 1) % len(SPINNER)
        secs = time.monotonic() - self.busy_since
        self.query_one("#status", Static).update(
            mk(f"{SPINNER[self._spin]} $b…  [$text-muted]{secs:.0f}s  ·  esc to stop[/]",
               b=str(self.busy_label)))

    def _stop_busy(self):
        self.busy = False
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None
        self.query_one("#prompt").placeholder = PROMPT_HINT
        self._set_status(None)

    # ------------------------------------------------------------ output helpers

    def _chat(self):
        return self.query_one("#chat", ChatPane)

    def _drop_welcome(self):
        for w in self.query(Welcome):
            w.remove()

    def _note(self, text, kind="note"):
        return self._chat().add(Note(text, kind))

    def _toast(self, text, title=None, error=False):
        # markup=False: toasts carry rule text, paths, job titles, errors.
        self.notify(str(text)[:300], title=title, severity="error" if error else "information",
                    timeout=12 if not error else 15, markup=False)

    def _remember_openables(self, text):
        for u in self.URL_RE.findall(text or ""):
            u = u.rstrip(".,;:")
            if u not in self.openables:
                self.openables.append(u)
        for f in self.FILE_RE.findall(text or ""):
            if f not in self.openables:
                self.openables.append(f)

    def _last_answer(self):
        for m in reversed(self.messages or []):
            if m.get("role") == "assistant" and m.get("content"):
                return m["content"]
        return None

    # ------------------------------------------------------------ input

    def on_key(self, event):
        """↑/↓ recall previous questions, like a shell; tab accepts the
        grey slash-command suggestion (it used to move focus instead, though
        help and the error note both said "press tab")."""
        if event.key == "tab":
            box = self.query_one("#prompt")
            hint = getattr(box, "_suggestion", "")
            if box.has_focus and hint and box.value.startswith("/") and hint != box.value:
                box.value = hint
                box.cursor_position = len(hint)
                event.prevent_default()
                event.stop()
            return
        if event.key not in ("up", "down") or not self.history:
            return
        box = self.query_one("#prompt")
        if not box.has_focus:
            return
        # In a multi-line message ↑/↓ move between lines; history recall only
        # from the first line (↑) or the last line (↓).
        row, last = box.cursor_location[0], box.document.line_count - 1
        if (event.key == "up" and row != 0) or (event.key == "down" and row != last):
            return
        if event.key == "up":
            self.history_pos = (len(self.history) - 1 if self.history_pos is None
                                else max(0, self.history_pos - 1))
        else:
            if self.history_pos is None:
                return
            self.history_pos += 1
            if self.history_pos >= len(self.history):
                self.history_pos = None
                box.value = ""
                event.prevent_default()
                return
        box.value = self.history[self.history_pos]
        box.cursor_position = len(box.value)
        event.prevent_default()

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if bid.startswith("att-"):
            return self.remove_attachment(int(bid.split("-")[1]))
        if bid.startswith("start-"):
            _, prompt = Welcome.STARTERS[int(bid.split("-")[1])]
            box = self.query_one("#prompt")
            if prompt.endswith(" "):          # a template: let him finish it
                box.value = prompt
                box.cursor_position = len(prompt)
                box.focus()
            else:
                self._submit(prompt)

    def on_tabbed_content_tab_activated(self, event):
        """Clicking a tab focuses its table, so its keys work immediately."""
        pid = event.pane.id if event.pane else ""
        target = {"tab-jobs": "#jtable", "tab-images": "#itable"}.get(pid)
        if target:
            try:
                self.query_one(target).focus()
            except Exception:
                pass

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id != "prompt":
            return
        text = event.value.strip()
        event.input.value = ""
        if self.approval is not None:
            # A change is waiting for his OK: typing says no + what instead,
            # an empty enter picks the highlighted option.
            if text:
                self._chat().add(UserMsg(text))
                return self._resolve_approval({"choice": "no", "reason": text},
                                              f"no — {text}", ok=False)
            return self.approval[0].choices.action_pick()
        if self.backlog_card is not None:
            if not text:
                return self.backlog_card.choices.action_pick()
            # Typing means he wants to talk, not decide now: the change stays
            # waiting and comes back the next time Johan opens.
            card, self.backlog_card = self.backlog_card, None
            card.decided("left waiting — it'll be here next time", ok=False)
        if not text and self.attachments and not self.busy:
            text = _attach.DEFAULT_QUESTION       # a picture alone: "what's this?"
        if text:
            self._submit(text)
        elif self.pending is not None and not self.busy:
            # enter on an empty prompt picks the highlighted option
            (self.pending.choices if isinstance(self.pending, PlanCard)
             else self.pending).action_pick()

    def _submit(self, text):
        if self.busy:
            self._toast("still working on the last one — esc stops it")
            return
        self.history.append(text)
        self.history_pos = None
        if text.startswith("/"):
            return self._slash(text)
        if isinstance(self.pending, PlanCard):
            return self._replan(text)       # typed changes to the plan
        self._retire_pending()              # a typed answer replaces the options
        self._ask(text)

    def _slash(self, text):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        handlers = {
            "/new": self.action_new_chat, "/history": self.action_history,
            "/sessions": self.action_history, "/jobs": self.action_jobs_tab,
            "/images": self.action_images_tab,
            "/help": self.action_help_me, "/review": self.action_review,
            "/retry": self.action_retry, "/copy": self.action_copy_last,
            "/theme": self.action_cycle_theme, "/refresh": self.action_refresh_jobs,
            "/rewind": self.action_rewind, "/undo": self.action_rewind,
            "/uncensored": self.action_uncensored, "/normal": self.action_normal_model,
            "/attach": self.action_attach_file,
        }
        if cmd in handlers:
            return handlers[cmd]()
        if cmd == "/draw":
            return self._draw(arg) if arg else self._note("/draw needs tags — e.g. /draw 1girl, "
                                                          "silver hair, rooftop, night", "err")
        if cmd == "/research":
            return self._ask(f"research this properly: {arg}") if arg else \
                self._note("/research needs a question", "err")
        if cmd == "/delete":
            arg = arg or ja.SESSION_ID          # bare /delete = this chat
            current = arg == ja.SESSION_ID
            res = ja.delete_session(arg)
            if not res.get("error"):
                self.after_delete(arg)
                if current:                     # the screen was cleared; toast said so
                    return
            return self._note(res.get("error") or f"deleted {res['deleted']}",
                              "err" if res.get("error") else "ok")
        if cmd == "/resume":
            return self._load_session(arg)
        if cmd in ("/changes", "/edits"):
            return self.action_changes(arg)
        if cmd == "/rules":
            if arg.split(" ")[0] in ("forget", "rm", "remove", "delete"):
                res = jx.remove_rule(arg.split(" ")[-1])
                return self._note(res.get("error") or f"forgot rule: {res['removed']}",
                                  "err" if res.get("error") else "ok")
            return self.action_list_rules()
        if cmd == "/forget":
            if not arg.isdigit():
                return self._note("/forget needs a memory id — e.g. /forget 12", "err")
            res = jt.forget(int(arg))
            return self._note(res.get("error") or f"forgot: {res['was']}",
                              "err" if res.get("error") else "ok")
        if self._file_command(text):
            return
        self._note(f"no command {cmd} — type / and press tab to see them", "err")

    def _file_command(self, text):
        """commands/<name>.md → its expanded prompt, asked like any message.
        Only reached after every built-in had its chance, so a file can
        never shadow one. True if it was a file command."""
        res = jc.resolve(text, builtins=BUILTIN_SLASH)
        if res is None:
            return False
        if res.get("error"):
            self._drop_welcome()
            self._note(res["error"], "err")
        else:
            self._ask(res["prompt"], shown=text)
        return True

    # ------------------------------------------------------------ asking

    def _ask(self, text, shown=None, images=None):
        """`shown` is what appears in the chat when it differs from what the
        model gets (a file command shows `/tldr <url>`, sends its prompt).
        Pictures: `images`, else whatever is attached right now."""
        self.query_one("#main", TabbedContent).active = "tab-chat"
        self._drop_welcome()
        if images is None:
            images, self.attachments = list(self.attachments), []
            self._refresh_chips()
        self._chat().add(UserMsg(shown or text))
        if images:
            self._chat().add(AttachedPics(images))
        tid = jx.new_turn()
        self.turn_ids[len(self.messages)] = tid
        self.turn_tools[tid] = []
        self.messages.append(_attach.user_message(text, images))
        self._start_turn(plan_check=True)

    # ---- attached pictures (johan_attach.py) ------------------------------

    def attach_paths(self, paths):
        """Attach pictures: copied into brain/uploads, shown as chips."""
        added = []
        for p in paths:
            try:
                added.append(str(_attach.keep(p)))
            except Exception as e:
                self._toast(f"couldn't attach {Path(p).name}: {e}", "attach", True)
        self.attachments += added
        self._refresh_chips()
        if added:
            self.query_one("#prompt").focus()
        return added

    def remove_attachment(self, i=-1):
        if self.attachments:
            self.attachments.pop(i)
            self._refresh_chips()

    def _refresh_chips(self):
        row = self.query_one("#attachments", Horizontal)
        row.remove_children()
        for i, p in enumerate(self.attachments):
            wh = _attach.size_of(p)
            label = f"📎 {_attach.display_name(p)}" + (f" ({wh[0]}×{wh[1]})" if wh else "") + "  ✕"
            # ids must be unique even while the old chips are still being removed
            row.mount(Button(label, id=f"att-{i}-{time.monotonic_ns()}"))
        row.set_class(bool(self.attachments), "has")

    def paste_clipboard(self):
        """ctrl+v in the prompt: a picture on the clipboard is attached, text
        is pasted. The terminal can't hand a picture to the app (WezTerm only
        forwards text), so Johan reads the clipboard itself with xclip.
        False = xclip gave nothing at all; the prompt falls back to its own."""
        img = _attach.clipboard_image()
        if img is not None:
            self.attach_paths([img])
            return True
        text = _attach.clipboard_text()
        if text is None:
            return False
        paths = _attach.dropped_paths(text)
        if paths:
            self.attach_paths(paths)
        elif text:
            self.query_one("#prompt").insert(text)
        return True

    def action_attach_file(self):
        self.pick_worker()

    @work(thread=True, exclusive=True, group="pick")
    def pick_worker(self):
        path = _attach.pick_file()
        if path and _attach.is_image(path):
            self.call_from_thread(self.attach_paths, [path])
        elif path:
            self.call_from_thread(self._toast, "that isn't a picture Johan can read",
                                  "attach", True)

    def _start_turn(self, plan_check=False, replan=None):
        self.cancel.clear()
        self.turn = self._chat().add(Turn())
        self._start_busy("planning" if replan else "thinking")
        self.ask_worker(plan_check, replan)

    # ---- ask-with-options and plan-first (johan_plan.py, johan_widgets.py)

    # ---- inline approval (johan_approval.py) ------------------------------

    def _approve(self, kind, item):
        """Called from the turn's tool thread: show the card, wait for him.
        esc (the turn's cancel) counts as no, so nothing runs."""
        ev, box = threading.Event(), {}
        self.call_from_thread(self._show_approval, kind, item, ev, box)
        while not ev.wait(0.1):
            if self.cancel.is_set():
                self.call_from_thread(self._resolve_approval,
                                      {"choice": "no", "reason": "he pressed esc"},
                                      "stopped — nothing ran", False)
                ev.wait(2)
                break
        return box.get("decision") or {"choice": "no", "reason": "no answer"}

    def _show_approval(self, kind, item, ev, box):
        card = ApprovalCard(kind, item)
        turn = self.turn
        if turn is not None and turn.is_mounted:
            # Inside the turn, between its steps and its answer — added at
            # the bottom of the chat it ended up BELOW the final answer.
            turn.mount(card, before=turn.body)
        else:
            self._chat().add(card)
        self.approval = (card, ev, box)
        self.busy_label = "waiting for your OK"
        card.choices.focus()
        self._chat().scroll_end(animate=False)

    def _resolve_approval(self, decision, text, ok=True):
        if self.approval is None:
            return
        card, ev, box = self.approval
        self.approval = None
        card.decided(text, ok)
        box["decision"] = decision
        ev.set()
        if decision["choice"] != "no":
            self.busy_label = "running the change"
        self.query_one("#prompt").focus()

    # ---- changes waiting from earlier, and /changes ------------------------

    def _show_backlog(self, intro=True):
        """Changes proposed while nobody could answer — self-review, the
        phone, one-shot `johan "…"` — shown as the same cards, one at a time.
        (This replaced the F4 tab: every approval happens in the chat.)"""
        if self.busy or self.backlog_card is not None or self.approval is not None:
            return
        items = ([("action", {**a, "can_always": jx.can_always(a["steps"])})
                  for a in jx.list_actions()]
                 + [("edit", e) for e in jt._load_edits()])
        if not items:
            return
        if intro:
            n = len(items)
            self._note(f"{n} change{'s' * (n > 1)} waiting from earlier — "
                       f"pick for {'each' if n > 1 else 'it'}, or just keep chatting", "note")
        kind, item = items[0]
        card = self._chat().add(ApprovalCard(kind, item, earlier=True))
        self.backlog_card = card
        # After the current event settles: shown from a slash command, the
        # prompt took focus back right after, and "1" was typed as text.
        self.call_after_refresh(card.choices.focus)
        self._chat().scroll_end(animate=False)

    @work(thread=True, exclusive=True, group="backlog")
    def backlog_worker(self, card, choice):
        item = card.item
        ok = True
        try:
            if choice == "no":
                (jx.discard_action if card.kind == "action" else jt.discard_edit)(item["id"])
                text, ok = "declined — nothing ran", False
            elif card.kind == "edit":
                res = jt.apply_edit(item["id"])
                text = res.get("error") or f"applied to {res.get('path')} (backup kept)"
                ok = not res.get("error")
            else:
                res = jx.allow_and_run(item["id"]) if choice == "always" else jx.run_action(item["id"])
                text = res.get("error") or jx.describe_result(res)
                if res.get("rules_added"):
                    text += " · always allowed from now on"
                ok = not res.get("error")
        except Exception as e:
            text, ok = f"failed: {type(e).__name__}: {e}", False
        self.call_from_thread(card.decided, text, ok)
        self.call_from_thread(self._backlog_next)

    def _backlog_next(self):
        self.backlog_card = None
        self._refresh_sidebar()
        self._show_backlog(intro=False)
        if self.backlog_card is None:
            self.query_one("#prompt").focus()

    def action_changes(self, arg=""):
        """/changes — what Johan changed lately; /changes undo — undo the last."""
        self.query_one("#main", TabbedContent).active = "tab-chat"
        self._drop_welcome()
        if arg.strip().lower() == "undo":
            return self.action_undo_last_change()
        recent = jx.recent(8)
        if not recent:
            return self._note("no changes yet", "note")
        lines = ["recent changes (newest last):"]
        for r in recent:
            mark = "✓" if r.get("ok") else "✗"
            undone = " · undone" if r.get("undone") else ""
            lines.append(f"  {mark} #{r['id']} {(r.get('why') or 'change')[:60]}{undone}")
        lines.append("/changes undo — undo the last one · ctrl+z rewinds a whole turn")
        self._note("\n".join(lines), "ok")

    def action_undo_last_change(self):
        if self.busy:
            return self._toast("still working — esc first")
        res = jx.propose_undo()
        if res.get("error"):
            return self._note(res["error"], "err")
        self._show_backlog(intro=False)            # the undo is a change like any other: asked here

    def _retire_pending(self):
        p, self.pending = self.pending, None
        if isinstance(p, PlanCard):
            p.superseded()
        elif p is not None:
            p.retire()

    def _replan(self, changes):
        self._chat().add(UserMsg(changes))
        self._start_turn(replan=changes)

    def _show_plan(self, turn, steps):
        """A plan instead of an answer: nothing runs until he picks go."""
        turn.remove()
        if not steps:              # a re-plan that failed: the old plan still waits
            self._note("couldn't rewrite the plan — the one above still stands", "err")
            return self._finish(focus=getattr(self.pending, "choices", self.pending))
        self._retire_pending()
        card = self._chat().add(PlanCard(steps))
        self.pending = card
        self._finish(focus=card.choices)

    def _show_choices(self, asked):
        self._retire_pending()
        ch = self._chat().add(Choices(asked["options"]))
        self.pending = ch
        ch.focus()

    def on_choices_picked(self, event: Choices.Picked):
        if self.backlog_card is not None and event.choices is self.backlog_card.choices:
            label = self.backlog_card.options[event.index]
            choice = ("no" if label == "No" else
                      "always" if label.startswith("Yes, and always") else "yes")
            return self.backlog_worker(self.backlog_card, choice)
        if self.approval is not None and event.choices is self.approval[0].choices:
            card = self.approval[0]
            label = card.options[event.index]
            choice = ("no" if label == "No" else
                      "always" if label.startswith("Yes, and always") else "yes")
            return self._resolve_approval({"choice": choice, "reason": ""},
                                          {"yes": "approved", "always": "approved · always allowed from now on",
                                           "no": "declined"}[choice], ok=choice != "no")
        owner = self.pending
        if self.busy or owner is None:
            return
        if isinstance(owner, PlanCard) and event.choices is owner.choices:
            self.pending = None
            if event.index == 0:
                jp.approve(self.messages)
                owner.start()
                self.plan_card = owner
                self._start_turn()
            else:
                jp.cancel(self.messages)
                owner.cancelled()
                self._finish()
            return
        if event.choices is owner:
            self._submit(event.text)        # the pick is his next message

    @work(thread=True)
    def ask_worker(self, plan_check=False, replan=None):
        # Everything this worker thread waits on — the plan check, model
        # calls, tools — now gives way to esc within ~0.1 s.
        ji.set_stop(self.cancel.is_set)
        # Changes Johan wants to make are asked about inline; the turn waits.
        jap.set_approver(self._approve)
        turn = self.turn
        self._asked = None
        q = ja._last_user_message(self.messages)
        # Plan-first: a regex costs nothing, so only messages it flags pay
        # for the calibrated check, and only a "yes" pays for the plan call.
        if replan is not None or (plan_check and jp.prefilter(q)):
            steps = []
            try:
                if replan is not None or jp.needs_plan(q):
                    self.busy_label = "planning"
                    steps = jp.make_plan(self.messages, changes=replan)
            except Exception:
                steps = []
            if steps or replan is not None:
                self.call_from_thread(self._show_plan, turn, steps)
                return

        tid = jx.TURN

        def on_tool(name, args, result):
            self.turn_tools.setdefault(tid, []).append((name, args))
            self.call_from_thread(turn.add_step, name, args, result)
            card = self.plan_card
            if card is not None:
                self.call_from_thread(card.tick, name, jp.failed_result(result))
            if name == "ask" and isinstance(result, dict) and result.get("asked"):
                self._asked = result
            self.call_from_thread(setattr, self, "busy_label", tool_word(name))
            if name == "generate_image" and isinstance(result, dict) and result.get("path"):
                self.call_from_thread(self._show_image, result)
            if name == "propose_action" and isinstance(result, dict) and result.get("auto_allowed"):
                self.call_from_thread(self._toast, result.get("result", ""),
                                      f"change a{result.get('action_id')} ran (always allowed)",
                                      not result.get("ok"))

        def on_delta(piece):
            self._stream_buf += piece
            now = time.monotonic()
            if now - self._stream_last >= 0.1:
                self._stream_last = now
                self.call_from_thread(turn.set_text, self._stream_buf)
                self.call_from_thread(self._chat().scroll_end, animate=False)

        interrupted, reply = False, None
        try:
            self._stream_buf, self._stream_last = "", 0.0
            reply = ja.run_turn(self.messages, on_tool=on_tool, on_delta=on_delta,
                                should_stop=self.cancel.is_set)
            interrupted = self.cancel.is_set()
            text = reply or self._stream_buf or "(no answer — try rephrasing)"
            if self._asked:
                text = self._asked["question"]     # the options show below as choices
            self.call_from_thread(turn.set_text, text)
            for r in ja.LAST_LEARNED:           # a correction became a rule
                self.call_from_thread(self._toast, f"{r['text']}\n/forget {r['memory_id']} to undo",
                                      "already knew that" if r.get("duplicate") else "learned")
            self.call_from_thread(self._remember_openables, text)
        except Exception as e:
            self.call_from_thread(self._note, f"{type(e).__name__}: {e}", "err")
        finally:
            card, self.plan_card = self.plan_card, None
            if card is not None:
                self.call_from_thread(card.finish, bool(reply), interrupted)
            self.call_from_thread(turn.finish, interrupted)
            self.call_from_thread(self._finish)
            if self._asked and not interrupted:
                self.call_from_thread(self._show_choices, self._asked)

    def _show_image(self, result):
        self._chat().add(ImageCard(result))
        if result.get("path") not in self.openables:
            self.openables.append(result["path"])
        self.query_one(ImagesPane).load()

    def _finish(self, focus=None):
        jx.end_turn()
        self._stop_busy()
        self._chat().scroll_end(animate=False)
        self._refresh_sidebar()
        try:
            self.query_one(JobsPane).load()        # never focus: see JobsPane.load
        except Exception:
            pass
        self._show_backlog()                       # e.g. changes staged from the phone
        try:
            (focus or self.query_one("#prompt")).focus()
        except Exception:
            pass
        try:
            ja.save_session(self.messages)
            ja.log_session(self.messages)
        except Exception:
            pass

    # ------------------------------------------------------------ drawing

    def _draw(self, arg):
        """/draw [landscape|square|portrait] [picture-path [strength]] tags"""
        shown = arg
        size, style = None, "anime"
        first = arg.split(" ", 1)[0].lower()
        if first in ("landscape", "square", "portrait"):
            size, arg = first, (arg.split(" ", 1)[1] if " " in arg else "")
        ref, strength, arg = _split_reference(arg)
        if not arg:
            return self._note("what should I draw? — /draw [picture] tags", "err")
        if ref is None and self.attachments:     # the attached picture is the reference
            ref = self.attachments.pop(0)
            shown = f"{shown}  (redrawing {_attach.display_name(ref)})"
            self._refresh_chips()
        self.query_one("#main", TabbedContent).active = "tab-chat"
        self._drop_welcome()
        self._chat().add(UserMsg(f"/draw {shown}"))
        self.turn = self._chat().add(Turn())
        self._start_busy("redrawing" if ref else "drawing")
        self.draw_worker(arg, size, None, ref, strength, style)

    @work(thread=True)
    def draw_worker(self, prompt, size, seed, reference=None, strength=None, style="anime"):
        import johan_images as ji
        turn = self.turn
        try:
            r = ji.generate_image(prompt, size=size, seed=seed,
                                  reference=reference, strength=strength, style=style,
                                  on_progress=lambda m: self.call_from_thread(
                                      setattr, self, "busy_label", m))
            if r.get("error"):
                self.call_from_thread(turn.set_text, f"Couldn't draw that: {r['error']}")
                self.call_from_thread(self._toast, r["error"], "drawing failed", True)
            else:
                self.call_from_thread(self._show_image, r)
                note = f"Drew it — saved to `{r['path']}` (seed {r['seed']})."
                self.messages += [{"role": "user", "content": f"draw: {prompt}"},
                                  {"role": "assistant", "content": note}]
                if self.query_one("#main", TabbedContent).active != "tab-chat":
                    self.call_from_thread(self._toast, "your image is ready — F4", "drawn")
        except Exception as e:
            self.call_from_thread(self._note, f"drawing failed: {e}", "err")
        finally:
            self.call_from_thread(turn.finish, False)
            self.call_from_thread(self._finish)

    # ------------------------------------------------------------ sessions

    def _load_session(self, sid):
        msgs = ja.load_session(sid) if sid else None
        if not msgs:
            return self._note(f"no saved conversation '{sid}' (F2 lists them)", "err")
        self.messages = msgs
        ja.use_session(sid)
        self.messages[0] = {"role": "system", "content": ja.prime_with_memories(ja.SYSTEM_PROMPT)}
        self.query_one("#main", TabbedContent).active = "tab-chat"
        self._replay(self.messages)
        self._note(f"resumed · {ja.humane_when(sid)}")
        self._refresh_sidebar()

    # ------------------------------------------------------------ app actions

    def action_help_me(self):
        self.push_screen(HelpScreen())

    def action_history(self):
        if not ja.list_sessions():
            return self._toast("no past conversations yet")
        self.push_screen(HistoryScreen(), lambda sid: sid and self._load_session(sid))

    def _goto(self, tab, pane):
        self.query_one("#main", TabbedContent).active = tab
        self.query_one(pane).load(focus=True)

    def action_jobs_tab(self):
        self._goto("tab-jobs", JobsPane)


    def action_images_tab(self):
        self._goto("tab-images", ImagesPane)

    def forget_images(self):
        """Any inline picture whose file is gone stops drawing itself, and
        the gallery reloads so it never previews a deleted image."""
        for card in self.query(ImageCard):
            if not Path(card.result["path"]).exists():
                card.file_gone()
        for pane in self.query(ImagesPane):
            pane.load()

    def after_delete(self, sid):
        """Deleting the chat you're looking at clears it from the screen —
        otherwise its (now deleted) pictures crash the next redraw."""
        if sid == ja.SESSION_ID:
            ja.LAST_USAGE.update(prompt_tokens=0, completion_tokens=0)
            ja.new_session()
            self._fresh()
            self._refresh_sidebar()
            self.forget_images()
            self._toast("chat deleted")
            self.warm_up()
        else:
            self.forget_images()

    def action_uncensored(self):
        """Swap the whole engine to the uncensored model for this chat. Same
        tools, same code-level guards; only what the model will say changes."""
        if self.busy:
            return self._toast("still working — esc first")
        if ja.is_uncensored():
            return self._toast("already uncensored — /normal switches back")
        if not ja.model_installed(ja.UNCENSORED_MODEL):
            return self._note(f"the uncensored model ({ja.UNCENSORED_MODEL}) isn't installed", "err")
        ja.use_model(ja.UNCENSORED_MODEL)
        self._note("uncensored mode for this chat — same tools; /normal switches back", "ok")
        self._status_right()
        self.warm_up()            # load it now (~10-15 s), not on the next question

    def action_normal_model(self):
        if self.busy:
            return self._toast("still working — esc first")
        if not ja.is_uncensored():
            return self._toast("already in normal mode")
        ja.use_model(ja.NORMAL_MODEL)
        self._note("back to normal mode", "ok")
        self._status_right()
        self.warm_up()

    def action_new_chat(self):
        if self.busy:
            return self._toast("still working — esc first")
        ja.LAST_USAGE.update(prompt_tokens=0, completion_tokens=0)
        if ja.is_uncensored():
            ja.use_model(ja.NORMAL_MODEL)     # a new chat always starts normal
        ja.new_session()
        self.query_one("#main", TabbedContent).active = "tab-chat"
        self._fresh()
        self._refresh_sidebar()
        self.query_one("#prompt").focus()
        self.warm_up()           # a new chat's first question re-reads the prefix

    def action_retry(self):
        idx = next((i for i in range(len(self.messages or []) - 1, -1, -1)
                    if self.messages[i].get("role") == "user"
                    and not str(self.messages[i].get("content", "")).startswith(("[", "draw:"))),
                   None)
        if idx is None:
            return self._note("nothing to retry yet", "err")
        last = self.messages[idx]
        # Drop the previous attempt so the retry isn't primed by it.
        self.messages = self.messages[:idx]
        self._ask(_attach.shown_text(last["content"]),
                  images=[p for p in last.get("image_paths", []) if Path(p).is_file()])

    def _last_user_idx(self):
        return next((i for i in range(len(self.messages or []) - 1, 0, -1)
                     if self.messages[i].get("role") == "user"
                     and not str(self.messages[i].get("content", "")).startswith("[")), None)

    def action_rewind(self):
        """ctrl+z: take back the last turn — the question and answer, and
        what it changed on disk — and put the question back to edit."""
        if self.busy:
            return self._toast("still working — esc first")
        idx = self._last_user_idx()
        if idx is None:
            return self._toast("nothing to rewind")
        tid = self.turn_ids.get(idx)
        plan = jx.rewind_plan(tid)
        plan["cannot"] += _side_effects(self.turn_tools.get(tid, []))
        if not any(plan.values()):
            return self._rewind_chat(idx)          # nothing changed: instant
        self.push_screen(RewindScreen(plan),
                         lambda ok: ok and self._rewind_changes(idx, tid, plan))

    def _rewind_changes(self, idx, tid, plan):
        if not plan["revert"] and not plan["discard"]:
            return self._rewind_chat(idx)
        self._rewinding = True
        self._start_busy("rewinding" + (" — enter your password in the system dialog"
                                        if "password" in " ".join(plan["revert"]) else ""))
        self.rewind_worker(idx, tid)

    @work(thread=True)
    def rewind_worker(self, idx, tid):
        try:
            res = jx.rewind_apply(tid)
        except Exception as e:
            res = {"reverted": [], "discarded": [], "failed": [f"{type(e).__name__}: {e}"]}
        self.call_from_thread(self._rewind_done, idx, res)

    def _rewind_done(self, idx, res):
        self._rewinding = False
        self._stop_busy()
        self._rewind_chat(idx)
        if res["failed"]:
            self._toast("; ".join(res["failed"])[:300], "rewind: not everything reverted", True)
        else:
            self._toast(f"reverted {len(res['reverted'])}, discarded {len(res['discarded'])}",
                        "rewound")

    def _rewind_chat(self, idx):
        text = _attach.shown_text(self.messages[idx].get("content", ""))
        pics = [p for p in self.messages[idx].get("image_paths", []) if Path(p).is_file()]
        if pics:                       # his pictures come back with the question
            self.attachments = pics + [p for p in self.attachments if p not in pics]
            self._refresh_chips()
        if text.startswith("draw: "):
            text = "/draw " + text[6:]
        self.messages = self.messages[:idx]
        self.turn_ids = {k: v for k, v in self.turn_ids.items() if k < idx}
        if len(self.messages) > 1:
            self._replay(self.messages)
        else:
            self._fresh()
        self.query_one("#main", TabbedContent).active = "tab-chat"
        box = self.query_one("#prompt")
        box.value, box.cursor_position = text, len(text)
        box.focus()
        self._refresh_sidebar()
        try:
            ja.save_session(self.messages)
        except Exception:
            pass

    def action_copy_last(self):
        text = self._last_answer()
        if not text:
            return self._toast("nothing to copy yet")
        self.copy_to_clipboard(text)
        self._toast("last answer copied")

    def action_open_last(self):
        target = (self.openables or [None])[-1]
        if not target:
            return self._toast("nothing to open yet")
        if not open_target(target):
            self._toast(f"couldn't open {target}", error=True)

    def action_toggle_sidebar(self):
        self._sidebar_chosen = True
        self.query_one(Sidebar).toggle_class("hidden")

    SIDEBAR_MIN_WIDTH = 120

    def on_resize(self, event):
        """Below ~120 columns the sidebar squeezed the chat and the jobs
        table into scrolling sideways; the status line under the prompt
        still shows model and context, so it steps aside on its own —
        until ctrl+b says otherwise."""
        if getattr(self, "_sidebar_chosen", False):
            return
        try:
            self.query_one(Sidebar).set_class(event.size.width < self.SIDEBAR_MIN_WIDTH, "hidden")
        except Exception:
            pass

    def action_refresh(self):
        self._refresh_sidebar()
        for pane in (JobsPane, ImagesPane):
            self.query_one(pane).load()
        self._show_backlog()
        for w in self.query(Welcome):
            w.refresh_state()
        self._toast("refreshed")

    def action_cycle_theme(self):
        self.theme_idx = (self.theme_idx + 1) % len(THEMES)
        self.theme = THEMES[self.theme_idx]

    def action_interrupt(self):
        """Esc stops generation mid-sentence, so a misread question costs
        seconds, not the whole answer."""
        if getattr(self, "_rewinding", False):
            return
        if not self.busy:
            if isinstance(self.pending, PlanCard):
                self.pending.choices.pick(1)      # esc on a waiting plan = cancel
            return
        self.cancel.set()
        self.busy_label = "stopping"
        self.messages.append({"role": "user", "content": "[interrupted you mid-answer]"})

    def action_start_draw(self):
        box = self.query_one("#prompt")
        box.value, box.cursor_position = "/draw ", 6
        box.focus()

    def action_start_research(self):
        box = self.query_one("#prompt")
        box.value, box.cursor_position = "/research ", 10
        box.focus()

    # --- review ---------------------------------------------------------

    def action_review(self):
        if self.busy:
            return
        self._drop_welcome()
        self._note("self-review: reading the last week of failures and staging the "
                   "best fixes for you to approve — about a minute")
        self._start_busy("reviewing my mistakes")
        self.review_worker()

    @work(thread=True)
    def review_worker(self):
        import johan_review as jr
        try:
            result = jr.analyse(7)
            staged = jr.stage_proposals(result)
            jr.write_report(result, jr._low_confidence(7), staged, 7)
            fixed = sum(1 for c in result["clusters"].values() if c["fixed"])
            msg = (f"{len(result['clusters'])} kinds of failure ({fixed} already fixed) · "
                   + (f"{len(staged)} proposal{'s' * (len(staged) != 1)} — shown below for your OK"
                    if staged else "nothing worth proposing this time"))
            self.call_from_thread(self._note, f"self-review: {msg}", "ok")
            self.call_from_thread(self._toast, msg, "self-review done")
        except Exception as e:
            self.call_from_thread(self._note, f"self-review failed: {e}", "err")
        finally:
            self.call_from_thread(self._finish)

    # --- edits ----------------------------------------------------------

    def action_list_rules(self):
        self.query_one("#main", TabbedContent).active = "tab-chat"
        rules = jx.load_rules()
        if not rules:
            return self._note("no always-allow rules — pick “always allow” on a change card to add one", "ok")
        self._note("always allowed (run without asking):\n"
                   + "\n".join(f"  {i}. {jx.rule_text(r)}" for i, r in enumerate(rules, 1))
                   + "\n/rules forget N removes one", "ok")

    def action_open_posting(self):
        job = self.query_one(JobsPane).selected()
        if job and open_target(job["url"]):
            self._toast(f"opened {job['company']} — {job['title']}")

    def action_open_cv(self):
        job = self.query_one(JobsPane).selected()
        if not job:
            return
        if job.get("pdf") and Path(job["pdf"]).exists():
            open_target(job["pdf"])
        else:
            self._toast("no CV built for that job yet", error=True)

    def action_toggle_applied(self):
        job = self.query_one(JobsPane).selected()
        if not job:
            return
        now_applied = job["job_id"] not in ja.applied_ids()
        ja.mark_applied(job["job_id"], applied=now_applied)
        self.query_one(JobsPane).load(focus=True)
        self._refresh_sidebar()
        self._toast(f"{'marked applied' if now_applied else 'unmarked'}: {job['company']}")

    def action_autofill(self):
        """Open the selected job's application form, filled. He submits."""
        job = self.query_one(JobsPane).selected()
        if not job or self.busy:
            return
        import johan_autofill as jf
        if not jf.application_url(job.get("url", "")):
            return self._toast("autofill supports Greenhouse and Ashby so far — o opens "
                               "this one", error=True)
        self._start_busy(f"filling {job['company']}'s application")
        self._toast("a browser window will open with the form — Johan never submits",
                    "filling the application")
        self.autofill_worker(job["job_id"])

    @work(thread=True)
    def autofill_worker(self, job_id):
        import johan_autofill as jf
        try:
            r = jf.autofill_application(job_id)
            if r.get("error"):
                self.call_from_thread(self._toast, r["error"], "autofill failed", True)
            else:
                c = r.get("counts") or {}
                msg = (f"{c.get('profile', 0) + c.get('answers', 0)} filled · "
                       f"{c.get('drafted', 0)} drafted · {c.get('needs_you', 0)} need you. "
                       f"{r.get('note', '')}")
                self.call_from_thread(self._toast, msg, "form is open in the browser")
        except Exception as e:
            self.call_from_thread(self._toast, str(e), "autofill failed", True)
        finally:
            self.call_from_thread(self._finish)

    def action_rebuild(self):
        if self.busy:
            return
        self._start_busy("rebuilding the job queue (a CV per job — about a minute)")
        self.rebuild_worker()

    def action_refresh_jobs(self):
        """Fetch new postings, close dead ones, re-check, rebuild — in the
        background, so chat keeps working. Only ever started by him."""
        if getattr(self, "refresh_progress", None):
            return self._toast(f"{self.refresh_progress} — started "
                               f"{int((time.monotonic() - self.refresh_started) / 60)} min ago",
                               "refresh running")
        self.refresh_progress = "starting"
        self.refresh_started = time.monotonic()
        self._toast("fetching new postings and checking them — about 20 min. Keep "
                    "chatting (answers are slower while jobs are being checked). "
                    "/refresh again shows progress.", "refresh started")
        self.refresh_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_worker(self):
        import johan_refresh as jr

        def progress(msg):
            self.refresh_progress = msg
        try:
            res = jr.refresh(progress)
        except Exception as e:
            res = {"error": f"refresh failed: {e}"}
        self.refresh_progress = None
        self.call_from_thread(self._toast, jr.describe(res),
                              "refresh failed" if res.get("error") else "jobs refreshed",
                              bool(res.get("error")))
        if not res.get("error"):
            self.call_from_thread(self._reload_jobs)

    def _reload_jobs(self):
        for pane in self.query(JobsPane):
            pane.load()

    def action_rebuild_any(self):
        self.query_one("#main", TabbedContent).active = "tab-jobs"
        self.action_rebuild()

    @work(thread=True)
    def rebuild_worker(self):
        import argparse
        try:
            args = argparse.Namespace(verdict="likely", limit=12, job_id=[],
                                      min_fit=5, include_overreach=False)
            rows = aq.build(args)
            if rows:
                aq.write_json(rows, "likely")
                aq.write_markdown(rows, "likely")
            self.call_from_thread(self._toast, f"{len(rows)} jobs, CVs written", "queue rebuilt")
        except Exception as e:
            self.call_from_thread(self._toast, str(e), "rebuild failed", True)
        finally:
            self.call_from_thread(self._finish)

    # --- images ---------------------------------------------------------

    def action_open_image(self):
        r = self.query_one(ImagesPane).selected()
        if r:
            open_target(r["path"])

    def action_reuse_prompt(self):
        r = self.query_one(ImagesPane).selected()
        if not r:
            return
        box = self.query_one("#prompt")
        box.value = f"/draw {r.get('prompt', '')}"
        box.cursor_position = len(box.value)
        box.focus()

    def action_vary_image(self):
        r = self.query_one(ImagesPane).selected()
        if not r or self.busy:
            return
        self.query_one("#main", TabbedContent).active = "tab-chat"
        self._drop_welcome()
        self._chat().add(UserMsg(f"/draw {r.get('prompt', '')}  (variation)"))
        self.turn = self._chat().add(Turn())
        self._start_busy("drawing a variation")
        self.draw_worker(r.get("prompt", ""), r.get("size", "portrait"), None,
                         r.get("reference"), r.get("strength"))

    def action_redraw_image(self):
        """Use the selected picture as the starting point for a new one."""
        r = self.query_one(ImagesPane).selected()
        if not r:
            return
        self.query_one("#main", TabbedContent).active = "tab-chat"
        box = self.query_one("#prompt")
        box.value = f"/draw {_quote_path(r['path'])} "
        box.cursor_position = len(box.value)
        box.focus()
        self._toast("type what to change, e.g. “night, rain” — add 0.3 after the "
                    "path for a small tweak, 0.9 for a big one")

    def action_delete_image(self):
        pane = self.query_one(ImagesPane)
        r = pane.selected()
        if not r:
            return
        if pane.pending_delete != r["path"]:
            pane.pending_delete = r["path"]
            return self._toast("press x again to delete this image")
        import johan_images as ji
        ji.delete_image(r["path"])
        pane.pending_delete = None
        self.forget_images()
        pane.load(focus=True)
        self._toast("image deleted")


def main():
    resume = "--continue" in sys.argv
    try:
        JohanApp(resume=resume).run()
    finally:
        # Every exit path frees the GPU: the chat model and the image engine.
        ja.release_model()
        try:
            import johan_images as ji
            ji.stop_server()
        except Exception:
            pass


if __name__ == "__main__":
    main()
