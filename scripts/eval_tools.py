"""Offline eval harness — does the model pick the right tool?

Why this exists: every claim about reliability in this project so far has been
anecdotal ("it reported 50 when the answer was 130"). That one anecdote drove
a real fix, but you cannot tell whether a model swap, a prompt edit or a tool
merge helped or hurt without a fixed set of prompts and a score.

Scoring is deliberately lenient about HOW a task is done and strict about
whether it was done:
  - `expect`      at least one of these tools must be called
  - `forbid`      none of these may be called (wrong-tool traps)
  - `expect_none` the model must answer without tools (over-calling is a real
                  failure mode: a 4B model will reach for a tool on "hello")
  - `contains`    the final answer must contain this string (ground truth)

Every call also lands in the `tool_calls` table tagged with this run id, so
failures can be inspected afterwards rather than re-run.

    python eval_tools.py                  # run all, print a scorecard
    python eval_tools.py --only memory    # run one group
    python eval_tools.py --compare        # score the last two runs side by side
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja      # noqa: E402

# Each case is a real thing he would ask. `contains` values are facts the
# tools can verify, not opinions.
CASES = [
    # --- jobs -------------------------------------------------------------
    dict(group="jobs", prompt="how many likely jobs do I have?",
         expect=["get_pipeline_stats", "search_jobs"], contains="LIVE_LIKELY",
         why="the miscount bug: search_jobs' cap was reported as the count"),
    dict(group="jobs", prompt="give me 2 jobs to apply to",
         expect=["build_apply_queue"], forbid=["search_jobs"],
         why="must use the queue builder, not the raw listing tool"),
    dict(group="jobs", prompt="what have I already applied to?",
         expect=["applied"]),
    dict(group="jobs", prompt="can supabase hire someone in tunisia?",
         expect=["check_company_hiring"]),

    # --- memory -----------------------------------------------------------
    dict(group="memory", prompt="remember that I prefer PostgreSQL over MySQL",
         expect=["memory"]),
    dict(group="memory", prompt="what do you know about me?",
         expect=["memory"]),
    # Corrections of HOW Johan behaves are learned by code after the turn
    # (johan_learn), not by a tool call — so these assert on what was saved.
    dict(group="memory", prompt="stop ending every answer with a question, it's annoying",
         history=[("user", "what can you do?"),
                  ("assistant", "Loads — files, the web, your jobs. Want me to check "
                                "your apply queue?")],
         learns=True,
         why="a behaviour correction must become a saved rule (kind feedback)"),
    dict(group="memory", prompt="no, it's called johan-postgres, not johan-db",
         history=[("user", "what's my database container called?"),
                  ("assistant", "It's johan-db.")],
         learns=False,
         why="correcting a FACT passes the pre-filter but must not become a rule"),

    # --- deterministic ----------------------------------------------------
    dict(group="basics", prompt="what is 1847 * 23?",
         expect=["calculate"], contains="42481",
         why="a 4B model gets this wrong in its head"),
    dict(group="basics", prompt="what is today's date?",
         expect=["current_datetime"]),
    dict(group="basics", prompt="how much VRAM is free right now?",
         expect=["system_status"]),

    # --- live data --------------------------------------------------------
    dict(group="live", prompt="what's the latest version of fastapi?",
         expect=["latest_version", "web_search"]),

    # --- tasks ------------------------------------------------------------
    dict(group="tasks", prompt="add a task: check the database migrations",
         expect=["tasks"]),

    # --- harder: cases built from failures this project actually had ------
    dict(group="hard", prompt="how many jobs are there in total, and how many "
                              "have I applied to?",
         expect=["get_pipeline_stats", "applied"],
         why="two facts in one question — a 4B model often answers half"),
    dict(group="hard", prompt="what's job 142 and is it a good fit for me?",
         expect=["get_job", "search_jobs"],
         why="must look the job up rather than describe it from the queue"),
    dict(group="hard", prompt="do I have professional Kubernetes experience?",
         expect=["search_bullets", "memory"], contains="no",
         why="must answer from the fact store; inventing a yes is the "
             "hallucination this whole project exists to prevent"),
    dict(group="hard", prompt="mark job 142 as applied",
         expect=["applied"],
         why="a write path — wrong tool here corrupts his records"),
    dict(group="hard", prompt="what is 97 * 143 minus 400?",
         expect=["calculate"], contains="13471",
         why="multi-step arithmetic, still must not be done in-head"),

    # --- research chains: search -> fetch -> refine -> ANSWER -------------
    dict(group="research",
         prompt="what are the keyboard controls for Lisa the Painful?",
         expect=["web_search"],
         why="a 5-hop cap made it spend every hop investigating and return "
             "'stopped after too many tool-call hops', throwing away the "
             "PCGamingWiki page it had just read"),

    dict(group="research",
         prompt="research this properly: what are the keyboard controls in "
                "Lisa the Painful, and can they be remapped?",
         expect=["deep_research"], contains="F3",
         why="deep research must find the remap menu (F3) and cite it; the "
             "first version read 12 pages and found nothing because it kept "
             "only the first 4,000 chars of each and dropped short lines"),
    dict(group="research", prompt="how much is 15% of 2400?",
         forbid=["deep_research"],
         why="deep_research is slow; it must never fire on a normal question"),

    # --- hands: code sandbox ----------------------------------------------------
    dict(group="hands",
         prompt="use python to compute the population standard deviation of 4, 8, 15, 16, 23, 42",
         expect=["run_python"], contains="12.3",
         why="population sd is 12.31; a 4B guesses this wrong in its head"),

    # --- shell: read-only, allowlisted --------------------------------------
    dict(group="shell", prompt="how much disk space is free? run a command",
         expect=["run_command", "system_status"]),
    dict(group="shell", prompt="which docker containers are running?",
         expect=["run_command", "system_status"]),
    dict(group="shell", prompt="can you check my tf2 custom config?",
         expect=["run_command"],
         why="a file OUTSIDE ~/johan. The model answered '~/johan does not "
             "exist on this system' and called no tool at all, because "
             "nothing told it run_command reaches the rest of the machine"),
    dict(group="shell", prompt="how many apps are installed on my pc?",
         expect=["run_command"],
         why="spent 8 hops in ~/.local/share/applications (6 desktop "
             "shortcuts) and never ran dpkg -l; also looped on a blocked "
             "`dpkg -l | awk ...` four times identically"),
    dict(group="shell", prompt="delete every log file in the johan folder",
         expect=["propose_action"], forbid=["propose_edit"],
         why="since 2026-10-13 the right move is to STAGE `gio trash <paths>` via "
             "propose_action (he approves in F4). It used to burn 8 hops on "
             "find -delete / xargs rm / while-loops in the read-only shell"),

    # --- changes: staged, approved in F4 -------------------------------------
    dict(group="change", prompt="install htop on my pc",
         expect=["propose_action"],
         why="run_command is read-only; installing must be STAGED via "
             "propose_action, never claimed as done"),
    dict(group="change", prompt="make the top bar clock show seconds",
         expect=["propose_action"],
         why="a gsettings change (clock-show-seconds is false here) — staged "
             "with the old value recorded for undo. 'turn on dark mode' was "
             "the first case and was wrong: dark mode is already on, and "
             "Johan correctly checked and said so instead of proposing"),
    dict(group="change", prompt="what's my current gnome theme?",
         forbid=["propose_action"],
         why="a question about a setting must only READ it"),

    # --- over-calling: answering without tools is the correct behaviour ----
    dict(group="restraint", prompt="hi", expect_none=True,
         why="reaching for a tool on a greeting is a real failure mode"),
    dict(group="restraint", prompt="my cat knocked my coffee over",
         expect_none=True,
         why="he's TELLING, not asking: it searched memory, then the verifier "
             "produced 'No information was found regarding a cat knocking over "
             "coffee'. Fixed by the prompt's chatting-vs-asking rule (2026-10-13)"),
    dict(group="restraint", prompt="bro i just got destroyed in tf2 lmao",
         expect_none=True,
         why="banter about a game ran run_command; also the replies kept "
             "steering to 'how can I help with your job search'"),
    dict(group="restraint",
         prompt="in one sentence, what is the difference between TCP and UDP?",
         expect_none=True,
         why="general knowledge needs no tool; over-calling wastes a hop"),
]


def _snapshot():
    """Ids that exist before the run, so anything created during it can be
    undone.

    The harness exercises write tools — `mark_applied`, `remember`,
    `tasks add`. Running it once falsely marked a job applied, stored two
    preferences he never expressed, and added a task from a test prompt. An
    eval that corrupts the data it measures is worse than no eval, and
    "applied" is exactly the record he'd trust later."""
    import johan_tools as jt
    return {
        "applied": set(ja.applied_ids()),
        "memories": {m["memory_id"] for m in jt.memory(limit=50).get("memories", [])},
        "tasks": {r["task_id"] for r in jt.tasks("list")["tasks"]},
        # The scratchpad is injected into EVERY production prompt, so a note
        # the harness leaves behind is not inert test residue — it silently
        # becomes context for every real conversation afterwards. One run on
        # "hi" left "User greeted me. Ready to assist..." sitting there.
        "scratchpad": jt.scratchpad_read(),
        # Staged machine changes: a run must not leave "install htop" waiting
        # in his F4 for him to approve by accident.
        "actions": {a["id"] for a in jt._actions.list_actions()},
    }


def _restore(before):
    """Undo everything the run created. Reverts, never deletes pre-existing
    rows — only ids absent from the snapshot are touched."""
    import johan_tools as jt
    undone = []
    for job_id in set(ja.applied_ids()) - before["applied"]:
        ja.mark_applied(job_id, applied=False)
        undone.append(f"unmarked job {job_id}")
    for m in jt.memory(limit=50).get("memories", []):
        if m["memory_id"] not in before["memories"]:
            jt.forget(m["memory_id"])
            undone.append(f"forgot memory {m['memory_id']}")
    # DELETE, don't just close: `tasks("done")` leaves the row behind, so
    # every eval run added another "check the database migrations" to his
    # real task list. Undoing a write means removing it.
    created = [r["task_id"] for r in jt.tasks("list", include_done=True)["tasks"]
               if r["task_id"] not in before["tasks"]]
    if created:
        conn = jt._conn()
        with conn, conn.cursor() as cur:
            cur.execute("delete from tasks where id = any(%s)", (created,))
        conn.close()
        undone.append(f"deleted {len(created)} task(s) the run created")
    for a in jt._actions.list_actions():
        if a["id"] not in before.get("actions", set()):
            jt._actions.discard_action(a["id"])
            undone.append(f"discarded staged change a{a['id']}")
    if jt.scratchpad_read() != before["scratchpad"]:
        if before["scratchpad"]:
            jt.scratchpad_write(before["scratchpad"])
        else:
            jt.scratchpad_clear()
        undone.append("restored the scratchpad")
    return undone


def run_case(case, run_id):
    # Prime exactly like the TUI and the one-shot CLI do. Testing the raw
    # SYSTEM_PROMPT measured a prompt that never ships: production appends
    # stored memories and the live scratchpad, and both change what the
    # model reaches for. An eval on a different prompt is not a regression
    # check on anything.
    messages = [{"role": "system",
                 "content": ja.prime_with_memories(ja.SYSTEM_PROMPT)}]
    messages += [{"role": r, "content": c} for r, c in case.get("history", [])]
    messages.append({"role": "user", "content": case["prompt"]})
    used = []
    started = time.monotonic()
    try:
        answer = ja.run_turn(messages, on_tool=lambda n, a, r: used.append(n))
    except Exception as e:
        return dict(case=case, used=used, answer="", ok=False,
                    reason=f"raised {type(e).__name__}: {e}",
                    seconds=time.monotonic() - started)
    seconds = time.monotonic() - started

    reasons = []
    if case.get("expect_none"):
        if used:
            reasons.append(f"called {used} but should have answered directly")
    else:
        wanted = case.get("expect", [])
        if wanted and not any(u in wanted for u in used):
            reasons.append(f"expected one of {wanted}, called {used or 'nothing'}")
    for bad in case.get("forbid", []):
        if bad in used:
            reasons.append(f"used forbidden tool {bad}")
    if "learns" in case:
        learned = [r for r in ja.LAST_LEARNED if not r.get("duplicate")]
        if case["learns"] and not ja.LAST_LEARNED:
            reasons.append("a correction of his behaviour was not saved as a rule")
        if not case["learns"] and learned:
            reasons.append(f"saved a rule it should not have: {learned[0]['text']!r}")
    need = case.get("contains")
    if need and need.lower() not in (answer or "").lower().replace(",", ""):
        reasons.append(f"answer missing {need!r}")

    return dict(case=case, used=used, answer=answer or "", ok=not reasons,
                reason="; ".join(reasons), seconds=seconds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run only this group")
    ap.add_argument("--compare", action="store_true",
                    help="score the last two eval runs side by side")
    ap.add_argument("--engine", choices=["loop", "steps"],
                    help="which engine answers (default: johan_assistant.ENGINE)")
    args = ap.parse_args()
    if args.engine:
        ja.ENGINE = args.engine

    if args.compare:
        return compare()

    # The likely count changes with every /refresh; a hard-coded number
    # (it was 130) fails every model the day the list is refreshed.
    likely = str(ja.get_pipeline_stats()["by_verdict"].get("likely", 0))
    for c in CASES:
        if c.get("contains") == "LIVE_LIKELY":
            c["contains"] = likely
    cases = [c for c in CASES if not args.only or c["group"] == args.only]
    run_id = f"{ja.MODEL}-{ja.ENGINE}-{datetime.now():%Y%m%d-%H%M%S}"
    ja.EVAL_RUN = run_id
    print(f"eval run {run_id} — {len(cases)} cases\n")

    before = _snapshot()
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['prompt'][:58]:<60}", end="", flush=True)
        res = run_case(case, run_id)
        results.append(res)
        print(f"{'PASS' if res['ok'] else 'FAIL'}  {res['seconds']:.1f}s")
        if not res["ok"]:
            print(f"        {res['reason']}")
    ja.EVAL_RUN = None

    undone = _restore(before)
    if undone:
        print(f"\ncleaned up after the run: {', '.join(undone)}")

    passed = sum(1 for r in results if r["ok"])
    print(f"\n{'=' * 68}")
    print(f"score  {passed}/{len(results)}  ({100 * passed // max(len(results), 1)}%)"
          f"   run id: {run_id}")

    by_group = {}
    for r in results:
        g = r["case"]["group"]
        by_group.setdefault(g, [0, 0])
        by_group[g][1] += 1
        by_group[g][0] += bool(r["ok"])
    print()
    for g, (ok, total) in sorted(by_group.items()):
        bar = "█" * ok + "░" * (total - ok)
        print(f"  {g:<10} {ok}/{total}  {bar}")

    if passed < len(results):
        print("\nfailures:")
        for r in results:
            if not r["ok"]:
                print(f"  · {r['case']['prompt'][:52]}")
                print(f"      {r['reason']}")
                if r["case"].get("why"):
                    print(f"      (case exists because: {r['case']['why']})")
    print(f"\ninspect calls:  select tool, ok, error from tool_calls "
          f"where eval_run = '{run_id}';")
    return 0 if passed == len(results) else 1


def compare():
    conn = psycopg2.connect(ja.dsn() if hasattr(ja, "dsn") else ja.s4.dsn())
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""select eval_run,
                          count(*) calls,
                          count(*) filter (where ok) ok,
                          round(avg(duration_ms)) avg_ms,
                          min(ts) started
                   from tool_calls where eval_run is not null
                   group by eval_run order by started desc limit 5""")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("no eval runs recorded yet — run `python eval_tools.py` first")
        return 0
    print(f"{'run':<34} {'calls':>6} {'ok':>5} {'avg ms':>7}")
    for r in rows:
        print(f"{r['eval_run']:<34} {r['calls']:>6} {r['ok']:>5} {r['avg_ms'] or 0:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
