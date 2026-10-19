"""Pictures he attaches: the clipboard, a file picker, a dragged-in path.

Attached pictures are copied into brain/uploads (so deleting the original —
or a chat — never breaks a message that points at it), stored on the user
message as PATHS (`image_paths`), and only turned into base64 for Ollama at
send time (`ollama_messages`). Sessions stay small, and old pictures drop
out of what the model sees after a couple of turns so they stop costing
context: a 1024 px picture is ~1,000 prompt tokens on every hop.

Nothing here talks to the model; the TUI calls it.
"""
import base64
import io
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

UPLOADS = Path.home() / "johan" / "brain" / "uploads"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
MAX_SIDE = 1024          # what the model sees; the saved copy stays full size
KEEP_TURNS = 2           # user turns whose pictures are still sent as pixels
NOTE_START = "\n\n[Attached picture"
DEFAULT_QUESTION = "what's in this picture?"


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def size_of(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def is_image(path):
    p = Path(path)
    return p.suffix.lower() in IMAGE_EXT and p.is_file() and size_of(p) is not None


# ---------------------------------------------------------------- sources

def clipboard_image(run=subprocess.run):
    """Save the X11 clipboard's picture to uploads; the path, or None when
    the clipboard holds no picture (then it's an ordinary text paste).

    The terminal can't paste a picture into a TUI — WezTerm only forwards
    text — so the app reads the clipboard itself with xclip."""
    try:
        targets = run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                      capture_output=True, timeout=3).stdout.decode(errors="replace").split()
    except Exception:
        return None
    kind = next((t for t in ("image/png", "image/jpeg", "image/webp", "image/bmp")
                 if t in targets), None)
    # Some copiers name their format differently (image/x-png…); any image/*
    # will do — PIL checks the bytes below.
    kind = kind or next((t for t in targets if t.startswith("image/")), None)
    if kind is None:
        # A copied picture FILE (file managers, some screenshot tools) comes
        # as a list of file:// links, not pixels.
        for t in ("x-special/gnome-copied-files", "text/uri-list"):
            if t in targets:
                try:
                    text = run(["xclip", "-selection", "clipboard", "-t", t, "-o"],
                               capture_output=True, timeout=3).stdout.decode(errors="replace")
                except Exception:
                    continue
                for p in dropped_paths(text.replace("copy\n", "").replace("cut\n", "")) or []:
                    if is_image(p):
                        return Path(p)
        return None
    try:
        data = run(["xclip", "-selection", "clipboard", "-t", kind, "-o"],
                   capture_output=True, timeout=5).stdout
    except Exception:
        return None
    if not data:
        return None
    UPLOADS.mkdir(parents=True, exist_ok=True)
    out = UPLOADS / f"clip-{_stamp()}.{kind.split('/')[1].replace('jpeg', 'jpg')}"
    out.write_bytes(data)
    if size_of(out) is None:            # not really a picture: don't keep it
        out.unlink(missing_ok=True)
        return None
    return out


def clipboard_text(run=subprocess.run):
    try:
        r = run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, timeout=3)
        return r.stdout.decode(errors="replace") if r.returncode == 0 else None
    except Exception:
        return None


def pick_file(run=subprocess.run):
    """A zenity file picker filtered to pictures; the chosen path or None."""
    try:
        r = run(["zenity", "--file-selection", "--title=Attach a picture",
                 "--file-filter=Pictures | " + " ".join(f"*{e} *{e.upper()}" for e in IMAGE_EXT)],
                capture_output=True, timeout=600)
    except Exception:
        return None
    path = r.stdout.decode(errors="replace").strip()
    return path if r.returncode == 0 and path else None


def dropped_paths(text):
    """The picture paths in a paste that is NOTHING BUT picture paths (what
    WezTerm pastes when files are dragged in: quoted, backslash-escaped or
    file:// URIs, space- or newline-separated). [] for anything else, so an
    ordinary message that mentions a path is never swallowed."""
    s = (text or "").strip()
    if not s or len(s) > 4000:
        return []
    tokens, i = [], 0
    while i < len(s):
        if s[i].isspace():
            i += 1
            continue
        if s[i] in "'\"":
            end = s.find(s[i], i + 1)
            if end < 0:
                return []
            tokens.append(s[i + 1:end])
            i = end + 1
            continue
        buf = []
        while i < len(s) and not s[i].isspace():
            if s[i] == "\\" and i + 1 < len(s):
                i += 1
            buf.append(s[i])
            i += 1
        tokens.append("".join(buf))
    out = []
    for t in tokens:
        if t.startswith("file://"):
            t = unquote(urlparse(t).path)
        p = Path(t).expanduser()
        if not p.is_absolute() or not is_image(p):
            return []
        out.append(str(p))
    return out


def display_name(path):
    """The name he knows: without keep()'s `att-<stamp>-` prefix."""
    return re.sub(r"^att-\d{8}-\d{6}-\d{3}-", "", Path(path).name)


def keep(path):
    """Copy a picture into uploads (unless it's already there); the copy's path."""
    src = Path(path).expanduser()
    try:
        if src.resolve().parent == UPLOADS.resolve():
            return src
    except OSError:
        pass
    UPLOADS.mkdir(parents=True, exist_ok=True)
    out = UPLOADS / f"att-{_stamp()}-{re.sub(r'[^A-Za-z0-9._-]', '_', src.name)[-60:]}"
    shutil.copy2(src, out)
    return out


# ---------------------------------------------------------------- messages

def note(paths):
    """The line the model reads with the pixels: where the picture is saved,
    so "redraw this as anime" can pass it on as generate_image(reference=…)."""
    bits = []
    for p in paths:
        wh = size_of(p)
        bits.append(f"{p}" + (f" ({wh[0]}×{wh[1]})" if wh else ""))
    word = "picture" if len(paths) == 1 else "pictures"
    return (f"{NOTE_START if len(paths) == 1 else NOTE_START + 's'}: {', '.join(bits)}. "
            f"You can see {'it' if len(paths) == 1 else 'them'}. To redraw or change the "
            f"{word}, call generate_image with reference=<that path>.]")


def user_message(text, paths):
    """The user message to store: his words + the path note, with the paths."""
    paths = [str(p) for p in paths]
    text = (text or "").strip() or DEFAULT_QUESTION
    if not paths:
        return {"role": "user", "content": text}
    return {"role": "user", "content": text + note(paths), "image_paths": paths}


def shown_text(content):
    """His words without the path note (for the chat bubble, rewind, retry)."""
    c = str(content or "")
    i = c.find(NOTE_START)
    return c[:i] if i >= 0 else c


_CACHE = {}


def encode(path, max_side=MAX_SIDE):
    """Base64 for Ollama, downscaled; cached by (path, mtime) so a 9-hop turn
    doesn't re-encode the same picture nine times. None if it's gone."""
    p = Path(path)
    try:
        key = (str(p), p.stat().st_mtime_ns, max_side)
    except OSError:
        return None
    if key in _CACHE:
        return _CACHE[key]
    try:
        from PIL import Image
        with Image.open(p) as im:
            im.load()
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
    except Exception:
        return None
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = base64.b64encode(buf.getvalue()).decode()
    return _CACHE[key]


def ollama_messages(messages, keep_turns=KEEP_TURNS):
    """What Ollama gets: `image_paths` become base64 `images` on the last
    `keep_turns` user messages that carry pictures counted over ALL user
    turns, and are dropped from older ones (their content still names the
    path). Other keys Ollama doesn't know are stripped. The stored history
    is never changed."""
    if not any(m.get("image_paths") for m in messages):
        return messages
    user_idx = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    recent = set(user_idx[-keep_turns:])
    out = []
    for i, m in enumerate(messages):
        if not m.get("image_paths"):
            out.append(m)
            continue
        m2 = {k: v for k, v in m.items() if k != "image_paths"}
        if i in recent:
            imgs = [b for b in (encode(p) for p in m["image_paths"]) if b]
            if imgs:
                m2["images"] = imgs
            else:
                m2["content"] = m2.get("content", "") + " [the picture file is gone]"
        out.append(m2)
    return out
