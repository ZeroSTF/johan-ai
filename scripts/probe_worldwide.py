"""Probe candidate ATS boards to find Canonical-shaped employers.

The core finding of this project is that ONE variable decides whether Riadh
can be hired: does the company hire home-based worldwide as policy, or only
where it holds a legal entity? That variable is invisible on a job board's
face — but it IS visible in aggregate across a company's whole board, because
companies that hire globally say so in their location fields ("Home based -
Worldwide", "Remote - Global", "Anywhere").

So instead of trusting a listicle of "best remote companies", this measures
each board directly:
  - does the token resolve at all (most guessed tokens don't)
  - how many engineering-ish roles it has
  - what share of postings have a worldwide/anywhere-shaped location
  - what share name a specific country/city (entity-bound tells)

Nothing here writes to the DB. Feed the winners into ATS_BOARDS in
stage1_scrape.py and let the existing pipeline do the rest.

    python probe_worldwide.py                 # probe the built-in candidates
    python probe_worldwide.py --token foo --token bar
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "johan-jobhunter/0.1 (personal job search)"}
SLEEP = 0.7            # be polite; these are free public endpoints

# Candidates: companies plausibly structured to hire worldwide — remote-first
# product companies, talent marketplaces (contractor by design), and crypto
# orgs (which pay contractors globally as a matter of course). Token guesses
# follow each ATS's usual "lowercase, no punctuation" convention.
CANDIDATES = [
    # from CLAUDE.md's research list
    "automattic", "toptal", "andela", "turing", "proxify", "crossover",
    "oyster", "oysterhr", "deel", "remotecom", "tether", "gunio", "ateam",
    "lemonio",
    # remote-first product companies known to hire broadly
    "percona", "doist", "zapier", "posthog", "supabase", "huggingface",
    "sourcegraph", "mattermost", "rocketchat", "nextcloud", "clickhouse",
    "temporaltechnologies", "temporal", "dbtlabs", "hasura", "strapi",
    "appwrite", "calcom", "flyio", "neondatabase", "railway", "weaviate",
    "qdrant", "prisma", "close", "customerio", "buffer", "hotjar",
    "clevertech", "xteam", "aiven", "cockroachlabs", "circleci", "netlify",
    "wikimedia", "odoo", "suse",
    # crypto / protocol orgs — contractor-friendly worldwide
    "chainlinklabs", "consensys", "protocollabs", "ethereumfoundation",
    "status", "paritytechnologies", "parity", "near", "nearprotocol",
    "ledger", "kraken", "bitfinex", "aragon", "gnosis", "polygon",
]

WORLDWIDE = re.compile(
    r"\b(worldwide|world wide|anywhere|global(?:ly)?|any country|"
    r"any time ?zone|fully remote|remote - remote)\b", re.I)

# EMEA matters as much as "worldwide" here: EMEA includes Africa, so
# Canonical's "Home based - EMEA" roles are the ones Stage 2 graded `likely`
# for Tunisia. The first version of this probe scored EMEA as 0 and would have
# thrown away exactly the shape of employer worth finding.
EMEA = re.compile(r"\bemea\b|europe.{0,25}africa|middle east", re.I)

# Bare "Remote" with no place named is not a permission, but it's not a
# rejection either — those land in the `unclear` pile, which is where outreach
# has any chance. Worth counting separately from a named country.
BARE_REMOTE = re.compile(r"^\s*(remote|remote\s*[-–]\s*remote)\s*$", re.I)

# A location that names a specific place is the entity-bound tell.
SPECIFIC = re.compile(
    r"\b(usa|united states|u\.s\.|us[- ]|canada|uk|united kingdom|london|"
    r"germany|berlin|munich|france|paris|spain|madrid|barcelona|poland|"
    r"warsaw|netherlands|amsterdam|ireland|dublin|india|bangalore|"
    r"singapore|australia|sydney|japan|tokyo|brazil|sao paulo|mexico|"
    r"new york|san francisco|seattle|austin|boston|chicago|denver|"
    r"portugal|lisbon|switzerland|zurich|sweden|stockholm|norway|oslo|"
    r"denmark|copenhagen|italy|milan|rome|israel|tel aviv|"
    r"nigeria|kenya|south africa|egypt|uae|dubai|abu dhabi|saudi)\b", re.I)

ENGISH = re.compile(
    r"\b(engineer|developer|backend|back-end|software|platform|api|sre|"
    r"infrastructure|devops|data|architect|programmer)\b", re.I)


def get_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe_greenhouse(token):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    jobs = data.get("jobs") or []
    return [(j.get("title") or "", (j.get("location") or {}).get("name") or "")
            for j in jobs]


def probe_lever(token):
    data = get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    return [(j.get("text") or "",
             (j.get("categories") or {}).get("location") or "")
            for j in data]


def probe_ashby(token):
    """Ashby's public board API. Added because the remote-first cohort that
    actually hires worldwide (Supabase, PostHog, Percona, Railway, Oyster)
    lives here, not on Greenhouse/Lever — so a probe without it misses
    exactly the companies worth finding."""
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    return [(j.get("title") or "", j.get("location") or "")
            for j in (data.get("jobs") or [])
            if j.get("isListed") is not False]


PROBES = [("greenhouse", probe_greenhouse), ("lever", probe_lever),
          ("ashby", probe_ashby)]


def bucket(loc):
    """Classify one location string. Order matters: an explicit worldwide
    claim wins, then EMEA, then a named place (entity-bound tell), then bare
    remote. Checking SPECIFIC before EMEA would misfile "EMEA - Germany"."""
    if WORLDWIDE.search(loc):
        return "worldwide"
    if EMEA.search(loc):
        return "emea"
    if BARE_REMOTE.search(loc):
        return "bare_remote"
    if SPECIFIC.search(loc):
        return "named_place"
    return "other"


def score(rows):
    total = len(rows)
    eng = sum(1 for t, _ in rows if ENGISH.search(t))
    counts = {"worldwide": 0, "emea": 0, "bare_remote": 0,
              "named_place": 0, "other": 0}
    reachable_locs = []
    for _, loc in rows:
        b = bucket(loc)
        counts[b] += 1
        if b in ("worldwide", "emea", "bare_remote"):
            reachable_locs.append(loc)
    # "Reachable" = worldwide + EMEA + bare remote: the postings that could
    # plausibly hire someone in Tunisia. This is the number worth ranking on,
    # not worldwide alone.
    reachable = counts["worldwide"] + counts["emea"] + counts["bare_remote"]
    return {
        "total": total, "eng": eng, **counts,
        "reachable": reachable,
        "reach_pct": round(100 * reachable / total) if total else 0,
        "examples": sorted(set(reachable_locs))[:3],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", action="append", default=[])
    args = ap.parse_args()
    candidates = args.token or CANDIDATES

    found = []
    for token in candidates:
        for source, fn in PROBES:
            try:
                rows = fn(token)
            except urllib.error.HTTPError as e:
                if e.code not in (404, 400, 403):
                    print(f"  {source}/{token}: HTTP {e.code}", file=sys.stderr)
                time.sleep(SLEEP)
                continue
            except Exception as e:
                print(f"  {source}/{token}: {type(e).__name__}", file=sys.stderr)
                time.sleep(SLEEP)
                continue
            time.sleep(SLEEP)
            if not rows:
                continue
            s = score(rows)
            s.update(source=source, token=token)
            found.append(s)
            print(f"{source:11}/{token:22} jobs {s['total']:4} eng {s['eng']:4} | "
                  f"ww {s['worldwide']:3} emea {s['emea']:3} bare-remote "
                  f"{s['bare_remote']:3} named {s['named_place']:4} | "
                  f"reachable {s['reach_pct']:3}%  {s['examples']}")
            break   # a token that resolved on one ATS won't be on the other

    print("\n=== ranked by reachable share (worldwide + EMEA + bare remote) ===")
    ranked = sorted([f for f in found if f["eng"] > 0],
                    key=lambda f: (-f["reach_pct"], -f["eng"]))
    for f in ranked:
        print(f"{f['reach_pct']:3}% reachable  {f['eng']:4} eng roles  "
              f"{f['source']}/{f['token']}")

    print("\n=== paste-ready for ATS_BOARDS (>=1 reachable posting) ===")
    picks = [f for f in ranked if f["reachable"] >= 1]
    print(", ".join(f'("{f["source"]}", "{f["token"]}")' for f in picks) or "(none)")
    print("\nNote: measured against 992 already-collected postings, adding a "
          "famous company's ATS board has ~zero yield here — see CLAUDE.md "
          "'Where eligible jobs actually come from'. Prefer boards that show "
          "real worldwide/EMEA share above.")


if __name__ == "__main__":
    main()
