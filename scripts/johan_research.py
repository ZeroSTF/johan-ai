"""deep_research — a few minutes of searching and reading, then a cited report.

Code runs the loop; the model only writes search queries and the final
report. Every other judgement is cheap and bounded:
  1. QUERIES   the model writes 3 different searches
  2. SEARCH    SearXNG, results deduped by URL
  3. PICK      results ranked by bge-m3 similarity (CPU — runs alongside the
               GPU, ~0.3 s a batch), then the top few scored with the
               calibrated decision layer: "likely to contain the answer?"
  4. READ      whole pages (headless-browser fallback for JS shells, gated
               pages skipped), ~600-char chunks ranked by the ASPECT asked
               about — the question minus the page's own subject
  5. ENOUGH?   decide() whether the notes answer it; if not and time remains,
               the model writes follow-up searches from what is missing and
               steps 2-4 run once more
  6. REPORT    written ONLY from numbered notes, citing them as [n]
  7. VERIFY    every cited sentence is checked against the notes it cites
               (the calibrated "is it backed?" check) and dropped if not
  8. SOURCES   appended by CODE from the notes actually cited — the model
               never writes a URL, so a source cannot be invented

Time-boxed: `minutes` (default 3, max 5 — Zeros asked for a few minutes, not
ten). Every stage checks the deadline; running out of time produces a
report from what was read so far, never an empty hand.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests

import johan_decide as jd
import johan_tools as jt


class _LazyEngine:
    """johan_assistant, resolved on first use. The engine imports this module
    to register its tool, so importing the engine at load time here was
    circular whenever this module was imported first (tests, scripts)."""
    def __getattr__(self, attr):
        import sys
        mod = sys.modules.get("johan_assistant")
        if mod is None:
            import johan_assistant as mod  # noqa: F811
        return getattr(mod, attr)


ja = _LazyEngine()

REPORTS_DIR = Path.home() / "johan/brain/research"
CHUNK_CHARS = 600
# Whole pages, not fetch_page's 4,000-char chat cap. The first version used
# that cap and read 12 pages without finding Lisa's controls — PCGamingWiki
# has them, but deep in the page, past the intro the cap kept. Chunks are
# ranked on the CPU, so reading all of a page costs seconds, not VRAM.
PAGE_CHARS = 60000
MAX_PAGES = 6
MAX_NOTES = 10
NOTES_PER_PAGE = 3

QUERIES_PROMPT = (
    "Write {n} different web search queries that together would answer this "
    "question well. Vary the wording and angle. One query per line, no "
    "numbering, no quotes, nothing else.\n\nQuestion: {q}")
FOLLOWUP_PROMPT = (
    "These notes do not fully answer the question yet.\n\nQuestion: {q}\n\n"
    "Notes so far:\n{notes}\n\nWrite {n} new web search queries that would "
    "find what is still missing. One per line, nothing else.")
# The model writes ONLY a short direct answer; the evidence below it is
# quoted verbatim by code. Paraphrasing is where a 4B garbles detail: one
# report said "WASD is movement, Q-E-R-T-Y are actions", and "Q-E-R-T-Y" is
# on no page it read. With the source's exact words printed under the
# answer, a garbled summary is visible at a glance instead of trusted — and
# a surprising-but-true one is too: "R = W, X = A, Y = S, Z = D" looked
# invented and was checked; it is the developers' own mapping, quoted in the
# Rock Paper Shotgun piece. Check the quote before calling a claim wrong.
REPORT_PROMPT = (
    "Answer the question in 1 to 3 sentences using ONLY the numbered notes "
    "below. Cite each fact with its note number like [2]. Use the notes' own "
    "words for any key, number or name — do not reinterpret them. If the "
    "notes do not answer part of the question, say so plainly. No sources "
    "list, no preamble.\n\nQuestion: {q}\n\nNotes:\n{notes}")
QUOTES = 4

RELEVANT_OPTS = {"yes": "the page is likely to contain information that "
                        "directly answers the question",
                 "no": "the page is unrelated, a list of links, or only "
                       "loosely connected to the question"}
ENOUGH_OPTS = {"yes": "the notes answer the question fully",
               "no": "important parts of the question are still unanswered"}
RELEVANT_Q = "Is this search result worth reading to answer the question?"
ENOUGH_Q = "Do these notes answer the question?"


EMBED_BATCH = 64      # embed_server.py MAX_BATCH; more in one request is a 413


def _embed(texts):
    # A long page chunks into more than 64 pieces; sending them in one request
    # got a 413 and failed the whole research run (found by the TUI e2e test).
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        r = requests.post(ja.EMBED_URL, json={"texts": texts[i:i + EMBED_BATCH]}, timeout=60)
        r.raise_for_status()
        out += r.json()["embeddings"]
    return out


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))      # bge-m3 vectors are normalised


def _lines(text, n):
    out = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line).strip().strip('"')
        if 3 <= len(line) <= 200 and line not in out:
            out.append(line)
    return out[:n]


def _generate(prompt, should_stop=None):
    msg = ja.call_model([{"role": "system", "content": ja.SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}],
                        no_tools=True, should_stop=should_stop)
    return (msg.get("content") or "").strip()


def _fetch_full(url):
    """Full visible text of a page, with the same gates and the same
    render-only browser fallback as fetch_page, but without its chat cap."""
    try:
        r = requests.get(url, headers=jt.UA, timeout=25)
        r.raise_for_status()
    except requests.RequestException:
        return None
    if "reddit.com" in url or "<title>Welcome to Reddit</title>" in r.text[:4000]:
        return None                 # gated for non-OAuth clients; never bypassed
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype:
        return r.text[:PAGE_CHARS] if "text" in ctype else None
    text = jt._visible_text(r.text)
    if any(g in (r.text[:20000] + text[:5000]).lower() for g in _CONTENT_GATES):
        return None
    try:
        import johan_browser as jb
        if jb.available() and jb.looks_like_js_shell(text):
            rendered = jb.render(url, max_chars=PAGE_CHARS)
            if rendered.get("gated"):
                return None
            if len(rendered.get("text") or "") > len(text):
                text = rendered["text"]
    except Exception:
        pass
    return text[:PAGE_CHARS]


# Pages that show a gate instead of content. Getting past any of these needs
# a login or an age/consent click — never done, the page is skipped.
_CONTENT_GATES = ("this game contains content you have asked not to see",
                  "<title>welcome to reddit</title>", "verify you are human",
                  "please verify your age", "just a moment...")


def _aspect(q, title):
    """The question minus the proper nouns it shares with the page title.

    Inside one page the subject is already given, so what should rank its
    chunks is the ASPECT asked about. Ranked on the full question, a
    PCGamingWiki page's top chunk was the "Anonymous edits have been
    disabled" banner, because it repeats "Lisa: The Painful"; ranked on the
    aspect, the Input/Remapping table comes first. Only CAPITALISED words are
    stripped: a Steam thread titled "Looking for the keyboard controls" must
    not lose "keyboard controls" from the question."""
    tw = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", title or "")}
    kept = [w for w in re.findall(r"[A-Za-z0-9']+", q)
            if not (w[:1].isupper() and w.lower() in tw and len(w) > 2)]
    return " ".join(kept) or q


def _chunks(text):
    """~CHUNK_CHARS windows of consecutive lines, overlapping by one line.

    Short lines are KEPT. The first version dropped every line under 40
    chars as noise — and wiki tables are nothing but short lines ("Input",
    "Remapping", "Controller support"). A 6,785-char PCGamingWiki page became
    two chunks, the Input section vanished, and the top-ranked "note" was
    "Anonymous edits have been disabled"."""
    lines = [l.strip() for l in (text or "").splitlines() if len(l.strip()) >= 2]
    out, cur = [], []
    for line in lines:
        if cur and sum(len(x) + 1 for x in cur) + len(line) > CHUNK_CHARS:
            out.append(" · ".join(cur))
            cur = cur[-1:]                     # one line of overlap
        cur.append(line[:CHUNK_CHARS])
    if cur:
        out.append(" · ".join(cur))
    return out


class _Budget:
    """Time left — and nothing left once he presses esc. Every search, read
    and check loop already asks ok(), so routing the stop signal through here
    makes all of them stop within one step instead of at the next round."""

    def __init__(self, minutes, should_stop=None):
        self.end = time.monotonic() + minutes * 60
        self.should_stop = should_stop

    def stopped(self):
        return bool(self.should_stop and self.should_stop())

    def left(self):
        return self.end - time.monotonic()

    def ok(self, need=10):
        return not self.stopped() and self.left() > need


def _search_round(queries, q, q_vec, seen_urls, budget, progress):
    results = []
    for query in queries:
        if not budget.ok(20):
            break
        progress(f"searching: {query}")
        res = jt.web_search(query)
        for r in res.get("results", []) if isinstance(res, dict) else []:
            url = r.get("url")
            if url and url not in seen_urls and not any(x["url"] == url for x in results):
                results.append(r)
    if not results:
        return []
    # Rank by meaning on the CPU, then ask the model only about the best few.
    vecs = _embed([f"{r.get('title', '')}. {r.get('snippet', '')}"[:500] for r in results])
    ranked = sorted(zip(results, vecs), key=lambda rv: -_cos(q_vec, rv[1]))[:MAX_PAGES + 2]
    picked = []
    for r, _ in ranked:
        if len(picked) >= MAX_PAGES or not budget.ok(15):
            break
        try:
            p, _d = jd.check(RELEVANT_Q, f"Question: {q}\nResult: {r.get('title', '')} — "
                                         f"{r.get('snippet', '')[:300]}",
                             yes=RELEVANT_OPTS["yes"], no=RELEVANT_OPTS["no"],
                             kind="research_pick", null_context="Question: N/A\nResult: N/A")
        except Exception:
            p = 0.5
        if p >= 0.5:
            picked.append(r)
    return picked


def _read(pages, q_text, q_vec, seen_urls, budget, progress):
    notes = []
    for r in pages:
        if not budget.ok(12):
            break
        url = r["url"]
        seen_urls.add(url)
        progress(f"reading: {url}")
        text = _fetch_full(url)
        if not text:
            continue
        chunks = _chunks(text)
        if not chunks:
            continue
        a_vec = _embed([_aspect(q_text, r.get("title", ""))])[0]
        vecs = _embed(chunks)
        # Rank within the page by aspect; keep the best of aspect/question as
        # the score used to compare notes ACROSS pages.
        best = sorted(zip(chunks, vecs), key=lambda cv: -_cos(a_vec, cv[1]))[:NOTES_PER_PAGE]
        for c, v in best:
            notes.append({"url": url, "title": r.get("title", "")[:120], "text": c,
                          "score": max(_cos(a_vec, v), _cos(q_vec, v))})
    return notes


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*\[(\"'])")


def _check_sentences(report, notes, q):
    """Drop cited sentences their own notes do not back. Returns
    (report, removed_count). Uncited sentences are left alone — they are
    usually 'the notes do not say X', which cannot be checked by lookup."""
    kept_paras, removed = [], 0
    for para in report.split("\n"):
        if not para.strip():
            kept_paras.append(para)
            continue
        kept = []
        for sent in _SENTENCE.split(para):
            refs = sorted({int(n) for n in re.findall(r"\[(\d+)\]", sent)
                           if 1 <= int(n) <= len(notes)})
            if not refs:
                kept.append(sent)
                continue
            evidence = " ".join(notes[i - 1]["text"] for i in refs)[:1400]
            claim = re.sub(r"\s*\[\d+\]", "", sent).strip()
            try:
                _, d = jd.check(jd.SUPPORT_Q, f"Tool result: {evidence}\nAnswer: {claim}",
                                yes=jd.SUPPORT_OPTS["yes"], no=jd.SUPPORT_OPTS["no"],
                                kind="research_claim", null_context=jd.NULLS["supported"])
            except Exception:
                kept.append(sent)
                continue
            if d.choice == "no" and d.confident:
                removed += 1
                jd.log_rewrite(sent, "", d, f"research: {q} | {evidence[:600]}")
            else:
                kept.append(sent)
        kept_paras.append(" ".join(kept))
    return "\n".join(kept_paras).strip(), removed


def _quotes(notes, q):
    """The QUOTES single lines, across all notes, closest to the question's
    aspect — verbatim, with the note number they came from."""
    lines = []
    for i, n in enumerate(notes, 1):
        for piece in n["text"].split(" · "):
            piece = piece.strip()
            if 40 <= len(piece) <= 400:
                lines.append((i, piece))
    if not lines:
        return []
    try:
        a_vec = _embed([_aspect(q, "")])[0]
        vecs = _embed([l for _, l in lines])
    except Exception:
        return []
    ranked = sorted(zip(lines, vecs), key=lambda lv: -_cos(a_vec, lv[1]))
    out, seen = [], set()
    for (i, text), _ in ranked:
        if text not in seen:
            seen.add(text)
            out.append((i, text))
        if len(out) >= QUOTES:
            break
    return out


def _numbered(notes):
    return "\n\n".join(f"[{i}] ({n['title'] or n['url']})\n{n['text']}"
                       for i, n in enumerate(notes, 1))


# "research this properly: X" -> "X". The instruction is for Johan, not the
# search engine: left in, it polluted every query and the chunk ranking, and
# one run's report answered a different question than the one asked.
_INSTRUCTION = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:research|dig into|look into|"
    r"investigate|do a deep[- ]dive on|deep[- ]dive(?: into)?)"
    r"(?:\s+(?:this|that|it))?(?:\s+(?:properly|thoroughly|in depth))?\s*[:,-]?\s+", re.I)


def research_question(text):
    q = _INSTRUCTION.sub("", str(text or "")).strip()
    q = re.sub(r",?\s*(?:properly|thoroughly|in depth)[.!]?$", "", q, flags=re.I).strip()
    return q or str(text or "").strip()


# A long message (his whole situation + the ask) is not a search query: sent
# as one, the only engine still answering (DuckDuckGo) crashed and research
# returned nothing. Above this length the model boils it down first.
LONG_QUESTION = 160
SHORTEN_PROMPT = ("Rewrite this message as ONE short web research question, under 15 words, "
                  "keeping what he wants found out. Only the question, nothing else.\n\n"
                  "Message: {q}")


def short_question(q, should_stop=None):
    if len(q) <= LONG_QUESTION:
        return q
    line = next(iter(_lines(_generate(SHORTEN_PROMPT.format(q=q), should_stop), 1)), "")
    return line if 3 <= len(line) <= LONG_QUESTION else q[:LONG_QUESTION]


def deep_research(question, minutes=3, on_progress=None, should_stop=None):
    """Research `question` for a few minutes and return a cited report."""
    question = short_question(research_question(question), should_stop)
    started = time.monotonic()
    try:
        minutes = max(1, min(float(minutes or 3), 5))
    except (TypeError, ValueError):
        minutes = 3
    budget = _Budget(minutes, should_stop)
    progress = on_progress or (lambda msg: None)
    q = str(question or "").strip()
    if not q:
        return {"error": "deep_research needs a question"}

    q_vec = _embed([q])[0]
    seen_urls, notes, rounds = set(), [], 0

    # His own words are one of the queries: forum threads are titled the way
    # people ask, and the source with the answer (a Rock Paper Shotgun piece
    # naming the F3 remap menu) came up for the literal question but not for
    # the model's reworded ones in some runs.
    queries = [q] + [x for x in _lines(_generate(QUERIES_PROMPT.format(n=3, q=q),
                                                 should_stop), 3) if x.lower() != q.lower()]
    while rounds < 2 and budget.ok(30):
        if should_stop and should_stop():
            break
        rounds += 1
        pages = _search_round(queries, q, q_vec, seen_urls, budget, progress)
        notes += _read(pages, q, q_vec, seen_urls, budget, progress)
        notes = sorted(notes, key=lambda n: -n["score"])[:MAX_NOTES]
        if not notes or not budget.ok(45):
            break
        try:
            _, d = jd.check(ENOUGH_Q, f"Question: {q}\nNotes: {_numbered(notes)[:1400]}",
                            yes=ENOUGH_OPTS["yes"], no=ENOUGH_OPTS["no"],
                            kind="research_enough", null_context="Question: N/A\nNotes: N/A")
            if d.choice == "yes":
                break
        except Exception:
            break
        queries = _lines(_generate(FOLLOWUP_PROMPT.format(
            n=2, q=q, notes=_numbered(notes)[:2000]), should_stop), 2)
        if not queries:
            break

    if budget.stopped():
        return {"error": "stopped — you pressed esc", "seconds": round(time.monotonic() - started)}
    if not notes:
        return {"error": "research found nothing readable — search may be "
                         "degraded (upstream engines refusing) or the pages "
                         "were gated.",
                "seconds": round(time.monotonic() - started)}

    progress("writing the report")
    numbered = _numbered(notes)
    convo = [{"role": "system", "content": ja.SYSTEM_PROMPT},
             {"role": "user", "content": REPORT_PROMPT.format(q=q, notes=numbered)}]
    report = (ja.call_model(convo, no_tools=True, should_stop=should_stop)
              .get("content") or "").strip()

    # Verify SENTENCE BY SENTENCE against the notes each one cites.
    #
    # One whole-report check was too coarse: a report on Lisa's controls
    # turned "WASD was used to attack in one fight" into "WASD is movement,
    # Q-E-R-T-Y are actions" — "Q-E-R-T-Y" appears nowhere in the source —
    # and passed, because the check saw a long report against truncated
    # evidence. Per sentence, the context is one claim and the one or two
    # notes it cites: short (fast), and precise enough to catch a detail.
    if budget.stopped():
        return {"error": "stopped — you pressed esc", "seconds": round(time.monotonic() - started)}
    report, removed = _check_sentences(report, notes, q)
    if removed:
        report += (f"\n\n({removed} statement{'s' if removed > 1 else ''} removed: "
                   f"not backed by the cited source.)")

    # Verbatim evidence, chosen by code: the best-matching lines from the
    # notes, cited. The reader checks the answer against the sources' words.
    quotes = _quotes(notes, q)
    if quotes:
        report += "\n\n**From the sources**\n" + "\n".join(
            f"> {text} [{n}]" for n, text in quotes)

    # Sources come from code, only for notes the report actually cites.
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", report)
                    if 1 <= int(n) <= len(notes)})
    sources = [{"n": i, "title": notes[i - 1]["title"], "url": notes[i - 1]["url"]}
               for i in cited]
    if not cited:
        report += "\n\n(The report cited no notes — treat it with suspicion.)"
    src_md = "\n".join(f"[{s['n']}] {s['title']} — {s['url']}" for s in sources)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", q.lower())[:50].strip("-") or "research"
    path = REPORTS_DIR / f"{datetime.now():%Y-%m-%d-%H%M}-{slug}.md"
    path.write_text(f"# {q}\n\n{report}\n\n## Sources\n{src_md}\n")
    return {"report": report, "sources": sources, "path": str(path),
            "pages_read": len(seen_urls), "notes": len(notes), "rounds": rounds,
            "seconds": round(time.monotonic() - started)}


TOOL = {"type": "function", "function": {
    "name": "deep_research",
    "description": (
        "Minutes of searching and reading, then a cited report. ONLY when he "
        "explicitly asks to research / dig into something — never for "
        "ordinary questions or writing tasks."),
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string"},
    }, "required": ["question"]}}}
