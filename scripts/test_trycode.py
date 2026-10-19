"""try_code against the real sandbox (docker, no network), and the answer
check for Python. No model. Files live in a temp folder under ~/.cache.

Every failed code edit in the hard eval ended "Done — it now raises
ValueError" without anything having run it; this is the tool that does.

    python test_trycode.py      # ~15 s (three sandbox runs)
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_tools as jt          # noqa: E402
import johan_assistant as ja      # noqa: E402

results = {}


def check(label, ok):
    results[label] = bool(ok)


d = Path(tempfile.mkdtemp(prefix="johan-trycode-", dir=Path.home() / ".cache"))
try:
    (d / "inv.py").write_text("STOCK = {}\n\ndef add(n, q):\n    STOCK[n] = STOCK.get(n, 0) + q\n")
    (d / "data.csv").write_text("a,b\n1,2\n")
    r = jt.try_code(str(d / "inv.py"), "from inv import add, STOCK\nadd('x', 2)\nassert STOCK['x'] == 2")
    check("a correct check passes", r.get("passed") is True and r["problems"] == ["none found"])
    check("…and loads the files beside it", "inv.py" in r.get("files_loaded", [])
          and "data.csv" in r.get("files_loaded", []))
    r = jt.try_code(str(d / "inv.py"), "from inv import add, STOCK\nadd('x', 2)\nassert STOCK['x'] == 3, 'want 3'")
    check("a failing check fails, with the reason", r.get("passed") is False
          and "want 3" in r["problems"][0])
    r = jt.try_code(str(d / "inv.py"), "import csv\nrows = list(csv.reader(open('data.csv')))\nassert rows[1] == ['1','2']")
    check("data files beside the code are readable", r.get("passed") is True)
    check("a check without asserts is refused", "assert" in jt.try_code(str(d / "inv.py"), "print(1)").get("error", ""))
    check("only files in his home", "home" in jt.try_code("/etc/passwd", "assert 1").get("error", ""))
    check("not for web pages", "try_page" in jt.try_code(str(d / "x.html"), "assert 1").get("error", "")
          if (d / "x.html").write_text("<p>") or True else False)
    check("it's a tool the model can call", "try_code" in jt.DISPATCH
          and any(t["function"]["name"] == "try_code" for t in jt.TOOLS))
finally:
    shutil.rmtree(d, ignore_errors=True)

# The answer check, for Python: "Done" after a code change needs a passing try_code.
wrote = {"role": "tool", "content": json.dumps({"applied": True, "path": "/home/zeros/p/inv.py"})}
bad = {"role": "tool", "content": json.dumps({"problems": ["the check FAILED: AssertionError"],
                                              "tried_code": True, "path": "/home/zeros/p/inv.py"})}
good = {"role": "tool", "content": json.dumps({"problems": ["none found"], "tried_code": True,
                                               "path": "/home/zeros/p/inv.py"})}
say = lambda *t: [*t, {"role": "assistant", "content": "Done."}]   # noqa: E731
check("'Done' after a code change that never ran is flagged",
      "wasn't run" in ja._flag_untested("Done.", say(wrote), 0))
check("'Done' after a failing check is flagged", "AssertionError" in ja._flag_untested("Done.", say(wrote, bad), 0))
check("'Done' after a passing check is left alone", ja._flag_untested("Done.", say(wrote, good), 0) == "Done.")
check("a change, a pass, then another change needs another run",
      "wasn't run" in ja._flag_untested("Fixed.", say(wrote, good, wrote), 0))

for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad_ = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad_)}/{len(results)} pass")
sys.exit(1 if bad_ else 0)
