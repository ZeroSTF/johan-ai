"""Slash commands written as files.

    commands/standup.md   →   /standup [args]

A file is an optional front matter block and a prompt template:

    ---
    description: one line for completion, the palette and help
    args: <url>          (a hint; shown in help, and means args are required)
    ---
    Summarise $ARGS in five bullets.

`$ARGS` or `{{args}}` is replaced with whatever followed the command. A
template with neither gets the args appended on a new paragraph, so a file
that is only an instruction still sees what was typed. The expanded text is
sent to Johan as an ordinary message — a file command can do nothing a
typed message couldn't.

Built-in commands always win: a file named like one is ignored everywhere
(completion, palette, help, dispatch), never half-shadowing it.

The directory is re-read only when a file's mtime/size changes, so adding
or editing a command takes effect on the next keystroke without a restart.
"""

import re
from pathlib import Path

COMMANDS_DIR = Path(__file__).resolve().parent.parent / "commands"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PLACEHOLDER = re.compile(r"\$ARGS\b|\{\{\s*args\s*\}\}", re.IGNORECASE)
MAX_BYTES = 16 * 1024

_cache = {}


def _parse(text):
    """(front-matter dict, body). Front matter is `key: value` lines between
    two `---` lines at the very top; anything else is body."""
    meta, body = {}, text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                for kv in lines[1:i]:
                    key, sep, value = kv.partition(":")
                    if sep and key.strip():
                        meta[key.strip().lower()] = value.strip().strip("\"'")
                body = "\n".join(lines[i + 1:])
                break
    return meta, body.strip()


def _signature(directory):
    try:
        files = sorted(p for p in directory.glob("*.md") if p.is_file())
    except OSError:
        return ()
    sig = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        sig.append((p.name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def load(directory=None, exclude=()):
    """{name: command} for every valid file, minus names in `exclude`
    (the built-ins, with or without their leading slash)."""
    directory = Path(directory) if directory else COMMANDS_DIR
    sig = _signature(directory)
    cached = _cache.get(str(directory))
    if not cached or cached[0] != sig:
        cmds = {}
        for name, _, size in sig:
            stem = name[:-3].lower()
            if not NAME_RE.match(stem) or size > MAX_BYTES:
                continue
            path = directory / name
            try:
                meta, body = _parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if not body:
                continue
            cmds[stem] = {"name": stem, "path": str(path), "template": body,
                          "description": meta.get("description", "").strip(),
                          "args": meta.get("args", "").strip()}
        cached = (sig, cmds)
        _cache[str(directory)] = cached
    skip = {str(e).lstrip("/").strip().lower() for e in exclude}
    return {k: v for k, v in cached[1].items() if k not in skip}


def expand(cmd, args=""):
    """The prompt to send, or {"error": ...} when required args are missing."""
    args = (args or "").strip()
    template = cmd["template"]
    if not args and (cmd.get("args") or PLACEHOLDER.search(template)):
        hint = cmd.get("args") or "<text>"
        return {"error": f"usage: /{cmd['name']} {hint}"}
    if PLACEHOLDER.search(template):
        return {"prompt": PLACEHOLDER.sub(lambda _: args, template)}
    return {"prompt": f"{template}\n\n{args}" if args else template}


def resolve(text, builtins=(), directory=None):
    """'/name rest' → {"name", "prompt"} | {"name", "error"} | None (not a
    file command — unknown, or a built-in that must win)."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text[1:].partition(" ")
    name = head.split("@")[0].lower()           # /tldr@BotName in Telegram groups
    cmd = load(directory, exclude=builtins).get(name)
    if not cmd:
        return None
    return {"name": name, **expand(cmd, rest)}


def usage(cmd):
    return f"/{cmd['name']}" + (f" {cmd['args']}" if cmd.get("args") else "")
