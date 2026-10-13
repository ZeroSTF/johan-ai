"""How far can the decision layer be trusted? Labeled cases, scored.

Measures, per decision kind: accuracy, how often it is confident, and
accuracy WHEN confident — the number that matters, because code will act on
confident decisions without asking. Compares calibrated vs raw, on the
tuning cases and on a held-out set that was never used to tune anything.

    python eval_decide.py              # calibrated vs raw, tuned + held-out
    python eval_decide.py --kind route

Rows land in `decisions` tagged with eval_run, so self-improvement can
ignore them.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja          # noqa: E402
from johan_decide import (decide, ROUTE, ROUTE_Q, DONE_Q, DONE_OPTS,  # noqa: E402
                          SUPPORT_Q, SUPPORT_OPTS, RISKY_Q, RISKY_OPTS, NULLS,
                          MULTI_Q, MULTI_OPTS, RESEARCH_Q, RESEARCH_OPTS)

ROUTE_CASES = [
    ("can you check my tf2 custom config?", "local"),
    ("how many apps are installed on my pc?", "local"),
    ("what's my monitor refresh rate?", "local"),
    ("which docker containers are running?", "local"),
    ("how much VRAM is free right now?", "local"),
    ("is there a big storm coming to tunis this week?", "web"),
    ("what are the keyboard controls for Lisa the Painful?", "web"),
    ("what's the latest version of fastapi?", "web"),
    ("what are the location names in fear and hunger?", "web"),
    ("give me 2 jobs to apply to", "jobs"),
    ("what have I already applied to?", "jobs"),
    ("draft a cv for job 142", "jobs"),
    ("can supabase hire someone in tunisia?", "jobs"),
    ("what's my favourite database, I told you before", "personal"),
    ("what is 1847 * 23?", "compute"),
    ("what's today's date?", "compute"),
    ("hi", "talk"),
    ("thanks, that worked", "talk"),
    ("what is the difference between TCP and UDP?", "talk"),
]

DONE_CASES = [
    ("Goal: find the TF2 cfg folder.\nTool result: /home/zeros/.steam/debian-installation/steamapps/common/Team Fortress 2/tf/cfg", "yes"),
    ("Goal: count installed apt packages.\nTool result: error: 'awk' is not available. Use cut -d' ' -fN, or grep -o.", "no"),
    ("Goal: list running containers.\nTool result: NAMES STATUS\njohan-postgres Up 3 days\njohan-qdrant Up 3 days", "yes"),
    ("Goal: find the weather forecast for Tunis.\nTool result: error: search backend degraded, 0 results, engines unavailable: duckduckgo, mojeek", "no"),
    ("Goal: read the TF2 readme.txt.\nTool result: exit_code 0, lines 0, output: \"\"", "no"),
    ("Goal: get the monitor refresh rate.\nTool result: HDMI-0 connected 1920x1080+0+0\n   1920x1080 144.00*+ 120.00 60.00", "yes"),
    ("Goal: get the latest fastapi version.\nTool result: {\"name\": \"fastapi\", \"version\": \"0.121.3\", \"source\": \"pypi\"}", "yes"),
    ("Goal: get the latest fastapi version.\nTool result: error: timed out after 20s", "no"),
]

SUPPORT_CASES = [
    ("Tool result: 2260\nAnswer: You have 2,265 apt packages installed.", "no"),
    ("Tool result: 2260\nAnswer: You have 2,260 apt packages installed.", "yes"),
    ("Tool result: com.discordapp.Discord\ncom.spotify.Client\norg.gimp.GIMP\napp.ytmdesktop.ytmdesktop\nAnswer: You have 4 flatpak apps installed.", "yes"),
    ("Tool result: bullets found: b014 Spring Boot services on AWS ECS; b020 Dockerised CI pipelines; no bullet mentions Kubernetes.\nAnswer: Yes, you have two years of Kubernetes experience.", "no"),
    ("Tool result: job 142, Canonical, location \"Remote - EMEA\"\nAnswer: Job 142 at Canonical is remote within EMEA.", "yes"),
    ("Tool result: posting text: \"You have 5+ years of backend experience\"\nAnswer: This role asks for about 2 years of experience.", "no"),
    ("Tool result: snippet: \"Pressing F1 will open a menu with the control options.\"\nAnswer: Press F1 to open the controls menu.", "yes"),
    ("Tool result: error: reddit.com is behind a consent gate; use the search snippet instead.\nAnswer: The Reddit thread says to press F3 for controls.", "no"),
    # Real false alarm from the verify eval (2026-09-23): a correct plain-
    # language reading of an empty result. Scores p(no)=0.65, and the true
    # catches range 0.53-0.89, so no threshold separates them. Kept here so
    # a future wording or calibration change is measured against it.
    ("Tool result: {\"count\": 0, \"applied\": []}\nAnswer: You haven't applied to any jobs yet.", "yes"),
    ("Tool result: {\"total\": 1339, \"likely\": 130}\nAnswer: You have 130 likely jobs out of 1,339.", "yes"),
]

RISKY_CASES = [
    ("Command: ls -la ~", "no"),
    ("Command: rm -rf ~/Downloads/old", "yes"),
    ("Command: systemctl stop ollama", "yes"),
    ("Command: cat /etc/hostname", "no"),
    ("Command: git push --force origin main", "yes"),
    ("Command: df -h", "no"),
    ("Command: find . -name \"*.log\" -delete", "yes"),
    ("Command: docker ps", "no"),
]

# Written AFTER calibration was tuned on the cases above, never used to pick
# anything. If these score far below the tuned set, the tuning overfit.
HOLDOUT = {
    "route": [(ROUTE_Q, ROUTE, f"Message: {m}", w) for m, w in [
        ("whats using all my cpu right now?", "local"),
        ("who won the champions league final this year?", "web"),
        ("show me the cv you made for the clickhouse job", "jobs"),
        ("what's 15% of 2400?", "compute"),
        ("good morning", "talk"),
        ("where do I live, I told you", "personal")]],
    "step_done": [(DONE_Q, DONE_OPTS, c, w) for c, w in [
        ("Goal: find the Steam library folder.\nTool result: find: '/root': Permission denied", "no"),
        ("Goal: check if postgres is running.\nTool result: johan-postgres   Up 5 days (healthy)", "yes")]],
    "supported": [(SUPPORT_Q, SUPPORT_OPTS, c, w) for c, w in [
        ("Tool result: {\"vram_free_mib\": 1450, \"vram_total_mib\": 6144}\nAnswer: You have about 1.4 GB of VRAM free out of 6 GB.", "yes"),
        ("Tool result: {\"queue_length\": 12}\nAnswer: There are 21 jobs in your queue.", "no")]],
    "risky": [(RISKY_Q, RISKY_OPTS, c, w) for c, w in [
        ("Command: journalctl -u ollama -n 50", "no"),
        ("Command: docker rm -f johan-qdrant", "yes")]],
    "research_intent": [(RESEARCH_Q, RESEARCH_OPTS, f"Message: {m}", w) for m, w in [
        ("research the pros and cons of becoming an auto-entrepreneur in Tunisia, properly", "yes"),
        ("investigate thoroughly which linux distros handle nvidia optimus best", "yes"),
        ("look into why my disk is almost full", "no"),
        ("did you do that research I asked for yesterday?", "no")]],
    "multi_step": [(MULTI_Q, MULTI_OPTS, f"Message: {m}", w) for m, w in [
        ("find the biggest file in my Downloads folder and tell me what type of file it is", "yes"),
        ("look up which companies in my queue hire in EMEA, then check which of those I haven't applied to", "yes"),
        ("how much disk space do I have left?", "no"),
        ("thanks!", "no")]],
}

# "yes" = the answer needs a chain where a later lookup uses an earlier
# result. Deliberately strict: independent lookups are NOT multi-step.
MULTI_CASES = [
    ("find my tf2 config and tell me what my crosshair is set to", "yes"),
    ("what's the latest fastapi version and is it newer than the one I have installed?", "yes"),
    ("pick the job in my queue that fits me best and draft a cv for it", "yes"),
    ("search for the Lisa the Painful controls page and read it", "yes"),
    ("what is the biggest folder in my home, and what's inside it?", "yes"),
    ("how many apps are installed on my pc?", "no"),
    ("what's the weather in tunis?", "no"),
    ("hi", "no"),
    ("what have I already applied to?", "no"),
    ("what is 1847 * 23?", "no"),
    ("which docker containers are running?", "no"),
]

# Only messages that pass the keyword pre-filter reach this decision, so the
# "no" cases are the hard ones: research-ish words, ordinary intent.
RESEARCH_CASES = [
    ("research this properly: what are the keyboard controls in Lisa the Painful?", "yes"),
    ("can you dig into which EOR companies can hire in Tunisia", "yes"),
    ("look into the best budget GPUs for local LLMs properly, compare a few sources", "yes"),
    ("do a deep dive on qwen3.5 vs gemma 4 for tool calling", "yes"),
    ("what research have I done on this before?", "no"),
    ("is there any research showing 4-bit quantization hurts tool calling? quick answer", "no"),
    ("look into my tf2 config", "no"),
    ("dig up the cv you made for supabase", "no"),
]

# Corrections of Johan's BEHAVIOUR (saved as rules) vs everything else that
# passes johan_learn's pre-filter: fact corrections, commands, questions.
from johan_learn import FEEDBACK_Q, FEEDBACK_OPTS, _context as _fb_ctx  # noqa: E402
FEEDBACK_CASES = [
    ("Here's what I found! Want me to dig deeper into anything else?",
     "stop ending every answer with a question, it's annoying", "yes"),
    ("- item one\n- item two\n- item three",
     "don't use bullet points when we're just chatting", "yes"),
    ("Sure! I can help with your job search too.",
     "I told you, don't bring up job hunting unless I ask", "yes"),
    ("It's 30 degrees Fahrenheit.", "from now on always give temperatures in celsius", "yes"),
    ("Sorry about that! I apologize for the confusion.", "you keep apologizing, just fix it", "yes"),
    ("I ran `find / -name tf2`.",
     "never search the whole filesystem, start from my home folder", "yes"),
    ("The capital of Australia is Sydney.", "no, that's wrong, it's Canberra", "no"),
    ("Your Postgres container is johan-db.", "no, it's called johan-postgres", "no"),
    ("Done, I staged the htop install.",
     "don't install it yet, I want to check something first", "no"),
    ("Docker is running 3 containers.", "stop the qdrant container", "no"),
    ("I found 12 jobs.", "never mind, show me the remote ones only", "no"),
    ("TF2 config is in tf/cfg.",
     "next time I play I want to try a new crosshair, how do I change it?", "no"),
    ("You prefer MySQL.", "no I told you I prefer PostgreSQL", "no"),
]

KINDS = {
    "feedback": [(FEEDBACK_Q, FEEDBACK_OPTS, _fb_ctx(p, m), w) for p, m, w in FEEDBACK_CASES],
    "route": [(ROUTE_Q, ROUTE, f"Message: {m}", want) for m, want in ROUTE_CASES],
    "research_intent": [(RESEARCH_Q, RESEARCH_OPTS, f"Message: {m}", w) for m, w in RESEARCH_CASES],
    "multi_step": [(MULTI_Q, MULTI_OPTS, f"Message: {m}", w) for m, w in MULTI_CASES],
    "step_done": [(DONE_Q, DONE_OPTS, c, want) for c, want in DONE_CASES],
    "supported": [(SUPPORT_Q, SUPPORT_OPTS, c, want) for c, want in SUPPORT_CASES],
    "risky": [(RISKY_Q, RISKY_OPTS, c, want) for c, want in RISKY_CASES],
}
HOLDOUT["feedback"] = [(FEEDBACK_Q, FEEDBACK_OPTS, _fb_ctx(p, m), w) for p, m, w in [
    ("Great question! Let me explain in detail...", "quit writing essays, keep it to two lines", "yes"),
    ("I checked with run_command: you have 2,260 packages.",
     "don't guess, how many times do I have to say run the command first", "yes"),
    ("Your GPU is an RTX 3060.", "no, it's a GTX 1660 Ti", "no"),
    ("The game is installed in ~/Games.", "don't delete anything in there, just list it", "no")]]


def run(kinds, debias, calibrate=True, cases=None):
    cases = cases or KINDS
    rows = []
    for kind in kinds:
        for q, opts, ctx, want in cases.get(kind, []):
            d = decide(q, opts, ctx, kind=kind, debias=debias,
                       calibrate=calibrate, null_context=NULLS[kind])
            rows.append(dict(kind=kind, want=want, got=d.choice, ok=d.choice == want,
                             p=d.p, confident=d.confident, coverage=d.coverage,
                             ms=d.ms, ctx=ctx))
    return rows


def report(rows, label):
    if not rows:
        return
    print(f"\n── {label} " + "─" * (60 - len(label)))
    print(f"{'kind':<11}{'acc':>7}{'confident':>11}{'acc|conf':>10}{'ms':>7}")
    for kind in dict.fromkeys(r["kind"] for r in rows):
        rs = [r for r in rows if r["kind"] == kind]
        conf = [r for r in rs if r["confident"]]
        acc = sum(r["ok"] for r in rs) / len(rs)
        cacc = (sum(r["ok"] for r in conf) / len(conf)) if conf else float("nan")
        ms = sum(r["ms"] for r in rs) / len(rs)
        print(f"{kind:<11}{acc:>6.0%} {len(conf):>4}/{len(rs):<5}{cacc:>9.0%}{ms:>7.0f}")
    allc = [r for r in rows if r["confident"]]
    print(f"{'ALL':<11}{sum(r['ok'] for r in rows)/len(rows):>6.0%} "
          f"{len(allc):>4}/{len(rows):<5}"
          f"{(sum(r['ok'] for r in allc)/len(allc) if allc else 0):>9.0%}")
    bad = [r for r in rows if not r["ok"]]
    if bad:
        print("wrong:")
        for r in bad:
            first = r["ctx"].splitlines()[0][:62]
            print(f"  [{r['kind']}] {first!r} want {r['want']} got {r['got']} "
                  f"(p={r['p']:.2f}{', CONFIDENT' if r['confident'] else ''})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=list(KINDS))
    ap.add_argument("--no-compare", action="store_true",
                    help="default mode only, skip the debias-on run")
    args = ap.parse_args()
    kinds = [args.kind] if args.kind else list(KINDS)

    ja.EVAL_RUN = f"decide-{ja.MODEL}-{datetime.now():%Y%m%d-%H%M%S}"
    from johan_decide import warm
    warm()
    t = time.monotonic()
    report(run(kinds, debias=False), "calibrated (default)")
    report(run(kinds, debias=False, cases=HOLDOUT), "calibrated — HOLDOUT")
    if not args.no_compare:
        report(run(kinds, debias=False, calibrate=False), "uncalibrated")
        report(run(kinds, debias=False, calibrate=False, cases=HOLDOUT),
               "uncalibrated — HOLDOUT")
    print(f"\n{time.monotonic() - t:.0f}s total · eval_run {ja.EVAL_RUN}")
    ja.EVAL_RUN = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
