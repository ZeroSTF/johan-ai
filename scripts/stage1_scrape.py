import hashlib
import html as htmllib
import json
import re
import sys
from pathlib import Path

import psycopg2
import requests
from bs4 import BeautifulSoup

ATS_BOARDS = [
    ("greenhouse", "gitlab"), ("greenhouse", "elastic"),
    ("greenhouse", "canonical"), ("greenhouse", "datadog"),
    ("greenhouse", "mongodb"), ("greenhouse", "cloudflare"),
    ("greenhouse", "twilio"), ("lever", "sonarsource"),
    ("greenhouse", "grafanalabs"), ("greenhouse", "vercel"),
    ("greenhouse", "n26"), ("greenhouse", "remotecom"),
    ("lever", "toptal"), ("greenhouse", "stripe"),
    ("greenhouse", "airbnb"), ("greenhouse", "dropbox"),
    ("greenhouse", "asana"), ("greenhouse", "figma"),

    # --- Added 2026-09-10, chosen by measurement, not reputation ------------
    # Every board above except canonical produced 0 `likely` across 563
    # postings (see CLAUDE.md "Where eligible jobs actually come from"), so
    # these were picked with probe_worldwide.py by their actual share of
    # worldwide/EMEA/bare-remote locations rather than by being well known.
    ("ashby", "supabase"),      # 35 eng roles, 36 worldwide/EMEA ("Remote, Global")
    ("ashby", "clickhouse"),    # 124 eng roles, 21 EMEA
    ("ashby", "percona"),       # 6 eng roles, 8 "Worldwide"/"EMEA" — Canonical-shaped
    ("ashby", "posthog"),       # 5 eng roles, 7 remote/EMEA
    ("ashby", "railway"),       # 8 eng roles, 6 "Global"
    ("ashby", "oyster"),        # 12 EMEA/"Any Location" (an EOR — hires broadly by nature)
    ("greenhouse", "wikimedia"),   # 94% bare-"Remote", 7 eng roles
    ("greenhouse", "customerio"),  # 4 "EMEA Remote", 10 eng roles
]

TITLE_INCLUDE = re.compile(
    r"\b(backend|back-end|software|platform|api|engineer|developer|"
    r"java|spring|python|full[- ]?stack|infrastructure|sre|"
    r"site reliability|devops|data engineer)\b", re.I)
TITLE_EXCLUDE = re.compile(
    r"\b(sales|account|recruit|talent|marketing|designer|design|"
    r"hardware|mechanical|nurse|driver|warehouse|merchandiser|"
    r"intern|graduate|apprentice|"
    r"staff|principal|distinguished|fellow|director|vp|head of|manager|"
    r"support|customer|solutions engineer|technical account|"
    r"security engineer|compliance|legal|finance)\b", re.I)

UA = {"User-Agent": "johan-jobhunter/0.1 (personal job search)"}


def dsn():
    env = {}
    for line in (Path.home() / "johan/compose/.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return (f"dbname=johan user=johan password={env['POSTGRES_PASSWORD']} "
            f"host=127.0.0.1 port=5432")


def clean(raw):
    if not raw:
        return ""
    if "&lt;" in raw or "&amp;" in raw:
        raw = htmllib.unescape(raw)
    text = BeautifulSoup(raw, "html.parser").get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def wanted(title):
    return bool(TITLE_INCLUDE.search(title or "")) and not TITLE_EXCLUDE.search(title or "")


def get(url, timeout=45):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def row(external_id, company, title, location, url, desc, **meta):
    return {"external_id": str(external_id), "company": company or "",
            "title": title or "", "location": location or "",
            "url": url or "", "desc": desc, "meta": meta}


def ats_greenhouse(token):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    for j in d.get("jobs", []):
        yield row(j["id"], token, j.get("title"),
                  (j.get("location") or {}).get("name"),
                  j.get("absolute_url"), clean(j.get("content")))


def ats_lever(token):
    for j in get(f"https://api.lever.co/v0/postings/{token}?mode=json"):
        cat = j.get("categories") or {}
        yield row(j.get("id"), token, j.get("text"), cat.get("location"),
                  j.get("hostedUrl"),
                  clean(j.get("descriptionPlain") or j.get("description")),
                  team=cat.get("team"), commitment=cat.get("commitment"))


def ats_ashby(token):
    """Ashby's public job-board API — documented, no auth, same shape as the
    other two.

    Worth having because of WHO uses it: guessing Greenhouse/Lever tokens for
    remote-first companies mostly 404'd (52 of 65 candidates), because that
    cohort — Supabase, PostHog, Percona, Railway, Oyster, ClickHouse — is on
    Ashby. And that cohort is where the worldwide/EMEA postings actually are.
    Note `secondaryLocations` is ignored on purpose: Stage 2 reads one
    location string, and the primary one is what the posting leads with."""
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        yield row(j.get("id"), token, j.get("title"), j.get("location"),
                  j.get("jobUrl") or j.get("applyUrl"),
                  j.get("descriptionPlain") or clean(j.get("descriptionHtml")),
                  workplace_type=j.get("workplaceType"),
                  department=j.get("department"),
                  employment_type=j.get("employmentType"),
                  posted=j.get("publishedAt"))


ATS = {"greenhouse": ats_greenhouse, "lever": ats_lever, "ashby": ats_ashby}


def agg_arbeitnow(base, pages=5, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    for page in range(1, pages + 1):
        url = f"{base}?page={page}" + (f"&{q}" if q else "")
        jobs = get(url).get("data", [])
        if not jobs:
            return
        for j in jobs:
            yield row(j.get("slug"), j.get("company_name"), j.get("title"),
                      j.get("location"), j.get("url"),
                      clean(j.get("description")),
                      remote=str(j.get("remote", "")).lower() == "true",
                      tags=j.get("tags"), job_types=j.get("job_types"),
                      posted=j.get("created_at"))


def agg_remotejobs(limit=100, max_offset=300):
    for offset in range(0, max_offset + 1, limit):
        d = get(f"https://remotejobs.org/api/v1/jobs?category=programming"
                f"&limit={limit}&offset={offset}")
        jobs = d.get("data", [])
        if not jobs:
            return
        for j in jobs:
            co = j.get("company") or {}
            yield row(j.get("id"), co.get("name"), j.get("title"),
                      j.get("location"), j.get("apply_url") or j.get("url"),
                      clean(j.get("description")),
                      remote=True, salary=j.get("salary_text"),
                      salary_min=j.get("salary_min"),
                      salary_max=j.get("salary_max"),
                      job_type=j.get("type"), posted=j.get("posted_at"))
        if not d.get("pagination", {}).get("has_more"):
            return


def agg_remoteok():
    for j in get("https://remoteok.com/api"):
        if not j.get("id") or not j.get("position"):
            continue
        yield row(j.get("id"), j.get("company"), j.get("position"),
                  j.get("location"), j.get("apply_url") or j.get("url"),
                  clean(j.get("description")),
                  remote=True, tags=j.get("tags"),
                  salary_min=j.get("salary_min"),
                  salary_max=j.get("salary_max"), posted=j.get("date"))


def agg_remotive():
    d = get("https://remotive.com/api/remote-jobs?category=software-dev")
    for j in d.get("jobs", []):
        yield row(j.get("id"), j.get("company_name"), j.get("title"),
                  j.get("candidate_required_location"), j.get("url"),
                  clean(j.get("description")),
                  remote=True, job_type=j.get("job_type"),
                  salary=j.get("salary"), posted=j.get("publication_date"))


AGGREGATORS = [
    ("arbeitnow-visa", lambda: agg_arbeitnow(
        "https://www.arbeitnow.com/api/job-board-api", visa_sponsorship="true")),
    ("arbeitnow-uk", lambda: agg_arbeitnow(
        "https://www.arbeitnow.co.uk/api/job-board-api")),
    ("remotejobs", agg_remotejobs),
    ("remoteok", agg_remoteok),
    ("remotive", agg_remotive),
]


def ingest(cur, source, jobs):
    seen = kept = new = 0
    for j in jobs:
        seen += 1
        if not wanted(j["title"]):
            continue
        if len(j["desc"]) < 200:
            continue
        kept += 1
        h = hashlib.sha256(
            f"{source}|{j['company']}|{j['external_id']}".encode()).hexdigest()
        # Still listed -> refresh last_seen (and reopen it if it had been
        # marked closed); johan_refresh.py closes what stops appearing.
        cur.execute("""
            INSERT INTO jobs (hash, source, url, company, title, location, raw_desc, meta,
                              last_seen)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (hash) DO UPDATE SET last_seen = now(), closed_at = NULL
            RETURNING (xmax = 0)
        """, (h, source, j["url"], j["company"], j["title"], j["location"],
              j["desc"], json.dumps(j["meta"])))
        new += bool(cur.fetchone()[0])
    return seen, kept, new


def main():
    conn = psycopg2.connect(dsn())
    conn.autocommit = True
    totals = [0, 0, 0]

    with conn.cursor() as cur:
        for source, token in ATS_BOARDS:
            try:
                s, k, n = ingest(cur, source, ATS[source](token))
                print(f"{source}/{token:12} seen {s:4}  kept {k:3}  new {n:3}")
                # A company board is the authoritative list of what's open:
                # anything of ours it no longer lists has closed. Only after a
                # fetch that returned postings — an outage must close nothing.
                if s:
                    cur.execute("UPDATE jobs SET closed_at = now() WHERE source = %s "
                                "AND company = %s AND closed_at IS NULL "
                                "AND last_seen < now() - interval '1 hour'", (source, token))
                    if cur.rowcount:
                        print(f"{source}/{token:12} closed {cur.rowcount}")
                totals = [a + b for a, b in zip(totals, (s, k, n))]
            except Exception as e:
                print(f"FAIL {source}/{token}: {e}", file=sys.stderr)

        for name, fn in AGGREGATORS:
            try:
                s, k, n = ingest(cur, name, fn())
                print(f"{name:22} seen {s:4}  kept {k:3}  new {n:3}")
                totals = [a + b for a, b in zip(totals, (s, k, n))]
            except Exception as e:
                print(f"FAIL {name}: {e}", file=sys.stderr)

    print(f"\nTOTAL seen {totals[0]}  kept {totals[1]}  new {totals[2]}")
    conn.close()


# This used to be top-level module code, which meant `import stage1_scrape`
# — to reuse a single helper, or just to inspect one adapter — silently ran
# the whole scrape: every ATS board, every aggregator, live HTTP, and writes
# to the jobs table. It is idempotent (ON CONFLICT DO NOTHING) so nothing was
# corrupted, but a module import must never do network I/O or touch the DB.
if __name__ == "__main__":
    main()
