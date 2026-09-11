"""Stage 4 — assemble a tailored CV for one or more jobs from bullets.yaml.

Design rules this script must honour (see job-hunter/facts/bullets.yaml header
and CLAUDE.md "The fact store"):
  - May ONLY emit bullets whose id appears in bullets.yaml. No paraphrase-by-
    model here at all in this version: text is rendered byte-for-byte from the
    fact store. Selection (which bullets, how many) is the only thing that
    varies per job.
  - known_gaps tokens must never appear in generated output.
  - dropped_metric values must never appear in generated output.
  - Every emitted line is recorded in a manifest with its source bullet_id, so
    Stage 5 can verify traceability without re-deriving anything.

This is intentionally code-only, no model call. The recurring bug documented
in CLAUDE.md is a model turning interpretation into a false positive signal —
the cheapest way to make that impossible in Stage 4 is to not give a model a
turn at all until selection-only has been proven out.

Usage:
    python stage4_assemble.py --job-id 837
    python stage4_assemble.py --verdict likely --top 5
    python stage4_assemble.py --job-id 837 --job-id 810 --job-id 179
"""

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path

import psycopg2
import psycopg2.extras
import yaml

ROOT = Path.home() / "johan"
FACTS = ROOT / "job-hunter/facts/bullets.yaml"
TEMPLATE = ROOT / "job-hunter/templates/base.tex.tmpl"
OUT_ROOT = ROOT / "job-hunter/staged/applications"

# Slot counts per role/project — matches the bullet counts in Riadh's own
# hand-written CV, so tailoring changes WHICH bullets show, not how long
# each section is relative to what he already uses.
ROLE_SLOTS = {
    "mass_swe": 5,
    "mass_intern": 2,
    "challenge": 3,
    "ghariani": 3,
    "dynamix": 1,
}
PROJECT_SLOTS = {
    "proj_marketmaster": 2,
    "proj_zchess": 2,
    "proj_mge": 2,
    "proj_bsde": 2,
    "proj_marketing": 2,
}
EXPERIENCE_ORDER = ["mass_swe", "mass_intern", "challenge", "ghariani", "dynamix"]
PROJECT_ORDER = ["proj_marketmaster", "proj_zchess", "proj_mge", "proj_bsde", "proj_marketing"]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

DISPLAY = {
    "postgresql": "PostgreSQL", "oracle": "Oracle", "soap": "SOAP",
    "mockito": "Mockito", "junit": "JUnit 5", "gitlab-ci": "GitLab CI",
    "spring-data-jpa": "Spring Data JPA", "spring-security": "Spring Security",
    "jwt": "JWT", "maven": "Maven", "rxjs": "RxJS",
    "java": "Java", "java-17": "Java 17", "python": "Python", "cpp": "C++",
    "cpp23": "C++23", "c": "C", "sql": "SQL", "javascript": "JavaScript",
    "typescript": "TypeScript", "csharp": "C#", "bash": "Bash",
    "spring-boot": "Spring Boot", "rest-api": "RESTful Services",
    "api-design": "API Design", "microservices": "Microservices Architecture",
    "backend": "Backend", "middleware": "Middleware",
    "state-machines": "State Machines", "real-time": "Real-Time Systems",
    "websockets": "WebSockets", "async-programming": "Asynchronous Programming",
    "aws": "AWS", "docker": "Docker", "jenkins": "Jenkins",
    "terraform": "Terraform", "ci-cd": "CI/CD",
    "bitbucket-pipelines": "Bitbucket Pipelines", "git": "Git", "linux": "Linux",
    "infrastructure-as-code": "Infrastructure as Code", "deployment": "Deployment",
    "ovh": "OVH", "snowflake": "Snowflake", "snowflake-cortex": "Snowflake Cortex",
    "snowpark": "Snowpark", "snowpark-container-services": "Snowpark Container Services",
    "llm-integration": "LLM Integration", "generative-ai": "Generative AI",
    "prompt-engineering": "Prompt Engineering", "mysql": "MySQL",
    "data-processing": "Data Processing", "data-extraction": "Data Extraction",
    "web-scraping": "Web Scraping", "system-design": "System Design",
    "architecture": "Architecture", "object-oriented-design": "Object-Oriented Design",
    "performance-tuning": "Performance Tuning", "testing": "Testing",
    "unit-testing": "Unit Testing", "code-review": "Code Review", "agile": "Agile",
    "technical-leadership": "Technical Leadership", "debugging": "Debugging",
    "refactoring": "Refactoring", "uml": "UML",
    "bitwise-algorithms": "Bitwise Algorithms", "algorithms": "Algorithms",
    "systems-programming": "Systems Programming",
    "correctness-verification": "Correctness Verification",
    "fullstack": "Fullstack", "ownership": "Ownership", "sdlc": "SDLC",
    "enterprise-integration": "Enterprise Integration", "integration": "Integration",
    "security": "Security", "authentication": "Authentication",
    "authorization": "Authorization", "domain-modelling": "Domain Modelling",
    "fintech-modelling": "Fintech Modelling",
    "remote-collaboration": "Remote Collaboration",
    "async-communication": "Async Communication",
    "cross-functional-collaboration": "Cross-Functional Collaboration",
    "developer-tooling": "Developer Tooling",
    "product-engineering": "Product Engineering",
    "code-maintenance": "Code Maintenance",
    "engineering-process": "Engineering Process",
    "software-engineering": "Software Engineering",
    "quantitative-algorithms": "Quantitative Algorithms",
    "data-structures": "Data Structures",
    "fastapi": "FastAPI", "pydantic-ai": "Pydantic AI", "langgraph": "LangGraph", 
    "mcp": "Model Context Protocol (MCP)", "angular": "Angular", "react": "React", 
    "flask": "Flask", "streamlit": "Streamlit", "javafx": "JavaFX", 
    "php": "PHP", "assembly": "Assembly", "cmake": "CMake",
}

SKILL_GROUP_LABELS = {
    "languages": "Languages",
    "backend": "Backend \\& Architecture",
    "frontend": "Frontend \\& UI",
    "cloud_devops": "Cloud \\& DevOps",
    "data_ai": "Data \\& AI Integrations",
    "practice": "Practice",
}

LATEX_ESCAPE = str.maketrans({
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
})


def esc(s):
    return s.translate(LATEX_ESCAPE)


def disp(token):
    return DISPLAY.get(token, token.replace("-", " ").title())


def load_facts():
    return yaml.safe_load(FACTS.read_text())


def dsn():
    env = {}
    for line in (ROOT / "compose/.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return (f"dbname=johan user=johan password={env['POSTGRES_PASSWORD']} "
            f"host=127.0.0.1 port=5432")


def fmt_date(ym):
    if ym == "present":
        return "Present"
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1]}. {y}"


def fmt_daterange(start, end):
    return f"{fmt_date(start)} -- {fmt_date(end)}"


def fmt_year(start, end):
    ys, ye = start[:4], (end[:4] if end != "present" else start[:4])
    return ys if ys == ye else f"{ys}--{ye}"


@lru_cache(maxsize=2048)
def _term_pattern(term):
    """Boundary-matched pattern for a skill term.

    Naive `term in text` substring matching is wrong here and quietly inflates
    everything: "erp" matches inside "ENT-ERP-RISE", so every posting that
    said "enterprise" scored an ERP skill hit, and "git" matches inside
    "DI-GIT-AL". Left boundary is always required. The right boundary is only
    applied when the term ends in an alphanumeric, so "c++" still matches
    "C++23" and "CI/CD" still matches, while "erp" no longer matches
    "enterprise"."""
    left = r"(?<!\w)"
    right = r"(?!\w)" if term[-1].isalnum() else ""
    return re.compile(left + re.escape(term) + right)


def skill_matches(tok, needle):
    """True if a posting mentions this skill, by token, spaced token, or
    display name ("ci-cd" / "ci cd" / "CI/CD")."""
    for t in {tok, tok.replace("-", " "), disp(tok).lower()}:
        if t and _term_pattern(t).search(needle):
            return True
    return False


def score_bullet(bullet, needle):
    """Count how many of the bullet's own skill terms appear in the job text.
    Only the bullet's OWN claimed skills are searched for — a job asking for
    something he doesn't have can't inflate a bullet's score, it can only fail
    to match any bullet at all. That's intentional."""
    return sum(1 for tok in bullet.get("skills", []) if skill_matches(tok, needle))


def bold_own_skills(text, skills):
    """Bold each skill's display name in the bullet's own text, if present
    verbatim. Never adds a skill that isn't already in the sentence.

    Matches are found against the ORIGINAL escaped text and kept only if they
    don't overlap a longer match already claimed (longest display name first)
    — e.g. "Java 17" claims its span so "Java" can't also wrap inside it and
    produce \\textbf{\\textbf{Java} 17}, which is what a sequential
    find-and-wrap-on-the-growing-string approach produced here originally."""
    plain = esc(text)
    claimed = []  # list of (start, end)
    spans = []    # list of (start, end, display_text)

    def overlaps(a, b):
        return a[0] < b[1] and b[0] < a[1]

    ordered = sorted({esc(disp(tok)) for tok in skills}, key=len, reverse=True)
    for d in ordered:
        pattern = re.compile(re.escape(d), re.IGNORECASE)
        m = pattern.search(plain)
        if not m:
            continue
        start, end = m.span()
        # Extend over a bare trailing "s" so a plural in the sentence
        # ("integrations") doesn't get split into "\textbf{integration}s".
        if end < len(plain) and plain[end] == "s" and (end + 1 == len(plain) or not plain[end + 1].isalpha()):
            end += 1
        span = (start, end)
        if any(overlaps(span, c) for c in claimed):
            continue
        claimed.append(span)
        spans.append((span[0], span[1], plain[span[0]:span[1]]))

    spans.sort()
    out, cursor = 0, 0
    out = []
    for start, end, matched in spans:
        out.append(plain[cursor:start])
        out.append(f"\\textbf{{{matched}}}")
        cursor = end
    out.append(plain[cursor:])
    return "".join(out)


def select_bullets(facts, job_needle):
    bullets_by_role = {}
    for b in facts["bullets"]:
        bullets_by_role.setdefault(b["role"], []).append(b)

    selected = {}  # role -> [bullet, ...] in display order
    for role, n in {**ROLE_SLOTS, **PROJECT_SLOTS}.items():
        cands = bullets_by_role.get(role, [])
        scored = [(score_bullet(b, job_needle), i, b) for i, b in enumerate(cands)]
        scored.sort(key=lambda x: (-x[0], x[1]))  # best score, stable by original order
        chosen = scored[:n]
        chosen.sort(key=lambda x: x[1])  # restore original order for display
        selected[role] = [b for _, _, b in chosen]
    return selected


def render_experience(facts, selected, manifest):
    roles = {r["id"]: r for r in facts["roles"]}
    blocks = []
    for role_id in EXPERIENCE_ORDER:
        r = roles[role_id]
        loc = r["location"]
        if r.get("mode") in ("hybrid", "remote"):
            loc = f"{loc} ({r['mode'].title()})"
        blocks.append(
            f"    \\resumeSubheading\n"
            f"      {{{esc(r['org'])}}}{{{fmt_daterange(r['start'], r['end'])}}}\n"
            f"      {{{esc(r['title'])}}}{{{esc(loc)}}}\n"
            f"      \\resumeItemListStart"
        )
        for b in selected[role_id]:
            blocks.append(f"        \\resumeItem{{{bold_own_skills(b['text'], b['skills'])}}}")
            manifest.append({"section": "experience", "role": role_id,
                              "bullet_id": b["id"], "text": b["text"]})
        blocks.append("      \\resumeItemListEnd\n")
    return "\n".join(blocks)


def render_projects(facts, selected, manifest, needle=""):
    """Projects ordered by relevance to the posting rather than a fixed list.
    All three still appear — he only has three — but the one that matches the
    job leads."""
    roles = {r["id"]: r for r in facts["roles"]}
    order = sorted(
        PROJECT_ORDER,
        key=lambda rid: -sum(score_bullet(b, needle) for b in selected.get(rid, [])))
    blocks = []
    for role_id in order:
        r = roles[role_id]
        b_all = selected[role_id]
        techs, seen = [], set()
        for b in b_all:
            for tok in b["skills"]:
                d = disp(tok)
                if d not in seen:
                    seen.add(d)
                    techs.append(d)
        tech_line = ", ".join(techs[:4])
        title = r["title"]
        if r.get("url"):
            heading = f"\\textbf{{\\href{{{r['url']}}}{{{esc(title)}}}}}"
        else:
            heading = f"\\textbf{{{esc(title)}}}"
        blocks.append(
            f"    \\resumeProjectHeading\n"
            f"      {{{heading} $|$ \\emph{{{esc(tech_line)}}}}}{{{fmt_year(r['start'], r['end'])}}}\n"
            f"      \\resumeItemListStart"
        )
        for b in b_all:
            blocks.append(f"        \\resumeItem{{{bold_own_skills(b['text'], b['skills'])}}}")
            manifest.append({"section": "projects", "role": role_id,
                              "bullet_id": b["id"], "text": b["text"]})
        blocks.append("      \\resumeItemListEnd\n")
    return "\n".join(blocks)


def render_education(facts):
    blocks = []
    for e in facts["education"]:
        blocks.append(
            f"    \\resumeSubheading\n"
            f"      {{{esc(e['institution'])}}}{{{fmt_daterange(e['start'], e['end'])}}}\n"
            f"      {{{esc(e['credential'])}}}{{{esc(e['location'])}}}\n"
            f"      \\resumeItemListStart\n"
            f"        \\resumeItem{{\\textbf{{Specialization:}} "
            f"{esc(e['specialization'])} ({esc(', '.join(e['coursework']))}).}}\n"
            f"      \\resumeItemListEnd"
        )
    return "\n".join(blocks)


def _skill_depth(facts):
    """How many of his bullets claim each skill — a usable proxy for depth,
    and the only evidence-based way to break ties when ordering."""
    depth = {}
    for b in facts["bullets"]:
        for tok in b.get("skills", []):
            depth[tok] = depth.get(tok, 0) + 1
    return depth


# Skills that can stand as a headline identity. SQL, C and Bash are real
# skills of his but nobody calls themselves a "Backend Software Engineer
# (SQL)", and a single stray "c" in a posting was enough to promote C.
HEADLINE_SKILLS = ["java", "python", "cpp", "typescript", "javascript",
                   "csharp", "spring-boot", "microservices"]


def tailored_headline(facts, needle):
    """Lead with the languages this posting actually asks for — but only ones
    he really has.

    The headline was static: every CV said "Backend Software Engineer (Java /
    Spring Boot)", including applications for a cross-platform C++ role and a
    QA role. It is the first line a recruiter reads. Only the parenthetical
    changes, only to skills already in the fact store, and it falls back to
    the fact store's own headline when the posting names nothing he has."""
    base = facts["owner"]["headline"].split("(")[0].strip()
    fallback = facts["owner"]["headline"]
    depth = _skill_depth(facts)
    base_words = set(base.lower().split())

    cands = []
    for tok in HEADLINE_SKILLS:
        if not depth.get(tok) or not skill_matches(tok, needle):
            continue
        name = disp(tok)
        # "Backend Software Engineer (Python / Backend)" reads like a bug.
        if name.lower() in base_words:
            continue
        cands.append((depth[tok], tok, name))
    # cpp23 is the same identity as cpp — never render "C++ / C++23"
    if any(c[1] == "cpp" for c in cands):
        cands = [c for c in cands if c[1] != "cpp23"]
    cands.sort(key=lambda c: -c[0])
    picks = [name for _, _, name in cands[:2]]
    if not picks:
        return fallback
    return f"{base} ({' / '.join(picks)})"


def render_skills(facts, needle=""):
    """Same skills, ordered so what the posting asks for comes first.

    Content is unchanged — this only reorders, so it cannot introduce a claim.
    Previously every CV carried the identical line, leading with "Java, Java
    17, Python" even on a C++ posting."""
    idx = facts["skill_index"]
    depth = _skill_depth(facts)

    def relevant(tok):
        # Tokens of 1-2 characters ("c") match incidental letters in prose and
        # got promoted to the front of the Languages line on a posting that
        # never asked for C. They stay in the list, they just don't jump it.
        return len(tok) > 2 and needle and skill_matches(tok, needle)

    def rank(tok):
        # mentioned in the posting first, then by how much of his history
        # actually backs it, then original order as a stable tiebreak
        return (0 if relevant(tok) else 1, -depth.get(tok, 0))

    groups = []
    for group, label in SKILL_GROUP_LABELS.items():
        toks = idx.get(group, [])
        hits = sum(1 for x in toks if relevant(x))
        ordered = sorted(toks, key=rank)
        groups.append((hits, label, ordered))

    # Languages stays pinned first — it is the row a reader scans for, and
    # sorting purely by match count buried it under Cloud & DevOps on a C++
    # posting that happened to mention Linux a lot. Everything below it is
    # ordered by relevance.
    groups.sort(key=lambda g: (0 if g[1] == "Languages" else 1, -g[0]))
    lines = [f"     \\textbf{{{label}}}{{: "
             f"{', '.join(esc(disp(x)) for x in ordered)}}} \\\\"
             for _, label, ordered in groups]
    lines[-1] = lines[-1].rstrip(" \\\\")
    return "\n".join(lines)


def assemble_one(facts, job, out_root=OUT_ROOT):
    needle = f"{job['title']} {job['title']} {job['title']} {job['raw_desc'] or ''}".lower()
    selected = select_bullets(facts, needle)
    manifest = []

    owner = facts["owner"]
    remote_line = ""
    is_foreign_remote = (job.get("parsed") or {}).get("work_mode") == "remote" \
        and job["source"] != "keejob"
    if is_foreign_remote:
        cv_line = facts["authorization"]["remote_for_foreign_employer"]["cv_line_to_include"].strip()
        remote_line = f" \\\\ \\vspace{{1pt}} \\footnotesize {esc(cv_line)}"

    tex = TEMPLATE.read_text()
    tex = (tex
           .replace("{{NAME}}", esc(owner["name"]))
           .replace("{{PHONE}}", esc(owner["phone"]))
           .replace("{{EMAIL}}", owner["email"])
           .replace("{{GITHUB_URL}}", owner["github"])
           .replace("{{GITHUB_DISPLAY}}", owner["github"].replace("https://", ""))
           .replace("{{HEADLINE}}", esc(tailored_headline(facts, needle)))
           .replace("{{REMOTE_LINE}}", remote_line)
           .replace("{{SKILLS_BLOCK}}", render_skills(facts, needle))
           .replace("{{EXPERIENCE_BLOCK}}", render_experience(facts, selected, manifest))
           .replace("{{PROJECTS_BLOCK}}", render_projects(facts, selected, manifest, needle))
           .replace("{{EDUCATION_BLOCK}}", render_education(facts)))

    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['title']}".lower()).strip("-")
    out_dir = out_root / f"{job['id']}-{slug}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cv.tex").write_text(tex)
    (out_dir / "manifest.json").write_text(json.dumps({
        "job_id": job["id"], "company": job["company"], "title": job["title"],
        "url": job["url"], "verdict": (job.get("parsed") or {}).get("tunisia_verdict"),
        "bullets": manifest,
    }, indent=2))
    return out_dir


def fetch_jobs(args):
    conn = psycopg2.connect(dsn())
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if args.job_id:
        cur.execute("select id, source, company, title, url, raw_desc, parsed "
                    "from jobs where id = any(%s)", (args.job_id,))
    else:
        cur.execute("select id, source, company, title, url, raw_desc, parsed "
                    "from jobs where parsed->>'tunisia_verdict' = %s "
                    "and closed_at is null order by length(raw_desc) desc limit %s",
                    (args.verdict, args.top))
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", type=int, action="append", default=[])
    ap.add_argument("--verdict", default="likely")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    facts = load_facts()
    jobs = fetch_jobs(args)
    if not jobs:
        print("No matching jobs.")
        return
    for job in jobs:
        out_dir = assemble_one(facts, job)
        print(f"[{job['id']}] {job['company']} — {job['title']} -> {out_dir}")


if __name__ == "__main__":
    main()
