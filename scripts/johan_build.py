"""The build engine: "make me a game / an app / a tool", done in checked steps.

Why: a 4B writes ~100 good lines. Asked for a whole game in one reply it
loses track halfway, says "it works", and the snake dies on frame 1. The
plain loop also gives it 9 tool calls for the whole job and one long chat
holding every file and error it has seen.

So code runs the job, the model only ever does one small thing at a time:
  1. PLAN    one call (JSON schema): a spec, small files, 3-7 steps, and for
             each step the actions + checks that prove it works — written
             BEFORE the code, so the code is written to pass them
  2. OK      he approves the plan once (an approval card, like a change);
             typing instead re-plans with what he said
  3. STEP    per step a FRESH context: spec, file map, the current files,
             this step and its checks. The model returns whole files (JSON
             schema, nothing to mis-parse); code writes them
  4. CHECK   code runs this step's checks AND every earlier step's
             (a new feature must not break an old one): a real browser for
             pages (build_check_page.py), the sandbox for Python
  5. RETRY   up to 3 tries per step with the real failure; a step that
             still breaks earlier checks or throws is rolled back
  6. ANSWER  written by code from the check results — what works, what
             doesn't — never the model's opinion of its own work

Same machine rules as everything else: every call keeps the chat's num_ctx
(a different one reloads the model), and the build writes only inside its
project folder, never in ~/johan.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_approval as jap     # noqa: E402
import johan_interrupt as ji     # noqa: E402

HOME = Path.home()
ROOT = HOME / "johan"
PROJECTS = HOME / "projects"
STATE_DIR = ROOT / "brain/builds"
SHOTS_DIR = ROOT / "brain/sandbox/build"
LOG_DIR = ROOT / "logs/builds"          # what each attempt wrote and what the checks said
BROWSER_PY = ROOT / "venv-browser/bin/python"
PAGE_CHECKER = Path(__file__).with_name("build_check_page.py")

MAX_STEPS = 7
MAX_TRIES = 3
MAX_REPLANS = 2
BUDGET_S = 720              # a build stops starting new work after 12 minutes
FILE_CHARS = 14000          # current files shown in full up to this much
ALLOWED_EXT = {".html", ".htm", ".css", ".js", ".json", ".py", ".txt", ".md", ".svg", ".csv"}

# --------------------------------------------------------------------------
# when
# --------------------------------------------------------------------------

# Only a gate for the calibrated decision — never the decision itself.
MAKE_VERB = re.compile(r"\b(make|build|create|code|write|program|develop|implement|add|"
                       r"extend|improve|turn|give|put)\b", re.I)
THING = re.compile(r"\b(game|app|application|web ?page|page|site|website|web ?app|tool|script|"
                   r"program|cli|bot|calculator|converter|tracker|dashboard|clone|feature|"
                   r"button|level|levels|menu|score|timer|player|enemy|enemies|todo|to-do|"
                   r"editor|quiz|planner|counter|list)\b|\.(html?|py|js)\b", re.I)

# Measured 2026-10-21 on 20 labeled messages: a three-way build/small/other
# wording missed 3 ("write a python script…" went to "small"); this yes/no
# missed 1 ("write a script for my youtube video" — a fair ambiguity).
BUILD_Q = "Is Riadh asking Johan to write software?"
BUILD_OPTS = {
    "build": "yes: he wants a program written or extended — a game, a web page or app, a "
             "script, a command-line tool, a bot — that he will run",
    "no": "no: he wants text, a list, an answer, a picture, a reminder, a setting changed, "
          "or a small tweak to code he already has",
}
BUILD_NULL = "Message: N/A"


def wants_build(q, messages=None, decide=None):
    """Code gate, then one calibrated decision (~0.2 s)."""
    if not q or not (MAKE_VERB.search(q) and THING.search(q)):
        return False
    prev = _previous_user(messages or [], q)
    ctx = (f"Earlier message: {prev[:300]}\n" if prev else "") + f"Message: {q[:600]}"
    try:
        if decide is None:
            import johan_decide as jd
            decide = jd.decide
        d = decide(BUILD_Q, BUILD_OPTS, ctx, kind="build_intent", null_context=BUILD_NULL)
    except Exception:
        return False
    return d.choice == "build" and d.confident


def _previous_user(messages, q):
    users = [m.get("content") or "" for m in messages if m.get("role") == "user"
             and not str(m.get("content") or "").startswith("[Johan")]
    if users and users[-1].strip() == q.strip():
        users = users[:-1]
    return users[-1] if users else ""


# --------------------------------------------------------------------------
# where
# --------------------------------------------------------------------------

_PATH = re.compile(r"(?:~|/home/\w+)(?:/[\w.\-]+)+/?")


def named_target(q):
    """(folder, main file name or None) for a path he wrote, else (None, None)."""
    m = _PATH.search(q or "")
    if not m:
        return None, None
    p = Path(os.path.expanduser(m.group(0).rstrip("/.,")))
    if p.suffix.lower() in ALLOWED_EXT:
        return p.parent, p.name
    return p, None


def folder_problem(folder):
    """Why Johan must not build in `folder`, or None."""
    try:
        f = Path(folder).resolve()
    except Exception:
        return "that isn't a usable folder"
    home = HOME.resolve()
    if f == home or home not in f.parents:
        return "I only build inside a folder in your home, not your home itself or outside it"
    if f == ROOT.resolve() or ROOT.resolve() in f.parents:
        return "I never build inside ~/johan — that's me"
    rel = f.relative_to(home).parts
    if rel and rel[0].startswith(".") and rel[0] != ".cache":
        return "that's a hidden settings folder — pick a normal folder"
    return None


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "project").lower()).strip("-")
    return s[:40] or "project"


def _state_file(folder):
    return STATE_DIR / (hashlib.sha1(str(Path(folder).resolve()).encode()).hexdigest()[:16] + ".json")


def load_state(folder):
    try:
        return json.loads(_state_file(folder).read_text())
    except Exception:
        return None


def save_state(folder, state):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state_file(folder).write_text(json.dumps(state, indent=1))
    except Exception:
        pass


# "make me a calculator" after a game in the same chat is a NEW project, not
# a change to the game; "now add a reset button" is a change.
NEW_THING = re.compile(r"\b(make|build|create|write|code)\s+(me\s+)?(a|an|another|some|one)\b", re.I)

LAST_FOLDER = {}        # session id -> folder of the last build in that chat


# --------------------------------------------------------------------------
# model calls
# --------------------------------------------------------------------------

BUILD_SYSTEM = """You are the build module of Johan, Riadh's local assistant. You write real,
complete, working code for small projects, one step at a time. Rules:
- Web projects: plain HTML/CSS/JavaScript files, no frameworks, nothing from the internet
  (no CDN, no fonts, no fetch). Load scripts with plain <script src="x.js"></script> tags
  at the end of <body> — never type="module" (it is blocked for local files).
  Declare shared state and functions at the top level of a script so other scripts and
  the checks can see them.
- Python projects: standard library only, files in one folder. A command-line program
  reads sys.argv and prints its results; it keeps its data in a file next to itself.
- Keep every file under about 150 lines: split by purpose into several files.
- Never write placeholder comments like "// rest of code here" — every file you return is
  complete and runs as is."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "kind": {"type": "string", "enum": ["web", "python"]},
        "main": {"type": "string"},
        "spec": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["path", "purpose"]}},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "goal": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "test": {"type": "string"}},
            "required": ["goal", "files", "test"]}},
    },
    "required": ["title", "kind", "main", "spec", "files", "steps"],
}

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}},
        "note": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["files", "note", "actions", "checks"],
}

PLAN_HOWTO = """Plan this project. Return JSON:
- title: a short name. kind: "web" (runs in a browser) or "python".
- main: the file he opens or runs (web: an .html file; python: the .py he runs).
- spec: 3-6 sentences — exactly what it does, the controls, what is shown on screen.
- files: every file, each with its purpose. Small files split by purpose.
- steps: 3-7 steps in build order. Step 1 makes the main file load/run with no errors and
  shows the basic layout. Each later step adds ONE piece of behaviour he could notice.
  For each step: goal (one sentence), files (the files it writes), and test: in plain
  words, what a person would do and then see that proves the step works, starting from
  a freshly opened page / a fresh folder — e.g. "type rent and 500, click Add: the list
  shows rent and the total shows 500", "press Space then hold the left arrow: the
  paddle moves left", "run `add 5 food` then `total`: it prints 5". Only behaviour the
  spec promises; something that can happen within a few seconds."""


_LOG = {"file": None}


def log(event, **data):
    """One JSON line per plan / attempt, so a failed build can be read afterwards
    instead of re-run. Never breaks a build."""
    try:
        if _LOG["file"] is None:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _LOG["file"] = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        with open(_LOG["file"], "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "event": event, **data}, default=str) + "\n")
    except Exception:
        pass


def _chat(messages, schema, should_stop=None, timeout=300):
    import johan_assistant as ja
    import johan_llm as llm
    with ji.stoppable(should_stop):
        r = llm.chat(messages, schema=schema, temperature=0.2, max_tokens=ja.NUM_PREDICT,
                     timeout=timeout)
    try:
        return json.loads(r["message"].get("content") or "")
    except ValueError:
        # Cut off at the token limit: the JSON never closed.
        return {"_error": "the reply was cut off or wasn't valid JSON"}


def _clean_plan(raw, folder, main_name, request):
    if not isinstance(raw, dict) or raw.get("_error"):
        return None
    kind = raw.get("kind") if raw.get("kind") in ("web", "python") else "web"
    main = _rel(main_name or raw.get("main") or "")
    if not main or Path(main).suffix.lower() not in ((".html", ".htm") if kind == "web" else (".py",)):
        main = "index.html" if kind == "web" else "main.py"
    files = []
    for f in raw.get("files") or []:
        p = _in_folder((f or {}).get("path"), folder)
        if p and p not in [x["path"] for x in files]:
            files.append({"path": p, "purpose": str(f.get("purpose") or "")[:200]})
    if main not in [f["path"] for f in files]:
        files.insert(0, {"path": main, "purpose": "the main file"})
    if kind == "python":       # the sandbox mounts one flat folder
        files = [f for f in files if "/" not in f["path"]][:9]
    steps = []
    for s in (raw.get("steps") or [])[:MAX_STEPS]:
        goal = str((s or {}).get("goal") or "").strip()
        if not goal:
            continue
        fs = [p for p in (_in_folder(x, folder) for x in s.get("files") or []) if p] or [main]
        for p in fs:
            if p not in [f["path"] for f in files] and (kind == "web" or "/" not in p):
                files.append({"path": p, "purpose": ""})
        steps.append({"goal": goal[:300], "files": fs[:4], "test": str(s.get("test") or "")[:400],
                      # a plan may already carry a concrete test (older plans, tests)
                      "actions": [str(a)[:120] for a in (s.get("actions") or [])][:12],
                      "checks": [str(c)[:300] for c in (s.get("checks") or []) if str(c).strip()][:6]})
    if not steps:
        return None
    return {"title": str(raw.get("title") or "project")[:60], "kind": kind, "main": main,
            "spec": str(raw.get("spec") or "")[:1500], "files": files, "steps": steps,
            "folder": str(folder), "request": request[:1500]}


def _in_folder(p, folder):
    """The model writes `tip-0/tip.html` (repeating the folder's name) or the
    full path; both mean a file in the project folder."""
    p = str(p or "").strip()
    if folder:
        f = str(Path(folder))
        if p.startswith(f + "/"):
            p = p[len(f) + 1:]
        name = Path(f).name
        if p.startswith(name + "/") and len(p) > len(name) + 1:
            p = p[len(name) + 1:]
    return _rel(p)


def _rel(p):
    """A safe path relative to the project folder, or None."""
    p = str(p or "").strip()
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith(("/", "~")) or ".." in Path(p).parts:
        return None
    if Path(p).suffix.lower() not in ALLOWED_EXT:
        return None
    return p


def make_plan(request, folder, main_name=None, changes=None, earlier=None, should_stop=None):
    lines = [f"What Riadh asked: {request}",
             f"Project folder: {folder}" if folder else "Project folder: a new folder (named after the title)"]
    if main_name:
        lines.append(f"The main file must be named {main_name}.")
    existing = _existing_files(folder) if folder else []
    if existing:
        lines.append("This folder already has files — this is a change to an existing "
                     "project. Plan only the steps for what he asks now; keep what works.")
        lines.append(_files_block(folder, existing, FILE_CHARS))
    if earlier:
        lines.append("Earlier steps of this project (already done):\n" +
                     "\n".join(f"- {s['goal']}" for s in earlier.get("steps", [])))
    if changes:
        lines.append(f"He saw your previous plan and said: {changes}\nChange the plan accordingly.")
    msgs = [{"role": "system", "content": BUILD_SYSTEM},
            {"role": "user", "content": "\n\n".join(lines) + "\n\n" + PLAN_HOWTO}]
    raw = _chat(msgs, PLAN_SCHEMA, should_stop)
    log("plan", request=request[:300], raw=raw)
    return _clean_plan(raw, folder, main_name, request)


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def _existing_files(folder):
    f = Path(folder)
    if not f.is_dir():
        return []
    out = []
    for p in sorted(f.rglob("*")):
        rel = p.relative_to(f)
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT and not any(
                x.startswith(".") or x in ("node_modules", "__pycache__", "venv") for x in rel.parts):
            out.append(str(rel))
    return out[:30]


_OUTLINE = re.compile(r"^\s*(?:(?:async\s+)?function\s+\w+|(?:const|let|var)\s+\w+\s*=|class\s+\w+|"
                      r"def\s+\w+|[A-Z_]+\s*=|<(?:button|input|canvas|div|ul|ol|p|h\d|span)\b[^>]*\bid=)",
                      re.M)


def _files_block(folder, paths, budget, full=None):
    """Current contents: files in `full` (or all, while they fit) whole,
    the rest as an outline of what they declare."""
    parts, used = [], 0
    full = set(paths if full is None else full)
    for rel in paths:
        p = Path(folder) / rel
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        n = text.count("\n") + 1
        if (rel in full or used + len(text) < budget // 2) and used + len(text) < budget:
            parts.append(f"=== {rel} ({n} lines) ===\n{text}")
            used += len(text)
        else:
            outline = [ln.strip()[:120] for ln in text.splitlines() if _OUTLINE.match(ln)][:40]
            parts.append(f"=== {rel} ({n} lines; outline only) ===\n" + "\n".join(outline))
    return "Current files:\n" + "\n\n".join(parts) if parts else "No files yet."


def _write(folder, rel, content):
    p = Path(folder) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _snapshot(folder, rels):
    snap = {}
    for rel in rels:
        p = Path(folder) / rel
        snap[rel] = p.read_text(errors="replace") if p.is_file() else None
    return snap


def _restore(folder, snap):
    for rel, text in snap.items():
        p = Path(folder) / rel
        if text is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(text)


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def _tests_for(plan, upto):
    return [{"name": str(i + 1), "actions": s["actions"], "checks": s["checks"]}
            for i, s in enumerate(plan["steps"][:upto + 1])]


def run_checks(plan, tests):
    """{name: {"passed", "problems"}} for each test, plus "_screenshot"."""
    if plan["kind"] == "web":
        return _web_checks(plan, tests)
    return _py_checks(plan, tests)


def _web_checks(plan, tests):
    main = Path(plan["folder"]) / plan["main"]
    if not main.is_file():
        return {t["name"]: {"passed": False, "problems": [f"{plan['main']} doesn't exist yet"]}
                for t in tests}
    # Step 1's page already links scripts a later step writes: an empty stub
    # keeps "file not found" from failing a step for something not due yet.
    linked = re.findall(r"""<(?:script[^>]*\bsrc|link[^>]*\bhref)\s*=\s*["']([^"':]+\.(?:js|css))["']""",
                        main.read_text(errors="replace"), re.I)
    for rel in [f["path"] for f in plan["files"]] + [r for r in (_rel(x) for x in linked) if r]:
        p = Path(plan["folder"]) / rel
        if p.suffix in (".js", ".css") and not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
    out = SHOTS_DIR / f"{int(time.time())}"
    try:
        r = subprocess.run([str(BROWSER_PY), str(PAGE_CHECKER),
                            json.dumps({"path": str(main), "out": str(out), "tests": tests})],
                           capture_output=True, text=True, timeout=60 + 25 * len(tests))
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:      # noqa: BLE001
        return {t["name"]: {"passed": False, "problems": [f"the browser check failed: {e}"]}
                for t in tests}
    if d.get("error"):
        return {t["name"]: {"passed": False, "problems": [d["error"]]} for t in tests}
    res = {"_screenshot": d.get("screenshot")}
    for t in d.get("tests", []):
        probs = [f"JavaScript error: {e}" for e in t.get("errors", [])]
        probs += [x for x in t.get("details", []) if not x.startswith("ok:")]
        res[t["name"]] = {"passed": bool(t.get("passed")), "problems": probs[:6],
                          "text": t.get("text", ""), "shot": t.get("shot")}
    return res


PY_PRELUDE = '''import json, os, shlex, shutil, subprocess, sys, traceback
MAIN = {main!r}
results = {{}}
for name, code in json.loads({tests!r}):
    shutil.rmtree("/tmp/p", ignore_errors=True)
    shutil.copytree("/work/in", "/tmp/p")
    prog = ("import os, shlex, subprocess, sys\\nos.chdir('/tmp/p'); sys.path.insert(0, '/tmp/p')\\n"
            "def run(args=''):\\n"
            "    r = subprocess.run([sys.executable, MAIN] + shlex.split(str(args)), capture_output=True, text=True, timeout=10)\\n"
            "    if r.returncode != 0:\\n"
            "        raise AssertionError(f'`python3 {{MAIN}} {{args}}` failed: ' + r.stderr.strip()[-400:])\\n"
            "    return r.stdout\\n"
            f"MAIN = {{MAIN!r}}\\n" + code)
    with open("/tmp/t.py", "w") as f:
        f.write(prog)
    r = subprocess.run([sys.executable, "/tmp/t.py"], capture_output=True, text=True, timeout=20, cwd="/tmp/p")
    err = r.stderr.strip().splitlines()
    results[name] = {{"passed": r.returncode == 0,
                      "problems": [] if r.returncode == 0 else [" | ".join(err[-4:]) or f"exit {{r.returncode}}"]}}
print("BUILD_RESULTS " + json.dumps(results))
'''


def _py_checks(plan, tests):
    import johan_sandbox as sb
    folder = Path(plan["folder"])
    files = [str(folder / f) for f in _existing_files(folder) if "/" not in f][:sb.MAX_FILES]
    if not (folder / plan["main"]).is_file():
        return {t["name"]: {"passed": False, "problems": [f"{plan['main']} doesn't exist yet"]}
                for t in tests}
    # The loads check: the main file imports and a bare run doesn't crash.
    payload = [(t["name"], "\n".join(t["checks"]) or "pass") for t in tests]
    code = PY_PRELUDE.format(main=plan["main"], tests=json.dumps(payload))
    res = sb.run_python(code, files=files, timeout=min(120, 20 + 12 * len(tests)))
    if res.get("error"):
        return {t["name"]: {"passed": False, "problems": [res["error"]]} for t in tests}
    m = re.search(r"BUILD_RESULTS (.*)", res.get("stdout") or "")
    if not m:
        err = (res.get("stderr") or "").strip().splitlines()[-3:]
        return {t["name"]: {"passed": False, "problems": [" | ".join(err) or "the check didn't run"]}
                for t in tests}
    return json.loads(m.group(1))


JUDGE_Q = "Does the page do what the test says it should?"
JUDGE_OPTS = {"yes": "yes: after those actions the page clearly shows what the test expects",
              "no": "no: it shows something else, or not enough to tell"}
JUDGE_NULL = "Test: N/A\nActions done: N/A\nPage text afterwards: N/A"


def _judge(plan, st, r):
    """A 4B writes working code more reliably than exact JavaScript asserts:
    a budget app showed "rent / $500" and failed a check that used
    getElementById with a CSS selector. When the asserts fail but the page
    ran cleanly (no JS error, every action found its target), the calibrated
    decision layer compares the page — its text and a screenshot — with the
    plan's plain-words test. Only a confident yes counts."""
    if plan["kind"] != "web" or not st.get("test") or r.get("passed"):
        return False
    if any(p.startswith(("JavaScript error", "action ")) for p in r.get("problems") or []):
        return False
    try:
        import base64
        import johan_decide as jd
        imgs = [base64.b64encode(Path(r["shot"]).read_bytes()).decode()] if r.get("shot") else None
        ctx = (f"Test: {st['test']}\nActions done: {'; '.join(st.get('actions') or []) or 'none'}\n"
               f"Page text afterwards: {' '.join(str(r.get('text') or '').split())[:700] or '(none)'}")
        d = jd.decide(JUDGE_Q, JUDGE_OPTS, ctx, kind="build_judge", null_context=JUDGE_NULL,
                      images=imgs)
        ok = d.choice == "yes" and d.confident
        log("judge", test=st["test"], text=str(r.get("text"))[:300], choice=d.choice,
            probs=getattr(d, "probs", None), ok=ok)
        return ok
    except Exception as e:      # noqa: BLE001 - no judge, no pass
        log("judge_error", error=str(e)[:200])
        return False


def _ok(plan, k, results):
    """Did step k's test pass — by its asserts, or by the judge? (cached)"""
    r = results.get(str(k + 1)) or {}
    if "ok" not in r:
        r["ok"] = bool(r.get("passed")) or _judge(plan, plan["steps"][k], r)
        if r["ok"] and not r.get("passed"):
            r["judged"] = True
    return r["ok"]


def _broke(results, plan, i):
    """Did this attempt break a step that passed before?"""
    return any(plan["steps"][k].get("passed") and not _ok(plan, k, results) for k in range(i))


def _report(results, plan, upto):
    """The failures to show the model, newest step first."""
    lines = []
    for i in range(upto, -1, -1):
        r = results.get(str(i + 1)) or {}
        if i < upto and not plan["steps"][i].get("passed"):
            continue              # failed before this step too: not this step's doing
        if not _ok(plan, i, results):
            who = "THIS step" if i == upto else f"step {i + 1} (worked before — your change broke it)"
            lines.append(f"- {who}: " + "; ".join(r.get("problems") or ["failed"])[:600])
            if r.get("text") is not None and i == upto:
                # Without this the model sees only "FALSE" — a working tip
                # calculator printing "Tip: $20.00" failed a check for
                # "Tip: 20" three times and it never knew why.
                lines.append("  the page showed after the actions: "
                             + (" ".join(str(r["text"]).split())[:300] or "(nothing)"))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the step
# --------------------------------------------------------------------------

TEST_HOWTO = {"web": """The test runs on a FRESHLY LOADED page — nothing from earlier tests
carries over, so do every action it needs (type into the inputs, click, press keys).
actions, written exactly like these examples:
  click Add            (clicks the button whose text is Add)
  fill 0 50            (types 50 into the first input; 1 is the second input)
  press ArrowUp        (also Space, Enter, a)
  hold ArrowLeft 500   (holds a key for 500 ms)
  wait 300
  shot a               (snapshot of the game canvas, compared in checks as shots.a)
  reload
checks: JavaScript expressions that must be true afterwards, using the REAL ids, texts
and global names in your code, e.g. document.body.innerText.includes('Total: 500'),
document.querySelectorAll('#list li').length === 1, paddle.x < 200, shots.a !== shots.b.""",
              "python": """The test runs in a FRESH copy of the folder. checks: Python lines with
assert. run("add 5 food") runs the main file with those arguments and returns what it
printed; you can also import the files. e.g.
  run("add 5 food")
  assert "5" in run("total")
actions: leave empty."""}


def _step_prompt(plan, i, feedback):
    st = plan["steps"][i]
    folder = plan["folder"]
    paths = [f["path"] for f in plan["files"]]
    full = set(st["files"]) | {plan["main"]}
    lines = [f"Project: {plan['title']} ({plan['kind']}) in {folder}",
             f"What Riadh asked: {plan['request']}",
             f"Spec: {plan['spec']}",
             "Files:\n" + "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan["files"]),
             "Steps:\n" + "\n".join(
                 f"{k + 1}. {s['goal']}" + ("  (done)" if k < i else "  <- NOW" if k == i else "")
                 for k, s in enumerate(plan["steps"])),
             _files_block(folder, [p for p in paths if (Path(folder) / p).is_file()], FILE_CHARS, full),
             f"Your step ({i + 1}/{len(plan['steps'])}): {st['goal']}",
             f"How to check it: {st.get('test') or 'the main file loads and runs with no errors'}"]
    if st.get("checks"):
        lines.append("The test for this step (keep it unless the test itself is wrong):\n"
                     + ("actions: " + "; ".join(st["actions"]) + "\n" if st["actions"] else "")
                     + "checks:\n" + "\n".join(st["checks"]))
    if feedback:
        lines.append("Your last attempt FAILED:\n" + feedback +
                     "\nFix the code so it passes. Change the test only if the test itself is "
                     "wrong (a check that throws by itself, an action naming something that isn't "
                     "on the page) — never weaken it to pass.")
    lines.append(TEST_HOWTO[plan["kind"]])
    lines.append("Return the COMPLETE new content of each file you create or change for this "
                 f"step (usually {', '.join(st['files'])}). Keep everything the earlier steps "
                 "built working. Also return this step's test (actions + checks): it must FAIL "
                 "on the code as it is now and PASS with your change. note: one short sentence "
                 "on what you did.")
    return "\n\n".join(lines)


def _passes_before(plan, test, snap):
    """Run a proposed test on a copy of the folder as it was BEFORE this step.
    True = it already passes there, so it proves nothing about the step."""
    tmp = SHOTS_DIR / f"before-{int(time.time() * 1000)}"
    try:
        shutil.copytree(plan["folder"], tmp)
        _restore(tmp, snap)
        copy = dict(plan, folder=str(tmp))
        r = run_checks(copy, [{"name": "t", "actions": test["actions"], "checks": test["checks"]}])
        return bool((r.get("t") or {}).get("passed"))
    except Exception:
        return True                # can't tell: don't accept it
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_problem(feedback):
    """After any failure the test may be rewritten — the red-first rule keeps it honest."""
    return bool(feedback)


VERBS = ("click", "fill", "press", "hold", "wait", "shot", "reload", "type")


def _join_actions(items):
    """["fill", "0", "rent"] -> ["fill 0 rent"]: the model sometimes splits one action."""
    out = []
    for a in (str(x).strip() for x in items or []):
        if not a:
            continue
        if out and a.split()[0].lower() not in VERBS:
            out[-1] = f"{out[-1]} {a}"
        else:
            out.append(a)
    return out


def _clean_test(out):
    return {"actions": [a[:120] for a in _join_actions(out.get("actions"))][:12],
            "checks": [str(c)[:300] for c in out.get("checks") or [] if str(c).strip()][:6]}


def _do_step(plan, i, should_stop, progress, deadline=None):
    """Up to MAX_TRIES attempts. Returns (passed, problems)."""
    folder = plan["folder"]
    st = plan["steps"][i]
    snap = _snapshot(folder, [f["path"] for f in plan["files"]])
    feedback, results = "", {}
    for attempt in range(MAX_TRIES):
        if should_stop and should_stop():
            raise ji.Interrupted()
        if attempt and deadline and time.monotonic() > deadline:
            break                   # out of time: keep what passes, report the rest
        progress(f"step {i + 1}/{len(plan['steps'])}: {st['goal']}"
                 + (f" — try {attempt + 1}" if attempt else ""))
        out = _chat([{"role": "system", "content": BUILD_SYSTEM},
                     {"role": "user", "content": _step_prompt(plan, i, feedback)}],
                    WRITE_SCHEMA, should_stop)
        if out.get("_error"):
            log("attempt", step=i + 1, attempt=attempt + 1, error=out["_error"])
            feedback = f"- {out['_error']}: write fewer or smaller files this time."
            continue
        # The step's test comes with its code: red before the change, green after.
        test_note = ""
        proposed = _clean_test(out)
        if proposed["checks"] and (not st.get("checks") or _test_problem(feedback)):
            if _passes_before(plan, proposed, snap):
                test_note = ("- your test already PASSES on the code as it was before this step, "
                             "so it proves nothing. Write checks that fail now and pass only "
                             "once the step works.")
                log("test_rejected", step=i + 1, test=proposed)
            else:
                log("test", step=i + 1, old={"actions": st.get("actions"), "checks": st.get("checks")},
                    new=proposed)
                st["actions"], st["checks"] = proposed["actions"], proposed["checks"]
        wrote = 0
        for f in out.get("files") or []:
            rel = _in_folder((f or {}).get("path"), folder)
            content = (f or {}).get("content")
            if not rel or not isinstance(content, str) or not content.strip():
                continue
            if plan["kind"] == "python" and "/" in rel:
                continue
            _write(folder, rel, content)
            snap.setdefault(rel, None)
            if rel not in [x["path"] for x in plan["files"]]:
                plan["files"].append({"path": rel, "purpose": ""})
            wrote += 1
        if not wrote:
            feedback = "- you returned no files. Return the full content of the files this step changes."
            continue
        if not st.get("checks"):
            feedback = test_note or "- you returned no test: give this step's actions and checks."
            continue
        results = run_checks(plan, _tests_for(plan, i))
        log("attempt", step=i + 1, attempt=attempt + 1, goal=st["goal"],
            wrote={f.get("path"): len(f.get("content") or "") for f in out.get("files") or []},
            note=out.get("note"), results={k: v for k, v in results.items() if k != "_screenshot"})
        if _ok(plan, i, results) and not _broke(results, plan, i):
            return True, [], results
        feedback = "\n".join(x for x in (test_note, _report(results, plan, i)) if x)
    # Out of tries. Keep the attempt only if it breaks nothing earlier and
    # doesn't throw — a half-working feature beats none. Otherwise roll back.
    earlier_broken = _broke(results, plan, i)
    had_working = any(plan["steps"][k].get("passed") for k in range(i))
    threw = any("JavaScript error" in p or "Traceback" in p or "didn't load" in p
                for p in (results.get(str(i + 1)) or {}).get("problems", []))
    # Roll back only to protect work that passed: with nothing working yet,
    # rolling step 1 back left an empty folder ("snake.html was never written").
    if earlier_broken or (threw and had_working) or (not results and had_working):
        _restore(folder, snap)
        return False, ["rolled back: " + (_report(results, plan, i) or "no working attempt")], results
    return False, (results.get(str(i + 1)) or {}).get("problems", []), results


# --------------------------------------------------------------------------
# the turn
# --------------------------------------------------------------------------

def maybe_build(messages, q, on_tool=None, on_delta=None, should_stop=None, decide=None):
    """The answer text when this message was a build (done or declined), else None."""
    if not wants_build(q, messages, decide):
        return None
    return build(messages, q, on_tool, on_delta, should_stop)


def build(messages, q, on_tool=None, on_delta=None, should_stop=None):
    import johan_assistant as ja
    t0 = time.monotonic()
    shown = []
    _LOG["file"] = None                  # one log file per build

    def progress(line):
        shown.append(line)
        if on_delta:
            on_delta(f"_{line}_\n")

    folder, main_name = named_target(q)
    earlier = None
    if folder is None:
        # "now add a reset button": the project this chat was just working on.
        last = LAST_FOLDER.get(ja.SESSION_ID)
        prev = "" if NEW_THING.search(q) else _previous_user(messages, q)
        if last and prev:
            folder = Path(last)
        elif prev:
            folder, main_name = named_target(prev)
    if folder is not None:
        earlier = load_state(folder)
        if earlier and not main_name:
            main_name = earlier.get("main")
    problem = folder_problem(folder) if folder is not None else None
    if problem:
        return f"I can't build there: {problem}."

    progress("planning")
    changes, plan = None, None
    for _ in range(MAX_REPLANS + 1):
        plan = make_plan(q, folder, main_name, changes, earlier, should_stop)
        if plan is None:
            return None            # no usable plan: the normal loop handles the message
        if folder is None:          # a new project gets its own folder
            f = PROJECTS / slug(plan["title"])
            n = 2
            while f.exists() and _existing_files(f):
                f, n = PROJECTS / f"{slug(plan['title'])}-{n}", n + 1
            plan["folder"] = str(f)
        problem = folder_problem(plan["folder"])
        if problem:
            return f"I can't build there: {problem}."
        overwrites = [p for p in (f["path"] for f in plan["files"])
                      if (Path(plan["folder"]) / p).is_file()]
        item = {"folder": plan["folder"], "title": plan["title"], "spec": plan["spec"],
                "steps": [s["goal"] for s in plan["steps"]], "files": [f["path"] for f in plan["files"]],
                "overwrites": overwrites, "why": plan["title"]}
        d = jap.ask("build", item)
        if d is None:
            if overwrites:
                return ("This would change files you already have " + ", ".join(overwrites[:5]) +
                        " — open me in the app (`johan`) so you can OK the plan first.")
            break                   # nobody to ask, and nothing of his is touched
        if d["choice"] in (jap.YES, jap.ALWAYS):
            break
        if d.get("reason") and "esc" not in d["reason"] and "no answer" not in d["reason"]:
            changes = d["reason"]
            progress("re-planning with what you said")
            continue
        return "Okay — I won't build it. Tell me what to change and I'll plan it again."
    else:
        return "I couldn't get a plan you liked — tell me again what you want and I'll start over."

    log("approved", plan=plan)
    Path(plan["folder"]).mkdir(parents=True, exist_ok=True)
    LAST_FOLDER[ja.SESSION_ID] = plan["folder"]
    # Earlier builds' passing checks stay part of the test suite.
    prior = [s for s in (earlier or {}).get("steps", []) if s.get("passed")]
    plan["steps"] = [dict(s, prior=True) for s in prior] + plan["steps"]
    first_new = len(prior)

    outcome, results = [], {}
    try:
        for i in range(first_new, len(plan["steps"])):
            if time.monotonic() - t0 > BUDGET_S:
                for s in plan["steps"][i:]:
                    outcome.append((s["goal"], False, ["not started — out of time; say \"go on\""]))
                break
            passed, problems, results = _do_step(plan, i, should_stop, progress,
                                                 deadline=t0 + BUDGET_S)
            plan["steps"][i]["passed"] = passed
            outcome.append((plan["steps"][i]["goal"], passed, problems))
            if on_tool:
                try:
                    on_tool("build_step", {"step": plan["steps"][i]["goal"]},
                            {"passed": passed, "problems": problems})
                except Exception:
                    pass
    except ji.Interrupted:
        outcome.append(("(stopped — you pressed esc)", False, []))
    for s in plan["steps"][:first_new]:
        s["passed"] = True
    save_state(plan["folder"], {"main": plan["main"], "kind": plan["kind"], "title": plan["title"],
                                "steps": [{k: s[k] for k in ("goal", "actions", "checks", "passed")
                                           if k in s} for s in plan["steps"]]})
    shot = results.get("_screenshot")
    if shot and on_tool:
        try:
            on_tool("build_done", {"path": str(Path(plan["folder"]) / plan["main"])},
                    {"path": str(Path(plan["folder"]) / plan["main"]), "screenshots": [shot],
                     "caption": f"{sum(1 for o in outcome if o[1])}/{len(outcome)} steps pass their checks",
                     "problems": ["none found"] if all(o[1] for o in outcome) else
                     [f"{o[0]}: {'; '.join(o[2])[:200]}" for o in outcome if not o[1]][:3]})
        except Exception:
            pass
    # Every step was a fresh prompt, and Ollama keeps each one in a RAM cache
    # (llama-server --cache-ram, 8 GB by default): a long build filled it and
    # the PC ran out of memory. Unloading frees it; the chat reloads behind.
    try:
        ja.release_model()
        ja.rewarm(messages)
    except Exception:
        pass
    return answer(plan, outcome, time.monotonic() - t0)


def answer(plan, outcome, seconds):
    ok = sum(1 for _, p, _ in outcome if p)
    main = Path(plan["folder"]) / plan["main"]
    head = (f"Built **{plan['title']}** — all {ok} steps pass their checks." if ok == len(outcome)
            else f"Built **{plan['title']}** — {ok} of {len(outcome)} steps pass their checks.")
    lines = [head, ""]
    for goal, passed, problems in outcome:
        lines.append(f"- {'✓' if passed else '✗'} {goal}" +
                     ("" if passed or not problems else f" — {'; '.join(problems)[:220]}"))
    run = (f"`{main}` (ctrl+o opens it)" if plan["kind"] == "web"
           else f"`cd {plan['folder']} && python3 {plan['main']}`")
    lines += ["", f"Open: {run}", f"_{len(outcome)} steps in {seconds:.0f}s · files in `{plan['folder']}`_"]
    if ok < len(outcome):
        lines.append("Say what to fix and I'll carry on from here.")
    return "\n".join(lines)
