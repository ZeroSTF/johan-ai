"""The build engine with the model stubbed: real browser and sandbox checks,
scripted "model" replies. No GPU. ~1 min.

What it pins: the code gate, where a build may write (never ~/johan, never
outside its folder), retries that carry the real failure, rollback when a step
breaks an earlier one, Python checks through run(), the approval paths, and
that a follow-up keeps the earlier checks as regressions.

    python test_build.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_approval as jap      # noqa: E402
import johan_build as jb          # noqa: E402

results = {}


def check(label, ok):
    results[label] = bool(ok)


class D:
    def __init__(self, choice, confident=True):
        self.choice, self.confident = choice, confident


asked = []


def dec(choice):
    def f(*a, **k):
        asked.append(a)
        return D(choice)
    return f


# --- when -------------------------------------------------------------------
asked.clear()
check("'hi' never reaches the decision", not jb.wants_build("hi", decide=dec("build")) and not asked)
check("a question with no make-verb never reaches it",
      not jb.wants_build("what's the best game this year?", decide=dec("build")) and not asked)
check("'make me a snake game' + decision build -> build",
      jb.wants_build("make me a snake game", decide=dec("build")))
check("…decision small -> not a build", not jb.wants_build("make the add button bigger in app.js",
                                                          decide=dec("small")))
check("the follow-up is judged WITH his previous message",
      jb.wants_build("now add a reset button", [{"role": "user", "content": "make a counter page"},
                                                {"role": "assistant", "content": "done"},
                                                {"role": "user", "content": "now add a reset button"}],
                     decide=dec("build")) and "make a counter page" in asked[-1][2])

# --- where ------------------------------------------------------------------
home = Path.home()
f, m = jb.named_target("make a counter at ~/projects/x/counter.html please")
check("a named file -> its folder + main name", f == home / "projects/x" and m == "counter.html")
f, m = jb.named_target("build it in /home/zeros/code/game/")
check("a named folder -> that folder", f == Path("/home/zeros/code/game") and m is None)
check("never inside ~/johan", "never" in (jb.folder_problem(home / "johan/scripts/x") or ""))
check("never ~/johan itself", jb.folder_problem(home / "johan"))
check("never home itself or outside it", jb.folder_problem(home) and jb.folder_problem("/etc/x"))
check("never a hidden settings folder", jb.folder_problem(home / ".config/x"))
check("~/projects/x and ~/.cache/x are fine",
      jb.folder_problem(home / "projects/x") is None and jb.folder_problem(home / ".cache/x") is None)
check("paths in a reply stay inside the folder",
      jb._rel("/etc/passwd.txt") is None and jb._rel("a/../../b.js") is None
      and jb._rel("x.exe") is None and jb._rel("js/game.js") == "js/game.js")
check("'make me a calculator' after a game is a NEW project",
      jb.NEW_THING.search("make me a calculator") and not jb.NEW_THING.search("now add a reset button"))

check("a path repeating the folder's name lands in the folder, not one deeper",
      jb._in_folder("tip-0/tip.html", "/home/zeros/x/tip-0") == "tip.html"
      and jb._in_folder("/home/zeros/x/tip-0/a.js", "/home/zeros/x/tip-0") == "a.js"
      and jb._in_folder("/etc/a.js", "/home/zeros/x/tip-0") is None)
import johan_decide as jd        # noqa: E402
_real_decide, judged = jd.decide, []
jd.decide = lambda *a, **k: judged.append(a) or D("yes")
try:
    stj = {"goal": "g", "test": "type rent and 500, click Add: the list shows rent", "actions": ["click Add"]}
    plan_w = {"kind": "web", "steps": [stj]}
    check("asserts failed on a clean page -> the judge decides (confident yes passes)",
          jb._ok(plan_w, 0, {"1": {"passed": False, "problems": ["FALSE: x"], "text": "rent / $500"}})
          and judged)
    judged.clear()
    check("…never when the page threw an error",
          not jb._ok(plan_w, 0, {"1": {"passed": False, "problems": ["JavaScript error: boom"]}}) and not judged)
    check("…never when an action found nothing to click",
          not jb._ok(plan_w, 0, {"1": {"passed": False, "problems": ["action 'click Add' failed: x"]}})
          and not judged)
    jd.decide = lambda *a, **k: D("yes", confident=False)
    check("…and an unsure yes is a no",
          not jb._ok(plan_w, 0, {"1": {"passed": False, "problems": ["FALSE: x"], "text": "?"}}))
finally:
    jd.decide = _real_decide

# --- the plan ---------------------------------------------------------------
p = jb._clean_plan({"title": "T", "kind": "web", "main": "page.html", "spec": "s",
                    "files": [{"path": "app.js", "purpose": "x"}],
                    "steps": [{"goal": "g", "files": ["app.js"], "actions": [], "checks": ["true"]}] * 9},
                   "/tmp/f", "counter.html", "req")
check("the main file he named wins", p["main"] == "counter.html" and p["files"][0]["path"] == "counter.html")
check("at most 7 steps", len(p["steps"]) == jb.MAX_STEPS)
check("an unusable plan -> None (the loop answers instead)",
      jb._clean_plan({"_error": "cut"}, "/tmp/f", None, "r") is None
      and jb._clean_plan({"title": "t", "kind": "web", "steps": []}, "/tmp/f", None, "r") is None)

# --- a whole build, model scripted ------------------------------------------
GOOD1 = "<html><body><p>Count: <span id=n>0</span></p><button id=b>Add</button><script src='app.js'></script></body></html>"
JS_OK = "let count = 0; b.onclick = () => { count++; n.textContent = count; };"
JS_THROWS = "let count = 0; b.onclick = () => { count++; undefinedThing.go(); };"
JS_BREAKS_PAGE = "document.body.innerHTML = ''; let count = 0;"

PLAN = {"title": "Counter", "kind": "web", "main": "index.html", "spec": "a counter",
        "files": [{"path": "index.html", "purpose": "page"}, {"path": "app.js", "purpose": "logic"}],
        "steps": [{"goal": "page with a count and an Add button", "files": ["index.html"],
                   "actions": [], "checks": ["document.body.innerText.includes('Count: 0')"]},
                  {"goal": "Add increases count", "files": ["app.js"], "actions": ["click Add", "click Add"],
                   "checks": ["document.body.innerText.includes('Count: 2')", "count === 2"]}]}

import johan_assistant as ja      # noqa: E402
ja.release_model = ja.rewarm = lambda *a, **k: None     # a test must not load the model
tmp = Path(tempfile.mkdtemp(prefix="johan-build-test-", dir=home / ".cache"))
real = (jb._chat, jb.STATE_DIR, jb.SHOTS_DIR)
jb.STATE_DIR, jb.SHOTS_DIR = tmp / "_state", tmp / "_shots"
prompts = []


def scripted(replies):
    seq = list(replies)

    def chat(messages, schema, should_stop=None, timeout=300):
        prompts.append(messages[-1]["content"])
        return seq.pop(0) if seq else {"_error": "no more replies"}
    return chat


def W(**files):
    return {"files": [{"path": k.replace("_", "."), "content": v} for k, v in files.items()], "note": "x"}


try:
    yes = lambda kind, item: {"choice": "yes"}     # noqa: E731
    folder = tmp / "counter"
    jb._chat = scripted([PLAN, W(index_html=GOOD1), W(app_js=JS_THROWS), W(app_js=JS_OK)])
    with jap.approver(yes):
        ans = jb.build([{"role": "user", "content": "q"}], f"make a counter page at {folder}/index.html")
    check("a 2-step build passes both steps", "all 2 steps pass" in ans)
    check("…the retry was told the real JavaScript error",
          any("FAILED" in p and "undefinedThing" in p for p in prompts))
    check("…the files are in its folder", (folder / "app.js").read_text() == JS_OK)
    check("…nothing was written outside it", sorted(x.name for x in tmp.iterdir()) == ["_shots", "_state", "counter"])
    state = jb.load_state(folder)
    check("…its checks are saved for next time", state and len(state["steps"]) == 2
          and all(s["passed"] for s in state["steps"]))

    # A follow-up whose every attempt breaks step 1 is rolled back.
    prompts.clear()
    PLAN2 = {"title": "Counter", "kind": "web", "main": "index.html", "spec": "a counter",
             "files": [{"path": "app.js", "purpose": "logic"}],
             "steps": [{"goal": "Reset button", "files": ["app.js"], "actions": ["click Reset"],
                        "checks": ["count === 0"]}]}
    jb._chat = scripted([PLAN2] + [W(app_js=JS_BREAKS_PAGE)] * 3)
    with jap.approver(yes):
        ans = jb.build([{"role": "user", "content": f"make a counter page at {folder}/index.html"},
                        {"role": "assistant", "content": "done"},
                        {"role": "user", "content": f"now add a reset button to {folder}/index.html"}],
                       f"now add a reset button to {folder}/index.html")
    check("a step that breaks earlier checks is rolled back", (folder / "app.js").read_text() == JS_OK)
    check("…and reported as not done", "0 of 1 steps" in ans and "rolled back" in ans)
    check("…the earlier steps' checks were run as regressions",
          any("step 1 (worked before" in p or "step 2 (worked before" in p for p in prompts))
    check("…the plan was told what was already built", "Earlier steps of this project" in prompts[0])

    # Python: checks run in the sandbox through run().
    PY = """import json, sys
from pathlib import Path
F = Path(__file__).with_name("data.json")
d = json.loads(F.read_text()) if F.exists() else []
if sys.argv[1] == "add":
    d.append(float(sys.argv[2])); F.write_text(json.dumps(d))
elif sys.argv[1] == "total":
    print(sum(d))
"""
    PPLAN = {"title": "Sum", "kind": "python", "main": "tally.py", "spec": "adds numbers",
             "files": [{"path": "tally.py", "purpose": "cli"}],
             "steps": [{"goal": "add and total", "files": ["tally.py"], "actions": [],
                        "checks": ['run("add 2")\nrun("add 3.5")\nassert "5.5" in run("total")']}]}
    jb._chat = scripted([PPLAN, W(tally_py=PY.replace("print(sum(d))", "print(sum(d) + 1)")), W(tally_py=PY)])
    with jap.approver(yes):
        ans = jb.build([{"role": "user", "content": "q"}], f"make a tally cli at {tmp}/tally/tally.py")
    check("a Python build passes through run() in the sandbox", "all 1 steps pass" in ans)
    check("…the failing try showed the assert", any("FAILED" in p and "AssertionError" in p for p in prompts))
    check("…and the data file it wrote in the check stayed in the sandbox",
          not (tmp / "tally/data.json").exists())

    # Red first: a step's own test must fail on the code before the change.
    prompts.clear()
    NEWPLAN = {"title": "Counter", "kind": "web", "main": "index.html", "spec": "a counter",
               "files": [{"path": "index.html", "purpose": "page"}, {"path": "app.js", "purpose": "logic"}],
               "steps": [{"goal": "page", "files": ["index.html"], "test": "the page shows Count: 0"},
                         {"goal": "Add counts", "files": ["app.js"], "test": "click Add twice: Count: 2"}]}

    def WT(actions, checks, **files):
        return dict(W(**files), actions=actions, checks=checks)
    jb._chat = scripted([NEWPLAN,
                         WT([], ["document.body.innerText.includes('Count: 0')"], index_html=GOOD1),
                         WT([], ["true"], app_js=JS_OK),
                         WT(["click Add", "click Add"], ["count === 2", "document.body.innerText.includes('Count: 2')"],
                            app_js=JS_OK)])
    with jap.approver(yes):
        ans = jb.build([], f"make a counter page at {tmp}/red/index.html")
    check("a test that already passes before the change is refused", any(
        "already PASSES" in p for p in prompts))
    check("…the real test that came next was used, and the build passed", "all 2 steps pass" in ans
          and jb.load_state(tmp / "red")["steps"][1]["checks"][0] == "count === 2")

    # Approval paths.
    jb._chat = scripted([PLAN])
    with jap.approver(lambda k, i: {"choice": "no", "reason": ""}):
        ans = jb.build([], f"make a counter page at {tmp}/c2/index.html")
    check("a plain no builds nothing", "won't build" in ans and not (tmp / "c2").exists())
    prompts.clear()
    jb._chat = scripted([PLAN, PLAN, W(index_html=GOOD1), W(app_js=JS_OK)])
    said = iter([{"choice": "no", "reason": "use a green button"}, {"choice": "yes"}])
    with jap.approver(lambda k, i: next(said)):
        ans = jb.build([], f"make a counter page at {tmp}/c3/index.html")
    check("typing instead re-plans with what he said", "use a green button" in prompts[1]
          and "all 2 steps pass" in ans)
    jb._chat = scripted([PLAN])
    ans = jb.build([], f"make a counter page at {folder}/index.html")
    check("nobody to ask + his files would change -> refuses, changes nothing",
          "OK the plan" in ans and (folder / "app.js").read_text() == JS_OK)
    check("building in ~/johan is refused before any model call",
          "never build inside ~/johan" in jb.build([], "make a page at ~/johan/scripts/x.html"))
finally:
    jb._chat, jb.STATE_DIR, jb.SHOTS_DIR = real
    shutil.rmtree(tmp, ignore_errors=True)

for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
