"""johan_reader — read long tool output by relevance, not by position.

fetch_page used to hand the model the first 1,800 characters of a page, and
the engine cut every tool result at 2,400. Answers that sit deep in a page
(PCGamingWiki's Input table, a forum reply on page two, a changelog entry)
never reached the model, so it searched again, fetched again, and gave up.

This module ranks a long text's chunks against what he actually asked and
returns the best ones, in page order, with a note of how much was skipped.

No model call. A summarising LLM pass over a long page would cost ~35 s at
this card's ~230 tok/s prefill. Ranking is on the CPU instead:
  1. LEXICAL  BM25-style score of every chunk against the question's words
              (instant, word-boundary terms — never substring matches)
  2. EMBED    bge-m3 on the embed server for a bounded set of candidates:
              the lexical top plus an even sample of the rest, so a chunk
              that says "rebind" can still win a question about "remapping".
              Measured on this box: bge-m3 embeds ~1,250 chars/s, so the
              candidate set is capped by characters (EMBED_CHARS), not count.
  3. PICK     best chunks until READER_CHARS, shown in page order.
If the embed server is down the lexical ranking alone is used.

The question reaches the tool through a context variable that run_turn sets
before dispatch: the tool schemas stay unchanged (no extra tokens on every
chat) and concurrent turns (worker threads) cannot see each other's.
"""

import contextvars
import math
import re
import time

import requests

EMBED_URL = "http://127.0.0.1:8100/embed"
EMBED_BATCH = 64            # embed_server.py MAX_BATCH; more is a 413
CHUNK_CHARS = 350
READER_CHARS = 3000         # what reaches the model (~750 tokens, ~3.5 s prefill)
EMBED_CHARS = 4000          # at most this much text embedded per read …
EMBED_SECONDS = 2.0         # … and never for longer than this
HEAD_CHARS = 160            # the page's opening line, for orientation

_QUESTION = contextvars.ContextVar("johan_question", default="")

STOP = set("""a an and are as at be but by can could do does did for from get got
has have how i if in into is it its just me my of on or our please so tell that
the their them then there these they this to us was we were what when where which
who why will with would you your yours about any some all also there here been
very much many more most should shall may might cant can't dont don't im i'm whats
what's hey yo bro okay ok thanks thank""".split())


# --------------------------------------------------------------------------
# the current question
# --------------------------------------------------------------------------

def set_question(text):
    _QUESTION.set((text or "").strip())


def question():
    return _QUESTION.get()


def question_from(messages):
    """The last thing he asked. A short follow-up ("and on linux?") keeps
    the previous question in front of it, or it ranks on two words."""
    users = [m.get("content") or "" for m in messages
             if m.get("role") == "user"
             and not str(m.get("content", "")).startswith("[Johan, internal]")]
    if not users:
        return ""
    q = users[-1]
    if len(_terms(q)) < 3 and len(users) > 1:
        q = f"{users[-2]} {q}"
    return q[:600]


# --------------------------------------------------------------------------
# chunking and scoring
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.\-]*[a-z0-9+#]|[a-z0-9]", re.I)


def _stem(w):
    for suf in ("ings", "ing", "ers", "er", "ies", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _terms(text):
    return [_stem(w.lower()) for w in _WORD.findall(text or "")
            if w.lower() not in STOP and len(w) >= 2]


def chunks(text, size=CHUNK_CHARS):
    """Non-overlapping windows of whole lines, ~`size` chars each. Short lines
    are kept (wiki tables are nothing but short lines — see johan_research).
    A single very long line is split at sentence or word boundaries."""
    pieces = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        while len(line) > size:
            cut = max(line.rfind(". ", 0, size), line.rfind(" ", 0, size))
            cut = cut + 1 if cut > size // 3 else size
            pieces.append(line[:cut].strip())
            line = line[cut:].strip()
        if line:
            pieces.append(line)
    # Inline HTML (links, table cells) comes out of the page as one word per
    # line; a short line is glued to the next with a space so a chunk reads
    # as text, and costs fewer tokens than a column of single words.
    out, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > size:
            out.append(cur)
            cur = ""
        if not cur:
            cur = p
        else:
            last = cur.rsplit("\n", 1)[-1]
            cur += (" " if len(last) < 40 or len(p) < 3 else "\n") + p
    if cur:
        out.append(cur)
    return out


def lexical(chs, q):
    """BM25-style score per chunk, normalised to 0..1."""
    qt = set(_terms(q))
    if not qt or not chs:
        return [0.0] * len(chs)
    docs = [_terms(c) for c in chs]
    n = len(docs)
    avg = sum(len(d) for d in docs) / n or 1
    df = {t: sum(1 for d in docs if t in d) for t in qt}
    scores = []
    for d in docs:
        s = 0.0
        for t in qt:
            tf = d.count(t)
            if not tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scores.append(s)
    top = max(scores)
    return [s / top if top else 0.0 for s in scores]


def embed(texts):
    """bge-m3 vectors (normalised), batched to the server's limit."""
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        r = requests.post(EMBED_URL, json={"texts": texts[i:i + EMBED_BATCH]}, timeout=60)
        r.raise_for_status()
        out += r.json()["embeddings"]
    return out


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _priority(chs, lex):
    """Order in which chunks are worth embedding: the lexical hits, with an
    even spread of the rest interleaved (two hits, then one spread chunk) —
    the spread is what lets a chunk that says "rebind" compete for a question
    about "remapping"."""
    hits = sorted((i for i in range(len(chs)) if lex[i] > 0), key=lambda i: (-lex[i], i))
    rest = [i for i in range(len(chs)) if lex[i] <= 0]
    k = max(1, len(hits) // 2) if hits else 12
    step = max(1.0, len(rest) / k)
    spread = [rest[int(j * step)] for j in range(k) if int(j * step) < len(rest)]
    order, h, sp = [], iter(hits), iter(spread)
    while True:
        got = [x for x in (next(h, None), next(h, None), next(sp, None)) if x is not None]
        if not got:
            return order
        order += got


def rank(text, q, embed_chars=EMBED_CHARS, seconds=EMBED_SECONDS):
    """[(score, index)] best first, the chunk list, and how it was ranked.

    Embedding is bounded twice: by characters and by wall-clock, because the
    embed server shares cores 8-15 with everything else in the background —
    measured 1,250 chars/s idle and half that under load. Whatever was not
    embedded in time keeps its lexical score."""
    chs = chunks(text)
    lex = lexical(chs, q)
    method = "lexical"
    score = {i: lex[i] * 0.5 for i in range(len(chs))}
    started = time.monotonic()
    try:
        qv = embed([q])[0]
        todo, used = [], 0
        for i in _priority(chs, lex):
            if used + len(chs[i]) > embed_chars:
                break
            todo.append(i)
            used += len(chs[i])
        for b in range(0, len(todo), 4):
            if time.monotonic() - started > seconds:
                break
            batch = todo[b:b + 4]
            for i, v in zip(batch, embed([chs[i] for i in batch])):
                # Cosine decides; lexical overlap breaks near-ties. Chunks
                # that were never embedded keep a lower, lexical-only score.
                score[i] = 1.0 + _cos(qv, v) + 0.25 * lex[i]
            method = "bge-m3"
    except Exception:
        pass                      # embed server down: lexical still works
    ranked = sorted(((s, i) for i, s in score.items()), key=lambda x: (-x[0], x[1]))
    return ranked, chs, method


def select(text, q, budget=READER_CHARS, embed_chars=EMBED_CHARS, seconds=EMBED_SECONDS):
    """The parts of `text` most relevant to `q`, in original order.

    Returns {"text", "shown_chars", "total_chars", "skipped_chars", "method",
    "ms"}. Text that already fits is returned whole."""
    started = time.monotonic()
    text = text or ""
    if len(text) <= budget or not (q or "").strip():
        return {"text": text[:budget], "shown_chars": min(len(text), budget),
                "total_chars": len(text), "skipped_chars": max(0, len(text) - budget),
                "method": "head", "ms": 0}
    head = text.strip().splitlines()[0][:HEAD_CHARS] if text.strip() else ""
    ranked, chs, method = rank(text, q, embed_chars, seconds)
    keep, used = set(), len(head)
    for _, i in ranked:
        if used + len(chs[i]) + 6 > budget:
            continue
        keep.add(i)
        used += len(chs[i]) + 6
    parts, prev = [], None
    if head and not (0 in keep and chs[0].startswith(head)):
        parts.append(head)
        prev = -2
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            parts.append("[...]")
        parts.append(chs[i])
        prev = i
    if prev is not None and prev != len(chs) - 1:
        parts.append("[...]")
    out = "\n".join(parts)
    shown = sum(len(chs[i]) for i in keep)
    return {"text": out, "shown_chars": shown, "total_chars": len(text),
            "skipped_chars": max(0, len(text) - shown), "method": method,
            "ms": int((time.monotonic() - started) * 1000)}


def read(text, budget=READER_CHARS):
    """`select` against the current turn's question, plus the note the model
    sees. None when there is no question (called outside a turn)."""
    q = question()
    if not q or len(text or "") <= budget:
        return None
    sel = select(text, q, budget=budget)
    sel["note"] = (f"long page: showing the {sel['shown_chars']:,} of "
                   f"{sel['total_chars']:,} chars most relevant to the question "
                   f"([...] marks skipped parts). If the answer isn't here, it "
                   f"isn't in the parts shown — say so or try another page.")
    return sel


def relevant_lines(text, budget=600, skip_first=0):
    """For paged command output: the lines beyond page 1 that best match
    the question, prefixed with their line numbers. Lexical ranking, then
    bge-m3 on a small candidate set (~0.5 s). '' when nothing matches."""
    q = question()
    lines = (text or "").splitlines()
    if not q or len(lines) <= skip_first:
        return ""
    tail = lines[skip_first:]
    groups = [(skip_first + k, "\n".join(tail[k:k + 3])) for k in range(0, len(tail), 3)]
    lex = lexical([g for _, g in groups], q)
    if not any(lex):
        return ""
    order = sorted(range(len(groups)), key=lambda i: (-lex[i], i))[:8]
    try:
        vecs = embed([q] + [groups[i][1][:300] for i in order])
        sc = {i: _cos(vecs[0], v) + 0.15 * lex[i] for i, v in zip(order, vecs[1:])}
        order.sort(key=lambda i: -sc[i])
    except Exception:
        pass
    out, used = [], 0
    for i in order:
        if lex[i] <= 0:
            continue
        start, g = groups[i]
        block = "\n".join(f"{start + 1 + k}: {l[:200]}" for k, l in enumerate(g.splitlines()))
        if used + len(block) > budget:
            break
        out.append(block)
        used += len(block)
    return "\n...\n".join(out)
