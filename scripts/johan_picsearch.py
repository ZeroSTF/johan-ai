"""find_images — real pictures from the web, checked by looking at them.

"show me pictures of X" → SearXNG image search → rank → fetch each result's
THUMBNAIL in parallel → drop broken/tiny/duplicate ones → the chat model
LOOKS at each (calibrated yes/no, johan_decide with an image) until `count`
pass → saved under brain/uploads/web/ with a sidecar naming the source page
→ the full-size originals of the kept ones download in the background and
replace the thumbnails on disk when they arrive.

Why thumbnails first, measured 2026-10-14: on this ~1.5 MB/s line, 8
originals of 2-10 MB in parallel took 10-15 s and a third timed out; the
search engines' own thumbnails (7-78 KB, ~474 px — enough to look at and to
show) all arrived, mostly in ~1 s. The answer doesn't wait for originals.

What the look-check can and can't do, measured the same day on 4 queries
plus 28 deliberately wrong pairs: it tells WHAT a picture shows (a GPU vs a
PC case, a red panda vs the Eiffel Tower — 24/28 mismatches rejected, every
real match kept), ~2 s per picture. It can NOT tell WHICH anime character it
is: four Johan Liebert pictures all passed as "Light Yagami". So identity
comes from the result's own title/page text (ranking), and the look-check
throws out pictures of the wrong kind of thing.

Engines: `artic` (a museum's collection) and the icon sets `lucide` /
`devicons` answer every query with unrelated art or 900 icons, so they're
dropped. Wikimedia answers 429 to a browser-looking User-Agent and 200 to a
descriptive one (their published policy), so it gets that. Pictures land in
brain/uploads (never brain/images, which is only what Johan drew)."""
import base64
import concurrent.futures as cf
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path.home() / "johan"
OUT = ROOT / "brain/uploads/web"
SEARX_URL = "http://127.0.0.1:8888/search"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) johan-assistant/0.1"}
POLICY_UA = {"User-Agent": "johan-assistant/0.1 (personal self-hosted assistant; "
                           "https://github.com/ZeroSTF/johan-ai)"}
POLICY_HOSTS = ("wikimedia.org", "wikipedia.org")

DROP_ENGINES = {"artic", "lucide", "devicons"}
CANDIDATES = 12            # looked at per search, best-ranked first
MAX_BYTES = 15 * 1024 * 1024
MIN_SIDE = 150             # a thumbnail smaller than this is an icon
MAX_SIDE = 2048            # saved size cap
THUMB_TIMEOUT = 6          # one thumbnail
THUMB_BUDGET = 8           # the whole thumbnail step
FULL_TIMEOUT = 40          # one original, in the background
CHECK_SIDE = 336           # what the model looks at: 29/30 like 448, 1.5 s vs 2.0 (224: 27/30)
DUP_BITS = 6               # dhash distance that counts as the same picture
SAME_BITS = 14             # an "original" further than this isn't the thumbnail's picture
STOP = {"a", "an", "the", "of", "in", "on", "at", "and", "or", "for", "to",
        "with", "from", "picture", "pictures", "photo", "photos", "image",
        "images", "pic", "pics", "me", "some", "show", "find"}

MATCH_Q = "Does this picture show {q}?"
_FULL = {}                 # path → Event, set when its original is in (or gave up)


class Stopped(Exception):
    pass


def _check_stop(should_stop):
    if should_stop and should_stop():
        raise Stopped()


# ----------------------------------------------------------------- search

def _search(query, safe):
    r = requests.get(SEARX_URL, params={"q": query, "categories": "images",
                                         "format": "json", "safesearch": safe},
                     headers=UA, timeout=30)
    r.raise_for_status()
    d = r.json()
    return d.get("results", []), [e[0] for e in d.get("unresponsive_engines", [])]


def _abs(url):
    url = (url or "").strip()
    return "https:" + url if url.startswith("//") else url


def _norm(url):
    """Same picture behind different query strings / schemes."""
    p = urlparse(_abs(url))
    return (p.netloc.lower().removeprefix("www.") + p.path).rstrip("/")


def _resolution(x):
    m = re.search(r"(\d+)\s*[x×]\s*(\d+)", str(x.get("resolution") or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 1 and w not in STOP}


def title_match(query, x):
    """Share of the query's words that appear as WHOLE words in the result's
    title / description / page URL. Whole words, not substrings: "ti" must
    not match inside "tiles" (this codebase's recurring bug class)."""
    want = _tokens(query)
    if not want:
        return 0.0
    have = _tokens(" ".join(str(x.get(k) or "") for k in ("title", "content", "url")))
    return len(want & have) / len(want)


def rank(results, query):
    """Drop junk engines / vector and animated formats / duplicate links /
    tiny originals, then best first: words of the query in the title, how
    many engines returned it, and resolution."""
    seen, out = set(), []
    for x in results:
        src = _abs(x.get("img_src"))
        if not src or x.get("engine") in DROP_ENGINES:
            continue
        fmt = (str(x.get("img_format") or "") + " " + urlparse(src).path).lower()
        if re.search(r"\bsvg\b|\.svg\b|\.gif\b|\bgif\b", fmt):
            continue
        key = _norm(src)
        if key in seen:
            continue
        seen.add(key)
        res = _resolution(x)
        if res and min(res) < 200:
            continue
        engines = x.get("engines") or [x.get("engine")]
        px = (res[0] * res[1]) if res else 400_000
        score = (3 * title_match(query, x) + 0.5 * (len(engines) - 1)
                 + 0.5 * min(1.0, px / 1_000_000))
        out.append((score, x))
    out.sort(key=lambda t: -t[0])
    return [x for _, x in out]


# --------------------------------------------------------------- download

def _ua(url):
    host = urlparse(url).netloc.lower()
    return POLICY_UA if any(host == h or host.endswith("." + h) for h in POLICY_HOSTS) else UA


def download(url, should_stop=None, timeout=THUMB_TIMEOUT, min_side=MIN_SIDE):
    """→ (RGB PIL image, None) or (None, reason). Size- and time-capped while
    streaming; the content type and the bytes both have to be a real still
    picture."""
    from PIL import Image
    url = _abs(url)
    try:
        end = time.monotonic() + timeout
        with requests.get(url, headers=_ua(url), timeout=timeout, stream=True) as r:
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith("image/"):
                return None, f"not a picture ({ctype})"
            if "svg" in ctype or "gif" in ctype:
                return None, f"skipped {ctype}"
            buf = bytearray()
            for chunk in r.iter_content(64 * 1024):
                if should_stop and should_stop():
                    return None, "stopped"
                if time.monotonic() > end:
                    return None, "too slow"
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return None, "over 15 MB"
        im = Image.open(io.BytesIO(bytes(buf)))
        if getattr(im, "is_animated", False):
            return None, "animated"
        im.load()
        if min(im.size) < min_side:
            return None, f"too small ({im.size[0]}×{im.size[1]})"
        if im.mode in ("RGBA", "LA", "P", "PA"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        return im.convert("RGB"), None
    except requests.RequestException as e:
        return None, f"download failed ({type(e).__name__})"
    except Exception as e:     # noqa: BLE001 — PIL raises many kinds
        return None, f"not a readable picture ({type(e).__name__})"


def dhash(im, size=8):
    g = im.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            i = row * (size + 1) + col
            bits = (bits << 1) | (px[i] > px[i + 1])
    return bits


def _bits(a, b):
    return bin(a ^ b).count("1")


def _b64(im, side=None):
    small = im.copy()
    small.thumbnail((side or CHECK_SIDE,) * 2)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def look(im, query):
    """(p_yes, confident): does this picture show `query`? The model looks."""
    import johan_decide as jd
    p, d = jd.check(MATCH_Q.format(q=query), "A picture is attached.",
                    yes=f"yes, it shows {query}", no="no, it shows something else",
                    kind="image_match", null_context="A picture is attached.",
                    images=[_b64(im)])
    return p, d.confident


# --------------------------------------------------------------- saving

def _write(path, im):
    """Atomic: a reader (the card, Telegram, /draw) never sees half a file."""
    im = im.copy()
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    tmp = path.with_name(path.name + ".part")
    im.save(tmp, "JPEG", quality=90)
    os.replace(tmp, path)
    return im.size


def _save(im, x, query, n, stamp):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{stamp}-{n}.jpg"
    w, h = _write(path, im)
    page = _abs(x.get("url"))
    meta = {"query": query, "image_url": _abs(x.get("img_src")), "source": page,
            "thumbnail_url": _abs(x.get("thumbnail_src")) or None,
            "title": (x.get("title") or "")[:200], "engine": x.get("engine"),
            "width": w, "height": h, "full": False, "saved": stamp}
    path.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    # Title and site FIRST: the answer check reads only the first ~1,400
    # chars of tool output, and with paths first it couldn't see the 3rd and
    # 4th titles — it called a word-for-word answer unbacked and rewrote it.
    return {"title": meta["title"][:80],
            "site": urlparse(page).netloc.removeprefix("www.") or "?",
            "path": str(path), "source": page[:100]}


def _fetch_full(path, src, thumb_hash, thumb_size):
    """Background: swap the thumbnail on disk for the original — only if it
    really is the same picture and bigger. Whatever happens, set the event."""
    try:
        im, _ = download(src, timeout=FULL_TIMEOUT, min_side=1)
        if im is not None and im.size[0] * im.size[1] > thumb_size[0] * thumb_size[1] \
                and _bits(dhash(im), thumb_hash) <= SAME_BITS:
            w, h = _write(path, im)
            side = path.with_suffix(".json")
            meta = json.loads(side.read_text())
            meta.update(width=w, height=h, full=True)
            side.write_text(json.dumps(meta, indent=1))
    except Exception:          # noqa: BLE001 — the thumbnail stays; that's fine
        pass
    finally:
        _FULL[str(path)].set()


def _start_full(path, x, im):
    src = _abs(x.get("img_src"))
    _FULL[str(path)] = ev = threading.Event()
    if not src or not x.get("thumbnail_src") or src == _abs(x.get("thumbnail_src")):
        ev.set()                      # what we have IS the original
        return
    threading.Thread(target=_fetch_full, args=(Path(path), src, dhash(im), im.size),
                     daemon=True, name="johan-fullpic").start()


def wait_full(paths, timeout=15):
    """Block until the originals of `paths` are in (or gave up), up to
    `timeout` s in total. For senders that want full size (Telegram)."""
    end = time.monotonic() + timeout
    for p in paths:
        ev = _FULL.get(str(p))
        if ev is not None:
            ev.wait(max(0.0, end - time.monotonic()))


# ------------------------------------------------------------------- tool

def _safe():
    """SearXNG safesearch: moderate normally, off in /uncensored mode."""
    try:
        import johan_assistant as ja
        return 0 if ja.is_uncensored() else 1
    except Exception:
        return 1


def find_images(query, count=4, should_stop=None):
    query = (query or "").strip()
    if not query:
        return {"error": "say what to find pictures of"}
    try:
        count = max(1, min(int(count or 4), 8))
    except (TypeError, ValueError):
        count = 4
    t0 = time.monotonic()
    try:
        raw, dead = _search(query, _safe())
    except requests.RequestException as e:
        return {"error": f"searxng unreachable ({type(e).__name__}) — is the "
                         f"johan-searxng container up?"}
    except ValueError:
        return {"error": "searxng returned non-JSON"}
    dead = [e for e in dead if e not in DROP_ENGINES]
    ranked = rank(raw, query)
    base = {"query": query}
    if dead:
        base["engines_unavailable"] = dead
    if not ranked:
        return {**base, "error": (
            "IMAGE SEARCH RETURNED NOTHING USABLE — the search backend is probably "
            "degraded, not that no pictures exist. "
            + (f"These engines failed: {', '.join(dead)}. " if dead else "")
            + "Tell Riadh the search failed and offer to retry.")}

    dropped = {"download": 0, "duplicate": 0, "not_a_match": 0}
    kept, hashes, maybes, check_errors = [], [], [], []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pool = ranked[:CANDIDATES]

    def keep(im, x, unsure=False):
        pic = _save(im, x, query, len(kept) + 1, stamp)
        _start_full(pic["path"], x, im)
        kept.append({**pic, "unsure": True} if unsure else pic)

    # Thumbnails download in the background while the model looks at the
    # ones already here, best-ranked first; the step gets THUMB_BUDGET.
    ex = cf.ThreadPoolExecutor(8)
    try:
        futs = [ex.submit(download, x.get("thumbnail_src") or x["img_src"], should_stop)
                for x in pool]
        deadline = time.monotonic() + THUMB_BUDGET
        for x, fut in zip(pool, futs):
            if len(kept) >= count:
                break
            _check_stop(should_stop)
            try:
                im, _why = fut.result(timeout=max(0.05, deadline - time.monotonic()))
            except cf.TimeoutError:
                im = None
            if im is None:
                dropped["download"] += 1
                continue
            h = dhash(im)
            if any(_bits(h, o) <= DUP_BITS for o in hashes):
                dropped["duplicate"] += 1
                continue
            hashes.append(h)
            _check_stop(should_stop)
            if check_errors:
                p, sure = 0.5, False          # the model failed once: don't retry per picture
            else:
                try:
                    p, sure = look(im, query)
                except Stopped:
                    raise
                except Exception as e:        # noqa: BLE001 — e.g. the GPU is busy drawing
                    check_errors.append(f"{type(e).__name__}: {e}"[:160])
                    p, sure = 0.5, False      # couldn't look: don't drop it blindly
            if sure and p >= 0.5:
                keep(im, x)
            elif sure:
                dropped["not_a_match"] += 1   # a confident no is the wrong KIND of thing
            elif p >= 0.5 or title_match(query, x) >= 0.99:
                maybes.append((p, im, x))     # unsure: only if nothing better
            else:
                dropped["not_a_match"] += 1
        for p, im, x in sorted(maybes, key=lambda t: -t[0]):
            if len(kept) >= count:
                break
            keep(im, x, unsure=True)
        dropped["unsure"] = len(maybes) - sum(1 for k in kept if k.get("unsure"))
    except Stopped:
        return {**base, "error": "stopped — Riadh pressed esc", "pictures": kept}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    out = {"count": len(kept), "pictures": kept, "query": query,
           "dropped": {k: v for k, v in dropped.items() if v},
           "seconds": round(time.monotonic() - t0, 1)}
    if not kept and dead:
        out["engines_unavailable"] = dead     # only worth the space when it explains nothing
    if check_errors:
        out["look_check_failed"] = check_errors[0]
        out["unchecked"] = "couldn't look at these pictures, so they're picked by title only — say so"
    if not kept:
        out["error"] = (f"Found {len(ranked)} pictures but none could be downloaded "
                        f"or none showed {query}. Say so; offer different words.")
    else:
        # "List title + site, nothing more": the model dressed the list up
        # ("lit up at night", "spring view") and the answer check then
        # rewrote it — ~10 s spent correcting decoration.
        out["note"] = ("They are already shown to him. Answer with one short line per "
                       "picture: its title and source site — add nothing the titles don't "
                       "say. The check confirms what KIND of thing each shows, not which "
                       "person or character; say so if identity matters.")
    return out


TOOL = {"type": "function", "function": {
    "name": "find_images",
    "description": ("Find real pictures on the web (pictures/photos/images of X, "
                    "\"show me X\") and show them to him. NOT for drawing — that's "
                    "generate_image."),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string"},
        "count": {"type": "integer", "description": "default 4"},
    }, "required": ["query"]}}}
