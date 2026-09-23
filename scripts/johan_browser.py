"""render(url) — read a JavaScript-heavy page through headless Chromium.

The fallback behind fetch_page: plain HTTP + BeautifulSoup reads most pages,
but client-rendered sites return an empty shell ("You need to enable
JavaScript"). This renders them in a subprocess (render_page.py, in its own
venv), pinned to cores 8-15 like every other background job, so rendering
never competes with a game for cores 0-3.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path.home() / "johan"
PYTHON = ROOT / "venv-browser/bin/python"
SCRIPT = Path(__file__).with_name("render_page.py")

# Signs a plain fetch got a JS shell instead of the page.
JS_SHELL = ("enable javascript", "you need to enable", "requires javascript",
            "javascript is disabled", "please turn on javascript",
            "this app works best with javascript")


def available():
    return PYTHON.exists() and SCRIPT.exists()


def looks_like_js_shell(text):
    t = (text or "").strip().lower()
    return len(t) < 400 or any(s in t[:3000] for s in JS_SHELL)


def render(url, timeout=20, max_chars=20000):
    if not available():
        return {"error": "browser not installed (~/johan/venv-browser)"}
    try:
        proc = subprocess.run(
            ["taskset", "-c", "8-15", str(PYTHON), str(SCRIPT), url,
             "--timeout", str(timeout), "--max-chars", str(max_chars)],
            capture_output=True, text=True, timeout=timeout + 25)
    except subprocess.TimeoutExpired:
        return {"error": f"browser timed out after {timeout + 25}s"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"browser failed: {(proc.stderr or 'no output')[-300:]}"}
