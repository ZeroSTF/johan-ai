"""try_page on pages with known faults. Real headless browser, no model (the
look step is off), nothing under brain/ (TRY_DIR points at a temp folder).

The snake game Johan called "verified" died on its first frame; this is the
tool that would have shown it. Each page here is one of the faults it must
report: a JS error, a page that never moves, keys that do nothing.

    python test_trypage.py      # ~10 s
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_tools as jt        # noqa: E402

results = {}


def check(label, ok):
    results[label] = bool(ok)


GOOD = """<!doctype html><html><body><p id=s>Score: 0</p><canvas id=c width=200 height=200></canvas>
<script>
const ctx = document.getElementById('c').getContext('2d'); let x = 10, dx = 0, t = 0, score = 0;
document.addEventListener('keydown', e => { if (e.key === 'ArrowRight') { dx = 2; score += 1;
  document.getElementById('s').textContent = 'Score: ' + score; } });
(function loop() { t++; x = (x + dx) % 200; ctx.fillStyle = '#000'; ctx.fillRect(0, 0, 200, 200);
  ctx.fillStyle = 'hsl(' + (t % 360) + ',80%,50%)'; ctx.fillRect(x, 90, 20, 20); requestAnimationFrame(loop); })();
</script></body></html>"""
BROKEN = """<!doctype html><html><body><canvas id=c></canvas>
<script>const ctx = document.getElementById('c').getContext('2d'); drawSnake();</script></body></html>"""
CLICKY = """<!doctype html><html><body><p id=t>You are in a room.</p>
<button onclick="document.getElementById('t').textContent='You open the door.'">Open the door</button></body></html>"""
TEXT_ONLY = """<!doctype html><html><body><p id=k>Last key:</p><canvas id=c width=200 height=200></canvas>
<script>const ctx = document.getElementById('c').getContext('2d'); ctx.fillStyle = '#0f0'; ctx.fillRect(150, 90, 20, 20);
document.addEventListener('keydown', e => document.getElementById('k').textContent = 'Last key: ' + e.key);</script></body></html>"""
CALC = """<!doctype html><html><body><input id=b><input id=t><button onclick="o.textContent=(b.value*t.value/100)">Calculate</button><p id=o></p></body></html>"""
STILL = """<!doctype html><html><body><h1>Snake</h1><p>Press any key</p></body></html>"""

home_tmp = Path(tempfile.mkdtemp(prefix="johan-trypage-", dir=Path.home() / ".cache"))
real_dir = jt.TRY_DIR
jt.TRY_DIR = Path(tempfile.mkdtemp())
try:
    for name, html in (("good", GOOD), ("broken", BROKEN), ("still", STILL), ("clicky", CLICKY), ("textonly", TEXT_ONLY), ("calc", CALC)):
        (home_tmp / f"{name}.html").write_text(html)

    r = jt.try_page(str(home_tmp / "good.html"), keys="ArrowRight ArrowRight", look=False)
    check("a working game: no errors", not r.get("errors") and not r.get("console_errors"))
    check("…it moves by itself", r.get("moves_by_itself") is True)
    check("…the keys change it", r.get("changed_after_keys") is True)
    check("…the score text after the keys is read", "Score: 2" in r.get("text_after", ""))
    check("…two screenshots exist", all(Path(p).exists() for p in r.get("screenshots", [])))
    check("…the result tells it to fix and retry", "try_page again" in r.get("note", ""))

    r = jt.try_page(str(home_tmp / "broken.html"), look=False)
    check("a JS error is reported", any("drawSnake" in e for e in r.get("errors", [])))

    r = jt.try_page(str(home_tmp / "still.html"), keys=["ArrowUp"], look=False)
    check("a page that never moves says so", r.get("moves_by_itself") is False)
    check("…and that the keys changed nothing", r.get("changed_after_keys") is False)

    r = jt.try_page(str(home_tmp / "textonly.html"), keys="ArrowLeft ArrowUp", look=False)
    check("a game whose canvas never moves fails, even if a debug text line changes",
          r.get("changed_after_keys") is False and "in the game area" in " ".join(r.get("problems", [])))

    r = jt.try_page(str(home_tmp / "clicky.html"), click="Open the door", look=False)
    check("a button is clicked by its text", r.get("clicked") == ["Open the door"]
          and "You open the door." in r.get("text_after", ""))
    r = jt.try_page(str(home_tmp / "clicky.html"), click="Fly away", look=False)
    check("a button that isn't there is reported", any("Fly away" in e for e in r.get("errors", [])))

    r = jt.try_page(str(home_tmp / "calc.html"), fill="50 | 20", click="Calculate",
                    check="document.getElementById('o').textContent === '10'", look=False)
    check("typing + clicking + his own check: a working calculator passes",
          r.get("filled") == ["50", "20"] and r.get("problems") == ["none found"])
    r = jt.try_page(str(home_tmp / "calc.html"), fill="50 | 20", click="Calculate",
                    check="document.getElementById('o').textContent === '11'", look=False)
    check("…a check that's false is a problem", any("your check failed" in p for p in r.get("problems", [])))
    r = jt.try_page(str(home_tmp / "calc.html"), check="snake[0].x > 1", look=False)
    check("…a check that errors says why", any("snake is not defined" in p for p in r.get("problems", [])))

    check("only files in his home", "home" in jt.try_page("/etc/hostname").get("error", ""))
    check("only .html files", ".html" in jt.try_page(str(Path(__file__))).get("error", ""))
    check("a missing file says so", "no such file" in jt.try_page(str(home_tmp / "nope.html")).get("error", ""))
    check("it's a tool the model can call", "try_page" in jt.DISPATCH
          and any(t["function"]["name"] == "try_page" for t in jt.TOOLS))
finally:
    shutil.rmtree(home_tmp, ignore_errors=True)
    shutil.rmtree(jt.TRY_DIR, ignore_errors=True)
    jt.TRY_DIR = real_dir

# The answer check: a claim that contradicts the browser test gets a note.
import json as _json                  # noqa: E402
import johan_assistant as ja          # noqa: E402
tried_bad = {"role": "tool", "content": _json.dumps({"problems": ["pressing ArrowRight changed nothing on screen"],
                                                      "screenshots": ["a.png"], "path": "/home/zeros/g/index.html"})}
tried_ok = {"role": "tool", "content": _json.dumps({"problems": ["none found"], "screenshots": ["a.png"]})}
wrote = {"role": "tool", "content": _json.dumps({"written": True, "path": "~/g/index.html"})}
ans = lambda t: [{"role": "user", "content": "q"}, t, {"role": "assistant", "content": "It works perfectly!"}]
m = ans(tried_bad)
check("'it works' after a failed browser test gets a visible note",
      "changed nothing" in ja._flag_untested("It works perfectly!", m, 0) and "check:" in m[-1]["content"])
check("…a passing test leaves the answer alone",
      ja._flag_untested("It works perfectly!", ans(tried_ok), 0) == "It works perfectly!")
check("'it works' after writing a page but never trying it is flagged",
      "untested" in ja._flag_untested("Done, it works!", ans(wrote), 0))
check("…a write followed by a passing try is fine",
      ja._flag_untested("It works!", [wrote, tried_ok, {"role": "assistant", "content": "It works!"}], 0) == "It works!")
check("…and no claim, no note", ja._flag_untested("Here's the file.", ans(tried_bad), 0) == "Here's the file.")

for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
