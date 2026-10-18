"""Assertion suite for the pure matching logic — runs in milliseconds, no DB,
no model, no GPU.

Every entry here is a bug that actually shipped in this project, not a
hypothetical. Per CLAUDE.md's working method: unit-test the pure logic instead
of re-running a 20-minute backfill to find out it broke.

    python test_matching.py
"""

import re
import sys

import stage4_assemble as s4
import stage5_verify as s5
import apply_queue as aq

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok    {label}")


# --------------------------------------------------------------------------
# skill_matches — naive substring matching inflated every score
# --------------------------------------------------------------------------
SKILL_CASES = [
    # (token, posting text, expected, why this case exists)
    ("erp",   "we build enterprise software", False, "'erp' inside 'ENT-ERP-RISE'"),
    ("erp",   "maintaining our ERP system",   True,  "real ERP mention"),
    ("git",   "digital transformation team",  False, "'git' inside 'DI-GIT-AL'"),
    ("git",   "experience with Git",          True,  "real Git mention"),
    ("java",  "JavaScript developer",         False, "'java' inside 'JavaScript'"),
    ("java",  "Java and Spring Boot",         True,  "real Java mention"),
    ("sql",   "PostgreSQL experience",        False, "'sql' inside 'PostgreSQL'"),
    ("cpp",   "strong C++ background",        True,  "matches via display name C++"),
    ("cpp",   "we use C++23 heavily",         True,  "no right-boundary for non-alnum tail"),
    ("ci-cd", "owns the CI/CD pipeline",      True,  "matches via display name CI/CD"),
]

for tok, text, want, why in SKILL_CASES:
    check(f"skill_matches({tok!r}, {why})", s4.skill_matches(tok, text.lower()), want)


# --------------------------------------------------------------------------
# dropped_metric leak detection — "25" is a substring of the year "2025"
# --------------------------------------------------------------------------
def metric_pattern(value):
    return re.compile(rf"(?<![\d.]){re.escape(str(value))}\\?%")


check("dropped_metric 25 vs year 2025",
      bool(metric_pattern(25).search("Sep. 2025 -- Jun. 2025")), False)
check("dropped_metric 25 real leak",
      bool(metric_pattern(25).search(r"a 25\% reduction")), True)
check("dropped_metric 25 vs 125%",
      bool(metric_pattern(25).search(r"a 125\% increase")), False)
check("dropped_metric 99.9 real leak",
      bool(metric_pattern(99.9).search(r"99.9\% uptime")), True)


# --------------------------------------------------------------------------
# gap_signal — checking one alias against title+desc in turn short-circuited
# before a more specific alias was ever tried against the title
# --------------------------------------------------------------------------
facts = s4.load_facts()

GAP_CASES = [
    ("C, Golang Software Engineer working on dqlite", "we use go and raft",
     ["go"], "'Golang' in title must beat a plain 'go' in the body"),
    ("Senior Juju Software Engineer (Go)", "golang service work",
     ["go"], "parenthesised (Go) in title"),
    ("Senior Software Engineer - Python/MongoDB", "python mongodb",
     ["mongodb"], "gap named in title"),
    ("Senior Software Engineer (Backend)", "java spring, kubernetes mentioned once",
     [], "gap only in body is not a title gap"),
]

for title, desc, want_title_gaps, why in GAP_CASES:
    got, _ = aq.gap_signal(facts, {"title": title, "raw_desc": desc})
    check(f"gap_signal({why})", got, want_title_gaps)


# --------------------------------------------------------------------------
# years_required / experience_tier — the queue handed him senior roles
# because min_years was extracted and then never used in ranking at all
# --------------------------------------------------------------------------
YEAR_CASES = [
    ("We need 7+ years of experience in backend", 7, "plain 7+"),
    ("Minimum 5 years of experience required", 5, "minimum N"),
    ("3-5 years of professional experience", 3, "range takes the low number it matches"),
    ("8+ years of professional experience and 3+ years with Python", 8, "max wins, not first"),
    ("A great place to work", None, "no figure"),
    ("Founded 25 years ago, we serve millions", None, "company age is not a requirement"),
]
for desc, want, why in YEAR_CASES:
    check(f"years_required({why})", aq.years_required({"raw_desc": desc}), want)

TIER_CASES = [
    ({"raw_desc": "7+ years of experience", "title": "Engineer",
      "parsed": {"seniority": "mid"}}, 2, "7 years is over-reach for 3"),
    ({"raw_desc": "5 years of experience", "title": "Engineer",
      "parsed": {"seniority": "mid"}}, 1, "5 years is a stretch"),
    ({"raw_desc": "", "title": "Senior Software Engineer",
      "parsed": {"seniority": "senior"}}, 1, "senior title, no years"),
    ({"raw_desc": "", "title": "Software Engineer",
      "parsed": {"seniority": "mid"}}, 0, "plain mid role fits"),
    ({"raw_desc": "", "title": "Staff Engineer",
      "parsed": {"seniority": "staff_plus"}}, 2, "staff is over-reach"),
]
for job, want, why in TIER_CASES:
    check(f"experience_tier({why})", aq.experience_tier(facts, job)[0], want)


# --------------------------------------------------------------------------
# build() must tolerate a hand-built Namespace. Adding --min-fit and
# --include-overreach to the CLI broke the build_apply_queue TOOL with
# AttributeError; dispatch handed the error to the model, which fell back to
# search_jobs (no experience filter) and recommended senior roles again.
# --------------------------------------------------------------------------
import argparse as _argparse

_orig_fetch = aq.fetch
aq.fetch = lambda args: []          # no DB, no model
try:
    minimal = _argparse.Namespace(verdict="likely", limit=3, job_id=[])
    aq.build(minimal)               # must not raise AttributeError
    check("build() works with a Namespace missing the newer flags", True, True)
except AttributeError as e:
    check(f"build() works with a Namespace missing the newer flags ({e})", False, True)
finally:
    aq.fetch = _orig_fetch


# --------------------------------------------------------------------------
# LaTeX escaping — an unescaped "C#" broke compilation entirely
# --------------------------------------------------------------------------
check("esc('C#')", s4.esc("C#"), r"C\#")
check("esc('R&D')", s4.esc("R&D"), r"R\&D")
check("csharp display name is escaped in skills block",
      r"C\#" in s4.render_skills(facts), True)


# --------------------------------------------------------------------------
# tailored_headline / render_skills — the top of the CV was identical on every
# application ("Java / Spring Boot" on a cross-platform C++ role). Reordering
# is allowed; claiming something he doesn't have is not.
# --------------------------------------------------------------------------
GAP_WORDS = ["kubernetes", "golang", " go ", "rust", "kafka", "react", "mongodb"]
for needle, why in [
    ("senior golang engineer, kubernetes, kafka, rust", "posting full of his gaps"),
    ("react frontend developer mongodb", "frontend gaps"),
]:
    head = s4.tailored_headline(facts, needle).lower()
    check(f"headline claims no known_gap ({why})",
          any(g.strip() in head for g in GAP_WORDS), False)

check("headline falls back to the fact store when nothing matches",
      s4.tailored_headline(facts, "we want a great person who ships"),
      facts["owner"]["headline"])
check("headline leads with C++ for a C++ posting",
      "C++" in s4.tailored_headline(facts, "cross-platform c++ systems programming"), True)

_plain = s4.render_skills(facts, "")
_cpp = s4.render_skills(facts, "cross-platform c++ multipass linux systems")
check("skills reorder changes order for a C++ posting", _plain != _cpp, True)
check("skills reorder does not change the SET of skills",
      sorted(re.findall(r"[A-Za-z0-9+#/ .-]+", _plain.replace("\\", ""))) ==
      sorted(re.findall(r"[A-Za-z0-9+#/ .-]+", _cpp.replace("\\", ""))), True)


# --------------------------------------------------------------------------
# bold_own_skills — nested \textbf{} produced malformed LaTeX
# --------------------------------------------------------------------------
out = s4.bold_own_skills("Wrote a chess engine in Java 17 using bitboards.",
                          ["java", "java-17"])
check("no nested textbf when one skill name contains another",
      "\\textbf{\\textbf{" in out, False)
check("plural is bolded whole, not 'integration'+'s'",
      "\\textbf{integration}s" in s4.bold_own_skills(
          "Implemented secure integrations between systems.", ["integration"]), False)


# --------------------------------------------------------------------------
# resume item extraction — the regex version matched zero items, ever
# --------------------------------------------------------------------------
TEX = r"""
        \resumeItem{Built \textbf{backend} services in \textbf{Java}.}
        \resumeItem{Plain item with no bolding.}
"""
items = s5.extract_resume_items(TEX)
check("extract_resume_items count with nested braces", len(items), 2)
check("extract_resume_items keeps full text",
      items[0], r"Built \textbf{backend} services in \textbf{Java}.")



# --- run_command: chaining, stderr sinks, and what stays refused ----------
# Every case here is a hop the model actually lost in a real conversation.
import johan_tools as _jt
from johan_assistant import _is_trivial as _triv

def _err(cmd):
    return _jt.run_command(cmd).get("error")

check("2>/dev/null is stripped, not refused", _err("ls /tmp 2>/dev/null") is None, True)
check("~ is expanded (no shell does it)", _jt.run_command("ls ~/johan/scripts")
      .get("command", "").startswith("ls /home/"), True)
check("~/.ssh still refused after expansion", bool(_err("cat ~/.ssh/id_rsa")), True)
check("2> /dev/null (spaced) is stripped", _err("ls /tmp 2> /dev/null") is None, True)
# A quoted pattern is the program's, not ours to expand: `find -name "*.py"`
# run from scripts/ became `find -name apply_queue.py embed_server.py ...`.
import os as _os
_here = _os.path.dirname(_os.path.abspath(__file__))
check("quoted -name \"*.py\" reaches find unexpanded",
      "test_matching.py" in _jt.run_command(f'find {_here} -maxdepth 1 -name "*matching*.py"')
      .get("output", ""), True)
check("escaped \\( and quoted globs work together",
      "test_matching.py" in _jt.run_command(
          f'find {_here} -maxdepth 1 -type f \\( -name "*.sh" -o -name "test_match*" \\)')
      .get("output", ""), True)
check("an unquoted glob is still expanded", "test_matching.py" in
      _jt.run_command(f"ls {_here}/test_match*").get("output", ""), True)
# `cd DIR && ls` is how the model writes it; with no shell, cd used to fall
# through to an approval card for a plain read.
_r = _jt.run_command(f"cd {_here} && ls test_matching.py")
check("cd sets the folder for the rest of the chain", "test_matching.py" in _r.get("output", ""), True)
check("cd into ~/.ssh is still refused", bool(_err("cd ~/.ssh && ls")), True)
check("a relative secret after cd is still refused", bool(_err("cd ~/johan/compose && cat .env")), True)
check("a quoted | is grep's, not a pipe", _jt.run_command(f"grep -c 'zzq|zzr' {_here}/test_matching.py")
      .get("exit_code"), 0)
check("2>&1 is stripped", _err("ls /tmp 2>&1") is None, True)
check("&& chains allowlisted commands", _err("echo a && echo b") is None, True)
check("|| chains allowlisted commands", _err("echo a || echo b") is None, True)
check("; chains allowlisted commands", _err("echo a ; echo b") is None, True)
check("a chain dies on its BAD link, before running the good one",
      "rm" in (_err("echo hi && rm -rf /tmp/x") or ""), True)
check("write redirect still refused", ">" in (_err("cat /etc/hostname > /tmp/x") or ""), True)
check("command substitution still refused", bool(_err("echo `whoami`")), True)
check("blocked command names a substitute, not all 127 names",
      "cut" in (_err("awk '{print $1}'") or "")
      and len(_err("awk '{print $1}'") or "") < 200, True)
check("flatpak is read-only: uninstall refused",
      "subcommands" in (_err("flatpak uninstall org.x") or ""), True)
check("snap is read-only: remove refused",
      "subcommands" in (_err("snap remove firefox") or ""), True)
check("&& runs the right-hand side when the left succeeds",
      "b" in _jt.run_command("echo a && echo b").get("output", ""), True)
check("|| skips the right-hand side when the left succeeds",
      "fallback" not in _jt.run_command("echo a || echo fallback").get("output", ""), True)

# --- trivial-message gate: whole message, never a substring --------------
check("'hi' is trivial", _triv("hi"), True)
check("'Thanks!' is trivial", _triv("Thanks!"), True)
check("'  OK  ' is trivial", _triv("  OK  "), True)
check("'hi, check my tf2 config' is NOT trivial (substring bug class)",
      not _triv("hi, can you check my tf2 config?"), True)
check("'ok so what is 2+2' is NOT trivial", not _triv("ok so what is 2+2"), True)
check("'no jobs found?' is NOT trivial", not _triv("no jobs found?"), True)


# --- decision layer: token parsing, option handling, calibration maths -----
# No model: _score_once is stubbed. These pin the arithmetic that code will
# act on without asking, so a refactor cannot silently flip a decision.
import johan_decide as _jd

check("letter token 'A'", _jd._letter_of("A"), "A")
check("letter token ' B' (leading space)", _jd._letter_of(" B"), "B")
check("letter token '**C'", _jd._letter_of("**C"), "C")
check("letter token 'a)' lowercase + paren", _jd._letter_of("a)"), "A")
check("prose token is not a letter", _jd._letter_of("The"), None)
check("'<think>' is not a letter", _jd._letter_of("<think>"), None)
try:
    _jd._normalise_options(["only one"])
    check("decide() refuses a single option", False, True)
except ValueError:
    check("decide() refuses a single option", True, True)

_orig_score, _orig_prior, _orig_log = _jd._score_once, _jd._prior, _jd._log
_jd._log = lambda *a, **k: None
try:
    # Raw model: 0.6 on "no", 0.4 on "yes". Prior leans 0.8 "no".
    # Calibrated: yes 0.4/0.2=2.0, no 0.6/0.8=0.75 -> yes wins (0.727).
    _jd._score_once = lambda q, items, c, s: ([0.4, 0.6], [])
    _jd._prior = lambda q, items, s, n: [0.2, 0.8]
    d = _jd.decide("q", {"yes": "y", "no": "n"}, "ctx", log=False)
    check("calibration flips a lean-driven 'no' to 'yes'", d.choice, "yes")
    check("calibrated probability", round(d.probs["yes"], 3), 0.727)
    d = _jd.decide("q", {"yes": "y", "no": "n"}, "ctx", calibrate=False, log=False)
    check("uncalibrated keeps the raw winner", d.choice, "no")
    # Floor: a prior of 0.001 must not turn a 0.02 option into the winner.
    _jd._score_once = lambda q, items, c, s: ([0.02, 0.98], [])
    _jd._prior = lambda q, items, s, n: [0.001, 0.999]
    d = _jd.decide("q", {"rare": "r", "usual": "u"}, "ctx", log=False)
    check("prior floor stops a near-zero prior inflating an option", d.choice, "usual")
    # Low coverage (model mostly wanted prose) is never confident.
    _jd._score_once = lambda q, items, c, s: ([0.30, 0.01], [])
    _jd._prior = lambda q, items, s, n: [0.5, 0.5]
    d = _jd.decide("q", {"a": "a", "b": "b"}, "ctx", log=False)
    check("coverage 0.31 is not confident even at p=0.97", d.confident, False)
    # debias maps reversed-order mass back to the right keys.
    calls = []
    def _stub(q, items, c, s):
        calls.append([k for k, _ in items])
        return ([0.9, 0.1], [])      # always favours whatever is listed first
    _jd._score_once = _stub
    d = _jd.decide("q", {"a": "a", "b": "b"}, "ctx", debias=True, calibrate=False, log=False)
    check("debias scores both orders", calls, [["a", "b"], ["b", "a"]])
    check("pure position bias averages out to 50/50", d.probs, {"a": 0.5, "b": 0.5})
finally:
    _jd._score_once, _jd._prior, _jd._log = _orig_score, _orig_prior, _orig_log


# --- propose_edit widened to git repos: every refusal must hold ------------
# Built in a throwaway repo under $HOME (the guard only allows $HOME), and
# every staged edit is discarded or applied inside it, so the real pending
# list and real files are untouched.
import shutil as _sh, subprocess as _sp, tempfile as _tf, os as _os
from pathlib import Path
import johan_tools as _jt2

_base = Path(_tf.mkdtemp(prefix="johan-edit-test-", dir=str(Path.home() / ".cache")))
_repo, _plain = _base / "repo", _base / "plain"
_repo.mkdir(); _plain.mkdir()
_sp.run(["git", "init", "-q", str(_repo)], check=True)
(_repo / "app.py").write_text("x = 1\n")
_sp.run(["git", "-C", str(_repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"], check=True)
_sp.run(["git", "-C", str(_repo), "add", "app.py"], check=True)
_sp.run(["git", "-C", str(_repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "app"], check=True)
(_repo / ".env").write_text("SECRET=1\n")
(_repo / "blob.bin").write_bytes(b"\x00\x01\x02 data")
(_plain / "notes.txt").write_text("hello\n")
(_repo / "escape").symlink_to("/etc/hostname")
(_repo / "sideways").symlink_to(_plain / "notes.txt")
_before = _jt2._load_edits()

def _prop(path, old="x = 1", new="x = 2"):
    return _jt2.propose_edit(str(path), old, new, "test")

try:
    r = _prop(_repo / "app.py")
    check("edit in another git repo is staged", r.get("proposed"), True)
    check("display names the repo", r.get("path", "").startswith("repo:"), True)
    a = _jt2.apply_edit(r["edit_id"])
    check("applied edit writes the file", (_repo / "app.py").read_text(), "x = 2\n")
    diff = _sp.run(["git", "-C", str(_repo), "diff", "--stat"], capture_output=True, text=True).stdout
    check("…and shows up in git diff", "app.py" in diff, True)
    check("outside home refused", "outside your home" in _prop("/etc/hostname", "pop", "x").get("error", ""), True)
    check("home but not a git repo refused", "not inside" in _prop(_plain / "notes.txt", "hello", "bye").get("error", ""), True)
    check("secret file refused", "secret" in _prop(_repo / ".env", "SECRET=1", "X").get("error", ""), True)
    check(".git internals refused", ".git internals" in _prop(_repo / ".git" / "config", "[core]", "x").get("error", ""), True)
    check("symlink out of home refused", "outside your home" in _prop(_repo / "escape", "pop", "x").get("error", ""), True)
    check("symlink to a non-repo file refused", "not inside" in _prop(_repo / "sideways", "hello", "x").get("error", ""), True)
    check("binary file refused", "binary" in _prop(_repo / "blob.bin", "data", "x").get("error", ""), True)
    check("relative path still means ~/johan", "no such file" in _prop("scripts/does-not-exist.py").get("error", ""), True)
    # Swap the file for an escaping symlink AFTER proposing: apply must refuse.
    (_repo / "app.py").write_text("y = 1\n")
    r2 = _jt2.propose_edit(str(_repo / "app.py"), "y = 1", "y = 2", "test")
    (_repo / "app.py").unlink(); (_repo / "app.py").symlink_to(_plain / "notes.txt")
    a2 = _jt2.apply_edit(r2["edit_id"])
    check("apply re-validates: a symlink swapped in after proposing is refused", "not inside" in a2.get("error", ""), True)
    check("…and the symlink target was not written", (_plain / "notes.txt").read_text(), "hello\n")
finally:
    _jt2._save_edits(_before)          # drop anything this test staged
    _sh.rmtree(_base, ignore_errors=True)
    for b in _jt2.EDIT_BACKUPS.glob("repo__*"):
        b.unlink()

# --- chats own their ids and their images ----------------------------------
# ctrl+n used to start a "new" chat that overwrote the previous one's file,
# and deleting a chat left the images drawn in it behind.
import json as _json, time as _time, johan_assistant as _ja, johan_images as _ji
_saved_sid = _ja.SESSION_ID
_tag = f"t{int(_time.time())}"
# save_session also writes the --continue pointer; never leave his pointing
# at a test chat.
_ptr = {f: (f.read_bytes() if f.exists() else None) for f in (_ja.SESSION_FILE, _ja.LAST_ID_FILE)}
try:
    _ja.use_session(f"2000-01-01-{_tag}a"); _ja.save_session([{"role": "user", "content": "chat A"}])
    _ja.use_session(f"2000-01-01-{_tag}b"); _ja.save_session([{"role": "user", "content": "chat B"}])
    _a = _ja.SESSIONS_DIR / f"2000-01-01-{_tag}a.json"
    check("a second chat does not overwrite the first", "chat A" in _a.read_text(), True)
    _ji.OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, sid in (("ia", "a"), ("ib", "b")):
        (_ji.OUT_DIR / f"{_tag}{name}.png").write_bytes(b"x")
        (_ji.OUT_DIR / f"{_tag}{name}.json").write_text(_json.dumps({"session": f"2000-01-01-{_tag}{sid}"}))
    _res = _ja.delete_session(f"2000-01-01-{_tag}a")
    check("deleting a chat deletes its images", (_ji.OUT_DIR / f"{_tag}ia.png").exists(), False)
    check("…and says so", any("image" in r for r in _res.get("removed", [])), True)
    check("another chat's images survive", (_ji.OUT_DIR / f"{_tag}ib.png").exists(), True)
    _old = _ja.SESSION_ID; _time.sleep(1.1)
    check("new_session() gives a new id", _ja.new_session() != _old, True)
finally:
    _ja.delete_session(f"2000-01-01-{_tag}b")
    for f in _ji.OUT_DIR.glob(f"{_tag}*"):
        f.unlink()
    _ja.use_session(_saved_sid)
    for f, data in _ptr.items():
        if data is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(data)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("all checks pass")
