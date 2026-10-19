"""johan_refresh — fetch new postings, close dead ones, re-check, rebuild.

The one explicit way to refresh the job list: `/refresh` in the TUI or on
or `python johan_refresh.py`. Deliberately NOT a tool the chat
model can call (CLAUDE.md "Deliberately not exposed: Stage 1 or Stage 2"):
it is ~20 minutes of network and GPU work, so only Riadh starts it. And not
automatic either — he declined the login-time refresh (2026-10-07).

Steps, each a subprocess pinned to cores 8-15 (games keep 0-3):
  1. stage1_scrape.py   — company boards + aggregators. Records last_seen;
                          a company board that no longer lists a posting
                          closes it on the spot (the board is authoritative).
  2. stage1_keejob.py   — Tunisian site; only fetches postings it hasn't
                          seen, closes what the full listing no longer shows.
  3. aggregator ageing  — aggregators show only a recent window, so absence
                          proves nothing; a posting unseen for AGG_DAYS is
                          treated as closed.
  4. stage2_parse.py    — eligibility check on every new posting (GPU).
                          If Ollama stops mid-run (GameMode), the rest stay
                          'new' and the next /refresh resumes there.
  5. apply queue        — rebuilt from open jobs only, CVs regenerated.

Progress goes to on_progress(str); a run log to logs/refresh-<date>.log.
Only one refresh runs at a time (logs/refresh.lock holds the pid).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2
import requests

ROOT = Path.home() / "johan"
SCRIPTS = ROOT / "scripts"
PY = ROOT / "venv-embed/bin/python3"
LOCK = ROOT / "logs/refresh.lock"
STATUS = ROOT / "logs/refresh.json"
AGG_SOURCES = ("arbeitnow-visa", "arbeitnow-uk", "remotejobs", "remoteok", "remotive")
AGG_DAYS = 21


def _dsn():
    env = dict(l.split("=", 1) for l in (ROOT / "compose/.env").read_text().splitlines()
               if "=" in l and not l.startswith("#"))
    return (f"dbname=johan user=johan password={env['POSTGRES_PASSWORD'].strip()} "
            f"host=127.0.0.1 port=5432")


def _counts(cur):
    cur.execute("select count(*) filter (where closed_at is null), "
                "count(*) filter (where closed_at is not null), "
                "count(*) filter (where status = 'new' and closed_at is null), "
                "count(*) filter (where closed_at is null and "
                "parsed->>'tunisia_verdict' = 'likely') from jobs")
    return dict(zip(("open", "closed", "unchecked", "likely"), cur.fetchone()))


def _ollama_up():
    try:
        return requests.get("http://127.0.0.1:11434/api/version", timeout=3).ok
    except requests.RequestException:
        return False


def _running():
    try:
        pid = int(LOCK.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def status():
    """Last run's summary (for the UI's home card), or None."""
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None


def _run(script, args, log, on_line):
    """Run one pipeline script; feed each output line to on_line. Returns rc."""
    proc = subprocess.Popen(
        ["taskset", "-c", "8-15", "nice", "-n", "10", str(PY), "-u", script, *args],
        cwd=str(SCRIPTS), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        log.write(line)
        log.flush()
        on_line(line.rstrip())
    return proc.wait()


def refresh(on_progress=None):
    """Run the whole refresh. Returns a summary dict ({"error": ...} on failure)."""
    say = on_progress or (lambda m: None)
    other = _running()
    if other:
        return {"error": f"a refresh is already running (pid {other})"}
    if not _ollama_up():
        return {"error": "the AI is stopped (GameMode / johan-pause) — the eligibility "
                         "check needs it. Run /refresh again after your game."}
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()))
    started = time.monotonic()
    log_path = ROOT / f"logs/refresh-{datetime.now():%Y-%m-%d}.log"
    try:
        conn = psycopg2.connect(_dsn())
        conn.autocommit = True
        cur = conn.cursor()
        before = _counts(cur)
        with open(log_path, "a") as log:
            log.write(f"\n=== refresh {datetime.now():%Y-%m-%d %H:%M} ===\n")

            say("fetching job boards")
            def board(line):
                m = re.match(r"(\S+?)\s+seen", line)
                if m:
                    say(f"fetching job boards · {m.group(1)}")
            _run("stage1_scrape.py", [], log, board)

            say("fetching Keejob")
            def keejob(line):
                m = re.match(r"page\s+(\d+):", line) or re.match(r"\s*(\d+)\s+(NEW|FAIL)", line)
                if m:
                    say(f"fetching Keejob · {m.group(1)}")
            _run("stage1_keejob.py", [], log, keejob)

            cur.execute("update jobs set closed_at = now() where source = any(%s) "
                        "and closed_at is null and last_seen < now() - %s * interval '1 day'",
                        (list(AGG_SOURCES), AGG_DAYS))
            log.write(f"aged out {cur.rowcount} aggregator postings unseen for {AGG_DAYS} days\n")

            todo = _counts(cur)["unchecked"]
            say(f"checking eligibility · 0/{todo}")
            def parse(line):
                m = re.match(r"\s*(\d+)/(\d+)", line)
                if m:
                    say(f"checking eligibility · {m.group(1)}/{m.group(2)}")
            rc = _run("stage2_parse.py", ["--limit", "100000", "--status", "new"], log, parse)
            paused = rc == 3

            queue = None
            if not paused:
                say("rebuilding the apply queue")
                import apply_queue as aq
                args = argparse.Namespace(verdict="likely", limit=12, job_id=[],
                                          min_fit=5, include_overreach=False)
                rows = aq.build(args)
                if rows:
                    aq.write_json(rows, "likely")
                    aq.write_markdown(rows, "likely")
                queue = len(rows)
                _run("report.py", [], log, lambda l: None)

        after = _counts(cur)
        conn.close()
        summary = {
            "finished": datetime.now().isoformat(timespec="minutes"),
            "minutes": round((time.monotonic() - started) / 60, 1),
            "open_jobs": after["open"],
            "newly_closed": after["closed"] - before["closed"],
            "new_postings": (after["open"] + after["closed"]) - (before["open"] + before["closed"]),
            "likely": after["likely"],
            "likely_change": after["likely"] - before["likely"],
            "still_unchecked": after["unchecked"],
            "queue": queue,
            "paused": paused,
            "log": str(log_path),
        }
        STATUS.write_text(json.dumps(summary, indent=1))
        return summary
    except Exception as e:
        return {"error": f"refresh failed: {e} (log: {log_path})"}
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


def describe(s):
    """One short human line for a summary dict."""
    if s.get("error"):
        return s["error"]
    if s.get("paused"):
        return (f"Paused — the AI stopped (GameMode?). {s['new_postings']} new postings "
                f"fetched, {s['still_unchecked']} still to check: /refresh resumes.")
    ch = s["likely_change"]
    return (f"Refreshed in {s['minutes']} min: {s['new_postings']} new postings, "
            f"{s['newly_closed']} closed, {s['likely']} eligible "
            f"({'+' if ch >= 0 else ''}{ch}). Queue rebuilt: {s['queue']} jobs.")


if __name__ == "__main__":
    res = refresh(lambda m: print(f"  {m}", flush=True))
    print(describe(res))
    sys.exit(1 if res.get("error") else 0)
