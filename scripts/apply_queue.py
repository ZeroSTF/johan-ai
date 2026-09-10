"""Build an apply queue: the jobs worth applying to, best fit first, each with
a compiled CV and the posting link.

This is the hand-off point of the whole pipeline. Riadh applies himself, so
all Johan owes him is: which jobs, in what order, the link, and a verified
PDF to upload. Nothing here submits anything.

Ranking: each job gets a fit score = the total keyword overlap between the
posting and the skills claimed by his own fact-store bullets, plus a count of
how many distinct skills of his the posting actually mentions. This is a
heuristic, not truth — it favours verbose postings that repeat technology
names. Treat the order as "read these first", not "objectively best".

Experience fit IS enforced (added 2026-09-11 after a queue full of senior
roles): postings asking for years or seniority beyond his are dropped, and
postings matching almost none of his skills are dropped.

Usage:
    python apply_queue.py                        # top 10 likely, ranked by fit
    python apply_queue.py --limit 20
    python apply_queue.py --verdict unclear      # the "nobody said no" pile
    python apply_queue.py --job-id 837 --job-id 179
"""

import argparse
import json
import re
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

import stage4_assemble as s4
import stage5_verify as s5
from report import BASIS_PRIORITY, norm_company, norm_title

ROOT = Path.home() / "johan"
OUT_DIR = ROOT / "job-hunter/staged"


# Surface forms for known_gaps entries whose token doesn't appear literally in
# postings. Word-boundary matching on "go" alone never catches "Golang", and
# "k8s" never matches "kubernetes" — without these the penalty below silently
# does nothing on exactly the roles it exists to catch.
GAP_ALIASES = {
    "go": ["go", "golang"],
    "kubernetes": ["kubernetes", "k8s"],
    "postgresql": ["postgresql", "postgres"],
    "react": ["react", "reactjs", "react.js"],
    "mongodb": ["mongodb", "mongo"],
    "elasticsearch": ["elasticsearch", "elastic search", "opensearch"],
    "kafka": ["kafka"],
    "grpc": ["grpc"],
    "graphql": ["graphql"],
    "redis": ["redis"],
    "rust": ["rust"],
    "scala": ["scala"],
    "kotlin": ["kotlin"],
    "gcp": ["gcp", "google cloud"],
    "azure": ["azure"],
}


def gap_signal(facts, job):
    """Which of his known_gaps the posting asks for, split by where it asks.

    A gap in the TITLE ("Golang Software Engineer", "Senior Software Engineer -
    Python/MongoDB") means the gap IS the job, and no amount of adjacent
    backend-keyword overlap makes it a good application. A gap in the body is
    usually one bullet in a requirements list and is survivable."""
    title = (job["title"] or "").lower()
    desc = (job["raw_desc"] or "").lower()
    in_title, in_desc = [], []
    for gap in facts["known_gaps"]:
        pats = [re.compile(r"\b" + re.escape(f) + r"\b")
                for f in GAP_ALIASES.get(gap, [gap])]
        # Check EVERY alias against the title before falling back to the
        # description. Checking one alias against both in turn meant a plain
        # "go" found in the body short-circuited before "golang" was ever
        # tried against the title — so "C, Golang Software Engineer" was
        # scored as a passing mention instead of a gap-shaped role.
        if any(p.search(title) for p in pats):
            in_title.append(gap)
        elif any(p.search(desc) for p in pats):
            in_desc.append(gap)
    return sorted(set(in_title)), sorted(set(in_desc))


TUNISIA = re.compile(r"\btunis(ia|ie)?\b|\btn\b", re.I)

# "8+ years of experience", "minimum 5 years' experience", "3-5 years exp".
# Extracted in CODE, not read from Stage 2's `min_years`: the 3B model found
# only 2 of the 5 postings in one queue that actually stated a figure, missing
# "5+ years of experience" and "7+ years of experience" outright. Same rule as
# everywhere else here — the model locates evidence, code decides.
YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*\d{0,2}\s*\+?\s*years?[^.\n]{0,45}?"
    r"(?:experience|exp\b|background|working)", re.I)

SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|lead|principal|staff|architect|"
                           r"head\s+of|director|vp)\b", re.I)


def years_required(job):
    """Highest years-of-experience figure the posting states, or None.

    Max rather than min: a posting saying both "3+ years with Python" and
    "8+ years overall" is an 8-year job. Values above 20 are discarded as
    noise (dates, "20 years in business" marketing)."""
    found = [int(m.group(1)) for m in YEARS_RE.finditer(job["raw_desc"] or "")]
    found = [v for v in found if 0 < v <= 20]
    return max(found) if found else None


def experience_tier(facts, job):
    """0 = fits, 1 = stretch, 2 = over-reach.

    This existed as data and was never used: `min_years` appeared nowhere in
    the ranking and `seniority` only in the printed line, so a posting asking
    for 7+ years ranked purely on keyword overlap and landed in the queue.
    Riadh has ~3 years; being handed senior roles wasted his time reading
    them."""
    his = int(facts["owner"].get("years_professional_experience") or 3)
    yrs = years_required(job)
    seniority = ((job["parsed"] or {}).get("seniority") or "").lower()
    title_senior = bool(SENIOR_TITLE.search(job["title"] or ""))

    if yrs is not None:
        if yrs >= his + 3:          # 6+ for a 3-year engineer
            return 2, f"asks {yrs}+ yrs, you have {his}"
        if yrs > his:
            return 1, f"asks {yrs} yrs, you have {his}"
    if seniority in ("staff_plus", "lead"):
        return 2, f"{seniority.replace('_', ' ')} role"
    if seniority == "senior" or title_senior:
        return 1, "senior title, no years stated"
    return 0, None


def mode_weight(facts, job):
    """Apply the work-mode preference already written in bullets.yaml's
    `geography.mode_weight`, which was dead config until now.

    His goals in order are (1) remote for a foreign employer while living in
    Tunisia, (2) sponsored relocation, (3) local Tunisian roles. Ranking on
    eligibility + skill fit alone put a local Tunis role first — correct on
    both of those axes, and still the lowest-paid outcome of the three. The
    weight encodes the pay-to-friction preference so foreign-remote wins ties
    without local roles being dropped entirely."""
    w = facts["geography"]["mode_weight"]
    parsed = job["parsed"] or {}
    mode = (parsed.get("work_mode") or "").lower()
    local = bool(TUNISIA.search(job["location"] or "")) or job["source"] == "keejob"

    if local:
        if mode == "remote":
            return w.get("remote_tunisia_employer", 0.60)
        if mode == "hybrid":
            return w.get("hybrid_tunisia", 0.55)
        return w.get("onsite_tunisia", 0.50)
    if mode == "remote":
        return w.get("remote_foreign_employer", 1.00)
    if mode == "hybrid":
        return w.get("hybrid_foreign_relocate", 0.85)
    return w.get("onsite_foreign_relocate", 0.80)


def basis_rank(job):
    """How much the eligibility evidence justifies spending an application on
    this. Reuses report.py's ordering rather than inventing a second one.

    This is the PRIMARY sort key, ahead of skill fit, because a perfect
    skills match on a job that only says "restricted location but sponsorship
    offered" is worth less than a decent match on one that actually says it
    hires worldwide. Ranking on keyword overlap alone put a Mexico-only role
    at the top of the first queue this script produced."""
    b = (job["parsed"] or {}).get("verdict_basis") or ""
    try:
        return BASIS_PRIORITY.index(b)
    except ValueError:
        return len(BASIS_PRIORITY)


def fit_score(facts, job):
    """Total overlap between the posting and the skills his bullets claim,
    plus how many distinct skills of his the posting mentions."""
    needle = f"{job['title']} {job['title']} {job['title']} {job['raw_desc'] or ''}".lower()
    total = 0
    matched = set()
    for b in facts["bullets"]:
        s = s4.score_bullet(b, needle)
        total += s
        if s:
            for tok in b.get("skills", []):
                if s4.skill_matches(tok, needle):   # same matcher as scoring
                    matched.add(tok)
    return total, sorted(matched)


def fetch(args):
    conn = psycopg2.connect(s4.dsn())
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if args.job_id:
        cur.execute("select id, source, company, title, url, location, raw_desc, parsed "
                    "from jobs where id = any(%s)", (args.job_id,))
    else:
        cur.execute("select id, source, company, title, url, location, raw_desc, parsed "
                    "from jobs where parsed->>'tunisia_verdict' = %s and closed_at is null",
                    (args.verdict,))
    rows = cur.fetchall()
    conn.close()
    return rows


def build(args):
    # Read options defensively. `build()` is called both from argparse and
    # from the build_apply_queue tool, which constructs a Namespace by hand —
    # adding --min-fit/--include-overreach to the CLI broke that tool with
    # AttributeError, dispatch handed the error to the model, and the model
    # quietly fell back to search_jobs (which has no experience filter) and
    # recommended senior roles again. A new flag must never be able to do
    # that, so defaults live here, not only in the parser.
    min_fit = getattr(args, "min_fit", 5)
    include_overreach = getattr(args, "include_overreach", False)
    facts = s4.load_facts()
    verify_facts, by_id = s5.load_facts()

    jobs = fetch(args)
    if not jobs:
        print("No matching jobs.")
        return []

    ranked = []
    dropped_overreach = 0
    dropped_lowfit = 0
    for job in jobs:
        raw, matched = fit_score(facts, job)
        gaps_title, gaps_desc = gap_signal(facts, job)
        weight = mode_weight(facts, job)
        score = round(raw * weight, 1)
        tier, why = experience_tier(facts, job)
        # A raw fit of 0 means not one of his skill terms appears in the
        # posting. Once over-reach roles were filtered out, the tail filled
        # with "Industrial Engineer", "Field Service Engineer", "Business
        # Developer" and "Assistante Développement" — non-software roles that
        # got through Stage 1's title filter on the word "engineer". Floor on
        # RAW fit, before the work-mode weight, so a legitimate local role
        # isn't cut just for being Tunisian (×0.50).
        if raw < min_fit:
            dropped_lowfit += 1
            continue
        # Over-reach is dropped, not just demoted: he read a queue full of
        # senior/7-years roles and it wasted his time. 87 of 130 `likely`
        # rows are mid/junior with <=5 years or none stated, so excluding
        # these does not empty the queue. --include-overreach shows them.
        if tier == 2 and not include_overreach:
            dropped_overreach += 1
            continue
        ranked.append((score, matched, gaps_title, gaps_desc, job, raw,
                        weight, tier, why))
    # Sort: strongest eligibility evidence first, then "is the job itself a
    # skill he doesn't have", then weighted skill fit.
    # experience fit ranks above raw keyword score: a perfect skills match on
    # a role wanting twice his experience is not a better application.
    ranked.sort(key=lambda x: (basis_rank(x[4]), x[7], 1 if x[2] else 0,
                                -x[0], x[4]["id"]))

    # Collapse the same role posted on a company ATS and on an aggregator —
    # different ids, different company spellings ("canonical" vs
    # "Canonical Ltd."), same job. Without this the queue hands him two links
    # and two CVs for one role, which reads as careless to the employer.
    # Reuses report.py's normalisation so there's one definition of "same job".
    if not args.job_id:
        seen, deduped = set(), []
        for entry in ranked:
            job = entry[4]
            key = (norm_company(job["company"]), norm_title(job["title"]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        dropped = len(ranked) - len(deduped)
        if dropped:
            print(f"(collapsed {dropped} cross-source duplicate postings)")
        ranked = deduped[:args.limit]

    if dropped_overreach:
        print(f"(excluded {dropped_overreach} roles asking for years/seniority "
              f"beyond his experience — --include-overreach to see them)")
    if dropped_lowfit:
        print(f"(excluded {dropped_lowfit} roles with almost no skill overlap "
              f"— --min-fit 0 to see them)")

    rows = []
    for (score, matched, gaps_title, gaps_desc, job, raw, weight,
         tier, why) in ranked:
        out_dir = s4.assemble_one(facts, job)
        problems = s5.verify_one(out_dir, verify_facts, by_id)
        pdf = out_dir / "cv.pdf"
        rows.append({
            "job_id": job["id"], "company": job["company"], "title": job["title"],
            "url": job["url"], "location": job["location"],
            "seniority": (job["parsed"] or {}).get("seniority"),
            "hiring_type": (job["parsed"] or {}).get("hiring_type"),
            "verdict": (job["parsed"] or {}).get("tunisia_verdict"),
            "basis": (job["parsed"] or {}).get("verdict_basis"),
            "eligibility_quote": (job["parsed"] or {}).get("eligibility_quote"),
            "fit_score": score, "raw_fit": raw, "mode_weight": weight,
            "years_required": years_required(job),
            "experience": "stretch" if tier == 1 else "fits",
            "experience_note": why,
            "matched_skills": matched,
            "gaps_in_title": gaps_title, "gaps_in_desc": gaps_desc,
            "pdf": str(pdf) if pdf.exists() else None,
            "verified": not problems, "problems": problems,
        })
        gap_note = f"  !gap-in-title: {','.join(gaps_title)}" if gaps_title else ""
        if why:
            gap_note += f"  !{why}"
        print(f"[{job['id']:>4}] fit {score:>5} (x{weight:.2f})  "
              f"{job['company'][:20]:<20} {job['title'][:40]:<40} "
              f"{'OK' if not problems else 'FAILED'}{gap_note}")
    return rows


def write_json(rows, verdict):
    """Structured sidecar next to the markdown.

    The markdown is for reading; this is for the UI, which needs to show a
    table you can act on. Parsing the markdown back would be silly."""
    path = OUT_DIR / f"apply-queue-{date.today()}.json"
    path.write_text(json.dumps(
        {"generated": str(date.today()), "verdict": verdict, "jobs": rows},
        indent=1))
    return path


def latest_queue():
    """Most recent queue as data, or None. Used by the UI at startup."""
    files = sorted(OUT_DIR.glob("apply-queue-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (ValueError, OSError):
        return None


def write_markdown(rows, verdict):
    path = OUT_DIR / f"apply-queue-{date.today()}.md"
    lines = [
        f"# Apply queue — {date.today()}",
        "",
        f"{len(rows)} jobs, `{verdict}` eligibility, best fit first.",
        "",
        "Each row: open the link, upload the PDF, apply. Nothing here has been "
        "submitted — that's your call, every time.",
        "",
    ]
    for i, r in enumerate(rows, 1):
        flag = "" if r["verified"] else "  **CV FAILED VERIFICATION — do not send**"
        lines += [
            f"## {i}. {r['company']} — {r['title']}{flag}",
            "",
            f"- **Apply:** {r['url']}",
            f"- **CV:** `{r['pdf']}`",
            f"- **Why it's eligible:** {r['basis'] or 'unknown'}",
            f"- Fit {r['fit_score']} (raw {r['raw_fit']} × work-mode "
            f"{r['mode_weight']}) · {r['seniority'] or '?'} · "
            f"{r['hiring_type'] or '?'} · {r['location'] or 'no location'}",
            f"- Matched skills: {', '.join(r['matched_skills'][:12]) or 'none'}",
            "",
        ]
        if r["experience_note"]:
            lines += [f"- ⚠ **Experience stretch: {r['experience_note']}**", ""]
        if r["gaps_in_title"]:
            lines += [f"- ⚠ **The role is built around a skill you don't have: "
                      f"{', '.join(r['gaps_in_title'])}** — expect this to be a stretch.", ""]
        elif r["gaps_in_desc"]:
            lines += [f"- Mentions, but isn't built around: "
                      f"{', '.join(r['gaps_in_desc'][:8])}", ""]
        if r["eligibility_quote"] and r["eligibility_quote"] != "not stated":
            lines += [f"  > {r['eligibility_quote'][:300]}", ""]
        if r["problems"]:
            lines += ["- Problems:"] + [f"  - {p}" for p in r["problems"]] + [""]
    path.write_text("\n".join(lines))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdict", default="likely")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--job-id", type=int, action="append", default=[])
    ap.add_argument("--min-fit", type=int, default=5,
                    help="drop postings matching fewer than N of his skill "
                         "terms (0 disables)")
    ap.add_argument("--include-overreach", action="store_true",
                    help="also show roles asking for more years than he has")
    args = ap.parse_args()

    rows = build(args)
    if rows:
        write_json(rows, args.verdict)
        path = write_markdown(rows, args.verdict)
        ok = sum(1 for r in rows if r["verified"])
        print(f"\n{ok}/{len(rows)} verified. Queue: {path}")


if __name__ == "__main__":
    main()
