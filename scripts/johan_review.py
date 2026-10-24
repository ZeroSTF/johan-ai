"""johan_review — read Johan's own recent failures, propose fixes for approval.

Roadmap step 6. Runs at login (systemd user timer, code only — no GPU, so it
can never hold VRAM when a game starts) or by hand:

    python johan_review.py            # analyse the last 7 days, report only (no GPU)
    python johan_review.py --stage    # + verify recipes with the model, stage for his OK
    python johan_review.py --days 30
    python johan_review.py --gate     # run the tool eval, compare to baseline

What it does NOT do: change behaviour on its own. Every fix is staged with
propose_edit and applied only when Zeros OKs it (shown as a card in the chat), and `--gate` runs
the eval so an approved batch is checked before it is trusted. A 4B model
editing its own prompt unreviewed is the fastest way to make it worse.

Two kinds of proposal, both mechanical — no model writes them:
  1. REGRESSION CASES for eval_tools.py — every real message that failed,
     so the eval (which only grows when someone remembers to add cases)
     starts covering what actually goes wrong.
  2. RECIPES for brain/recipes.md — when a turn FAILED and then found a call
     that ANSWERED it (re-run now, non-empty, and the calibrated step-done
     check agrees), that call is written down as a one-line recipe.
     Recipes are injected into the prompt (prime_with_memories). The single
     most effective fix this project found was exactly this: one line naming
     `dpkg -l | grep -c ^ii` took "what's installed" from 8 hops to 2.

It re-runs failed calls of read-only LOCAL tools (run_command, project_files)
with the exact same arguments. If the call now succeeds, the failure was
fixed since and is not proposed again — most of the first week's failures
(`2>/dev/null`, `||`) were fixed by code changes, not by recipes.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import stage4_assemble as s4  # noqa: E402  (dsn only)

ROOT = Path.home() / "johan"
REVIEWS = ROOT / "brain/reviews"
RECIPES = ROOT / "brain/recipes.md"
EVAL_FILE = Path(__file__).with_name("eval_tools.py")
EVAL_MARKER = "    # --- added by johan_review (approved in F4) ---"
RECIPE_MARKER = "<!-- johan_review adds recipes below this line; newest first -->"
BASELINE = ROOT / "brain/eval-baseline.json"
RERUNNABLE = {"run_command", "project_files"}      # read-only and local


def _conn():
    return psycopg2.connect(s4.dsn())


def _norm_error(e):
    e = re.sub(r"/[\w./-]+", "<path>", e or "")
    e = re.sub(r"\d+", "#", e)
    return e[:120]


def _turns(days):
    cur = _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""select id, ts, session_id, user_message, hop, tool, args, ok, error
                   from tool_calls
                   where eval_run is null and ts > now() - %s * interval '1 day'
                   order by id""", (days,))
    turns = defaultdict(list)
    for r in cur.fetchall():
        turns[(r["session_id"], r["user_message"])].append(r)
    return turns


def _rerun(call):
    """Re-run a read-only local call; return its result dict, or None."""
    if call["tool"] not in RERUNNABLE:
        return None
    try:
        import johan_tools as jt
        args = call["args"] if isinstance(call["args"], dict) else json.loads(call["args"] or "{}")
        fn = {"run_command": jt.run_command, "project_files": jt.project_files}[call["tool"]]
        return fn(**args)
    except Exception:
        return None


def _answered(msg, result):
    """Did this result actually contain what the question asked for?

    'ok' in tool_calls only means 'no error'. The first version turned every
    first successful call after a failure into a recipe, and most were wrong:
    `find ~/johan -name "*tf2*"` "worked" by returning nothing (searching
    ~/johan was the original TF2 bug), `ls ~/.local/bin` "answered" what's
    installed. So: non-empty output, AND the calibrated step-done check
    (10/10 on eval_decide.py) says the result contains what was asked."""
    if not isinstance(result, dict) or result.get("error"):
        return 0.0
    out = str(result.get("output") or result.get("text") or result.get("matches") or "")
    if len(out.strip()) < 3 or out.strip() in ("(no output)",):
        return 0.0
    import johan_decide as jd
    try:
        p, d = jd.check(jd.DONE_Q, f"Goal: answer “{msg[:200]}”\nTool result: {out[:1500]}",
                        yes=jd.DONE_OPTS["yes"], no=jd.DONE_OPTS["no"],
                        kind="review_recipe", null_context=jd.NULLS["step_done"])
    except Exception:
        return 0.0
    return p if d.confident else 0.0


def _still_fails(call):
    """Re-run a read-only local call. True = still fails, False = fixed
    since, None = cannot tell (not rerunnable)."""
    if call["tool"] not in RERUNNABLE:
        return None
    try:
        import johan_tools as jt
        args = call["args"] if isinstance(call["args"], dict) else json.loads(call["args"] or "{}")
        fn = {"run_command": jt.run_command, "project_files": jt.project_files}[call["tool"]]
        res = fn(**args)
        return bool(isinstance(res, dict) and res.get("error"))
    except Exception:
        return None


def analyse(days=7):
    turns = _turns(days)
    clusters = defaultdict(lambda: {"count": 0, "messages": set(), "sample": None, "fixed": None})
    long_turns, recipes, failed_msgs = [], [], []
    for (sid, msg), calls in turns.items():
        fails = [c for c in calls if not c["ok"]]
        for c in fails:
            k = (c["tool"], _norm_error(c["error"]))
            cl = clusters[k]
            cl["count"] += 1
            cl["messages"].add((msg or "")[:120])
            if cl["sample"] is None:
                cl["sample"] = c
        if max(c["hop"] for c in calls) >= 7:
            long_turns.append({"message": msg, "hops": max(c["hop"] for c in calls) + 1,
                               "failed_calls": len(fails)})
        if fails:
            failed_msgs.append(msg)
            # Failed, then found a call that worked: a recipe CANDIDATE. Only
            # read-only local calls — web results can't be re-run faithfully
            # and go stale ("storm September 2025"). Verified later, when the
            # model is available (stage_proposals), latest call first: the
            # call that answered is usually the last one, not the first.
            first_fail = min(c["id"] for c in fails)
            later_ok = [c for c in calls if c["ok"] and c["id"] > first_fail
                        and c["tool"] in RERUNNABLE]
            if later_ok:
                recipes.append({"message": msg, "candidates": list(reversed(later_ok)),
                                "failed_first": len(fails)})
    for k, cl in clusters.items():
        cl["fixed"] = _still_fails(cl["sample"]) is False
    return {"turns": len(turns), "clusters": clusters, "long_turns": long_turns,
            "recipes": recipes, "failed_messages": sorted(set(m for m in failed_msgs if m))}


def _low_confidence(days):
    cur = _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""select kind, count(*) n, count(*) filter (where not confident) unsure,
                          count(*) filter (where kind='rewrite') rewrites
                   from decisions where eval_run is null and ts > now() - %s * interval '1 day'
                   group by kind order by n desc""", (days,))
    return cur.fetchall()


def _existing_eval_prompts():
    return set(re.findall(r'prompt="((?:[^"\\]|\\.)*)"', EVAL_FILE.read_text()))


def _ensure_markers():
    """Anchors every proposal inserts after. A fixed marker line stays unique
    after each approval, so several proposals can be applied one by one — an
    anchor on the last real line would be consumed by the first."""
    t = EVAL_FILE.read_text()
    if EVAL_MARKER not in t:
        t = t.replace("\n]\n\n\ndef _snapshot", f"\n{EVAL_MARKER}\n]\n\n\ndef _snapshot", 1)
        EVAL_FILE.write_text(t)
    if not RECIPES.exists():
        RECIPES.write_text(
            "# Recipes Johan learned from its own failures\n\n"
            "Each line was proposed by johan_review.py from a real turn that failed and\n"
            "then found a call that worked, and approved by Zeros in F4. Injected into\n"
            "the system prompt. Delete any line that stops being true.\n\n"
            f"{RECIPE_MARKER}\n")


MAX_RECIPES_PER_RUN = 5


def verify_recipes(result, limit=12):
    """Keep only candidates whose call, re-run now, answers the question.
    One recipe per message; near-duplicate messages share one. Needs the
    model (the step-done check), so it runs at staging, not at login."""
    verified, seen_calls = [], set()
    for rec in result["recipes"][:limit]:
        for c in rec["candidates"][:4]:
            args = c["args"] if isinstance(c["args"], dict) else json.loads(c["args"] or "{}")
            sig = json.dumps(args, sort_keys=True)
            if sig in seen_calls:
                break
            p = _answered(rec["message"], _rerun(c))
            # 0.95, not 0.6. Measured on real turns: at 0.6, 9 "verified"
            # recipes, roughly half wrong — `ls ~/.local/share/applications/`
            # (the exact bad habit fixed by hand) scored 0.98 as an answer to
            # "what apps are installed". The check knows RELATED, not RIGHT,
            # so the threshold only trims; Zeros's y/n in F4 is the filter.
            if p >= 0.95:
                seen_calls.add(sig)
                verified.append({"message": rec["message"], "tool": c["tool"], "args": args,
                                 "failed_first": rec["failed_first"], "p": round(p, 2)})
                break
        if len(verified) >= MAX_RECIPES_PER_RUN:
            break
    return verified


def _recipe_line(r):
    args = r["args"] if isinstance(r["args"], dict) else json.loads(r["args"] or "{}")
    call = args.get("command") or args.get("query") or args.get("url") or \
        json.dumps(args, ensure_ascii=False)
    return (f"- For questions like “{(r['message'] or '')[:90]}”: `{r['tool']}` with "
            f"`{str(call)[:140]}` worked — after {r['failed_first']} failed attempt(s).")


def stage_proposals(result):
    """Stage regression cases and recipes as propose_edit edits. Returns ids."""
    import johan_tools as jt
    _ensure_markers()
    known = _existing_eval_prompts()
    pending = {(e.get("path"), e.get("new_string")) for e in jt._load_edits()}
    staged = []

    verified = verify_recipes(result)
    # A regression case needs an expectation, or it always passes and detects
    # nothing. Only messages with a VERIFIED answering call get one, and it
    # expects that call's tool.
    for rec in verified:
        msg = rec["message"]
        if msg in known or len(msg) > 200 or "\n" in msg:
            continue
        case = (f'{EVAL_MARKER}\n    dict(group="review", prompt={json.dumps(msg)},\n'
                f'         expect=[{json.dumps(rec["tool"])}],\n'
                f'         why="failed in real use, then {rec["tool"]} answered it; '
                f'added by johan_review"),')
        if ("scripts/eval_tools.py", case) in pending:
            continue
        r = jt.propose_edit("scripts/eval_tools.py", EVAL_MARKER, case,
                            f"regression case: {msg[:60]}")
        if r.get("proposed"):
            staged.append(r["edit_id"])

    recipes_text = RECIPES.read_text()
    for rec in verified:
        line = _recipe_line(rec)
        if line in recipes_text or ("brain/recipes.md", f"{RECIPE_MARKER}\n{line}") in pending:
            continue
        r = jt.propose_edit(
            "brain/recipes.md", RECIPE_MARKER, f"{RECIPE_MARKER}\n{line}",
            f"recipe (check p={rec['p']}, failed {rec['failed_first']}x first) — "
            f"reject if this call wasn't really the right way: "
            f"{(rec['message'] or '')[:50]}")
        if r.get("proposed"):
            staged.append(r["edit_id"])
    return staged


def write_report(result, low, staged, days):
    REVIEWS.mkdir(parents=True, exist_ok=True)
    path = REVIEWS / f"{datetime.now():%Y-%m-%d}.md"
    L = [f"# Johan self-review — {datetime.now():%Y-%m-%d %H:%M}",
         f"\nLast {days} days: {result['turns']} real turns. "
         f"{len(staged)} proposals staged — they show up in Johan's chat for your OK.\n",
         "## Failure clusters (tool · error · count)"]
    for (tool, err), cl in sorted(result["clusters"].items(), key=lambda kv: -kv[1]["count"]):
        tag = " — FIXED since (re-ran the same call, it works now)" if cl["fixed"] else ""
        L.append(f"- `{tool}` · {err} · ×{cl['count']}{tag}")
    L.append("\n## Turns that ran to the hop limit")
    L += [f"- {t['hops']} hops, {t['failed_calls']} failed calls: “{(t['message'] or '')[:100]}”"
          for t in result["long_turns"]] or ["- none"]
    L.append("\n## Decisions")
    L += [f"- {r['kind']}: {r['n']} made, {r['unsure']} not confident"
          + (f", {r['rewrites']} rewrites" if r["rewrites"] else "") for r in low] or ["- none logged"]
    L.append("\n## Recipe candidates")
    L += [f"- “{(r['message'] or '')[:90]}” — {len(r['candidates'])} call(s) to verify"
          for r in result["recipes"]] or ["- none"]
    L.append("\nRun `/review` in Johan (or `python johan_review.py --stage`) to verify "
             "these against the model and stage the best — at most 5 — for your OK. Expect "
             "to reject some: the check knows a result is RELATED to the question, "
             "not that it was the right way to answer it.")
    L.append("\n## After approving proposals\nRun `python johan_review.py --gate` — "
             "it runs the tool eval and compares with the stored baseline.")
    path.write_text("\n".join(L) + "\n")
    return path


def gate():
    """Run the tool eval; compare with the last accepted baseline."""
    out = subprocess.run([sys.executable, str(EVAL_FILE)], capture_output=True,
                         text=True, cwd=str(EVAL_FILE.parent)).stdout
    m = re.search(r"score\s+(\d+)/(\d+)", out)
    if not m:
        print(out[-2000:])
        return 1
    passed, total = int(m.group(1)), int(m.group(2))
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    print(out[out.find("score"):][:1500])
    # New cases join the suite, so compare FAILURES, not raw score.
    fails = total - passed
    if base and fails > base["failures"]:
        print(f"\nGATE: FAIL — {fails} failures vs baseline {base['failures']} "
              f"({base['date']}). Something approved made Johan worse; see above.")
        return 1
    BASELINE.write_text(json.dumps({"passed": passed, "total": total, "failures": fails,
                                    "date": f"{datetime.now():%Y-%m-%d %H:%M}"}))
    print(f"\nGATE: PASS — {passed}/{total}; baseline updated.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--stage", action="store_true",
                    help="verify recipe candidates with the model and stage "
                         "proposals for your OK (the login run is report-only: no GPU)")
    a = ap.parse_args()
    if a.gate:
        return gate()
    result = analyse(a.days)
    low = _low_confidence(a.days)
    staged = stage_proposals(result) if a.stage else []
    path = write_report(result, low, staged, a.days)
    fixed = sum(1 for cl in result["clusters"].values() if cl["fixed"])
    print(f"{result['turns']} turns · {len(result['clusters'])} failure clusters "
          f"({fixed} fixed since) · {len(result['recipes'])} recipe candidates · "
          f"{len(staged)} proposals staged · report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
