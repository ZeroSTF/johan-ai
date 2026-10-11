"""General-ability eval — how good is the model itself, not its tool choice?

eval_tools.py measures which tool gets picked; eval_decide.py measures the
multiple-choice decision layer. Neither tells you whether a model reasons,
knows things, follows a format, writes working code, admits ignorance or
uses a long context — which is what changes when the model is swapped.

Every case here is graded by CODE, never by another model:
  - exact answers are matched with word boundaries (the "25" in "2025" bug
    class applies here too)
  - format instructions are checked structurally (JSON parses, line counts,
    word counts, lowercase)
  - code answers are EXECUTED against tests in the no-network sandbox
  - "admits it doesn't know" is a phrase match on questions about things
    that do not exist

Each case runs REPEATS times with different seeds at production settings
(temperature 0.3, think off, num_ctx 32768), so one lucky sample can't
decide a comparison. Tools are not offered: this is the bare model.

    JOHAN_MODEL=johan-agent python eval_general.py
    python eval_general.py --model johan-neohorse --only code
    python eval_general.py --compare            # latest run of each model
"""

import argparse
import base64
import io
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja      # noqa: E402

OUT = ja.ROOT / "logs/eval-general"
REPEATS = 3
SYSTEM = ("You are Johan, a helpful personal assistant. Answer accurately and "
          "concisely. If you don't know something or it doesn't exist, say so "
          "plainly instead of guessing.")


# --- checkers -------------------------------------------------------------

def _norm(a):
    # Markdown emphasis splits phrases ("does **not** contain"); strip it.
    return re.sub(r"\*\*|__|`", "", a or "").replace(",", "").replace("\u202f", " ")


def has(*alts):
    """Answer contains one of these, on word boundaries, case-insensitive."""
    def check(a):
        t = _norm(a).lower()
        return any(re.search(rf"(?<![\w.]){re.escape(x.lower())}(?![\w])", t) for x in alts)
    return check


def all_of(*checks):
    return lambda a: all(c(a) for c in checks)


def none_of(*alts):
    h = has(*alts)
    return lambda a: not h(a)


ADMITS = re.compile(
    r"not (aware|familiar|able to (find|verify|confirm))|no (record|information|"
    r"evidence|such|known|reliable)|(don't|do not|can't|cannot|couldn't|could not) "
    r"(find|verify|confirm|locate)|(doesn't|does not|didn't|did not) (appear to |seem to )?"
    r"exist|unable to (find|verify|locate)|fictional|not a real|isn't a real|"
    r"is not a (real|known|standard)|no (standard|built-in)|i don't (know|have)|"
    r"i'm not sure|i am not sure|there is no|there's no|not part of the (python )?standard|"
    r"(does not|doesn't) (contain|include|have) (a|any)", re.I)


def admits(a):
    return bool(ADMITS.search(_norm(a)))


FRENCH = {"le", "la", "les", "des", "est", "et", "du", "une", "pour", "avec",
          "à", "sont", "de", "en", "vous", "plus", "au", "aux", "qui", "que"}


def french(a):
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", (a or "").lower())
    return sum(w in FRENCH for w in words) >= 3


def bullets(n):
    def check(a):
        lines = [l for l in (a or "").splitlines() if l.strip()]
        items = [l for l in lines if re.match(r"\s*([-*•]|\d+[.)])\s+", l)]
        return len(items) == n and len(lines) <= n + 1    # allow one heading line
    return check


def json_with(**expected):
    def check(a):
        t = (a or "").strip()
        t = re.sub(r"^```(json)?\s*|\s*```$", "", t)
        try:
            d = json.loads(t)
        except ValueError:
            return False
        return isinstance(d, dict) and all(
            k in d and (v is None or str(d[k]) in v) for k, v in expected.items())
    return check


def under_words(n):
    return lambda a: 0 < len((a or "").split()) < n


def lowercase(a):
    a = (a or "").strip()
    return bool(re.search(r"[a-z]", a)) and a == a.lower()


def exactly(text):
    return lambda a: (a or "").strip().rstrip(".").strip().lower() == text.lower()


def runs(tests):
    """Extract the model's code and run it with `tests` in the sandbox."""
    def check(a):
        m = re.findall(r"```(?:python|py)?\s*\n(.*?)```", a or "", re.S)
        code = max(m, key=len) if m else (a or "")
        prog = code + "\n\n" + tests + "\nprint('ALL_OK')\n"
        import johan_sandbox
        res = johan_sandbox.run_python(prog, timeout=20)
        return "ALL_OK" in (res.get("stdout") or "")
    return check


# --- cases ----------------------------------------------------------------

JOB_POST = ("Backend Engineer (Java/Kotlin) at Fjordlane. You'll need 4+ years of "
            "professional backend experience, strong SQL, and experience with "
            "Kafka. Location: remote within the EU only — we cannot sponsor visas "
            "or hire contractors outside the EU. Salary: €70–85k.")

REPORT = ("Q3 incident review. After moving the session store from MySQL to Redis, "
          "p95 login latency fell from 840 ms to 190 ms. Error rate stayed at 0.2%. "
          "The migration took 3 engineers 11 days. Next step: move rate limiting "
          "to Redis as well.")


def _filler(n_sentences):
    topics = ["the harbour", "the old library", "a mountain trail", "the night market",
              "a chess club", "the train depot", "a bakery", "the river delta"]
    verbs = ["was renovated", "hosted a festival", "closed for a week", "got new lights",
             "was painted blue", "welcomed visitors", "hired two guides", "changed hours"]
    return [f"In year {1900 + i % 120}, {topics[i % 8]} {verbs[(i * 3) % 8]} "
            f"and the records list entry {i * 7919 % 100000}." for i in range(n_sentences)]


def _haystack(n_sentences, needle_at):
    s = _filler(n_sentences)
    s.insert(needle_at, "IMPORTANT: the vault code for the east archive is 7316.")
    return " ".join(s)


CASES = [
    # --- reasoning: exact answers ------------------------------------------
    dict(kind="reasoning", prompt="A train leaves at 14:35 and the trip takes 2 hours 50 minutes. What time does it arrive?", check=has("17:25", "5:25")),
    dict(kind="reasoning", prompt="I have 3 boxes with 12 apples each. I give away 7 apples, then buy 2 more boxes of 12. How many apples do I have now?", check=lambda a: (re.findall(r"\d+", _norm(a)) or [""])[-1] == "53"),
    dict(kind="reasoning", prompt="A shirt costs 40 dinars. It's discounted 25%, then the discounted price is raised by 10%. What is the final price?", check=has("33", "33.0", "33.00")),
    dict(kind="reasoning", prompt="If 5 machines make 5 widgets in 5 minutes, how many minutes do 100 machines need to make 100 widgets?", check=all_of(has("5", "five"), none_of("100 minutes"))),
    dict(kind="reasoning", prompt="A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How many cents does the ball cost?", check=all_of(has("5", "0.05", "five"), none_of("10 cents"))),
    dict(kind="reasoning", prompt="How many times does the letter r appear in the word 'strawberry'?", check=has("3", "three")),
    dict(kind="reasoning", prompt="What is the next number in the sequence 2, 6, 12, 20, 30, ...?", check=has("42")),
    dict(kind="reasoning", prompt="Today is Monday. What day of the week will it be in 100 days?", check=has("wednesday")),
    dict(kind="reasoning", prompt="Sara is older than Mehdi. Mehdi is older than Lina. Who is the youngest? One word.", check=all_of(has("lina"), none_of("sara is the youngest", "mehdi is the youngest"))),
    dict(kind="reasoning", prompt="Sort ascending and reply with only the list: 42, 7, 19, 3, 88, 21", check=lambda a: re.sub(r"[^\d,]", "", a or "") == "3,7,19,21,42,88"),
    dict(kind="reasoning", prompt="A meeting is at 10:00 in Tunis (UTC+1). What time is that in New York (UTC-4)?", check=has("5:00", "05:00", "5 am", "5am", "5 a.m")),

    # --- knowledge ----------------------------------------------------------
    dict(kind="knowledge", prompt="What is the default value of an int field in a Java class?", check=has("0", "zero")),
    dict(kind="knowledge", prompt="Which Spring annotation combines @Controller and @ResponseBody?", check=has("restcontroller", "@restcontroller")),
    dict(kind="knowledge", prompt="Which HTTP status code means 'Too Many Requests'?", check=has("429")),
    dict(kind="knowledge", prompt="What is the average time complexity of a lookup in a hash map?", check=has("o(1)", "constant")),
    dict(kind="knowledge", prompt="What is the capital of Australia?", check=has("canberra")),
    dict(kind="knowledge", prompt="What is the chemical symbol for tungsten?", check=lambda a: bool(re.search(r"\bW\b", a or ""))),
    dict(kind="knowledge", prompt="Who wrote 'One Hundred Years of Solitude'?", check=has("márquez", "marquez")),
    dict(kind="knowledge", prompt="What TCP port does PostgreSQL listen on by default?", check=has("5432")),
    dict(kind="knowledge", prompt="In which year did the Tunisian revolution begin?", check=has("2010")),

    # --- instruction following, checked structurally -----------------------
    dict(kind="instructions", prompt="List exactly 3 benefits of unit testing as a bulleted list. Nothing else.", check=bullets(3)),
    dict(kind="instructions", prompt='Reply with JSON only (no prose, no code fence) with keys "language" and "year" for the year Java was first publicly released.', check=json_with(language=None, year=("1995", "1996"))),
    dict(kind="instructions", prompt="Describe Docker in fewer than 20 words.", check=under_words(20)),
    dict(kind="instructions", prompt="Write one sentence about the sea, entirely in lowercase letters.", check=lowercase),
    dict(kind="instructions", prompt="Answer with only 'yes' or 'no': is 91 a prime number?", check=exactly("no")),
    dict(kind="instructions", prompt="Repeat the word banana exactly 4 times, separated by commas, and nothing else.", check=lambda a: re.sub(r"\s", "", (a or "").lower()).rstrip(".") == "banana,banana,banana,banana"),

    # --- French -------------------------------------------------------------
    dict(kind="french", prompt="Traduis en anglais : « Je cherche un poste de développeur backend à distance. »", check=all_of(has("remote", "remotely"), has("backend", "back-end"), has("looking", "seeking", "searching"))),
    dict(kind="french", prompt="Quelle est la capitale du Canada ? Réponds en français, en une phrase.", check=all_of(has("ottawa"), french)),
    dict(kind="french", prompt="Donne deux avantages du télétravail. Réponds uniquement en français.", check=french),

    # --- code, executed in the sandbox --------------------------------------
    dict(kind="code", prompt="Write a Python function parse_duration(s) that converts strings like '1h30m', '45s', '2h' or '1h2m3s' to a number of seconds. Reply with the code only, in a ```python block.",
         check=runs("assert parse_duration('1h30m') == 5400\nassert parse_duration('45s') == 45\nassert parse_duration('2h') == 7200\nassert parse_duration('1h2m3s') == 3723")),
    dict(kind="code", prompt="Write a Python function merge_intervals(intervals) that merges overlapping [start, end] intervals and returns them sorted. Touching intervals like [1,4] and [4,5] merge. Reply with the code only, in a ```python block.",
         check=runs("assert [list(x) for x in merge_intervals([[1,3],[2,6],[8,10],[15,18]])] == [[1,6],[8,10],[15,18]]\nassert [list(x) for x in merge_intervals([[1,4],[4,5]])] == [[1,5]]\nassert [list(x) for x in merge_intervals([[5,6],[1,2]])] == [[1,2],[5,6]]")),
    dict(kind="code", prompt="Write a Python function to_roman(n) converting an integer 1-3999 to a Roman numeral string. Reply with the code only, in a ```python block.",
         check=runs("assert to_roman(1994) == 'MCMXCIV'\nassert to_roman(4) == 'IV'\nassert to_roman(3999) == 'MMMCMXCIX'\nassert to_roman(58) == 'LVIII'")),
    dict(kind="code", prompt="Write a Python function is_balanced(s) that returns True if every (, [ and { in s is closed in the right order, ignoring other characters. Reply with the code only, in a ```python block.",
         check=runs("assert is_balanced('([]{})')\nassert not is_balanced('(]')\nassert not is_balanced('((')\nassert is_balanced('')\nassert is_balanced('a(b)c')\nassert not is_balanced(')(')")),
    dict(kind="code", prompt="Write a Python function top_words(text, k) returning the k most frequent words (lowercased, letters only), most frequent first, ties broken alphabetically. Reply with the code only, in a ```python block.",
         check=runs("assert top_words('b a b c a b', 2) == ['b', 'a']\nassert top_words('Dog cat DOG bird cat', 3) == ['cat', 'dog', 'bird']\nassert top_words('x y z', 2) == ['x', 'y']")),
    dict(kind="code", prompt="Write a Python function second_largest(nums) returning the second largest DISTINCT value, or None if there isn't one. Reply with the code only, in a ```python block.",
         check=runs("assert second_largest([3,1,3,2]) == 2\nassert second_largest([5]) is None\nassert second_largest([7,7]) is None\nassert second_largest([-1,-5,-3]) == -3")),

    # --- admits ignorance / rejects a false premise --------------------------
    dict(kind="honesty", prompt="Summarize the 2019 paper 'Quantum Gravy Networks for Sentient Toasters' by Dr. Elmo Pickering.", check=admits),
    dict(kind="honesty", prompt="What is the Python standard library module `fastjsonx` used for?", check=admits),
    dict(kind="honesty", prompt="Why did Einstein receive the Nobel Prize for his theory of relativity?", check=has("photoelectric")),
    dict(kind="honesty", prompt="What did Habib Bourguiba say about smartphones in his 2015 speech?", check=lambda a: admits(a) or has("2000", "died", "passed away", "death")(a)),

    # --- reading what it was given ------------------------------------------
    dict(kind="reading", prompt=f"{JOB_POST}\n\nBased only on this posting, can someone living in Tunisia be hired for it? Start your answer with yes or no.", check=lambda a: (a or "").strip().lower().startswith("no")),
    dict(kind="reading", prompt=f"{JOB_POST}\n\nHow many years of experience does it ask for? Reply with the number only.", check=exactly("4")),
    dict(kind="reading", prompt=f"{REPORT}\n\nWhat was p95 login latency before and after the change?", check=all_of(has("840"), has("190"))),
    dict(kind="reading", messages=[
        {"role": "user", "content": "My cat is called Zorro and my sister is called Ines."},
        {"role": "assistant", "content": "Got it — Zorro the cat and your sister Ines."},
        {"role": "user", "content": "What's 12 times 12?"},
        {"role": "assistant", "content": "144."},
        {"role": "user", "content": "What's my sister's name?"}], check=has("ines")),

    # --- long context: a fact buried deep in filler --------------------------
    dict(kind="long-context", prompt=_haystack(250, 60) + "\n\nWhat is the vault code for the east archive? Reply with the number only.", check=has("7316")),
    dict(kind="long-context", prompt=_haystack(900, 150) + "\n\nWhat is the vault code for the east archive? Reply with the number only.", check=has("7316")),
]


def _vision_case():
    """A picture of a number. Not scored with the rest: a text-only model
    cannot pass it, and that is a capability difference, not a quality one."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (420, 160), "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 90)
    except OSError:
        font = ImageFont.load_default()
    d.text((40, 30), "4721", fill="black", font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return dict(kind="vision", prompt="What number is written in this image? Reply with the number only.",
                images=[base64.b64encode(buf.getvalue()).decode()], check=has("4721"))


# --- runner ---------------------------------------------------------------

def ask(model, case, seed):
    msgs = case.get("messages") or [{"role": "user", "content": case["prompt"]}]
    msgs = [{"role": "system", "content": SYSTEM}] + [dict(m) for m in msgs]
    if case.get("images"):
        msgs[-1]["images"] = case["images"]
    r = requests.post(ja.OLLAMA_CHAT, timeout=300, json={
        "model": model, "messages": msgs, "stream": False, "think": False,
        "keep_alive": "5m",
        "options": {"temperature": 0.3, "num_ctx": ja.NUM_CTX, "seed": seed}})
    if r.status_code != 200:
        return f"[error {r.status_code}: {r.text[:200]}]", {}
    b = r.json()
    return b["message"].get("content", ""), b


def run(model, only=None, repeats=REPEATS):
    cases = [c for c in CASES if not only or c["kind"] == only]
    if not only or only == "vision":
        cases.append(_vision_case())
    print(f"general eval — {model} — {len(cases)} cases × {repeats}\n")
    results, gen_tok, gen_ns, started = [], 0, 0, time.monotonic()
    for i, case in enumerate(cases, 1):
        label = (case.get("prompt") or case["messages"][-1]["content"]).replace("\n", " ")
        label = label[-50:] if case["kind"] == "long-context" else label[:50]
        print(f"[{i:>2}/{len(cases)}] {case['kind']:<12} {label:<52}", end="", flush=True)
        passes, samples = 0, []
        for s in range(repeats):
            ans, body = ask(model, case, seed=s + 1)
            ok = bool(case["check"](ans))
            passes += ok
            samples.append({"ok": ok, "answer": ans})
            gen_tok += body.get("eval_count", 0)
            gen_ns += body.get("eval_duration", 0)
        print(f"{passes}/{repeats}")
        results.append({"kind": case["kind"], "prompt": label, "passes": passes,
                        "repeats": repeats, "samples": samples})
    summary = {"model": model, "when": datetime.now().isoformat(timespec="seconds"),
               "minutes": round((time.monotonic() - started) / 60, 1),
               "tokens_per_s": round(gen_tok / (gen_ns / 1e9), 1) if gen_ns else None,
               "results": results}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{model.replace('/', '_').replace(':', '_')}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print_summary(summary)
    print(f"\nsaved {path}")
    return summary


def _by_kind(summary):
    out = {}
    for r in summary["results"]:
        k = out.setdefault(r["kind"], [0, 0])
        k[0] += r["passes"]
        k[1] += r["repeats"]
    return out


def print_summary(s):
    kinds = _by_kind(s)
    scored = [(p, t) for k, (p, t) in kinds.items() if k != "vision"]
    p, t = sum(x for x, _ in scored), sum(y for _, y in scored)
    print(f"\n{s['model']}: {p}/{t} ({100 * p / max(t, 1):.0f}%) excluding vision, "
          f"{s['tokens_per_s']} tok/s, {s['minutes']} min")
    for k, (a, b) in kinds.items():
        print(f"  {k:<13} {a:>3}/{b:<3}")


def regrade():
    """Re-apply the CURRENT graders to each model's latest saved answers, so a
    grader fix needs no GPU and every model is graded identically."""
    # By position, not label: two reading cases share their first 50 chars.
    cases = CASES + [_vision_case()]
    latest = {}
    for f in sorted(OUT.glob("*.json")):
        latest[json.loads(f.read_text())["model"]] = f
    for model, f in latest.items():
        s = json.loads(f.read_text())
        if len(s["results"]) != len(cases):
            print(f"skipped {f.name}: partial run")
            continue
        for r, c in zip(s["results"], cases):
            assert r["kind"] == c["kind"], (r["kind"], c["kind"])
            check = c["check"]
            for x in r["samples"]:
                x["ok"] = bool(check(x["answer"]))
            r["passes"] = sum(x["ok"] for x in r["samples"])
        f.write_text(json.dumps(s, indent=1, ensure_ascii=False))
        print(f"regraded {f.name}")


def compare():
    latest = {}
    for f in sorted(OUT.glob("*.json")):
        s = json.loads(f.read_text())
        latest[s["model"]] = s
    if not latest:
        print("no runs yet")
        return
    models = list(latest)
    kinds = list(dict.fromkeys(k for s in latest.values() for k in _by_kind(s)))
    print(f"{'':<14}" + "".join(f"{m[:22]:>24}" for m in models))
    for k in kinds + ["TOTAL (no vision)", "tokens/s"]:
        row = f"{k:<14}"
        for m in models:
            bk = _by_kind(latest[m])
            if k == "tokens/s":
                cell = str(latest[m]["tokens_per_s"])
            elif k.startswith("TOTAL"):
                p = sum(a for kk, (a, _) in bk.items() if kk != "vision")
                t = sum(b for kk, (_, b) in bk.items() if kk != "vision")
                cell = f"{p}/{t} ({100 * p / max(t, 1):.0f}%)"
            else:
                a, b = bk.get(k, (0, 0))
                cell = f"{a}/{b}" if b else "-"
            row += f"{cell:>24}"
        print(row)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=ja.MODEL)
    ap.add_argument("--only")
    ap.add_argument("--repeats", type=int, default=REPEATS)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--regrade", action="store_true")
    a = ap.parse_args()
    if a.regrade:
        regrade()
        compare()
    elif a.compare:
        compare()
    else:
        run(a.model, a.only, a.repeats)
