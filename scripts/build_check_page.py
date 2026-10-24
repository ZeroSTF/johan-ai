"""Run a build's checks against a local page. Used by johan_build.py.

Runs in ~/johan/venv-browser (Playwright). One JSON argument:
    {"path": ".../index.html", "out": ".../shots", "tests": [
        {"name": "1", "actions": ["click Add", "press ArrowUp", "hold ArrowLeft 400",
                                  "fill 0 hello", "wait 300", "shot a", "reload"],
         "checks": ["document.body.innerText.includes('1')", "shots.a !== shots.b"]}]}
Each test gets a FRESH page load, so one step's clicks never leak into the
next step's test. Prints one JSON line: {"tests": [{"name", "passed",
"details", "errors"}], "screenshot"}.

Like try_page: file:// only, every other request is refused, nothing is
submitted anywhere. A check is a JS expression evaluated in the page; `shots`
holds canvas snapshots taken by "shot <name>" (the largest canvas, as a data
URL), so "did the game move" is `shots.a !== shots.b`.
"""
import json
import re
import shlex
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SETTLE_MS = 500
GAP_MS = 120


KEYS = {"space": "Space", " ": "Space", "enter": "Enter", "return": "Enter", "esc": "Escape",
        "escape": "Escape", "tab": "Tab", "backspace": "Backspace", "up": "ArrowUp",
        "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight", "arrowup": "ArrowUp",
        "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight"}


def _unquote(t):
    t = t.strip()
    while len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"`":
        t = t[1:-1].strip()
    return t


def _key(k):
    k = _unquote(k)
    return KEYS.get(k.lower(), k)


def _target(page, t, kind="click"):
    """What the model wrote → a locator. It writes 'Add', "Add", #add,
    <button text='Add'>, button Add, button#add — all mean the same element."""
    t = t.strip()
    m = re.search(r"""(?:text|name|label)\s*=\s*['"]([^'"]+)['"]""", t)
    if m:
        t = m.group(1)
    elif t.startswith("<"):
        inner = re.sub(r"<[^>]*>", "", t).strip()
        q = re.findall(r"""['"]([^'"]+)['"]""", t)
        t = inner or (q[-1] if q else t.strip("<>"))
    t = _unquote(t)
    if t[:1] in "#.[" or re.match(r"^\w+[#.\[]", t) or re.search(r":nth-|\s>\s", t):
        return page.locator(t).first              # a CSS selector
    t = re.sub(r"^(?:the\s+)?(?:button|link)\s+", "", t, flags=re.I)
    t = _unquote(t)
    ident = re.sub(r"[^\w-]", "", t)
    by_id = page.locator(f"#{ident}, [name='{ident}']") if ident else None
    if kind == "click":
        loc = page.get_by_role("button", name=t).or_(page.get_by_text(t))
    else:
        loc = page.get_by_label(t).or_(page.get_by_placeholder(t))
    return (loc.or_(by_id) if by_id is not None else loc).first


def act(page, line, shots):
    line = str(line).strip()
    verb, _, rest = line.partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb == "click":
        _target(page, rest).click(timeout=2000)
    elif verb == "press":
        page.keyboard.press(_key(rest))
    elif verb == "hold":
        parts = rest.rsplit(None, 1)
        ms = int(re.sub(r"\D", "", parts[1]) or 400) if len(parts) == 2 and re.search(r"\d", parts[1]) else 400
        key = _key(parts[0] if len(parts) == 2 and re.search(r"\d", parts[1]) else rest)
        page.keyboard.down(key)
        page.wait_for_timeout(min(ms, 5000))
        page.keyboard.up(key)
    elif verb == "fill":
        try:
            parts = shlex.split(rest)
        except ValueError:
            parts = rest.split()
        where, text = (parts[0], " ".join(parts[1:])) if parts else ("0", "")
        if re.fullmatch(r"\d+", _unquote(where)):
            box = page.locator("input, textarea, select").nth(int(_unquote(where)))
        else:
            box = _target(page, where, kind="fill")
        box.fill(text, timeout=2000)
    elif verb == "type":
        page.keyboard.type(_unquote(rest))
    elif verb == "wait":
        page.wait_for_timeout(min(int(re.sub(r"\D", "", rest) or 300), 8000))
    elif verb == "shot":
        shots[_unquote(rest) or "x"] = page.evaluate(
            "() => { const c = [...document.querySelectorAll('canvas')]"
            ".sort((a, b) => b.width * b.height - a.width * a.height)[0];"
            " return c ? c.toDataURL() : document.body.innerHTML; }")
    elif verb == "reload":
        page.reload(wait_until="load")
        page.wait_for_timeout(SETTLE_MS)
    else:
        raise ValueError(f"unknown action {verb!r} (click, press, hold, fill, type, wait, shot, reload)")


def run_test(browser, url, test):
    page = browser.new_page(viewport={"width": 1000, "height": 750})
    errors, console = [], []
    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    page.on("console", lambda m: m.type == "error" and console.append(m.text[:300]))

    def gate(route):
        if route.request.url.startswith(("file://", "data:", "blob:")):
            return route.continue_()
        errors.append(f"the page tried to load {route.request.url[:120]} from the internet "
                      f"(blocked — everything must be local files)")
        return route.abort()
    page.route("**/*", gate)
    res = {"name": test.get("name", ""), "passed": False, "details": [], "errors": errors}
    page.goto(url, wait_until="load", timeout=15000)
    page.wait_for_timeout(SETTLE_MS)
    page.mouse.click(3, 3)
    shots = {}
    for line in test.get("actions") or []:
        try:
            act(page, line, shots)
        except Exception as e:      # noqa: BLE001 - reported, the test fails
            msg = str(e).splitlines()[0][:160]
            if "Timeout" in msg and str(line).lower().startswith(("click", "fill")):
                msg = "nothing on the page matched it (no such button / input / text)"
            res["details"].append(f"action {line!r} failed: {msg}")
        page.wait_for_timeout(GAP_MS)
    ok = not res["details"]
    page.evaluate("s => { window.__shots = s }", shots)
    for chk in test.get("checks") or []:
        try:
            r = bool(page.evaluate(f"(() => {{ const shots = window.__shots; return ({chk}); }})()"))
            res["details"].append(f"{'ok' if r else 'FALSE'}: {chk[:160]}")
        except Exception as e:      # noqa: BLE001
            r = False
            res["details"].append(f"ERROR in {chk[:120]}: {str(e).splitlines()[0][:160]}")
        ok = ok and r
    missing = [c for c in console if "ERR_FILE_NOT_FOUND" in c or "Failed to load" in c]
    if missing:
        errors.append("a file the page loads is missing: " + missing[0][:200])
    res["passed"] = ok and not errors
    res["text"] = page.inner_text("body")[:800]
    return page, res


def main():
    job = json.loads(sys.argv[1])
    url = Path(job["path"]).resolve().as_uri()
    out = Path(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    results, shot = [], None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-gpu"])
        last = None
        for test in job.get("tests") or [{"name": "loads", "actions": [], "checks": []}]:
            try:
                page, res = run_test(browser, url, test)
            except Exception as e:  # noqa: BLE001 - a page that won't even load
                results.append({"name": test.get("name", ""), "passed": False,
                                "details": [f"the page didn't load: {str(e).splitlines()[0][:200]}"],
                                "errors": []})
                continue
            try:                    # each test's own picture, for the judge
                res["shot"] = str(out / f"test-{res['name']}.png")
                page.screenshot(path=res["shot"])
            except Exception:
                res.pop("shot", None)
            results.append(res)
            if last is not None:
                last.close()
            last = page
        if last is not None:
            shot = str(out / "after.png")
            last.screenshot(path=shot)
        browser.close()
    print(json.dumps({"tests": results, "screenshot": shot}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:          # always one JSON line
        print(json.dumps({"error": f"{type(e).__name__}: {str(e)[:300]}"}))
