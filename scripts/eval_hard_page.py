"""Grade a page Johan built: run hidden actions, evaluate hidden checks.

Runs in ~/johan/venv-browser (Playwright). eval_hard.py calls it with one
JSON argument {"path", "steps": [...], "checks": [...]}; prints one JSON line
{"passed": bool, "details": [...]}. Steps: ["click", text] | ["press", key]
| ["fill", n, value] (n-th input/textarea) | ["wait", ms] | ["shot", name]
| ["hold", key, ms] | ["reload"] | ["check", js] (a check in the middle).
Checks are JS expressions returning true/false; `shots[name]` holds earlier
canvas data URLs so a check can ask "did the canvas change?".
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    job = json.loads(sys.argv[1])
    out = {"passed": False, "details": []}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--disable-gpu"])
        p = b.new_page(viewport={"width": 1000, "height": 800})
        errors = []
        p.on("pageerror", lambda e: errors.append(str(e)[:200]))
        p.route("**/*", lambda r: r.continue_() if r.request.url.startswith(("file://", "data:"))
                else r.abort())
        p.goto(Path(job["path"]).resolve().as_uri(), wait_until="load", timeout=15000)
        p.wait_for_timeout(500)
        p.evaluate("window.__shots = {}")
        p.mouse.click(3, 3)
        mid_ok = True
        for st in job.get("steps", []):
            try:
                if st[0] == "click":
                    p.get_by_role("button", name=st[1]).or_(p.get_by_text(st[1], exact=False)).first.click(timeout=2000)
                elif st[0] == "press":
                    p.keyboard.press(st[1])
                elif st[0] == "fill":
                    p.locator("input, textarea").nth(st[1]).fill(str(st[2]), timeout=2000)
                elif st[0] == "hold":
                    p.keyboard.down(st[1])
                    p.wait_for_timeout(st[2])
                    p.keyboard.up(st[1])
                elif st[0] == "reload":
                    kept = p.evaluate("window.__shots || {}")
                    p.reload(wait_until="load")
                    p.wait_for_timeout(500)
                    p.evaluate("s => { window.__shots = s }", kept)
                elif st[0] == "check":
                    r = bool(p.evaluate(f"(() => {{ const shots = window.__shots; return ({st[1]}); }})()"))
                    out["details"].append(f"{'ok' if r else 'FAIL'}: {st[1][:90]}")
                    mid_ok = mid_ok and r
                elif st[0] == "wait":
                    p.wait_for_timeout(st[1])
                elif st[0] == "shot":
                    p.evaluate("n => { const c = [...document.querySelectorAll('canvas')]"
                               ".sort((a, b) => b.width * b.height - a.width * a.height)[0];"
                               " window.__shots[n] = c ? c.toDataURL() : null; }", st[1])
                p.wait_for_timeout(120)
            except Exception as e:
                out["details"].append(f"step {st}: {type(e).__name__}")
        ok = mid_ok
        for chk in job.get("checks", []):
            try:
                r = bool(p.evaluate(f"(() => {{ const shots = window.__shots; return ({chk}); }})()"))
            except Exception as e:
                r = False
                out["details"].append(f"check error {chk[:60]}: {str(e)[:100]}")
            out["details"].append(f"{'ok' if r else 'FAIL'}: {chk[:90]}")
            ok = ok and r
        out["passed"] = ok and not errors
        if errors:
            out["details"].append(f"page errors: {errors[:2]}")
        b.close()
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"passed": False, "details": [f"grader crashed: {type(e).__name__}: {e}"]}))
