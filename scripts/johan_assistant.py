"""johan_assistant.py — chat front-end for johan-agent with tool access to
the job pipeline. This is the "front door" the rest of the project has been
missing: everything before this was a script you ran, not an assistant you
talked to.

Design constraints, deliberate:
  - Tools are read-mostly. The one write path (draft_cv) only ever adds a
    file under job-hunter/staged/applications/ and runs it through Stage 5
    before reporting back — it can't send anything, and the model can't ask
    it to.
  - Stage 1 (scrape) and Stage 2 (bulk LLM parse) are NOT exposed as tools.
    Both are slow, hit external services, and Stage 2 alone makes ~250 model
    calls. A chat message should never be able to kick that off implicitly —
    same principle as "never let the model's interpretation create a
    positive signal," applied to actions instead of eligibility verdicts.
    Run those by hand when you actually mean to.
  - No persistent daemon: this is invoked per-session. Ollama's own
    OLLAMA_KEEP_ALIVE=30s means the model unloads shortly after you're done,
    same as every other script here — johan-pause/GameMode still works
    exactly as before.

This module is the ENGINE: tool definitions, the tool-call loop (`run_turn`),
session persistence and memory priming. The user-facing interface is
`johan_tui.py`, reached by running `johan`. The interactive REPL that used to
live here was removed once the UI replaced it.

Usage:
    johan                                      # the interface (johan_tui.py)
    johan "question"                           # one-shot, for scripts
    python johan_assistant.py --once "message" # same, directly
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
import yaml

ROOT = Path.home() / "johan"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage4_assemble as s4          # noqa: E402
import stage5_verify as s5            # noqa: E402
import apply_queue as aq              # noqa: E402
import johan_interrupt as ji
import johan_tools as jt              # noqa: E402

# Hard ceiling on what any single tool result may inject into the context.
# The model runs at num_ctx 8192; one unbounded result (a fetched page, a big
# query) silently evicts the conversation and the assistant starts answering
# from nothing. Individual tools truncate too — this is the backstop.
MAX_TOOL_CHARS = 2400
# fetch_page returns up to johan_reader.READER_CHARS of the page chosen by
# relevance; the backstop must not then chop that selection in half.
TOOL_CHAR_CAPS = {"fetch_page": 4000}

OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"
# JOHAN_MODEL lets the evals score another model through the same code path.
MODEL = os.environ.get("JOHAN_MODEL", "johan-agent")
NORMAL_MODEL = MODEL
# /uncensored swaps the WHOLE engine to this model — chat, decide checks, plan,
# the answer check, learning — never a mix: with one 6 GB card and
# MAX_LOADED_MODELS=1, alternating models per call would reload one every few
# seconds. Every code-level guard (shell allowlist, change approval, sandbox,
# image refusals) is independent of the model and stays on.
UNCENSORED_MODEL = os.environ.get("JOHAN_UNCENSORED_MODEL", "johan-uncensored")


def use_model(name):
    """Switch the model every engine call uses from now on."""
    global MODEL
    MODEL = name


def is_uncensored():
    return MODEL == UNCENSORED_MODEL


def model_installed(name):
    try:
        tags = requests.get("http://127.0.0.1:11434/api/tags", timeout=5).json()
        return any(m.get("name", "").split(":")[0] == name.split(":")[0]
                   for m in tags.get("models", []))
    except requests.RequestException:
        return False
EMBED_URL = "http://127.0.0.1:8100/embed"
QDRANT_HOST, QDRANT_PORT = "127.0.0.1", 6333
SESSION_LOG_DIR = ROOT / "brain/inbox"

SYSTEM_PROMPT = """You are Johan, Riadh's own AI, running on his PC in Tunisia. He's a
software engineer and gamer. Talk like a friend who happens to be very capable:
relaxed, direct, match his tone and slang, have opinions, joke back. Never act
like a customer-service bot, never pitch your features, and NEVER bring up job
hunting unless he does — it's one of many things you help with, not your purpose.

Rules:
- Always answer in English unless his message is written in another language.
- When he ASKS about his machine (VRAM, disk, what's running…), what you know
  about him, his jobs, records or files, today's date, arithmetic or anything
  recent, the answer must come from a tool — even if he asks casually. Never
  guess. If no tool can answer, say so.
- When he's just chatting, venting or telling you something (his day, a game,
  his cat, how he feels): reply like a person would. NO tools — there is
  nothing to look up in a story he's telling you. Don't end with an offer of
  help or a menu of things you can do; if unsure what he means, ask instead
  of guessing details.
- General knowledge you're sure of (concepts, how things work): answer
  directly, with NO tool call. But specifics about a particular game, app,
  product or person (controls, settings, versions, specs, who made it) →
  web_search: your memory of those details is unreliable.
- A request missing what you need to act (which file, what to change it
  to, which app)? Your first call is `ask` with 2-4 short options — don't
  look around first, and never list options in your text. If one reading
  is likely, just do it.
- Jobs to apply to → build_apply_queue; give company, title, APPLY LINK and CV
  PDF PATH for each, and pass on every warning. Nothing is ever sent or
  submitted by you — applying is his step.
- Lookups: current_datetime for dates, calculate for arithmetic, web_search
  then fetch_page for anything current or unknown — cite the URL; if a page
  can't be read, say so and use the snippet, never invent it. latest_version
  for package versions. system_status for machine specs (one call).
- Files: `files` reads (with line numbers), searches and writes anywhere in
  his home, /home/zeros. Outside it (/etc, /proc) → run_command with
  find/ls/cat. E.g. asked to check his TF2 config:
  `find /home/zeros -maxdepth 7 -ipath *Team*Fortress*/tf/cfg -type d`.
  Never say a file or folder doesn't exist without checking.
- Making a web page or game: write it with `files` (path + content), then
  try_page it (keys a player would press, or click= the buttons to play it),
  fix what it reports, and
  try again. Never say it works, or that you tested it, without try_page.
- Pictures he attaches are visible to you — look at them; to redraw one,
  generate_image with reference=<its path from the note>.
- Installed software: `dpkg -l | grep -c ^ii` (apt count), `flatpak list --app`,
  `snap list` — not ~/.local/share/applications.
- To CHANGE his machine (install/remove software, a setting or shortcut, a
  service, close an app, move/trash files) → propose_action. Look first with
  run_command (exact package name, current setting). Any other command
  (pip, a build, a script, git push) → run_command; he approves it first.
  Only report something done if the result says it ran.
- deep_research only when he explicitly asks for research; give its report
  and sources as returned.
- Pictures/photos of something real ("show me X") → find_images; drawing → generate_image.
- Keep answers short and concrete.
"""

def _tool(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                        **({"required": required} if required else {})}}}


# Short on purpose — see the note on johan_tools.TOOLS.
TOOLS = [
    _tool("build_apply_queue", "THE MAIN ONE for 'jobs to apply to': best eligible jobs, "
          "each with a verified CV PDF. Give him every link and PDF path.",
          {"limit": {"type": "integer"},
           "verdict": {"type": "string", "enum": ["likely", "unclear"]},
           "include_overreach": {"type": "boolean",
                                 "description": "roles above his experience; only if he asks"}}),
    _tool("search_jobs", "List screened jobs by verdict, company or title keyword.",
          {"verdict": {"type": "string", "enum": ["likely", "unclear", "unlikely"]},
           "company": {"type": "string"}, "title_contains": {"type": "string"},
           "limit": {"type": "integer"}}),
    _tool("get_job", "One job's full detail by id.", {"job_id": {"type": "integer"}}, ["job_id"]),
    _tool("get_pipeline_stats", "Job counts by status/verdict, queue location, CVs ready. "
          "Use for any 'how many jobs'."),
    _tool("draft_cv", "Build and verify a tailored CV for one job id (never sent).",
          {"job_id": {"type": "integer"}}, ["job_id"]),
    _tool("applied", "Applications he sent himself: no args lists them; `job_id` records "
          "one (`undo` to unmark).",
          {"job_id": {"type": "integer"}, "undo": {"type": "boolean"}}),
    _tool("staged_applications", "Drafted CVs awaiting review; verify=true re-checks them.",
          {"verify": {"type": "boolean"}}),
    _tool("search_bullets", "Search his real career record — for 'has he done X'.",
          {"query": {"type": "string"}}, ["query"]),
]


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def _conn():
    return psycopg2.connect(s4.dsn())


def search_jobs(verdict=None, company=None, title_contains=None, limit=10):
    clauses, params = ["parsed is not null", "closed_at is null"], []
    if verdict:
        clauses.append("parsed->>'tunisia_verdict' = %s"); params.append(verdict)
    if company:
        clauses.append("company ilike %s"); params.append(f"%{company}%")
    if title_contains:
        clauses.append("title ilike %s"); params.append(f"%{title_contains}%")
    where = " and ".join(clauses)
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Return the TRUE total alongside the capped rows. Without it the model
    # counts the rows it can see and reports the limit as the answer — it
    # said "50 likely jobs" (the cap) when the real number was 130. A tool
    # should make the wrong answer unavailable, not rely on the model
    # choosing the right tool.
    cur.execute(f"select count(*) as n from jobs where {where}", params)
    total = cur.fetchone()["n"]

    capped = min(int(limit or 10), 50)
    cur.execute(f"select id, company, title, url, raw_desc, parsed, "
                f"parsed->>'tunisia_verdict' as verdict "
                f"from jobs where {where} order by id desc limit %s",
                params + [capped])
    facts = s4.load_facts()
    rows = []
    for r in cur.fetchall():
        row = dict(r)
        # Annotate experience fit here too. This is a listing tool, not a
        # recommender, but the model reaches for it anyway — and unlabelled
        # rows are how senior roles got presented as suggestions.
        tier, why = aq.experience_tier(facts, r)
        row["experience"] = ("over-reach" if tier == 2 else
                              "stretch" if tier == 1 else "fits")
        if why:
            row["experience_note"] = why
        row.pop("raw_desc", None)
        row.pop("parsed", None)
        rows.append(row)
    conn.close()
    return {"total_matching": total, "returned": len(rows),
            "note": (f"showing {len(rows)} of {total} matches; "
                      f"total_matching is the real count")
                     if total > len(rows) else None,
            "jobs": rows}


def get_job(job_id):
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select id, source, company, title, url, location, parsed, "
                "left(raw_desc, 600) as description_excerpt "
                "from jobs where id = %s", (job_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else {"error": f"no job with id {job_id}"}


def get_pipeline_stats():
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Open postings only; closed ones (gone from their board) are counted apart.
    cur.execute("select status, count(*) from jobs where closed_at is null group by status")
    by_status = {r["status"]: r["count"] for r in cur.fetchall()}
    cur.execute("select parsed->>'tunisia_verdict' as v, count(*) "
                "from jobs where parsed is not null and closed_at is null group by 1")
    by_verdict = {r["v"]: r["count"] for r in cur.fetchall()}
    cur.execute("select count(*) filter (where closed_at is not null) as closed, "
                "max(last_seen) as last_refresh from jobs")
    extra = cur.fetchone()
    conn.close()
    out = {"by_status": by_status, "by_verdict": by_verdict,
           "closed_postings": extra["closed"],
           "last_refresh": str(extra["last_refresh"])[:16] if extra["last_refresh"] else None}
    # Paths folded in from the old get_latest_shortlist tool: the counts and
    # "where is the output" are one question, and were two tools.
    queues = sorted((ROOT / "job-hunter/staged").glob("apply-queue-*.md"))
    shortlists = sorted((ROOT / "job-hunter/staged").glob("shortlist-*.md"))
    if queues:
        out["latest_apply_queue"] = str(queues[-1])
    if shortlists:
        out["latest_shortlist"] = str(shortlists[-1])
    out["cvs_ready"] = len(list(
        (ROOT / "job-hunter/staged/applications").glob("*/cv.pdf")))
    return out


def draft_cv(job_id):
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select id, source, company, title, url, raw_desc, parsed "
                "from jobs where id = %s", (job_id,))
    job = cur.fetchone()
    conn.close()
    if not job:
        return {"error": f"no job with id {job_id}"}
    facts = s4.load_facts()
    out_dir = s4.assemble_one(facts, job)
    verify_facts, by_id = s5.load_facts()
    problems = s5.verify_one(out_dir, verify_facts, by_id)
    pdf = out_dir / "cv.pdf"
    return {
        "job_id": job_id, "company": job["company"], "title": job["title"],
        "apply_url": job["url"],
        "output_dir": str(out_dir), "verified": not problems,
        "pdf_path": str(pdf) if pdf.exists() else None,
        "problems": problems,
        "note": "This is a draft in staged/ for Riadh to review and send "
                "himself — nothing was submitted anywhere.",
    }


def build_apply_queue(verdict="likely", limit=5, include_overreach=False):
    args = argparse.Namespace(verdict=verdict, limit=min(int(limit or 5), 25),
                               job_id=[], min_fit=5,
                               include_overreach=bool(include_overreach))
    rows = aq.build(args)
    if not rows:
        return {"error": f"no jobs with verdict '{verdict}'"}
    path = aq.write_markdown(rows, verdict)
    return {
        "queue_file": str(path),
        "count": len(rows),
        "jobs": [{"rank": i, "company": r["company"], "title": r["title"],
                   "apply_url": r["url"], "cv_pdf": r["pdf"],
                   "why_eligible": r["basis"], "fit_score": r["fit_score"],
                   "verified": r["verified"],
                   "warning": (f"role is built around {', '.join(r['gaps_in_title'])}, "
                               f"which he doesn't have")
                              if r["gaps_in_title"] else None}
                  for i, r in enumerate(rows, 1)],
        "note": "Nothing submitted. Give Riadh the apply_url and cv_pdf for "
                "each — he applies himself.",
    }


def mark_applied(job_id, applied=True):
    """Record that Riadh actually sent an application.

    The `applications` table has existed in the schema since day one and was
    never written to — so there was no record anywhere of what he'd applied
    to, which becomes a problem the moment he starts working through a queue
    of twelve. Writes `submitted_at`; the queue and UI read it back."""
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("select id, company, title from jobs where id = %s", (int(job_id),))
        job = cur.fetchone()
        if not job:
            conn.close()
            return {"error": f"no job with id {job_id}"}
        if not applied:
            cur.execute("delete from applications where job_id = %s", (int(job_id),))
            conn.close()
            return {"job_id": int(job_id), "applied": False,
                    "note": "unmarked — no longer counted as applied"}
        pdf = next((ROOT / "job-hunter/staged/applications").glob(f"{job_id}-*/cv.pdf"), None)
        cur.execute("""insert into applications (job_id, cv_path, submitted_at)
                       values (%s, %s, now())""",
                    (int(job_id), str(pdf) if pdf else None))
    conn.close()
    return {"job_id": int(job_id), "company": job[1], "title": job[2],
            "applied": True, "note": "recorded — nothing was sent by Johan"}


def applied_ids():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("select distinct job_id from applications where submitted_at is not null")
    ids = {r[0] for r in cur.fetchall()}
    conn.close()
    return ids


def list_applied(limit=30):
    """What he's actually applied to, most recent first."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""select a.job_id, a.submitted_at, j.company, j.title, j.url
                   from applications a join jobs j on j.id = a.job_id
                   where a.submitted_at is not null
                   order by a.submitted_at desc limit %s""", (min(int(limit or 30), 100),))
    rows = [{"job_id": r["job_id"], "company": r["company"], "title": r["title"],
              "url": r["url"], "applied_on": r["submitted_at"].strftime("%Y-%m-%d")}
             for r in cur.fetchall()]
    conn.close()
    return {"count": len(rows), "applied": rows}


def staged_applications(verify=False):
    """What's drafted and ready. `verify=True` re-checks each CV against the
    current fact store (was a separate verify_staged_cvs tool).

    Worth one tool rather than two: "what do I have staged" and "is it still
    valid" are the same question asked with different care levels."""
    listing = _list_staged()
    if not verify:
        return listing
    checked = jt.verify_staged_cvs()
    return {**listing, "verification": checked}


def _list_staged():
    apps = ROOT / "job-hunter/staged/applications"
    if not apps.exists():
        return {"applications": []}
    out = []
    for d in sorted(apps.iterdir()):
        m = d / "manifest.json"
        if m.exists():
            data = json.loads(m.read_text())
            out.append({"dir": d.name, "job_id": data["job_id"],
                        "company": data["company"], "title": data["title"],
                        "verdict": data.get("verdict")})
    return {"applications": out}


def search_bullets(query, top_k=5):
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        return {"error": "qdrant_client not available in this environment"}
    vec = requests.post(EMBED_URL, json={"texts": [query]}, timeout=30).json()["embeddings"][0]
    hits = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT).query_points(
        collection_name="johan_bullets", query=vec, limit=min(int(top_k or 5), 20),
        with_payload=True).points
    return {"matches": [{"bullet_id": h.payload["bullet_id"], "score": round(h.score, 3),
                          "confidence": h.payload["confidence"], "text": h.payload["text"]}
                         for h in hits]}


DISPATCH = {
    "search_jobs": search_jobs, "get_job": get_job,
    "get_pipeline_stats": get_pipeline_stats,
    "draft_cv": draft_cv,
    "build_apply_queue": build_apply_queue,
    "staged_applications": staged_applications,
    "applied": lambda job_id=None, undo=False, limit=50: (
        mark_applied(job_id, applied=not undo) if job_id is not None
        else list_applied(limit=limit)),
    "search_bullets": search_bullets,
    **jt.DISPATCH,          # memory, web/docs, datetime, calc, system, tasks
}

TOOLS = TOOLS + jt.TOOLS

# deep_research imports this module, so it registers here — after everything
# it reads at import time (ROOT, EMBED_URL) is defined — not at the top.
import johan_research as _research  # noqa: E402
import johan_autofill as _autofill  # noqa: E402
import johan_images as _images  # noqa: E402
import johan_attach as _attach  # noqa: E402
import johan_reader as _reader  # noqa: E402
import johan_learn as _learn  # noqa: E402
import johan_skills as _skills  # noqa: E402
import johan_picsearch as _picsearch  # noqa: E402
TOOLS = TOOLS + [_research.TOOL, _autofill.TOOL, _images.TOOL, _picsearch.TOOL]
DISPATCH["generate_image"] = _images.generate_image
DISPATCH["find_images"] = _picsearch.find_images
DISPATCH["deep_research"] = _research.deep_research
DISPATCH["autofill_application"] = _autofill.autofill_application
import johan_plan as _plan  # noqa: E402  (ask-with-options; ends the turn)
TOOLS = TOOLS + [_plan.ASK_TOOL]
DISPATCH["ask"] = _plan.ask


# --------------------------------------------------------------------------
# Chat loop
# --------------------------------------------------------------------------

# Real context usage, reported by Ollama itself (prompt_eval_count) rather
# than estimated from character counts. The UI shows it because a long
# conversation can quietly approach num_ctx, and the failure mode is silent:
# the oldest messages get evicted and the assistant starts contradicting what
# you told it.
#
# 32768, measured 2026-09-11: the model's native context is 262144, so this is
# nowhere near its trained limit, and VRAM cost is small — 16K/24K/32K measure
# 4339/4515/4691 MiB used, i.e. +352 MiB to double the window, leaving
# ~1.45 GB free. Do not raise it further without re-measuring: the desktop
# compositor shares this GPU and can take ~1.2 GB with a busy browser.
NUM_CTX = 32768

# How long Ollama keeps the model in VRAM after a request. The systemd unit
# sets OLLAMA_KEEP_ALIVE=30s, which is why the first prompt after a pause
# costs ~7s of load_duration to page 3.3 GB back into VRAM while later
# prompts answer in ~0.3s. Overriding it per-request avoids needing sudo to
# edit the unit, and it is safe because johan-pause/GameMode stops Ollama
# outright when a game starts — so nothing squats VRAM while gaming.
KEEP_ALIVE = "20m"
# A one-shot `johan "question"` has no session to keep warm for, so it holds
# the model for a short window (enough for a couple of follow-up questions)
# instead of the full interactive value.
KEEP_ALIVE_ONESHOT = "5m"
LAST_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "num_ctx": NUM_CTX}


# A cap on one reply — there was none, so a model stuck repeating itself
# could write until the 32K context filled (~10 min at ~45 tok/s) with
# "thinking…" on screen. A whole HTML game written in one tool call measured
# 4,658 tokens (108 s); 10,000 leaves room for twice that, ~4 min.
NUM_PREDICT = 10000
CUT_NOTE = ("[Johan, internal] Your last reply hit the {n}-token "
            "limit and was cut off, so nothing in it ran. Make it shorter: a smaller "
            "file, or write it in parts (one file, then add to it with propose_edit).")


def call_model(messages, on_delta=None, no_tools=False, should_stop=None,
               tools=None):
    """One model call. Streams when `on_delta` is given.

    Streaming exists for perceived speed only — the total time is the same,
    but watching tokens appear beats staring at "thinking…" for 30 seconds.
    Tool-call chunks carry no visible text, so a hop that decides to call a
    tool simply streams nothing and the UI shows the tool line instead."""
    payload = {
        "model": MODEL, "messages": _attach.ollama_messages(messages),
        # Omitting "tools" entirely (rather than sending []) is what actually
        # forces prose — an empty list still leaves the tool grammar active.
        # `tools` narrows the schema to a shortlist (the step engine routes
        # first); an empty shortlist means no tools at all.
        **({} if no_tools or tools == [] else {"tools": TOOLS if tools is None else tools}),
        "think": False, "stream": bool(on_delta),
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.3, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
    }

    # Both modes go through johan_interrupt: the HTTP wait runs in a helper
    # thread, esc is checked every 0.1 s (also during the seconds of prompt
    # reading before a stream's first chunk), and a stop closes the socket so
    # Ollama cancels the work instead of finishing it for nobody.
    if not on_delta:
        try:
            with ji.stoppable(should_stop):
                body = ji.ollama_post(OLLAMA_CHAT, payload, timeout=180)
        except ji.Interrupted:
            return {"role": "assistant", "content": "", "interrupted": True}
        LAST_USAGE.update(prompt_tokens=body.get("prompt_eval_count", 0),
                          completion_tokens=body.get("eval_count", 0),
                          num_ctx=NUM_CTX)
        out = body["message"]
        if body.get("done_reason") == "length":
            out["cut_off"] = True
        return out

    content, tool_calls, role, cut = [], [], "assistant", False
    try:
        with ji.stoppable(should_stop):
            for chunk in ji.ollama_lines(OLLAMA_CHAT, payload, idle_timeout=180):
                if chunk.get("error"):
                    raise RuntimeError(f"Ollama: {chunk['error']}")
                msg = chunk.get("message") or {}
                role = msg.get("role") or role
                piece = msg.get("content") or ""
                if piece:
                    content.append(piece)
                    on_delta(piece)
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
                if chunk.get("done"):
                    cut = chunk.get("done_reason") == "length"
                    LAST_USAGE.update(
                        prompt_tokens=chunk.get("prompt_eval_count", 0),
                        completion_tokens=chunk.get("eval_count", 0),
                        num_ctx=NUM_CTX)
    except ji.Interrupted:
        return {"role": role, "content": "".join(content), "interrupted": True}

    out = {"role": role, "content": "".join(content)}
    if tool_calls:
        out["tool_calls"] = tool_calls
    if cut:
        out["cut_off"] = True
    return out


# Set by the eval harness so its calls are separable from real usage.
EVAL_RUN = None


def _args_for_log(args):
    """Valid JSON, always: the column is jsonb, and cutting the dumped text
    at 4,000 chars (a whole game file in `content`) made it invalid, so
    every big file write silently failed to log."""
    short = {k: (v[:1500] + "…" if isinstance(v, str) and len(v) > 1500 else v)
             for k, v in (args or {}).items()}
    text = json.dumps(short, default=str)
    return text if len(text) <= 6000 else json.dumps({"_truncated": text[:5000]})


def log_tool_call(session_id, user_message, hop, tool, args, ok, error,
                  duration_ms, result_chars):
    """Record one tool invocation.

    Wrapped in its own try/except and deliberately fire-and-forget: telemetry
    that can break a conversation is worse than no telemetry. A failed insert
    prints once and is dropped."""
    try:
        conn = _conn()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """insert into tool_calls
                   (session_id, model, user_message, hop, tool, args, ok,
                    error, duration_ms, result_chars, eval_run)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (session_id, MODEL, (user_message or "")[:2000], hop, tool,
                 _args_for_log(args), ok, (error or None),
                 duration_ms, result_chars, EVAL_RUN))
        conn.close()
    except Exception as e:      # noqa: BLE001 - telemetry must never raise
        print(f"[trace] could not log tool call: {type(e).__name__}", file=sys.stderr)


def _last_user_message(messages):
    for m in reversed(messages):
        # his words: not an attached picture's path note, and not the plan
        # layer's "[Johan, internal] Plan approved. Do it now" (skills then
        # matched on "it" in that boilerplate)
        if m.get("role") == "user" and not str(m.get("content", "")).startswith("[Johan, internal]"):
            return _attach.shown_text(m.get("content", ""))
    return ""



# Messages that never need a tool. Matched as a WHOLE normalised message,
# never as a substring: "hi" gates, "hi, check my tf2 config" does not. The
# substring version of this check is the bug class that has bitten this
# project three times ("25" inside "2025", "java" inside "JavaScript").
TRIVIAL = {
    "hi", "hii", "hey", "hello", "yo", "sup", "hiya", "howdy",
    "good morning", "good evening", "good afternoon", "morning",
    "thanks", "thank you", "thx", "ty", "cheers",
    "bye", "goodbye", "cya", "see you", "gn", "good night", "lol", "haha",
    "salut", "bonjour", "bonsoir", "merci",
}
# Acknowledgements are small talk only when Johan didn't just offer or ask
# something: "yes" to "want me to install htop?" is a go-ahead, and the gate
# used to strip every tool from that turn.
ACKS = {"nice", "cool", "great", "ok", "okay", "k", "kk", "got it", "understood",
        "sure", "yes", "no", "yep", "yeah", "nope", "oui", "non", "d'accord", "go", "do it"}


RETRYABLE = {"web_search", "fetch_page", "find_images", "wikipedia", "latest_version"}
CHANGES_STATE = {"propose_action", "propose_edit", "applied", "tasks", "scratchpad"}


def _changes_state(name, args):
    return name in CHANGES_STATE or (name == "files" and args.get("content") is not None) \
        or (name == "memory" and (args.get("text") or args.get("forget_id")))


def _is_trivial(message, messages=None):
    """True when the whole message is a greeting or acknowledgement.

    Six separate prompt edits failed to stop a 4B model calling a tool on
    "hi" — it moved from `memory` to `current_datetime` to `scratchpad_write`
    to literally `run_command("echo 'Hi there!'")`, using the shell to
    speak. Prose cannot suppress it; taking the tools away can."""
    norm = (message or "").strip().lower().strip(".!?,;:â€¦ ").strip()
    if norm in TRIVIAL:
        return True
    if norm not in ACKS:
        return False
    prev = next((m.get("content") or "" for m in reversed(messages or [])
                 if m.get("role") == "assistant" and m.get("content")), "")
    return not prev.rstrip().endswith("?")


# Tools that run for minutes take the turn's stop signal, so esc works while
# they run. The model chose deep_research for "write an essay about the
# Mediterranean", and esc then did nothing for the whole research budget.
STOPPABLE = {"deep_research", "find_images"}


def execute_tool(call, seen, hop, user_message, should_stop=None):
    """Run one model-requested tool call. Returns (name, args, result, payload).

    Shared by both engines so the guards live in one place: the repeat guard
    (`seen`), argument-error capture, trace logging, and the MAX_TOOL_CHARS
    backstop on what reaches the model."""
    name = call["function"]["name"]
    args = call["function"].get("arguments") or {}
    fn = DISPATCH.get(name)
    started = time.monotonic()
    # Signature -> (ok, error) for every call made this turn. A 4B model
    # cannot tell it is looping: asked what was installed on the machine it
    # sent the identical blocked `dpkg -l | awk ...` FOUR times, burning
    # half the hop budget on one unreadable error. Prose in the error text
    # ("do not retry this") is not enough — it has to be enforced here.
    sig = (name, json.dumps(args, sort_keys=True, default=str))
    repeat = seen.get(sig)
    # The web fails for a minute at a time (engines refusing): one retry of
    # the same call is allowed, not refused as "will fail again".
    if repeat is not None and not repeat[0] and name in RETRYABLE and (sig, "retry") not in seen:
        seen[(sig, "retry")] = True
        repeat = None
    if repeat is not None:
        prev_ok, prev_err = repeat
        if prev_ok:
            # Re-running a successful identical call wastes a hop and
            # returns the same bytes. Hand back what it already has.
            result = {"error": f"you already called {name} with these "
                               f"exact arguments this turn and it "
                               f"succeeded — the result is above in "
                               f"this conversation. Use it, or call "
                               f"something different."}
        else:
            result = {"error": f"you already tried this exact "
                               f"{name} call and it failed: "
                               f"{prev_err}. Repeating it will fail "
                               f"again. Change the arguments or the "
                               f"approach, or answer with what you "
                               f"have."}
        fn = None
    if not fn:
        if repeat is None:
            result = {"error": f"unknown tool {name} — the tools are: "
                               f"{', '.join(sorted(DISPATCH))}"}
    else:
        try:
            extra = {"should_stop": should_stop} if should_stop and name in STOPPABLE else {}
            result = ji.run_stoppable(fn, **args, **extra)
        except ji.Interrupted:
            result = {"error": "stopped — Riadh pressed esc"}
        except TypeError as e:
            # A 4B model gets argument names wrong often enough that an
            # uncaught TypeError killing the whole turn is the common
            # failure mode. Hand the error back so it can retry.
            result = {"error": f"bad arguments for {name}: {e}"}
        except Exception as e:
            result = {"error": f"{name} failed: {type(e).__name__}: {e}"}
    payload = json.dumps(result)
    err = result.get("error") if isinstance(result, dict) else None
    if repeat is None:
        seen[sig] = (not err, str(err)[:160] if err else None)
        if not err and _changes_state(name, args):
            # After a change, an earlier read is stale: re-checking
            # `gsettings get` after the change must really run again.
            for k in [k for k, v in seen.items() if isinstance(v, tuple) and v[0] and k != sig]:
                del seen[k]
    log_tool_call(SESSION_ID, user_message, hop, name, args,
                  ok=not err, error=str(err)[:500] if err else None,
                  duration_ms=int((time.monotonic() - started) * 1000),
                  result_chars=len(payload))
    cap = TOOL_CHAR_CAPS.get(name, MAX_TOOL_CHARS)
    if len(payload) > cap:
        payload = payload[:cap] + '..."[truncated]"}'
    return name, args, result, payload


def notify_tool(on_tool, name, args, result):
    if on_tool:
        try:
            on_tool(name, args, result)
        except Exception:
            pass          # a broken UI callback must not kill the turn


# Which engine answers a turn. "loop" is the free-form tool loop; "steps"
# (johan_steps.py) plans, does one step at a time and checks each. Measured
# 2026-09-23: steps was 1.5-2x slower and no more accurate than the loop on
# eval_tools.py (loop 24/24), so the loop stays the default and the step
# engine is used where minutes are the budget anyway (deep research).
ENGINE = "loop"

# After any turn that used tools, check the answer against the tool results
# with the calibrated decision layer (100% on held-out cases) and rewrite it
# once if it is confidently NOT backed. Caught "2,260 apps" when the tool
# had returned 2,260 apt *packages*. Costs one decision (~1-3 s) per
# tool-using turn; a rewrite is one more short call.
# OFF since 2026-10-14: switched on and off over the same 39 eval cases it
# scored 39 vs 38 (the one miss unrelated) while the turns took 465 s vs
# 331 s, and in real use that day it wrecked three answers (business ideas
# cut to a blog's list, "what skills do you have?" rewritten into nonsense,
# a TF2 search "corrected"). Kept switchable for measuring.
VERIFY_ANSWERS = False


# Words that make it worth ASKING whether he wants deep research. Only a
# gate for the calibrated decision below — never the decision itself (a
# bare "research" substring match is the bug class this project keeps
# relearning: "what research have I done?" is not a request for any).
RESEARCH_HINT = re.compile(r"\b(research|dig into|deep[- ]dive|look into|in depth|"
                           r"investigate|thorough(ly)?|properly)\b", re.I)


# "research X and draw/write/set up Y": look things up, then do the thing.
MAKE_NOTE = ("He wants something made or done, and looking things up is only a step. "
             "First look up what you need with a few quick web_search / fetch_page calls "
             "(not deep_research — it takes minutes), then do what he asked in this same "
             "turn — don't stop at a summary. If he asked for ideas or a plan, write them "
             "yourself, concrete and specific to him (his skills are in his records: "
             "search_bullets); what you looked up only informs them. Don't end by asking "
             "for more information you could look up.")


FAILED_NOTE = ("Deep research failed this time ({err}). Say so in one short line, then "
               "answer as well as you can from what you know and his records, and say "
               "which parts are your own knowledge rather than looked up.")


def _maybe_research(q, on_tool, on_delta, should_stop, messages=None):
    """Run deep_research directly when he explicitly asks for it.

    Code decides, not tool selection: asked "research this properly: ...",
    the model skipped deep_research, ran eight web_search/fetch_page calls
    itself and missed the answer. Returns the formatted report, or None."""
    if _is_trivial(q) or not RESEARCH_HINT.search(q or ""):
        return None
    try:
        import johan_decide as jd
        d = jd.decide(jd.RESEARCH_Q, jd.RESEARCH_OPTS, f"Message: {q}",
                      kind="research_intent", null_context=jd.NULLS["research_intent"])
    except Exception:
        return None
    if not (d.choice == "yes" and d.confident):
        return None
    try:
        g = jd.decide(jd.GOAL_Q, jd.GOAL_OPTS, f"Message: {q}",
                      kind="research_goal", null_context=jd.NULLS["research_goal"])
        if g.choice == "make" and g.confident:
            return MAKE_NOTE      # research is a step: the normal loop does both
    except Exception:
        pass
    # Progress streams into the answer bubble; the returned report replaces it.
    progress = (lambda m: on_delta(f"_{m}_\n")) if on_delta else None
    # "dig into that more thoroughly": research what "that" was, not the words.
    topic = _skills._match_text(messages or [], q)
    res = _research.deep_research(topic, on_progress=progress, should_stop=should_stop)
    notify_tool(on_tool, "deep_research", {"question": q}, res)
    if res.get("error"):
        if "stopped" in res["error"]:
            return f"Research stopped: {res['error']}"
        # Failed research must not end the turn with an error meant for the
        # model: asked for business ideas, he got "Say so rather than
        # answering from memory" and nothing else. The loop answers instead.
        return {"role": "system", "content": FAILED_NOTE.format(err=res["error"])}
    src = "\n".join(f"[{s['n']}] {s['title']} — {s['url']}" for s in res.get("sources", []))
    return (f"{res['report']}\n\n**Sources**\n{src}\n\n"
            f"_{res['pages_read']} pages read in {res['seconds']}s · saved to {res['path']}_")


# Rules learned during the last run_turn ({memory_id, text, duplicate}), so a
# UI can show "learned: …" with the id to undo it. Empty almost always.
LAST_LEARNED = []


# Set when the answer check rewrote this turn's answer; the UIs say so.
LAST_CORRECTED = []


def run_turn(messages, max_hops=9, on_tool=None, on_delta=None,
             should_stop=None):
    """Answer one user turn. Dispatches to the configured engine.

    Everything the turn does in this thread — model calls, decide checks,
    tools, the answer check — is stoppable by `should_stop` (esc)."""
    with ji.stoppable(should_stop):
        try:
            return _run_turn(messages, max_hops, on_tool, on_delta,
                             should_stop or ji.stop_requested)
        except ji.Interrupted:
            return "(interrupted)"


# "It works perfectly — the snake moved when I pressed the keys", right after
# try_page reported the keys changed nothing. A 4B says what it expects, not
# what the tool showed. Code compares the claim with the evidence and ADDS a
# visible note; it never replaces the answer.
_WORKS = re.compile(r"\b(works?|working|tested|functional|ready to play|good to go|"
                    r"no (?:errors|problems|issues|glitches))\b", re.I)


def _tool_results(messages, start):
    out = []
    for m in messages[start:]:
        if m.get("role") == "tool":
            try:
                d = json.loads(m.get("content") or "")
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
    return out


def _flag_untested(answer, messages, start):
    if not answer or not _WORKS.search(answer):
        return answer
    res = _tool_results(messages, start)
    page = lambda d: str(d.get("path", "")).lower().endswith((".html", ".htm"))   # noqa: E731
    wrote = [i for i, d in enumerate(res) if (d.get("written") or d.get("applied")) and page(d)]
    tried = [i for i, d in enumerate(res) if "problems" in d and "screenshots" in d]
    note = None
    if wrote and (not tried or tried[-1] < wrote[-1]):
        note = "it wasn't opened in the browser after the last change, so this is untested"
    elif tried and res[tried[-1]]["problems"] != ["none found"]:
        note = "the browser test found: " + "; ".join(map(str, res[tried[-1]]["problems"][:2]))
    if not note:
        return answer
    flagged = f"{answer}\n\n_(check: {note})_"
    if messages and messages[-1].get("role") == "assistant":
        messages[-1]["content"] = flagged
    return flagged


def _run_turn(messages, max_hops, on_tool, on_delta, should_stop):
    import johan_steps
    LAST_LEARNED[:] = []
    LAST_CORRECTED[:] = []
    ANSWERED_BY_CODE[:] = []
    if ENGINE == "steps":
        return johan_steps.run_turn_steps(messages, max_hops=max_hops,
                                          on_tool=on_tool, on_delta=on_delta,
                                          should_stop=should_stop)
    start = len(messages)
    q = _last_user_message(messages)
    report = _maybe_research(q, on_tool, on_delta, should_stop, messages)
    note = None
    if report is MAKE_NOTE or isinstance(report, dict):
        note = report if isinstance(report, dict) else {"role": "system", "content": MAKE_NOTE}
        messages.append(note)
    elif report is not None:
        messages.append({"role": "assistant", "content": report})
        return report
    if not _is_trivial(q, messages):
        _skills.inject(messages, q)        # a matching recipe from skills/, before his message
    answer = run_turn_loop(messages, max_hops=max_hops, on_tool=on_tool,
                           on_delta=on_delta, should_stop=should_stop)
    if note is not None and note in messages:
        messages.remove(note)          # a one-turn instruction, not history
    if _plan.ended_by_ask(messages, start):
        return answer          # a question to him, not facts to verify
    # Small talk ending "code or game?" is conversation, not an ambiguous
    # request — re-asking it as options turned "good morning" into a menu.
    if answer and not answer.startswith("(interrupted") and not _is_trivial(q, messages) \
            and not ANSWERED_BY_CODE:
        seen = {}
        asked = _plan.reask_as_tool(
            messages, q, answer, call_model=call_model, on_tool=on_tool,
            should_stop=should_stop,
            execute=lambda c: execute_tool(c, seen, 0, q, should_stop))
        if asked:
            return _plan.render_ask(asked)
    answer = _flag_untested(answer, messages, start)
    # Not on "make" turns (ideas, a plan, a text): the check demands every
    # line come from a tool result, so it cut his business ideas down to the
    # ones a blog listed and opened with "Here is the corrected breakdown".
    if VERIFY_ANSWERS and answer and not answer.startswith("(interrupted") \
            and not ANSWERED_BY_CODE and report is not MAKE_NOTE:
        answer = johan_steps._verify_and_fix(messages, start, answer, should_stop)
    # A correction of how Johan behaves becomes a saved rule. Costs nothing
    # unless the word-boundary pre-filter fires; the UIs show LAST_LEARNED.
    LAST_LEARNED[:] = [] if (should_stop and should_stop()) else \
        _learn.after_turn(messages, start, q)
    # The saved rule only reaches the system prompt in the NEXT chat; this
    # one gets it now, appended (cheap — the cached prefix is untouched).
    new_rules = [r.get("text") for r in LAST_LEARNED if r.get("text") and not r.get("duplicate")]
    if new_rules:
        messages.append({"role": "system", "content": "From now on, as he just asked: "
                         + " ".join(new_rules)})
    return answer


def run_turn_loop(messages, max_hops=9, on_tool=None, on_delta=None,
                  should_stop=None, tools=None):
    """Run tool-call hops until the model gives a final text answer.

    `on_tool(name, args, result)` is called for each tool invocation so a UI
    can show what the assistant is actually doing — with 25 tools, "thinking..."
    tells the user nothing, while "web_search(...)" makes it legible.
    `tools` optionally narrows the schema to a shortlist."""
    user_message = _last_user_message(messages)
    _reader.set_question(_reader.question_from(messages))   # long pages read by relevance
    seen = {}
    for hop in range(max_hops):
        # On the final hop, take the tools away and demand an answer.
        #
        # A real research chain is search -> fetch -> refine -> fetch -> answer.
        # With a 5-hop cap the model spent every hop investigating ("what are
        # the keyboard controls for Lisa the Painful" went web_search ->
        # reddit (gated) -> web_search PCGamingWiki -> fetch -> fetch#Input)
        # and the loop then returned "stopped after too many tool-call hops",
        # throwing away the answer it had just read into context. Whatever it
        # learned must get summarised, not discarded.
        if should_stop and should_stop():
            return "(interrupted)"
        last = hop == max_hops - 1
        # A greeting gets no tools at all, not a request to please not use
        # them. Only the first hop: if a later hop somehow gets here the
        # conversation is no longer trivial.
        quiet = last or (hop == 0 and _is_trivial(user_message, messages))
        # The last hop runs with no tool schemas, and the model read that as
        # "I have no tools": "I can't make a game — I don't have file writing
        # tools… we'll have to wait until those tools are available again".
        sent = messages + [{"role": "system", "content": LAST_HOP_NOTE}] if (
            last and hop > 0) else messages
        msg = call_model(sent, on_delta=on_delta, no_tools=quiet,
                         should_stop=should_stop, tools=tools)
        if msg.pop("interrupted", False):
            # Keep the partial text in history: the model should see what it
            # had started saying when the user cut in, so "no, the other one"
            # has something to refer to.
            if msg.get("content"):
                messages.append(msg)
            return msg.get("content", "") or "(interrupted)"
        if msg.pop("cut_off", False) and not last:
            # The cut text is half a file or half a tool call: never run or
            # show it; say so and let the next hop try smaller.
            messages.append({"role": "user", "content": CUT_NOTE.format(n=NUM_PREDICT)})
            continue
        if last and (msg.get("tool_calls") or not (msg.get("content") or "").strip()):
            # Leaving `tools` out does NOT stop this model calling them:
            # Ollama's parser lifts tool calls out of the raw output either
            # way (tool_calls rows at hop 8 — the tools-off hop — existed in
            # real use). Those calls were then EXECUTED on a hop that offered
            # no tools. Never execute them; insist once, then fall back to a
            # summary built in code, so this hop always yields an answer.
            return _final_answer(messages, on_delta, should_stop)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content", "")
        done = []
        for call in calls:
            name, args, result, payload = execute_tool(call, seen, hop, user_message,
                                                       should_stop)
            messages.append({"role": "tool", "content": payload})
            notify_tool(on_tool, name, args, result)
            done.append((name, result))
        asked = _plan.ask_result_of(done)
        if asked:
            # `ask` ends the turn: his pick arrives as the next user message.
            text = _plan.render_ask(asked)
            messages.append({"role": "assistant", "content": text})
            return text
        drawn = _drawn_answer(done)
        said = (msg.get("content") or "").strip()
        if drawn and said:
            # "draw a red panda and tell me the prompt": what the model wrote
            # beside the call is part of the answer, not to be dropped.
            drawn = f"{said}\n\n{drawn}"
        if drawn:
            # The chat model was unloaded to make room for the drawing; one
            # more hop reloads it and re-reads the whole conversation just to
            # say "here it is" — measured 29 s after a 33 s drawing. Code
            # says it, and the reload happens in the background instead.
            messages.append({"role": "assistant", "content": drawn})
            if on_delta:
                on_delta(drawn)
            ANSWERED_BY_CODE[:] = [True]
            rewarm(messages)
            return drawn
    # Unreachable by construction: the final hop above always returns.
    return _final_answer(messages, on_delta, should_stop)


# Set when the turn's answer was written by code (nothing to fact-check).
ANSWERED_BY_CODE = []


def _drawn_answer(done):
    """The answer for a hop that only drew pictures, all successfully."""
    if not done or any(n != "generate_image" or not isinstance(r, dict) or not r.get("path")
                       for n, r in done):
        return None
    lines = []
    for _, r in done:
        how = {"lines": " — same outline, redrawn", "character": " — same character, new scene",
               "look": " — touched up", "add": " — with the newcomer added"
               }.get(r.get("keep"), "") if r.get("reference") else ""
        lines.append(f"Here it is{how} ({r.get('seconds')} s): `{r['path']}` — ctrl+o opens it.")
    return "\n".join(lines)


def rewarm(messages):
    """Load the chat model again and read this conversation into its cache,
    in the background, so his next message doesn't pay for it. Fire and
    forget: a failure only means the next message is slower."""
    import copy
    import threading
    snapshot = copy.deepcopy(messages)

    def go():
        try:
            requests.post(OLLAMA_CHAT, timeout=180, json={
                "model": MODEL, "messages": _attach.ollama_messages(snapshot),
                "tools": TOOLS, "think": False, "stream": False, "keep_alive": KEEP_ALIVE,
                "options": {"temperature": 0.3, "num_ctx": NUM_CTX, "num_predict": 1}})
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


FINAL_NUDGE = ("[note from Johan's runtime, not Riadh] No more tool calls in this "
               "message — your tools are back in his next one. Answer him now in plain "
               "text from what the tool results above show; say plainly what you "
               "couldn't finish and that you can carry on.")


LAST_HOP_NOTE = ("This is your last step for this message, so no more tool calls now: answer "
                 "with what you found and what's still to do. You still have all your tools "
                 "(files, run_command, try_page…) — he can say \"go on\" and you continue.")


def _final_answer(messages, on_delta=None, should_stop=None):
    """A text answer for the turn's last hop, guaranteed: one tools-off retry
    with an explicit instruction (not kept in history), then a summary of
    the tool results written by code."""
    msg = call_model(messages + [{"role": "user", "content": FINAL_NUDGE}],
                     on_delta=on_delta, no_tools=True, should_stop=should_stop)
    text = (msg.get("content") or "").strip()
    if msg.pop("interrupted", False):
        return text or "(interrupted)"
    if not text or msg.get("tool_calls"):
        text = _summary_of_tools(messages)
    messages.append({"role": "assistant", "content": text})
    return text


def _summary_of_tools(messages):
    """Last resort, no model: what ran this turn and what came back."""
    start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    names = {}
    for m in messages[start:]:
        for c in m.get("tool_calls") or []:
            n = (c.get("function") or {}).get("name", "?")
            names[n] = names.get(n, 0) + 1
    ok = failed = 0
    for m in messages[start:]:
        if m.get("role") == "tool":
            if _steps_failed_only(m.get("content")):
                failed += 1
            else:
                ok += 1
    ran = ", ".join(f"{n} ×{k}" if k > 1 else n for n, k in names.items()) or "nothing"
    # Raw tool JSON used to be pasted here — including error text written for
    # the model ("Say so rather than…"). He gets a plain account instead.
    return (f"I ran out of steps before I could write an answer. I ran {ran} "
            f"({ok} worked, {failed} failed). Ask again more narrowly, or say "
            f"\"go on\" and I'll continue from what I found.")


def _steps_failed_only(content):
    import johan_steps
    return johan_steps._failed_only(content)


SESSION_FILE = ROOT / "brain/last-session.json"
SESSIONS_DIR = ROOT / "brain/sessions"

# One id per process, so every save during a session overwrites that
# session's file instead of littering the directory with partial copies.
SESSION_ID = datetime.now().strftime("%Y-%m-%d-%H%M%S")
LAST_ID_FILE = ROOT / "brain/last-session.id"


def new_session():
    """Start a fresh conversation id. The id used to be fixed for the whole
    process, so ctrl+n in the UI began a "new" chat that silently
    OVERWROTE the previous one's history file."""
    global SESSION_ID
    SESSION_ID = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return SESSION_ID


def use_session(sid):
    """Continue an existing conversation under its own id, so a reopened
    chat keeps saving to its file instead of being duplicated."""
    global SESSION_ID
    if sid:
        SESSION_ID = sid
    return SESSION_ID


def save_session(messages):
    """Persist the raw message list twice: `last-session.json` for
    `--continue`, and `sessions/<id>.json` so older conversations remain
    resumable and searchable rather than only the most recent one.

    Separate from the human-readable transcript in brain/inbox/: that's for
    reading, these are for replaying into the model."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(messages[-40:], indent=1)
    SESSION_FILE.write_text(payload)
    LAST_ID_FILE.write_text(SESSION_ID)
    (SESSIONS_DIR / f"{SESSION_ID}.json").write_text(payload)
    # Keep the last 100 conversations. Without this the directory grows
    # forever and the history browser slows down reading all of them.
    saved = sorted(SESSIONS_DIR.glob("*.json"), reverse=True)
    for old in saved[100:]:
        try:
            old.unlink()
            (SESSION_LOG_DIR / f"chat-{old.stem}.md").unlink(missing_ok=True)
        except OSError:
            pass


def load_session(session_id=None):
    """Most recent session by default, or a specific one by id."""
    path = (SESSIONS_DIR / f"{session_id}.json") if session_id else SESSION_FILE
    if not path.exists():
        return None
    try:
        msgs = json.loads(path.read_text())
        return msgs if isinstance(msgs, list) and msgs else None
    except (ValueError, OSError):
        return None


def release_model(model=None):
    """Unload the model from VRAM now (keep_alive: 0).

    KEEP_ALIVE keeps it resident for fast follow-up prompts, which would
    otherwise mean 3.3 GB squatting in VRAM for 20 minutes after you quit.
    Called on every exit path so "closed Johan" means "VRAM is free"."""
    model = model or MODEL
    try:
        response = requests.post(OLLAMA_CHAT, timeout=20, json={
            "model": model, "messages": [], "keep_alive": 0})
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False


def delete_session(session_id):
    """Delete one saved conversation: its json, its transcript, and the
    `--continue` pointer if it happened to be the most recent."""
    sid = str(session_id or "").strip()
    if not sid or "/" in sid or ".." in sid:
        return {"error": "give a session id from the history list"}
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.exists():
        return {"error": f"no saved conversation with id '{sid}'"}
    first = ""
    try:
        msgs = json.loads(path.read_text())
        users = [m.get("content", "") for m in msgs if m.get("role") == "user"]
        first = users[0][:70] if users else ""
    except (ValueError, OSError):
        pass
    path.unlink()
    removed = ["session"]
    transcript = SESSION_LOG_DIR / f"chat-{sid}.md"
    if transcript.exists():
        transcript.unlink()
        removed.append("transcript")
    # Images drawn in this conversation go with it (Riadh, 2026-10-03:
    # "they should get deleted when i delete the chat").
    try:
        import johan_images
        n = johan_images.delete_for_session(sid)
        if n:
            removed.append(f"{n} image{'s' * (n > 1)}")
    except Exception:
        pass
    # If this was the conversation --continue would reopen, drop the pointer
    # too, otherwise --continue resurrects what was just deleted.
    if SESSION_FILE.exists():
        try:
            if json.loads(SESSION_FILE.read_text()) == msgs:
                SESSION_FILE.unlink()
                removed.append("last-session pointer")
        except (ValueError, OSError, NameError):
            pass
    try:
        if LAST_ID_FILE.exists() and LAST_ID_FILE.read_text().strip() == sid:
            LAST_ID_FILE.unlink()
    except OSError:
        pass
    return {"deleted": sid, "was": first, "removed": removed}


def humane_when(session_id):
    """'today 14:32' / 'yesterday 09:05' / 'Mon 3 Sep' — a raw
    2026-09-11-001234 stamp is unreadable at a glance and impossible to
    recognise your own conversation by."""
    try:
        when = datetime.strptime(session_id, "%Y-%m-%d-%H%M%S")
    except ValueError:
        return session_id
    today = datetime.now().date()
    delta = (today - when.date()).days
    if delta == 0:
        return f"today {when:%H:%M}"
    if delta == 1:
        return f"yesterday {when:%H:%M}"
    if delta < 7:
        return f"{when:%a} {when:%H:%M}"
    return f"{when:%-d %b}"


def list_sessions(limit=50):
    """Past conversations, newest first, each with its first user message so
    it's recognisable."""
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            msgs = json.loads(p.read_text())
        except (ValueError, OSError):
            continue
        users = [m.get("content", "") for m in msgs if m.get("role") == "user"]
        if not users:
            continue
        out.append({"id": p.stem, "when": humane_when(p.stem),
                     "messages": len(users),
                     "first": users[0][:70], "last": users[-1][:70]})
    return out


def session_preview(session_id, limit=40):
    """The conversation itself, for showing before you commit to reopening
    it. Tool chatter is dropped — you recognise a conversation by what was
    said, not by which tools ran."""
    msgs = load_session(session_id) or []
    out = []
    for m in msgs:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = (m.get("content") or "").strip()
        if text:
            out.append((role, text))
    return out[:limit]


def prime_with_memories(system_prompt, limit=8):
    """Stored facts AND the live scratchpad, injected into the prompt.

    Without this the model only knows him after it thinks to call `memory`,
    which a 4B model often doesn't — so it would introduce itself to him
    every session despite having his details on disk. Kept short because
    context is the scarce resource here."""
    try:
        # Behaviour rules (kind 'feedback') get their own block below, so
        # they never crowd facts out of this list or vice versa.
        mems = [m for m in jt.list_memories(limit=limit + 20).get("memories", [])
                if m["kind"] != _learn.KIND][:limit]
    except Exception:
        return system_prompt          # DB down: still usable as a chat model
    out = system_prompt
    try:
        out += _learn.rules_block(_learn.rules())
    except Exception:
        pass
    if mems:
        lines = "\n".join(f"- ({m['kind']}) {m['text']}" for m in mems)
        # "use `memory` for more" was an open invitation, and the model took
        # it — on "hi" it called `memory` with no arguments, i.e. re-fetched
        # this exact list it was already holding. Say they are loaded.
        out = (f"{out}\nWhat you already know about Riadh — these are "
               f"ALREADY LOADED, do not call `memory` to re-read them. Only "
               f"call `memory(query=...)` to look for something specific "
               f"that is NOT listed here; `forget` if one is wrong:\n"
               f"{lines}\n")
    # Recipes Riadh approved from johan_review's proposals (brain/recipes.md).
    # Capped: the prompt is prefilled on every new conversation (~2.6 s for the
    # current ~4K tokens), so every line here costs time on every chat.
    try:
        rec = (ROOT / "brain/recipes.md").read_text()
        lines = [l for l in rec.split("-->", 1)[-1].splitlines() if l.startswith("- ")]
        if lines:
            out = (f"{out}\nRecipes learned from earlier mistakes (approved by "
                   f"Riadh):\n" + "\n".join(lines)[:1200] + "\n")
    except OSError:
        pass
    # The scratchpad is injected unconditionally and last, so it is the most
    # recent thing in the prompt. This is the whole point: the model cannot
    # forget to look it up, which is exactly what happens with recall-style
    # memory on a 4B.
    try:
        notes = jt.scratchpad_read()
    except Exception:
        notes = ""
    if notes:
        out = (f"{out}\nYour working notes for what you are currently doing "
               f"(update with `scratchpad(text=...)`, wipe with "
               f"`scratchpad(clear=true)` when the task is done):\n{notes}\n")
    return out


def log_session(messages):
    SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    # SESSION_ID, not now(): this is called after every turn, so using the
    # current time wrote a fresh transcript per reply (30 files from one
    # afternoon of testing). Same id as the session json, so they pair up.
    path = SESSION_LOG_DIR / f"chat-{SESSION_ID}.md"
    lines = [f"# Johan chat — {datetime.now():%Y-%m-%d %H:%M}\n"]
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            lines.append(f"**[tool result]** `{m['content'][:200]}`\n")
        else:
            lines.append(f"**{m['role']}:** {m.get('content', '')}\n")
    path.write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", help="single message, print the reply, exit")
    ap.add_argument("--continue", "-c", dest="cont", action="store_true",
                    help="resume the previous conversation")
    ap.add_argument("--resume", "-r", nargs="?", const="", default=None,
                    help="a one-shot answer inside that conversation (no id: the last)")
    ap.add_argument("--uncensored", action="store_true",
                    help="use the uncensored model for this invocation")
    args = ap.parse_args()

    if args.uncensored:
        if not model_installed(UNCENSORED_MODEL):
            print(f"the uncensored model ({UNCENSORED_MODEL}) isn't installed",
                  file=sys.stderr)
            raise SystemExit(1)
        use_model(UNCENSORED_MODEL)

    messages = None
    if args.cont or args.resume is not None:
        messages = load_session(args.resume or None)
        if messages:
            # Refresh the system prompt rather than replaying a stale one, so
            # prompt edits and newly stored memories take effect on resume.
            messages[0] = {"role": "system",
                            "content": prime_with_memories(SYSTEM_PROMPT)}
            turns = sum(1 for m in messages if m.get("role") == "user")
            print(f"(resumed previous conversation — {turns} earlier "
                  f"message{'s' if turns != 1 else ''})\n")
        else:
            print("(no previous conversation to resume)\n")
    if not messages:
        messages = [{"role": "system",
                      "content": prime_with_memories(SYSTEM_PROMPT)}]

    if not args.once:
        # The interactive REPL that used to live here was replaced by
        # johan_tui.py, which shows live state and tool calls. This module is
        # now the engine (run_turn, tools, session persistence) plus one-shot
        # answering for scripts; `johan` with no arguments opens the UI.
        print("Use `johan` for the interface, or `johan \"your question\"` "
              "for a one-shot answer.", file=sys.stderr)
        return 1

    global KEEP_ALIVE
    KEEP_ALIVE = KEEP_ALIVE_ONESHOT
    messages.append({"role": "user", "content": args.once})
    print(run_turn(messages))
    log_session(messages)
    save_session(messages)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
