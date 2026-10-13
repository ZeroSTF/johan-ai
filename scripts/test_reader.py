"""Tests for johan_reader — long tool output read by relevance.

The embed server is stubbed (a bag-of-words fake), so this runs offline and
in milliseconds. fetch_page is exercised with requests stubbed too.

    python test_reader.py
"""
import contextvars
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_reader as jr           # noqa: E402

results = {}

VOCAB = ["keyboard", "control", "remap", "rebind", "key", "f3", "weather",
         "history", "release", "price", "menu", "button"]


def fake_embed(texts):
    fake_embed.calls += 1
    fake_embed.chars += sum(len(t) for t in texts)
    out = []
    for t in texts:
        low = t.lower()
        v = [float(low.count(k)) for k in VOCAB]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


fake_embed.calls = 0
fake_embed.chars = 0


def check(name, cond):
    results[name] = bool(cond)


def filler(n, topic="history"):
    return "\n".join(f"The {topic} of the studio, paragraph {i}, says nothing useful "
                     f"about anything a player would ask about today." for i in range(n))


def main():
    real_embed = jr.embed
    jr.embed = fake_embed
    try:
        # ----------------------------------------------------- chunking
        ch = jr.chunks("a\nb\n" + "x" * 1000)
        check("chunks: a 1,000-char line is split", all(len(c) <= jr.CHUNK_CHARS for c in ch))
        ch = jr.chunks("Input\nRemapping\nKeyboard\nYes")
        check("chunks: short lines kept (wiki tables)", "Remapping" in ch[0] and "Yes" in ch[0])

        # ----------------------------------------------------- lexical
        lx = jr.lexical(["javascript tutorial", "java backend"], "java jobs")
        check("lexical: word boundary — 'java' does not score 'javascript'",
              lx[0] == 0 and lx[1] > 0)

        # ----------------------------------------------------- ranking
        answer = ("Input settings: press F3 to open the button menu, where every "
                  "keyboard key can be rebound (remap controls freely).")
        page = filler(120) + "\n" + answer + "\n" + filler(60, "price")
        q = "what are the keyboard controls and can I remap them?"
        sel = jr.select(page, q, budget=1200)
        check("select: the relevant chunk deep in a long page is returned",
              "press F3" in sel["text"])
        check("select: the page's first 1,200 chars would NOT have contained it",
              "press F3" not in page[:1200])
        check("select: stays within the budget (+ separators)", len(sel["text"]) <= 1200 + 200)
        check("select: reports what was skipped",
              sel["total_chars"] == len(page) and sel["skipped_chars"] > 0)
        check("select: skipped parts are marked", "[...]" in sel["text"])
        check("select: ranked with embeddings", sel["method"] == "bge-m3")

        # A chunk with NO shared words must still be able to win via the
        # embedding: the question says "remap", the page only "rebind".
        syn = filler(80) + "\nYou can REBIND every key in the options." + "\n" + filler(80)
        sel = jr.select(syn, "how do I remap keys", budget=600)
        check("select: synonym-only chunk reachable through the spread sample",
              "REBIND" in sel["text"])

        # Embedding is bounded by characters.
        fake_embed.chars = 0
        jr.select(filler(2000), "keyboard", budget=800, embed_chars=3000)
        check("select: embedded text bounded by embed_chars",
              fake_embed.chars <= 3000 + 600)

        # Embed server down: lexical still finds it.
        def boom(texts):
            raise ConnectionError("down")
        jr.embed = boom
        sel = jr.select(page, q, budget=1200)
        check("select: embed server down -> lexical ranking, no exception",
              sel["method"] == "lexical" and "press F3" in sel["text"])
        jr.embed = fake_embed

        # Short text or no question: unchanged behaviour.
        check("select: short text returned whole",
              jr.select("short page", q)["text"] == "short page")
        check("select: no question -> head of the text",
              jr.select(page, "", budget=500)["text"] == page[:500])

        # ----------------------------------------------------- question plumbing
        def in_turn():
            jr.set_question("keyboard controls")
            return jr.question()
        check("question: set inside a context is visible there",
              contextvars.copy_context().run(in_turn) == "keyboard controls")
        check("question: and does not leak out of it", jr.question() == "")
        msgs = [{"role": "user", "content": "what are the keyboard controls for Lisa?"},
                {"role": "assistant", "content": "..."},
                {"role": "user", "content": "and remapping?"}]
        check("question: a short follow-up keeps the previous question",
              "Lisa" in jr.question_from(msgs) and "remapping" in jr.question_from(msgs))

        # ----------------------------------------------------- fetch_page
        import johan_tools as jt

        class Resp:
            def __init__(self, text):
                self.text = text
                self.headers = {"content-type": "text/html"}

            def raise_for_status(self):
                pass

        html = "<html><body>" + "".join(f"<p>{l}</p>" for l in page.splitlines()) + "</body></html>"
        real_get = jt.requests.get
        jt.requests.get = lambda *a, **k: Resp(html)
        try:
            def fetch_in_turn():
                jr.set_question(q)
                return jt.fetch_page("https://example.org/wiki")
            r = contextvars.copy_context().run(fetch_in_turn)
            check("fetch_page: long page returns the relevant part, with a note",
                  "press F3" in r["text"] and "most relevant" in r.get("note", ""))
            r = jt.fetch_page("https://example.org/wiki")
            check("fetch_page: outside a turn, the old head-of-page behaviour",
                  r["truncated"] and len(r["text"]) == 1800 and "note" not in r)
            jt.requests.get = lambda *a, **k: Resp(
                "<html><title>Welcome to Reddit</title></html>")
            r = contextvars.copy_context().run(
                lambda: (jr.set_question(q), jt.fetch_page("https://www.reddit.com/r/x/comments/1"))[1])
            check("fetch_page: reddit gate still detected", "error" in r and "Reddit" in r["error"])
        finally:
            jt.requests.get = real_get

        # ----------------------------------------------------- run_command lines
        out = "\n".join(f"line {i} ok" for i in range(300)) + \
            "\nERROR keyboard driver failed to bind key\n" + \
            "\n".join(f"line {i} ok" for i in range(300, 400))
        rel = contextvars.copy_context().run(
            lambda: (jr.set_question("why did the keyboard driver fail"),
                     jr.relevant_lines(out, skip_first=40))[1])
        check("run_command: the matching line past page 1 is surfaced with its number",
              "301: ERROR keyboard driver" in rel)
    finally:
        jr.embed = real_embed

    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    bad = [k for k, v in results.items() if not v]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
