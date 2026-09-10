"""Stage 1 adapter — Keejob (Tunisia).

Structure, from probing the site:
  listing pages : https://www.keejob.com/offres-emploi/?page=N   (N = 1..61)
  job links     : /offres-emploi/<id>/<slug>
  company pages : /offres-emploi/companies/<id>/...   <- must be excluded
  no JSON-LD, so the description is pulled from the DOM

Postings are in French, so the title filters below are bilingual.

Politeness: robots.txt allows everything except /api/, /utils/, /i18n/,
/comments/, /banners/click/ and /recruiter/*, none of which we touch. One
request per second, listing pages first, and detail pages fetched only for
titles that survive filtering.

Usage:
    python stage1_keejob.py --pages 61
    python stage1_keejob.py --pages 5 --dry-run
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import psycopg2
import requests
from bs4 import BeautifulSoup

BASE = "https://www.keejob.com"
LIST = BASE + "/offres-emploi/"
SOURCE = "keejob"
UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                   "Gecko/20100101 Firefox/128.0"),
    "Accept-Language": "fr,en;q=0.8",
}
DELAY = 1.0

JOB_PATH = re.compile(r"^/offres-emploi/(\d+)/([^/]+)/?$")

# Bilingual include: French tech vocabulary plus the English terms that appear
# in Tunisian postings for offshore roles.
TITLE_INCLUDE = re.compile(
    r"\b(d[eé]veloppeur|d[eé]veloppeuse|d[eé]veloppement|"
    r"ing[eé]nieur\s*(logiciel|informatique|d[eé]veloppement|syst[eè]me|"
    r"donn[eé]es|data|devops|cloud|qa|test)|"
    r"architecte\s*(logiciel|technique|solution)|"
    r"analyste\s*programmeur|programmeur|informaticien|"
    r"backend|back[- ]end|frontend|front[- ]end|fullstack|full[- ]?stack|"
    r"software|developer|engineer|devops|sre|"
    r"java|spring|python|node|angular|react|\.net|php|symfony|laravel|"
    r"api|microservice|cloud|kubernetes|docker|"
    r"data engineer|ing[eé]nieur data|base de donn[eé]es|"
    r"technicien\s*d[eé]veloppement)\b", re.I)

# Bilingual exclude. Keejob is a general board, so most of it is irrelevant.
TITLE_EXCLUDE = re.compile(
    r"\b(commercial|commerciale|vente|vendeur|ventes|"
    r"comptab|comptable|financ|recouvrement|facturation|audit|fiscal|"
    r"ressources humaines|\brh\b|recrut|paie|"
    r"conseiller|conseill[eè]re|t[eé]l[eé]conseiller|call center|centre d.appel|"
    r"service client|customer service|support client|hotline|"
    r"marketing|community manager|brand manager|graphiste|designer|"
    r"r[eé]dacteur|traducteur|"
    r"agricole|agronome|hse|hsse|qhse|s[eé]curit[eé] au travail|"
    r"g[eé]nie civil|b[aâ]timent|travaux|chantier|"
    r"m[eé]canique|[eé]lectrique|[eé]lectrom[eé]canique|maintenance industrielle|"
    r"chaudronn|soudeur|usinage|"
    r"point de vente|magasin|caissier|logistique|magasinier|"
    r"infirmier|m[eé]decin|pharmac|dentaire|"
    r"chauffeur|livreur|gardien|agent de|"
    r"stagiaire|stage\b|pfe\b|internship|"
    r"professeur|enseignant|formateur|"
    r"salesforce|servicenow|sap\b|odoo|sage|"
    r"administrateur (r[eé]seau|syst[eè]me|applications)|"
    r"technicien (r[eé]seau|informatique|help ?desk|support))\b", re.I)

CHROME_TAGS = ["script", "style", "nav", "header", "footer", "noscript",
               "svg", "form", "aside"]


def dsn():
    env = {}
    for line in (Path.home() / "johan/compose/.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return (f"dbname=johan user=johan password={env['POSTGRES_PASSWORD']} "
            f"host=127.0.0.1 port=5432")


def get(url, session):
    r = session.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    time.sleep(DELAY)
    return r.text


def wanted(title):
    t = title or ""
    return bool(TITLE_INCLUDE.search(t)) and not TITLE_EXCLUDE.search(t)


def list_page(html):
    """Yield (job_id, url, title) for real job postings on one listing page."""
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(BASE, a["href"])).path
        m = JOB_PATH.match(path)
        if not m:
            continue
        job_id = m.group(1)
        if job_id in seen:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        seen.add(job_id)
        yield job_id, urljoin(BASE, path), title


def company_from_text(text):
    """Keejob links the company only when it has a company page; otherwise
    the name is plain text on the line under the title:
        Retour aux offres / <title> / <COMPANY> / Publiée le …
    22 open postings showed as "unknown" in the queue before this."""
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    if len(lines) > 2 and lines[0].lower().startswith("retour aux offres"):
        cand = lines[2]
        if not re.match(r"(publi|d[ée]tails|r[ée]f[ée]rence)", cand, re.I) and len(cand) <= 80:
            return cand
    return ""


def detail(html):
    """Pull description text, plus company and location if we can find them."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(CHROME_TAGS):
        tag.decompose()

    # Prefer the largest single content container; fall back to body.
    best, best_len = None, 0
    for tag in soup.find_all(["main", "article", "section", "div"]):
        text = tag.get_text("\n", strip=True)
        if 400 < len(text) > best_len and len(text) < 25000:
            best, best_len = tag, len(text)
    node = best or soup.find("body") or soup
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))

    company = ""
    for a in soup.find_all("a", href=True):
        if "/offres-emploi/companies/" in a["href"]:
            company = a.get_text(" ", strip=True)
            if company:
                break
    company = company or company_from_text(text)

    location = ""
    m = re.search(r"\b(Tunis|Ariana|Ben Arous|Manouba|Nabeul|Bizerte|B[eé]ja|"
                  r"Jendouba|Kef|Siliana|Sousse|Monastir|Mahdia|Sfax|Kairouan|"
                  r"Kasserine|Sidi Bouzid|Gab[eè]s|M[eé]denine|Tataouine|"
                  r"Gafsa|Tozeur|K[eé]bili|Zaghouan|T[eé]l[eé]travail|Remote)\b",
                  text, re.I)
    if m:
        location = f"{m.group(1)}, Tunisia"
    return text, company, location or "Tunisia"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=61)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be inserted, touch no database")
    args = ap.parse_args()

    session = requests.Session()
    candidates = {}
    skipped = 0
    pages_failed = 0
    reached_end = False

    for page in range(1, args.pages + 1):
        url = LIST if page == 1 else f"{LIST}?page={page}"
        try:
            html = get(url, session)
        except Exception as e:
            print(f"page {page} FAILED: {str(e)[:70]}", file=sys.stderr)
            pages_failed += 1
            continue
        found = 0
        for job_id, job_url, title in list_page(html):
            found += 1
            if wanted(title):
                candidates[job_id] = (job_url, title)
            else:
                skipped += 1
        print(f"page {page:3}: {found:3} postings, "
              f"{len(candidates):3} candidates so far", flush=True)
        if found == 0:
            print("  no postings on this page, stopping")
            reached_end = True
            break

    print(f"\n{len(candidates)} candidate titles, {skipped} off-target\n")

    if args.dry_run:
        for job_id, (url, title) in list(candidates.items())[:40]:
            print(f"  {job_id:>8}  {title[:70]}")
        return

    conn = psycopg2.connect(dsn())
    conn.autocommit = True
    inserted = short = failed = 0

    with conn.cursor() as cur:
        # Already stored -> still listed: refresh last_seen and skip the
        # detail fetch (a refresh used to re-download every posting).
        known = set()
        for job_id in candidates:
            h = hashlib.sha256(f"{SOURCE}|{job_id}".encode()).hexdigest()
            cur.execute("UPDATE jobs SET last_seen = now(), closed_at = NULL "
                        "WHERE hash = %s", (h,))
            if cur.rowcount:
                known.add(job_id)
        print(f"{len(known)} already known, {len(candidates) - len(known)} to fetch\n",
              flush=True)
        # The listing was walked to its end with no failed page, so it is the
        # whole site: anything of ours not on it has closed.
        if reached_end and not pages_failed and candidates:
            cur.execute("UPDATE jobs SET closed_at = now() WHERE source = %s "
                        "AND closed_at IS NULL AND last_seen < now() - interval '1 hour'",
                        (SOURCE,))
            print(f"closed {cur.rowcount} postings no longer listed\n", flush=True)
        for i, (job_id, (url, title)) in enumerate(candidates.items(), 1):
            if job_id in known:
                continue
            try:
                text, company, location = detail(get(url, session))
            except Exception as e:
                failed += 1
                print(f"{i:4} FAIL {title[:50]}: {str(e)[:60]}",
                      file=sys.stderr)
                continue
            if len(text) < 400:
                short += 1
                continue
            h = hashlib.sha256(f"{SOURCE}|{job_id}".encode()).hexdigest()
            cur.execute("""
                INSERT INTO jobs (hash, source, url, company, title,
                                  location, raw_desc, meta, last_seen)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (hash) DO NOTHING
            """, (h, SOURCE, url, company or "unknown", title, location, text,
                  json.dumps({"language": "fr", "job_id": job_id})))
            inserted += cur.rowcount
            print(f"{i:4} {'NEW' if cur.rowcount else '  .'} "
                  f"{(company or '?')[:18]:18} {title[:46]:46} "
                  f"{len(text):6}ch", flush=True)

    print(f"\nnew rows {inserted} | too short {short} | failed {failed}")
    conn.close()


if __name__ == "__main__":
    main()
