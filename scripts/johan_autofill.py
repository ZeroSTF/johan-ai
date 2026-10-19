"""autofill_application — fill a job application form; Riadh clicks Submit.

Roadmap step 5 (approved 2026-09-23: "Johan fills, you click Submit").
Opens the posting's form in a VISIBLE browser window (fill_form.py, in
~/johan/venv-browser), fills what it can, colour-codes every field, and
leaves the window to him. Nothing here, or in fill_form.py, clicks anything.

Where each value comes from — the whole design is this table:
  profile fields (name, email, phone, GitHub...)  answers.yaml `profile`
  screening questions (authorization, country,    answers.yaml `screening`,
    relocation, EEO, consent...)                  ONLY after `confirmed: true`
  the CV                                          the verified tailored PDF
  free-text questions                             drafted from his fact store,
                                                  then checked against it
  anything else                                   left for him, red

Screening questions never come from the model: a wrong dropdown on "do you
require sponsorship?" is a silent auto-reject. The model's only role there
is MATCHING a question to an answer he wrote (calibrated decide(), and only
when confident).

Free text is never drafted when the form asks for the candidate's own
words. Canonical — 51 of his 130 likely jobs — requires the declaration
"During this application process I agree to use only my own words".
Drafting their essays would break a statement he is signing.

v1 supports Greenhouse (job-boards.greenhouse.io) and Ashby
(jobs.ashbyhq.com). Greenhouse's custom dropdowns are React widgets, not
real <select>s; choosing an option needs keyboard events, and a stray Enter
in a form can submit it. So v1 leaves them red with his saved answer shown
beside each — one click each, and zero risk of Johan submitting.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

import johan_decide as jd


class _LazyEngine:
    """johan_assistant, resolved on first use. The engine imports this module
    to register its tool, so importing the engine at load time here was
    circular whenever this module was imported first (tests, scripts)."""
    def __getattr__(self, attr):
        import sys
        mod = sys.modules.get("johan_assistant")
        if mod is None:
            import johan_assistant as mod  # noqa: F811
        return getattr(mod, attr)


ja = _LazyEngine()

ROOT = Path.home() / "johan"
ANSWERS = ROOT / "job-hunter/answers.yaml"
PY = ROOT / "venv-browser/bin/python"
FILLER = Path(__file__).with_name("fill_form.py")
LOG = ROOT / "logs/autofill.log"

OWN_WORDS = re.compile(
    r"(only my own words|in my own words|own words|without (the )?(use|help) of "
    r"(ai|artificial intelligence|generative|chatgpt|llm)|not (to )?use (ai|chatgpt|"
    r"generative)|ai[- ]generated (answers|responses|content) (are|is) not)", re.I)

# Anchored on the whole label so "Email" matches but "Describe an email you
# wrote" does not — the substring bug class this project keeps re-learning.
PROFILE = [
    (r"(legal )?first name|given name", "first_name"),
    (r"(legal )?last name|surname|family name", "last_name"),
    (r"(full )?name|your name|name \(full\)", "full_name"),
    (r"e-?mail( address)?", "email"),
    (r"(phone|mobile|telephone|cell)( number)?", "phone"),
    (r"linked ?in( profile| url)?", "linkedin"),
    (r"git ?hub( profile| url)?", "github"),
    (r"(personal )?(website|portfolio|blog|site)( url)?", "website"),
    (r"(twitter|x)( ?/ ?x)?( profile| handle)?", "twitter"),
    (r"(current )?(location|city)( \(city\))?", "location"),
    (r"school|university|college", "school"),
    (r"degree", "degree"),
    (r"discipline|field of study|major", "discipline"),
]
PROFILE = [(re.compile(rf"^\s*(?:{p})\s*$", re.I), k) for p, k in PROFILE]
RESUME = re.compile(r"\b(resume|résumé|cv|curriculum)\b", re.I)
COVER = re.compile(r"cover letter", re.I)

DRAFT_PROMPT = (
    "Write Riadh's answer to this job application question, in the first "
    "person, 50-120 words, plain and specific. Use ONLY the facts below from "
    "his real career record. Do not add any technology, employer, project, "
    "number, practice or claim that is not in them, and no superlatives "
    "(extensive, expert, passionate). If the facts do not answer the "
    "question, reply with exactly: NO_FACTS\n\n"
    "Job: {title} at {company}\nQuestion: {question}\n\nFacts:\n{facts}")

MATCH_Q = "Which saved answer does this application question ask for?"
OPTION_Q = "Which option on the form matches Riadh's saved answer?"


def _clean(label):
    return re.sub(r"\s+", " ", (label or "").replace("*", "")).strip()


def application_url(url):
    """The form's URL for a posting URL, or None if unsupported."""
    if re.match(r"https://(job-boards|boards)\.greenhouse\.io/[^/]+/jobs/\d+", url or ""):
        return url
    m = re.match(r"(https://jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{36})", url or "")
    if m:
        return m.group(1) + "/application"
    return None


def _cv_for(job_id):
    apps = ROOT / "job-hunter/staged/applications"
    for d in sorted(apps.glob(f"{job_id}-*")):
        if (d / "cv.pdf").exists():
            return d / "cv.pdf"
    return None


def _facts_for(question, job):
    try:
        res = ja.search_bullets(f"{question} {job.get('title', '')}", top_k=6)
        return [m["text"] for m in res.get("matches", []) if m.get("score", 0) >= 0.45]
    except Exception:
        return []


def _draft(question, job, facts):
    """Returns the drafted text or None. A draft that the calibrated checker
    says is not backed by the facts is thrown away, not filled."""
    if not facts:
        return None
    fact_txt = "\n".join(f"- {f}" for f in facts)
    msg = ja.call_model([{"role": "system", "content": ja.SYSTEM_PROMPT},
                         {"role": "user", "content": DRAFT_PROMPT.format(
                             title=job.get("title", ""), company=job.get("company", ""),
                             question=question, facts=fact_txt)}], no_tools=True)
    text = (msg.get("content") or "").strip()
    if not text or "NO_FACTS" in text:
        return None
    # Every sentence must be backed by his record; unbacked ones are cut.
    # If most of the draft had to go, it wasn't his experience talking —
    # leave the field to him rather than fill a stub.
    kept, dropped = jd.drop_unbacked(text, fact_txt, kind="autofill_draft")
    if not kept or len(dropped) > len(kept):
        return None
    return kept


def _match_screening(question, screening):
    keys = [k for k in screening][:19]
    opts = {k: screening[k].get("means", k) for k in keys}
    opts["none"] = "none of these — a different kind of question"
    d = jd.decide(MATCH_Q, opts, f"Question: {question}", kind="autofill_match",
                  null_context="Question: N/A")
    return (d.choice if d.choice != "none" and d.confident else None), d


def _pick_option(answer, options):
    low = {o.lower().strip(): o for o in options}
    if str(answer).lower().strip() in low:
        return low[str(answer).lower().strip()]
    if not 2 <= len(options) <= 20:
        return None
    d = jd.decide(OPTION_Q, {str(i): o for i, o in enumerate(options)},
                  f"Saved answer: {answer}", kind="autofill_option",
                  null_context="Saved answer: N/A")
    return options[int(d.choice)] if d.confident else None


def build_plan(fields, answers, cv, job):
    """Decide what goes in every field. Pure planning — nothing is typed."""
    profile = answers.get("profile") or {}
    screening = answers.get("screening") or {}
    confirmed = answers.get("confirmed") is True
    no_ai = any(OWN_WORDS.search(f.get("label", "")) for f in fields)
    values, needs, hints, report = {}, [], {}, []
    resume_done = False

    def need(f, hint):
        if f.get("required") or f.get("kind") in ("textarea",):
            needs.append(f["key"])
            hints[f["key"]] = hint
            report.append({"field": _clean(f["label"])[:90], "status": "needs you", "why": hint})

    for i, f in enumerate(fields):
        label, kind = _clean(f.get("label")), f.get("kind")
        nxt = fields[i + 1] if i + 1 < len(fields) else {}
        if kind == "input" and label.lower().startswith("select"):
            continue                      # companion input of a React dropdown
        combo = nxt.get("kind") == "input" and _clean(nxt.get("label")).lower().startswith("select")

        if kind == "file":
            if COVER.search(label):
                need(f, "cover letter — attach one yourself if you want")
            elif not resume_done and (RESUME.search(label) or f.get("required") or label.lower() == "attach"):
                if cv:
                    values[f["key"]] = {"value": str(cv), "how": "profile"}
                    report.append({"field": label or "resume", "status": "CV attached"})
                    resume_done = True
                else:
                    need(f, "no verified CV for this job yet")
            continue

        prof = next((k for rx, k in PROFILE if rx.match(label)), None)
        if prof and kind not in ("select", "radio", "checkbox") and not combo:
            if profile.get(prof):
                values[f["key"]] = {"value": profile[prof], "how": "profile"}
            else:
                need(f, f"'{prof}' is empty in answers.yaml")
            continue

        is_choice = kind in ("select", "radio", "checkbox") or combo
        long_q = kind == "textarea" or len(label) > 70
        if is_choice or (not long_q and label):
            key, _d = _match_screening(label, screening)
            ans = (screening.get(key) or {}).get("answer") if key else None
            if key and ans is not None and not confirmed:
                need(f, f"answers.yaml says '{ans}' for {key} — set confirmed: true to let Johan fill it")
            elif key and ans is not None and combo:
                need(f, f"your answer: {ans}")
            elif key and ans is not None and is_choice:
                opt = _pick_option(ans, f.get("options") or [])
                if opt:
                    values[f["key"]] = {"value": opt, "how": "answers"}
                else:
                    need(f, f"your answer: {ans} — no matching option")
            elif key and ans is not None:
                values[f["key"]] = {"value": ans, "how": "answers"}
            else:
                need(f, f"no saved answer{f' for {key}' if key else ''} — answer it yourself")
            continue

        if long_q:
            if no_ai:
                need(f, "this form asks for your own words — Johan did not draft")
                continue
            text = _draft(label, job, _facts_for(label, job))
            if text:
                values[f["key"]] = {"value": text, "how": "drafted"}
                report.append({"field": label[:90], "status": "drafted — read it"})
            else:
                need(f, "your record has no facts that answer this — write it yourself")
            continue

        need(f, "unknown field")
    return {"values": values, "needs_you": needs, "hints": hints,
            "own_words_form": no_ai, "confirmed": confirmed, "report": report}


def autofill_application(job_id, dry_run=False):
    """Fill the application form for `job_id` in a visible browser window.
    Riadh reviews and clicks Submit himself. `dry_run` plans without opening
    a window (reads the form headlessly)."""
    # The form opens in a window on the PC's screen. With no display (a
    # service, an ssh session) that would fail as a confusing "could not
    # read the form". Say why.
    if not dry_run and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return {"error": "autofill opens the application in a browser window on "
                         "the PC's screen, so it only works from Johan on the PC "
                         "itself, with a screen."}
    job = ja.get_job(int(job_id))
    if not isinstance(job, dict) or job.get("error"):
        return {"error": f"no job {job_id}"}
    url = application_url(job.get("url", ""))
    if not url:
        return {"error": "autofill supports Greenhouse and Ashby forms so far — "
                         "open this one yourself", "url": job.get("url")}
    cv = _cv_for(job_id)
    if not cv:
        ja.draft_cv(int(job_id))
        cv = _cv_for(job_id)
    try:
        answers = yaml.safe_load(ANSWERS.read_text()) or {}
    except Exception as e:
        return {"error": f"cannot read {ANSWERS}: {e}"}
    if not PY.exists():
        return {"error": "browser not installed (~/johan/venv-browser)"}

    LOG.parent.mkdir(parents=True, exist_ok=True)
    mode = "--extract" if dry_run else "--fill"
    proc = subprocess.Popen(
        ["taskset", "-c", "8-15", str(PY), str(FILLER), mode, url],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(LOG, "a"),
        text=True, start_new_session=True, env=dict(os.environ))
    form = json.loads(proc.stdout.readline() or "{}")
    fields = form.get("fields") or []
    if not fields:
        proc.kill()
        # Say WHY. The first real use failed silently: the visible-window
        # Chromium had never been installed (only the headless one), and the
        # error sat in the log while the UI just went back to "ready".
        cause = ""
        try:
            tail = LOG.read_text().strip().splitlines()[-40:]
            errs = [l for l in tail if "Error" in l or "doesn't exist" in l]
            cause = errs[-1].strip()[:200] if errs else ""
        except OSError:
            pass
        return {"error": "could not open or read the form"
                         + (f" — {cause}" if cause else " (page changed, or it needs a login)")
                         + f". Details: {LOG}", "url": url}
    plan = build_plan(fields, answers, cv, job)
    summary = {"job_id": int(job_id), "company": job.get("company"),
               "title": job.get("title"), "url": url, "cv": str(cv) if cv else None,
               "fields": len(fields), "filled": len(plan["values"]),
               "needs_you": len(plan["needs_you"]),
               "own_words_form": plan["own_words_form"],
               "answers_confirmed": plan["confirmed"], "details": plan["report"][:25]}
    if dry_run:
        proc.wait(timeout=30)
        summary["dry_run"] = True
        return summary
    proc.stdin.write(json.dumps(plan) + "\n")
    proc.stdin.flush()
    done = json.loads(proc.stdout.readline() or "{}")
    summary["counts"] = done.get("counts")
    summary["note"] = ("The form is open in a browser window, colour-coded. "
                       "Review every field and click Submit yourself — Johan "
                       "never submits. Then say 'mark {id} as applied'.".format(id=job_id))
    if not plan["confirmed"]:
        summary["note"] += (" Screening answers were NOT filled: set "
                            "`confirmed: true` in job-hunter/answers.yaml once "
                            "you've checked them.")
    if plan["own_words_form"]:
        summary["note"] += (" This company requires answers in your own words, "
                            "so Johan drafted none.")
    return summary


TOOL = {"type": "function", "function": {
    "name": "autofill_application",
    "description": (
        "Open a job's application form on his screen and fill it. Never "
        "submits — he reviews and clicks Submit."),
    "parameters": {"type": "object", "properties": {
        "job_id": {"type": "integer"}}, "required": ["job_id"]}}}
