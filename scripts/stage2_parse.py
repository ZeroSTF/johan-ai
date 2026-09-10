"""Stage 2 — turn raw postings into structured facts.

Division of labour:
  1. PRESCREEN (free, no model): off-target title, excluded place, or an
     unrecognised specific place -> rejected without inference.
  2. LLM (johan-extract): extract only what the posting SAYS.
  3. VERDICT (code): restrictions checked BEFORE positive rules, so a
     vocabulary gap fails safe to "unclear" rather than a false "likely".

Seniority comes from the title by regex, not from the model.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "johan-extract"

# --------------------------------------------------------------------------
# Location vocabulary
# --------------------------------------------------------------------------

# Phrases that unambiguously mean "no geographic restriction". Safe to trust
# anywhere, including in text the model lifted from a description.
WORLDWIDE_STRICT = re.compile(
    r"\b(worldwide|world[- ]wide|anywhere in the world|from anywhere|"
    r"work from anywhere|global remote|remote[- ]global|any country|"
    r"no geographic restriction|any location)\b", re.I)

# Weak tokens. Meaningful in a structured location field ("Anywhere",
# "Global"), but in body text they are usually careers-page boilerplate
# ("we hire globally"), so they are NEVER trusted outside the location field.
WORLDWIDE_LOOSE = re.compile(
    r"\b(anywhere|global|globally|international|fully distributed)\b", re.I)

# Places that INCLUDE Tunisia, or MENA markets Riadh is targeting.
# "africa" must not match inside "South Africa", which is an excluded market.
INCLUSIVE = re.compile(
    r"\b(emea|mena|middle east|maghreb|"
    r"(?<!south )africa|north africa|"
    r"tunisia|tunisie|tunis|sfax|sousse|ariana|nabeul|bizerte|monastir|"
    r"morocco|maroc|casablanca|rabat|marrakech|"
    r"algeria|alg[eé]rie|algiers|alger|"
    r"egypt|cairo|libya|"
    r"uae|u\.a\.e|emirates|dubai|abu dhabi|sharjah|"
    r"saudi|ksa\b|riyadh|jeddah|dammam|"
    r"qatar|doha|kuwait|bahrain|manama|oman|muscat|"
    r"jordan|amman|lebanon|beirut)\b", re.I)

HARD_BLOCK = re.compile(
    r"(export control|export[- ]controlled|itar|ear99|"
    r"security clearance|clearance required|"
    r"us person|u\.s\. person|citizenship required|"
    r"must be a citizen|nationals? only|"
    r"public trust|polygraph|sc cleared|dv cleared)", re.I)

EXCLUSIVE = re.compile(
    r"\b(amer|amers|americas|latam|apac|apj|anz|nam\b|"
    r"eu[- ]based|\beu\b|european union|\beea\b|european economic area|"
    r"schengen|eurozone|europe|"
    r"united states|u\.?s\.?a?\b|canada|toronto|vancouver|montreal|ottawa|"
    r"mexico|m[eé]xico|brazil|brasil|chile|argentina|colombia|peru|"
    r"costa rica|uruguay|panama|"
    r"india|bangalore|bengaluru|hyderabad|pune|delhi|mumbai|chennai|gurgaon|"
    r"noida|kolkata|ahmedabad|japan|tokyo|osaka|china|shanghai|beijing|"
    r"shenzhen|korea|seoul|singapore|vietnam|hanoi|ho chi minh|"
    r"philippines|manila|cebu|indonesia|jakarta|malaysia|kuala lumpur|"
    r"thailand|bangkok|taiwan|taipei|hong kong|pakistan|bangladesh|"
    r"australia|sydney|melbourne|brisbane|perth|adelaide|"
    r"new zealand|auckland|wellington|"
    r"israel|tel aviv|haifa|ukraine|kyiv|kiev|turkey|istanbul|ankara|"
    r"united kingdom|\buk\b|u\.k\.|great britain|england|scotland|wales|"
    r"northern ireland|london|manchester|birmingham|bristol|leeds|"
    r"liverpool|sheffield|newcastle|nottingham|cambridge|oxford|cardiff|"
    r"edinburgh|glasgow|aberdeen|belfast|reading|brighton|milton keynes|"
    r"ireland|dublin|cork|galway|limerick|"
    r"germany|deutschland|berlin|munich|m[uü]nchen|hamburg|cologne|k[oö]ln|"
    r"frankfurt|stuttgart|d[uü]sseldorf|leipzig|dresden|hannover|nuremberg|"
    r"n[uü]rnberg|karlsruhe|mannheim|bremen|essen|dortmund|bonn|"
    r"austria|[oö]sterreich|vienna|wien|graz|linz|salzburg|innsbruck|"
    r"switzerland|schweiz|suisse|zurich|z[uü]rich|geneva|gen[eè]ve|basel|"
    r"lausanne|bern|lugano|"
    r"france|paris|lyon|toulouse|marseille|bordeaux|lille|nantes|nice|"
    r"grenoble|montpellier|strasbourg|rennes|sophia antipolis|"
    r"netherlands|nederland|holland|amsterdam|rotterdam|utrecht|eindhoven|"
    r"the hague|den haag|delft|groningen|"
    r"belgium|belgique|belgi[eë]|brussels|bruxelles|antwerp|antwerpen|"
    r"ghent|gent|leuven|li[eè]ge|luxembourg|"
    r"spain|espa[nñ]a|madrid|barcelona|valencia|seville|sevilla|bilbao|"
    r"m[aá]laga|zaragoza|alicante|"
    r"portugal|lisbon|lisboa|porto|braga|coimbra|"
    r"italy|italia|milan|milano|rome|roma|turin|torino|bologna|naples|"
    r"napoli|florence|firenze|venice|"
    r"greece|athens|thessaloniki|cyprus|nicosia|malta|valletta|"
    r"sweden|sverige|stockholm|gothenburg|g[oö]teborg|malm[oö]|uppsala|"
    r"norway|norge|oslo|bergen|trondheim|"
    r"denmark|danmark|copenhagen|k[oø]benhavn|aarhus|odense|"
    r"finland|suomi|helsinki|espoo|tampere|oulu|iceland|reykjavik|"
    r"poland|polska|warsaw|warszawa|krakow|krak[oó]w|wroclaw|wroc[lł]aw|"
    r"gdansk|gda[nń]sk|poznan|pozna[nń]|katowice|lodz|"
    r"czech|czechia|prague|praha|brno|ostrava|slovakia|bratislava|kosice|"
    r"hungary|budapest|debrecen|"
    r"romania|bucharest|bucure[sș]ti|cluj|timisoara|ia[sș]i|"
    r"bulgaria|sofia|plovdiv|varna|serbia|belgrade|novi sad|"
    r"croatia|zagreb|split|slovenia|ljubljana|bosnia|sarajevo|"
    r"estonia|tallinn|tartu|latvia|riga|lithuania|vilnius|kaunas|"
    r"south africa|cape town|johannesburg|pretoria|durban|"
    r"nigeria|lagos|kenya|nairobi|ghana|accra|"
    r"nyc|new york|san francisco|seattle|austin|texas|boston|chicago|"
    r"denver|atlanta|los angeles|san jose|san diego|miami|"
    r"washington d\.?c|remote[- ]us)\b", re.I)

# Tokens carrying no geography, stripped before deciding "is this a place?"
GENERIC_LOC = re.compile(
    r"\b(remote|remotely|hybrid|on[- ]?site|onsite|in[- ]office|office|"
    r"full[- ]?time|part[- ]?time|permanent|contract|contractor|freelance|"
    r"temporary|flexible|various|multiple|several|locations?|other|"
    r"headquarters|hq|home|based|work from home|wfh|distributed|anywhere|"
    r"worldwide|world|wide|global|globally|international|"
    r"not stated|unspecified|unknown|none|every|everywhere|"
    r"and|or|the|in|at|any|all|na|n/a|tbd|"
    r"time ?zone|timezones?|utc|gmt|cet|est|pst|eu time|"
    r"eligible|candidates?|applicants?|open to)\b", re.I)


def residual_place(text):
    """What remains after stripping words that carry no specific geography."""
    s = GENERIC_LOC.sub(" ", text or "")
    s = re.sub(r"[^A-Za-z\u00C0-\u024F\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Sentences that describe the COMPANY rather than the role. A strict
# worldwide phrase inside one of these is worthless as evidence.
BOILERPLATE = re.compile(
    r"(we hire|we\'re a|we are a|we have|our roles|our team|team members|"
    r"colleagues|employees (are|in)|great ideas|equal opportunit|"
    r"regardless of|diversity|diverse|inclusiv|our company|the company is|"
    r"we believe|we welcome|applicants from|candidates from all|"
    r"anyone, anywhere|all over the world)", re.I)

RESIDENCY_REQ = re.compile(
    r"(must (be |reside|live|are )?(based|located|resident|residing)|"
    r"required to (be based|reside|live)|"
    r"residency in|residencia en|wohnsitz in|"
    r"only considering candidates (based|located|in)|"
    r"eligible to work in|right to work in|existing work authoriz)", re.I)


def prescreen(location):
    """Decide from the location field alone, for free."""
    loc = location or ""
    if INCLUSIVE.search(loc) and not EXCLUSIVE.search(loc):
        return "ask_model", None
    if WORLDWIDE_STRICT.search(loc) or WORLDWIDE_LOOSE.search(loc):
        return "ask_model", None
    if EXCLUSIVE.search(loc):
        return "reject", "location names an excluded place (prescreen)"
    residue = residual_place(loc)
    if len(residue) >= 3:
        return "reject", f"unrecognised specific place: {residue[:40]!r} (prescreen)"
    return "ask_model", None


# --------------------------------------------------------------------------
# Seniority and off-target titles
# --------------------------------------------------------------------------

SENIORITY_RULES = [
    ("staff_plus", r"\b(staff|principal|distinguished|fellow|architect)\b"),
    ("senior", r"\b(senior|sr\.?|snr|lead)\b"),
    ("junior", r"\b(junior|jr\.?|entry[- ]level|graduate|associate)\b"),
    ("intern", r"\b(intern|internship|trainee|apprentice)\b"),
]


def seniority_from_title(title):
    t = (title or "").lower()
    if re.search(r"\b(intermediate|mid[- ]level|ii|iii)\b", t):
        return "mid"
    for label, pattern in SENIORITY_RULES:
        if re.search(pattern, t):
            return label
    return "mid"


OFF_TARGET = re.compile(
    r"\b(advocate|evangelist|comercial|ventas|vendas|"
    r"pre[- ]?sales|account executive|recruiter|sourcer|"
    r"embedded|firmware|bare[- ]metal|fpga|asic|rtl|emulation engineer|"
    r"verification engineer|compiler engineer|synthesis engineer|hardware|"
    r"electrical engineer|piping engineer|structural engineer|"
    r"land development|mechanical engineer|power systems|"
    r"service desk|salesforce|servicenow|peoplesoft|windchill|mulesoft|"
    r"power bi|genesys|product owner)\b", re.I)


# --------------------------------------------------------------------------
# Excerpting
# --------------------------------------------------------------------------

ELIGIBILITY_CUES = re.compile(
    r"(eligib|authoriz|authoris|right to work|work permit|visa|sponsor|"
    r"relocat|located in|based in|reside|residen|time ?zone|export control|"
    r"clearance|anywhere|worldwide|globally|country|countries|contractor|"
    r"freelance|employer of record|\beor\b|years of experience|\d\+? years)",
    re.I)


def focus(desc, head=3500, cue_chars=2500):
    if not desc:
        return ""
    head_text = desc[:head]
    hits = [ln.strip() for ln in desc[head:].split("\n")
            if ln.strip() and ELIGIBILITY_CUES.search(ln)]
    extra = "\n".join(hits)[:cue_chars]
    return head_text + ("\n\n[further relevant lines]\n" + extra if extra else "")


# --------------------------------------------------------------------------
# Extraction schema
# --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "min_years": {"type": ["integer", "null"]},
        "must_have": {"type": "array", "items": {"type": "string"}},
        "nice_to_have": {"type": "array", "items": {"type": "string"}},
        "work_mode": {"type": "string",
                      "enum": ["onsite", "hybrid", "remote", "unclear"]},
        "eligible_locations": {"type": "array", "items": {"type": "string"}},
        "states_worldwide": {"type": "boolean"},
        "hiring_type": {"type": "string",
                        "enum": ["employee", "contractor", "either", "unclear"]},
        "visa_sponsorship": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "eligibility_quote": {"type": "string"},
    },
    "required": ["must_have", "work_mode", "eligible_locations",
                 "states_worldwide", "hiring_type", "visa_sponsorship",
                 "eligibility_quote"],
}

SYSTEM = """You extract facts from job postings. Report only what the posting
states about THIS role. Never infer, never guess, never fill gaps.

Careers pages contain generic company copy such as "all of our roles are
remote", "we hire in countries around the world", "great ideas can come from
anywhere", or standard export-control and equal-opportunity paragraphs. That
copy describes the COMPANY, not this role. Ignore it completely.

eligible_locations: list EVERY country, region, bloc (such as EU or EEA), city,
or timezone the posting names in connection with where THIS role can be
performed. Copy them as written, in the posting's own language. List only
place names that literally appear in the posting. Do NOT summarise, do NOT
add "Worldwide", "Global" or "Remote" yourself, and do NOT write "not stated"
or "N/A". Use an empty array when the posting names no place.

states_worldwide: true only if the posting says THIS role may be performed from
anywhere in the world with no geographic restriction. If it names any country,
bloc, city, or timezone requirement, this is false.

hiring_type: "contractor" if it mentions contractor, freelance, or B2B
engagement. "employee" if it describes payroll or employee benefits. "either"
if both. "unclear" if unaddressed.

visa_sponsorship: "yes" only if the posting offers visa sponsorship or paid
relocation. "no" if it states sponsorship is unavailable or requires existing
work authorization. Otherwise "unclear". "Relocation required" without an offer
of support is NOT "yes".

eligibility_quote: copy ONE sentence verbatim, word for word, from the posting.
Choose the sentence that most directly restricts or describes where this role
can be performed, or who is eligible. Prefer sentences naming a place, a
residency requirement, or a work-authorization requirement. REJECT sentences
describing the company generally. Do not paraphrase. If no such sentence
exists, write exactly: not stated

min_years: minimum years of experience required as an integer, or null."""


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

JUNK_VALUES = {"", "not stated", "n/a", "na", "none", "unknown", "unspecified",
               "not specified", "not applicable", "-", "tbd"}


def clean_locations(values):
    out = []
    for v in values or []:
        s = (v or "").strip()
        if s.lower() in JUNK_VALUES:
            continue
        out.append(s)
    return out


def compute_verdict(location, p, title=""):
    quote = p.get("eligibility_quote") or ""
    locs = clean_locations(p.get("eligible_locations"))
    places = f"{location or ''} ; " + " ; ".join(locs)
    everything = f"{places} ; {quote}"

    if HARD_BLOCK.search(everything):
        return "unlikely", "export control, clearance or citizenship requirement"

    # Inclusive wins only when nothing excluded is also named
    if INCLUSIVE.search(places) and not EXCLUSIVE.search(places):
        return "likely", "names Tunisia or a targeted MENA market"

    if EXCLUSIVE.search(everything):
        if p.get("visa_sponsorship") == "yes":
            return "likely", "restricted location but sponsorship offered"
        return "unlikely", "restricted to places excluding Tunisia"

    if RESIDENCY_REQ.search(quote) and p.get("visa_sponsorship") != "yes":
        return "unlikely", "states a residency or authorization requirement"

    # Worldwide claims are trusted from TWO auditable sources only:
    #   (a) the structured location field or the title, which come from the
    #       source system verbatim, and
    #   (b) a quoted sentence that is not company boilerplate.
    # eligible_locations is NOT trusted here: the model synthesises
    # "Worldwide" out of careers-page copy.
    source_fields = f"{location or ''} ; {title or ''}"

    if WORLDWIDE_STRICT.search(source_fields):
        return "likely", "worldwide stated in location/title field"

    if WORLDWIDE_LOOSE.search(location or ""):
        return "likely", "location field states no geographic restriction"

    if WORLDWIDE_STRICT.search(quote) and not BOILERPLATE.search(quote):
        return "likely", "worldwide stated in a quoted role sentence"

    if p.get("visa_sponsorship") == "yes":
        return "likely", "sponsorship or relocation offered"

    residue = residual_place(" ".join(locs))
    if len(residue) >= 3:
        return "unlikely", f"unrecognised place named: {residue[:36]!r}"

    if p.get("hiring_type") in ("contractor", "either"):
        return "unclear", "contractor engagement, no restriction found"

    return "unclear", "no eligibility information found"


# --------------------------------------------------------------------------

def dsn():
    env = {}
    for line in (Path.home() / "johan/compose/.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return (f"dbname=johan user=johan password={env['POSTGRES_PASSWORD']} "
            f"host=127.0.0.1 port=5432")


def parse_one(job, timeout=420):
    user = (f"Title: {job['title']}\n"
            f"Company: {job['company']}\n"
            f"Location field: {job['location']}\n\n"
            f"Posting:\n{focus(job['raw_desc'])}")
    r = requests.post(OLLAMA, json={
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "think": False,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0.0, "num_ctx": 8192, "seed": 42},
    }, timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--status", default="new",
                    help="new | parse_failed | geo_blocked | parsed")
    ap.add_argument("--sources", default=None)
    args = ap.parse_args()

    conn = psycopg2.connect(dsn())
    conn.autocommit = True

    sql = ("SELECT id, title, company, location, raw_desc FROM jobs "
           "WHERE status = %s AND closed_at IS NULL")
    params = [args.status]
    if args.sources:
        sql += " AND source = ANY(%s)"
        params.append([s.strip() for s in args.sources.split(",")])
    sql += " ORDER BY id LIMIT %s"
    params.append(args.limit)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        jobs = cur.fetchall()

    label = f", sources={args.sources}" if args.sources else ""
    print(f"working {len(jobs)} rows (status={args.status}{label})\n", flush=True)

    counts = {}
    prescreened = off_target = model_calls = 0
    t0 = time.time()
    flags = {"likely": "++", "unclear": " ?", "unlikely": " x"}

    def store(cur, job_id, payload, status):
        cur.execute("UPDATE jobs SET parsed = %s, status = %s WHERE id = %s",
                    (json.dumps(payload), status, job_id))
        counts[status] = counts.get(status, 0) + 1

    with conn.cursor() as cur:
        for i, job in enumerate(jobs, 1):
            try:
                base = {"seniority": seniority_from_title(job["title"])}

                if OFF_TARGET.search(job["title"] or ""):
                    store(cur, job["id"], {**base,
                          "tunisia_verdict": "unlikely",
                          "verdict_basis": "off-target title (no model)",
                          "prescreened": True}, "off_target")
                    off_target += 1
                    continue

                decision, basis = prescreen(job["location"])
                if decision == "reject":
                    store(cur, job["id"], {**base,
                          "tunisia_verdict": "unlikely",
                          "verdict_basis": basis,
                          "prescreened": True}, "geo_blocked")
                    prescreened += 1
                    continue

                p = parse_one(job)
                model_calls += 1
                p.update(base)
                p["eligible_locations"] = clean_locations(
                    p.get("eligible_locations"))
                verdict, vbasis = compute_verdict(job["location"], p, job["title"])
                p["tunisia_verdict"] = verdict
                p["verdict_basis"] = vbasis
                p["prescreened"] = False

                store(cur, job["id"], p,
                      "geo_blocked" if verdict == "unlikely" else "parsed")
                print(f"{i:4}/{len(jobs)} {flags[verdict]} "
                      f"{(job['company'] or '')[:14]:14} "
                      f"{job['title'][:40]:40} {p['seniority']:10} "
                      f"{p['hiring_type']:10} {vbasis[:40]}", flush=True)
            except requests.ConnectionError:
                # Ollama stopped — johan-pause / GameMode. Leave this row and
                # the rest as 'new' so the next run resumes here.
                print(f"\nOllama stopped at row {i}/{len(jobs)} — the rest stay "
                      f"queued for the next run.", file=sys.stderr, flush=True)
                conn.close()
                sys.exit(3)
            except Exception as e:
                cur.execute("UPDATE jobs SET status = 'parse_failed' "
                            "WHERE id = %s", (job["id"],))
                counts["parse_failed"] = counts.get("parse_failed", 0) + 1
                print(f"{i:4}/{len(jobs)} FAIL {job['title'][:40]}: "
                      f"{str(e)[:70]}", file=sys.stderr, flush=True)

    dt = time.time() - t0
    print(f"\n{counts}")
    print(f"prescreen-rejected {prescreened} | off-target {off_target} | "
          f"model calls {model_calls}")
    if model_calls:
        print(f"{dt:.0f}s total, {dt / model_calls:.1f}s per model call")
    conn.close()


if __name__ == "__main__":
    main()
