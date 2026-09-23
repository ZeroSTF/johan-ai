"""Integration test for application autofill, against a LOCAL mock form.

Never touches a real company: Ashby uploads an attached CV to the employer
the moment it is attached, before Submit, so testing on a live form would
send his CV somewhere he did not choose. The mock has a submit trap —
`onsubmit` sets window.__submitted — and the one assertion that matters most
is that it never fires.

Needs the model (drafting) and ~/johan/venv-browser. ~1 minute.

    python test_autofill.py
"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import yaml                                   # noqa: E402
import johan_autofill as jf                    # noqa: E402

MOCK = Path(__file__).parent / "testdata/mock_application.html"


def main():
    url = f"file://{MOCK}"
    out = subprocess.run([str(jf.PY), str(jf.FILLER), "--extract", url],
                         capture_output=True, text=True, timeout=120).stdout
    fields = json.loads(out.strip().splitlines()[-1])["fields"]
    answers = copy.deepcopy(yaml.safe_load(jf.ANSWERS.read_text()))
    answers["confirmed"] = True            # in-memory copy; the real file is untouched
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(b"%PDF-1.4\n% dummy\n")
        cv = fh.name
    plan = jf.build_plan(fields, answers, cv, {"title": "Backend Engineer", "company": "Mock"})
    proc = subprocess.run([str(jf.PY), str(jf.FILLER), "--fill", url, "--test"],
                          input=json.dumps(plan) + "\n", capture_output=True,
                          text=True, timeout=180)
    lines = proc.stdout.strip().splitlines()
    state = json.loads(lines[-1])
    v, r = state["values"], state["radios"]
    checks = {
        "first name from profile": v.get("f0") == answers["profile"]["first_name"],
        "email from profile": v.get("f1") == answers["profile"]["email"],
        "github from profile": v.get("f2") == answers["profile"]["github"],
        "CV attached": v.get("f3") == [Path(cv).name],
        "radio 'Yes' checked, 'No' not": r.get("f4_0") is True and r.get("f4_1") is False,
        "select set to the saved answer": v.get("f5") == answers["screening"]["country_of_residence"]["answer"],
        "banner shown": state["banner"] is True,
        "THE FORM WAS NOT SUBMITTED": state["submitted"] is False,
        "no .click( in the filler": ".click(" not in "".join(
            l for l in jf.FILLER.read_text().splitlines() if not l.strip().startswith(("#", '"', "no code path"))),
    }
    # The visible window needs FULL Chromium; every other check here runs
    # headless, which only needs the headless shell. That gap is exactly how
    # the first real `f` press failed with nothing on screen.
    probe = subprocess.run([str(jf.PY), "-c",
        "from playwright.sync_api import sync_playwright\n"
        "import os\n"
        "with sync_playwright() as p: print(os.path.exists(p.chromium.executable_path))"],
        capture_output=True, text=True, timeout=60)
    checks["full Chromium installed (needed for the visible window)"] = probe.stdout.strip() == "True"
    bad = [k for k, ok in checks.items() if not ok]
    for k, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'}  {k}")
    Path(cv).unlink(missing_ok=True)
    print("\nall autofill checks pass" if not bad else f"\n{len(bad)} FAILURE(S)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
