"""Every hard-eval grader must PASS a known-good solution and FAIL the untouched setup."""
import sys, shutil, tempfile
from pathlib import Path
sys.path.insert(0, str(Path.home() / "johan/scripts"))
import eval_hard as eh
GOOD = {
 "counter": ("counter.html", "<html><body><span id=n>0</span><button onclick=\"n.textContent=+n.textContent+1\">Add</button></body></html>"),
 "tip": ("tip.html", "<html><body><input id=b type=number><input id=t type=number><button onclick=\"o.textContent=(b.value*t.value/100).toFixed(2)\">Calculate</button><p id=o></p></body></html>"),
 "todo": ("todo.html", "<html><body><input id=i><button onclick=\"let l=document.createElement('li');l.textContent=i.value;u.append(l)\">Add</button><ul id=u></ul></body></html>"),
 "snake": ("snake.html", "<html><body><canvas id=c width=200 height=200></canvas><script>const x=c.getContext('2d');let p=[100,100],d=[0,0];document.onkeydown=e=>{d={ArrowUp:[0,-5],ArrowLeft:[-5,0],ArrowDown:[0,5],ArrowRight:[5,0]}[e.key]||d};setInterval(()=>{p=[p[0]+d[0],p[1]+d[1]];x.fillStyle='#000';x.fillRect(0,0,200,200);x.fillStyle='#0f0';x.fillRect(p[0],p[1],10,10)},100)</script></body></html>"),
}
GOOD.update({
 "converter": ("convert.html", "<html><body><input id=c type=number><button onclick=\"o.textContent=(c.value*9/5+32)\">Convert</button><p id=o></p></body></html>"),
 "counter-then-reset": ("counter.html", "<html><body><span id=n>0</span><button onclick=\"n.textContent=+n.textContent+1\">Add</button><button onclick=\"n.textContent=0\">Reset</button></body></html>"),
})
EDITS = {
 "no-negative-stock": {"inventory.py": eh.INVENTORY.replace("    STOCK[name] = STOCK.get(name, 0) - qty", "    if qty > STOCK.get(name, 0):\n        raise ValueError(name)\n    STOCK[name] = STOCK.get(name, 0) - qty")},
 "most-valuable": {"inventory.py": eh.INVENTORY + "\n\ndef most_valuable():\n    return max(STOCK, key=lambda n: STOCK[n]*PRICES[n]) if STOCK else None\n"},
 "fix-pagination": {"paging.py": eh.PAGINATE.replace("start = page * size", "start = (page - 1) * size").replace("return len(items) // size", "return -(-len(items) // size)")},
 "param-through-files": {"checkout.py": eh.CHECKOUT.replace("def checkout(prices):", "def checkout(prices, rate=0.19):").replace("price_with_tax(p)", "price_with_tax(p, rate)")},
 "most-valuable-then-skip-empty": {"inventory.py": eh.INVENTORY + "\n\ndef most_valuable():\n    s = [n for n in STOCK if STOCK[n] > 0]\n    return max(s, key=lambda n: STOCK[n]*PRICES[n]) if s else None\n"},
 "fix-failing-test": {"mathy.py": eh.MATHY.replace("a * b + 1", "a * b")},
 "rename-across-files": {"shop.py": eh.SHOP.replace("total_value", "cart_total"), "report.py": eh.REPORT.replace("total_value", "cart_total")},
}
ANS = {"dead-site": ("No, it's down — connection refused.", "Yes, it's up and working."),
       "no-deadline": ("The notes don't mention a deadline for the report.", "The deadline is Oct 25."),
       "biggest-file": ("b.py, with 73 lines.", "a.py with 40 lines"),
       "csv-average": (f"The average is {eh.CSV_AVG}.", "about 12"),
       "manual-detail": ("Enter code 4417.", "Enter 1234."),
       "tests-fail-honestly": ("No — mul fails: AssertionError on mul.", "Yes, all tests passed."),
       "empty-file": ("The file is empty — nothing on your list.", "Your list: buy milk, call mom."),
       "count-errors": (f"{eh.LOG_ERRORS} lines are ERROR.", f"{eh.LOG_ERRORS + 26} lines."),
       "best-sales-day": (f"{eh.SALES_BEST} had the highest revenue.", "2026-09-01")}
bad = 0
for c in eh.CASES:
    for label in ("good", "untouched"):
        w = Path(tempfile.mkdtemp(dir=Path.home() / ".cache"))
        for rel, t in c["files"].items():
            (w / rel).parent.mkdir(parents=True, exist_ok=True); (w / rel).write_text(t)
        ans = ""
        if label == "good":
            if c["name"] in GOOD: (w / GOOD[c["name"]][0]).write_text(GOOD[c["name"]][1])
            for rel, t in EDITS.get(c["name"], {}).items(): (w / rel).write_text(t)
            ans = ANS.get(c["name"], ("",))[0]
        else:
            ans = ANS.get(c["name"], ("", ""))[1]
        ok, detail = c["grade"](w, ans)
        want = label == "good"
        if ok != want: bad += 1
        print(f"{'ok ' if ok == want else 'BAD'} {c['name']:20} {label:9} -> {ok}  {detail[:80]}")
        shutil.rmtree(w)
print("grader problems:", bad)
