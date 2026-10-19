"""johan_learn — corrections become durable behaviour rules.

When Riadh corrects HOW Johan behaves ("stop ending every answer with a
question", "I told you, use metric", "don't use bullet points"), the rule is
saved as a memory of kind 'feedback' and injected into every later
session's system prompt, in a short "How he wants you to behave" block.

Three gates, cheapest first, so a normal turn pays nothing:
  1. PRE-FILTER  word-boundary patterns ("stop doing", "I told you",
                 "don't use", "from now on"…). Never substring checks — this
                 codebase has shipped "java" in "JavaScript" and "25" in
                 "2025". Most messages stop here at zero cost.
  2. DECIDE      one calibrated multiple-choice call (johan_decide): is this
                 a correction of Johan's behaviour, or him correcting a fact
                 about the world / asking for something? Only a CONFIDENT
                 "behaviour" goes on. ~0.2-1 s, only when (1) fired.
  3. WRITE       one short constrained-JSON call turns the message into
                 {rule, why}. Code then checks it is short, imperative-ish
                 and not a duplicate of a stored rule before saving.

Runs AFTER the answer, so he sees the reply first. The rule is shown back to
him (a TUI toast) with the id to undo it — a wrongly learned
rule would otherwise steer every session silently.
"""

import json
import re

import requests

import johan_tools as jt

KIND = "feedback"
MAX_RULES = 10           # injected into the prompt, newest first …
MAX_BLOCK_CHARS = 600    # … and never more than this, so prompt growth stays bounded
RULE_CHARS = 140
WHY_CHARS = 100
DUP_SCORE = 0.90         # bge-m3 cosine above which a new rule repeats an old one

# Gate 1. Phrases a correction of BEHAVIOUR is made of. Every alternative is
# word-bounded; the verbs after "don't"/"stop" are listed so that "why don't
# my headphones work" and "stop the container" never reach the model.
_VERBS = (r"(do|doing|say|saying|use|using|ask|asking|add|adding|end|ending|start|"
          r"starting|give|giving|call|calling|run|running|put|putting|make|making|"
          r"tell|telling|bring|bringing|suggest|suggesting|show|showing|write|writing|"
          r"answer|answering|reply|replying|talk|talking|offer|offering|mention|"
          r"mentioning|be|being|repeat|repeating|apologi[sz]e|apologi[sz]ing|"
          r"pitch|pitching|explain|explaining|list|listing|guess|guessing|assume|"
          r"assuming|send|sending|recommend|recommending|format|formatting|include|"
          r"including|lecture|lecturing|summari[sz]e|summari[sz]ing|greet|greeting|"
          r"search|searching|look|looking|check|checking|read|reading|open|opening)")
PREFILTER = re.compile(
    r"(^\s*(no+|nope|nah|wrong|please (don'?t|stop|never))\b)"
    r"|\b(i (already |just )?(told|asked) you|i said (to|not|no)|you keep|you always|"
    r"how many times|from now on|next time|in (the )?future|going forward|"
    r"that'?s (wrong|not what i)|not what i (asked|wanted|meant)|enough with|"
    r"stop " + _VERBS + r"|quit " + _VERBS + r"|"
    r"(don'?t|do not|never) (ever )?" + _VERBS + r"|"
    r"always " + _VERBS + r"|(i|we) (want|prefer|need) you to|"
    r"i (hate|don'?t like) (it )?when you|stop it|cut it out)\b",
    re.I)

FEEDBACK_Q = "Is Riadh correcting how Johan behaves?"
FEEDBACK_OPTS = {
    "yes": "he tells Johan to change how it behaves from now on — what it does, "
           "how it answers, its tone, format or habits",
    "no": "he corrects a fact about the world or his own situation, gives new "
          "information, asks a question, or asks for a task to be done",
}
FEEDBACK_NULL = "Johan said: N/A\nRiadh: N/A"
try:                                   # so eval_decide.py can score this kind
    import johan_decide as _jd
    _jd.NULLS.setdefault("feedback", FEEDBACK_NULL)
except Exception:
    pass

_SCHEMA = {"type": "object", "properties": {
    "rule": {"type": "string"}, "why": {"type": "string"}},
    "required": ["rule", "why"]}
_WRITE_PROMPT = (
    "Riadh just corrected his assistant Johan. Turn the correction into ONE "
    "standing rule for Johan.\n"
    "rule: one short imperative sentence addressed to Johan, under 15 words "
    "(e.g. \"Don't end answers with a question.\"). Write it from Johan's "
    "side: \"his\" home folder, not \"my\".\n"
    "why: the reason in under 12 words, from what he said; if he gave none, "
    "describe what Johan did that annoyed him.\n"
    "Use only what he said. Reply as JSON.\n\n")


def might_be_correction(message):
    """Gate 1: True when the message is worth asking the model about."""
    text = (message or "").strip().replace("\u2019", "'")   # phones send don’t
    if len(text) < 6 or len(text) > 600:
        return False
    return bool(PREFILTER.search(text))


def _engine():
    import sys
    ja = sys.modules.get("johan_assistant")
    if ja is None:
        import johan_assistant as ja  # noqa: F811
    return ja


def _context(prev_answer, message):
    prev = re.sub(r"\s+", " ", prev_answer or "").strip()
    if len(prev) > 500:
        prev = prev[:500] + "…"
    return f"Johan said: {prev or '(nothing yet)'}\nRiadh: {message.strip()}"


def is_correction(prev_answer, message):
    """Gate 2: the calibrated decision. Returns the Decision (or None)."""
    import johan_decide as jd
    try:
        return jd.decide(FEEDBACK_Q, FEEDBACK_OPTS, _context(prev_answer, message),
                         kind="feedback", null_context=FEEDBACK_NULL)
    except Exception:
        return None


def write_rule(prev_answer, message):
    """Gate 3: {rule, why} from one short constrained-JSON call, or None."""
    ja = _engine()
    try:
        import johan_interrupt as ji     # esc stops it mid-wait
        body = ji.ollama_post(ja.OLLAMA_CHAT, {
            "model": ja.MODEL, "think": False,
            "keep_alive": ja.KEEP_ALIVE, "format": _SCHEMA,
            # Same num_ctx as the chat — anything else reloads the model.
            "options": {"temperature": 0, "num_ctx": ja.NUM_CTX, "num_predict": 80},
            "messages": [{"role": "user", "content":
                          _WRITE_PROMPT + _context(prev_answer, message)}],
        }, timeout=60)
        out = json.loads(body["message"]["content"])
    except Exception:
        return None
    return clean(out.get("rule"), out.get("why"))


def clean(rule, why):
    """Code checks what the model wrote. None when it isn't a usable rule."""
    rule = re.sub(r"\s+", " ", str(rule or "")).strip().strip('"').strip()
    why = re.sub(r"\s+", " ", str(why or "")).strip().strip('"').strip().rstrip(".")
    if len(rule) < 8 or len(rule) > RULE_CHARS:
        return None
    if not rule.endswith((".", "!")):
        rule += "."
    rule = rule[0].upper() + rule[1:]
    return {"rule": rule, "why": why[:WHY_CHARS]}


def as_text(rule):
    return f"{rule['rule']} (why: {rule['why']})" if rule.get("why") else rule["rule"]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def rules(limit=MAX_RULES):
    """Stored behaviour rules, newest first."""
    import psycopg2.extras
    conn = jt._conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select id, text from memories where kind = %s "
                "order by created_at desc, id desc limit %s", (KIND, int(limit)))
    out = [{"memory_id": r["id"], "text": r["text"]} for r in cur.fetchall()]
    conn.close()
    return out


def rules_block(rows, max_rules=MAX_RULES, max_chars=MAX_BLOCK_CHARS):
    """The prompt block. Newest rules win when the cap is reached."""
    lines, used = [], 0
    for r in rows[:max_rules]:
        line = f"- {r['text']}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return ("How he wants you to behave (rules from his own corrections — "
            "follow them):\n" + "\n".join(lines) + "\n")


def _duplicate(rule_text):
    """An existing rule saying the same thing, or None."""
    norm = re.sub(r"\W+", " ", rule_text.lower()).strip()
    try:
        existing = rules(limit=50)
    except Exception:
        return None
    for r in existing:
        if re.sub(r"\W+", " ", r["text"].split(" (why:")[0].lower()).strip() == norm:
            return r
    try:
        hits = jt.recall(rule_text, top_k=5).get("memories", [])
    except Exception:
        return None
    for h in hits:
        if h.get("kind") == KIND and (h.get("score") or 0) >= DUP_SCORE:
            return {"memory_id": h["memory_id"], "text": h["text"]}
    return None


def save(rule):
    text = as_text(rule)
    dup = _duplicate(rule["rule"])
    if dup:
        return {"memory_id": dup["memory_id"], "text": dup["text"], "duplicate": True}
    res = jt.remember(text, kind=KIND)
    if not res.get("stored"):
        return None
    return {"memory_id": res["memory_id"], "text": text, "duplicate": False}


# --------------------------------------------------------------------------
# the hook run_turn calls
# --------------------------------------------------------------------------

def _prev_answer(messages, start):
    for m in reversed(messages[:start]):
        if m.get("role") == "assistant" and m.get("content"):
            return m["content"]
    return ""


def after_turn(messages, start, message):
    """Learn from `message` if it corrects Johan's behaviour. Returns a list
    of {memory_id, text, duplicate} (empty almost always). Never raises:
    learning must not be able to break a turn."""
    try:
        if not might_be_correction(message):
            return []
        prev = _prev_answer(messages, start)
        d = is_correction(prev, message)
        if d is None or not (d.choice == "yes" and d.confident):
            return []
        rule = write_rule(prev, message)
        if not rule:
            return []
        saved = save(rule)
        return [saved] if saved else []
    except Exception:
        return []
