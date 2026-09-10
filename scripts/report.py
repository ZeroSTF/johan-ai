"""Turn the parsed jobs table into a shortlist you can actually work through.

Handles the cross-source duplicates Stage 1 cannot catch: the same role posted
on a company ATS and on an aggregator has different ids, so hash dedupe misses
it. Here we collapse on normalised company + title and keep the row with the
strongest verdict.

Usage:
    python report.py                 # write the shortlist
    python report.py --min-desc 400  # skip stub postings
"""

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

OUT = Path.home() / "johan/job-hunter/staged"

VERDICT_RANK = {"likely": 0, "unclear": 1, "unlikely": 2}

# Basis strings ordered by how much they justify spending time
BASIS_PRIORITY = [
    "names Tunisia or a targeted MENA market",
    "worldwide stated in location/title field",
    "worldwide stated in a quoted role sentence",
    "location field states no geographic restriction",
    "sponsorship or relocation offered",
    "restricted location but sponsorship offered",
    "contractor engagement, no restriction found",
    "no eligibility information found",
]

SUFFIXES = re.compile(
    r"\b(ltd|ltd\.|limited|inc|inc\.|llc|gmbh|bv|b\.v\.|ag|sa|s\.a\.|"
    r"plc|corp|corporation|co|company|group|solutions|technologies|"
    r"operations|holdings|labs|software|third|referrals)\b", re.I)


# Board tokens arrive glued together with no separator ("monzoreferrals",
# "cloudbedsthird"), so a word-boundary regex cannot see them. Strip these
# after collapsing to bare alphanumerics.
GLUED_SUFFIX = re.compile(
    r"(referrals?|thirdparty|third|careers?|jobs?|hiring|"
    r"ltd|limited|inc|llc|gmbh|corp|group)$")


def norm_company(name):
    s = (name or "").lower()
    s = SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    for _ in range(3):                     # e.g. "acmeincgroup"
        s2 = GLUED_SUFFIX.sub("", s)
        if s2 == s or len(s2) < 3:
            break
        s = s2
    return s


def norm_title(title):
    s = (title or "").lower()
    s = re.sub(r"\((?:f/m/d|m/w/d|m/f/d|f/m/x|100\s*%\s*remote)[^)]*\)", " ", s)
    s = re.sub(r"\b(senior|sr|snr|junior|jr|lead|staff|principal)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def dsn():
    env = {}
    for line in (Path.home() / "johan/compose/.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return (f"dbname=johan user=johan password={env['POSTGRES_PASSWORD']} "
            f"host=127.0.0.1 port=5432")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-desc", type=int, default=300)
    args = ap.parse_args()

    conn = psycopg2.connect(dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, source, company, title, location, url,
                   length(raw_desc) AS desc_len, parsed
            FROM jobs
            WHERE status = 'parsed' AND closed_at IS NULL AND length(raw_desc) >= %s
            ORDER BY id
        """, (args.min_desc,))
        rows = cur.fetchall()
    conn.close()

    # collapse cross-source duplicates
    best = {}
    dupes = 0
    for r in rows:
        p = r["parsed"] or {}
        key = (norm_company(r["company"]), norm_title(r["title"]))
        cur_rank = (VERDICT_RANK.get(p.get("tunisia_verdict"), 3),
                    -(r["desc_len"] or 0))
        if key in best:
            dupes += 1
            prev = best[key]
            pp = prev["parsed"] or {}
            prev_rank = (VERDICT_RANK.get(pp.get("tunisia_verdict"), 3),
                         -(prev["desc_len"] or 0))
            if cur_rank >= prev_rank:
                continue
        best[key] = r

    jobs = list(best.values())

    # group by verdict then basis
    groups = defaultdict(lambda: defaultdict(list))
    for r in jobs:
        p = r["parsed"] or {}
        groups[p.get("tunisia_verdict", "?")][p.get("verdict_basis", "?")].append(r)

    def basis_order(b):
        return BASIS_PRIORITY.index(b) if b in BASIS_PRIORITY else 99

    lines = [f"# Johan shortlist — {date.today().isoformat()}", ""]
    n_likely = sum(len(v) for v in groups.get("likely", {}).values())
    n_unclear = sum(len(v) for v in groups.get("unclear", {}).values())
    lines += [
        f"{len(jobs)} unique postings after collapsing {dupes} duplicates.",
        f"**{n_likely} likely** — apply to these.",
        f"**{n_unclear} unclear** — nobody said no; ask before applying.",
        "",
        "---",
        "",
    ]

    headings = {
        "likely": "## Likely eligible — apply",
        "unclear": "## Unclear — worth one email first",
    }

    for verdict in ("likely", "unclear"):
        if verdict not in groups:
            continue
        lines += [headings[verdict], ""]
        for basis in sorted(groups[verdict], key=basis_order):
            bucket = groups[verdict][basis]
            lines += [f"### {basis} ({len(bucket)})", ""]
            for r in sorted(bucket, key=lambda x: (x["company"] or "").lower()):
                p = r["parsed"] or {}
                skills = ", ".join((p.get("must_have") or [])[:6])
                yrs = p.get("min_years")
                bits = [p.get("seniority", "?"), p.get("hiring_type", "?")]
                if yrs:
                    bits.append(f"{yrs}+ yrs")
                lines.append(
                    f"- **{r['company']}** — [{r['title']}]({r['url']})  \n"
                    f"  `{' | '.join(bits)}` · {r['location'] or 'no location'}"
                    f" · via {r['source']}")
                if skills:
                    lines.append(f"  Skills: {skills}")
                quote = (p.get("eligibility_quote") or "").strip()
                if quote and quote.lower() != "not stated":
                    lines.append(f"  > {quote[:180]}")
                lines.append("")
        lines.append("---")
        lines.append("")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"shortlist-{date.today().isoformat()}.md"
    path.write_text("\n".join(lines))

    print(f"{len(rows)} parsed rows -> {len(jobs)} unique "
          f"({dupes} duplicates collapsed)")
    print(f"  likely  {n_likely}")
    print(f"  unclear {n_unclear}")
    print(f"\nwritten to {path}")


if __name__ == "__main__":
    main()
