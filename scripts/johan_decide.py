"""The decision layer: score a fixed set of options in one forward pass.

Why this exists: a 4B model is weak at long free-form reasoning and much
better at picking one of a few options it can see. So control flow — which
tool, did this step work, is this claim backed, is this risky — is asked as
a multiple-choice question and answered from the model's own token
probabilities (Ollama `logprobs`), not from generated prose. Same idea as
Jev / "System One" scoring. No extra model and no extra VRAM: it is the chat
model, warm, reading one token.

Measured on this box (qwen3.5:4b, johan-agent) before building on it:
  - ~0.1-1 s per decision warm. Context must come FIRST in the prompt:
    context-first 110 ms, context-last 1,044 ms (the hybrid model is slow
    when a prompt shares a long prefix with the previous one, then diverges)
  - a DIFFERENT num_ctx than the chat uses forces a full model reload
    (8-12 s) — so every call here reuses the chat's exact num_ctx/keep_alive
  - a decision between two chat hops does NOT evict the chat's cache (next
    hop stayed ~200 ms; reprocessing 8K tokens cold costs 36 s)
  - WORDING dominates: options that name tools ("run_command") got the wrong
    answer in both orders; options that describe the situation ("needs a
    file on this machine") got it right. Write options as situations.
  - the model LEANS: on content-free input it puts 0.83 on "not backed" and
    0.90 on "just talk". Calibration divides that out: eval_decide.py 84% ->
    95% tuned, 83% -> 100% on held-out cases.
  - debias (score both orders) measured no better and costs a slow 2nd call,
    so it is off by default.

Probabilities from a 4B are rough. 0.8 is not "80% right" — use thresholds
set by eval_decide.py, never treat a number as a fact.

    from johan_decide import decide, check
    d = decide("What does this message need?", {
        "local": "a file, program or setting on this machine",
        "web": "live information from the internet",
        "talk": "nothing — small talk or general knowledge"},
        context="User: can you check my tf2 config?")
    d.choice, d.probs, d.confident
"""

import json
import math
import string
import sys
import time
from dataclasses import dataclass, field

import requests

LETTERS = string.ascii_uppercase[:20]      # Ollama accepts top_logprobs up to 20

DECIDE_SYSTEM = (
    "You are the decision module of Johan, a local assistant on Riadh's Linux "
    "PC (Pop!_OS). Johan can run read-only shell commands anywhere on the "
    "machine, read and search files, search the web, query Riadh's job-search "
    "database, and remember facts about him. You are shown a situation and "
    "lettered options. Pick the option that best fits. Reply with the option "
    "letter only.")

# Below these the decision is reported as not confident. Defaults; the real
# values are whatever eval_decide.py says separates right from wrong.
DEFAULT_THRESHOLD = 0.60
MIN_COVERAGE = 0.50        # share of probability the model put on ANY letter

# Contextual calibration (Zhao et al. 2021). A 4B leans hard toward some
# options regardless of input — scored on content-free "N/A" it put 0.83 on
# "the answer is NOT backed" and 0.90 on "just talk". Dividing that lean out
# took eval_decide.py from 84% to 91%: "claim backed?" 5/8 -> 8/8 (every miss
# had been a false "no"), "risky?" 7/8 -> 8/8. The floor stops a near-zero
# prior from inflating an option into a win (routing dropped a case at
# floor 0.001, gained one at 0.05). Priors depend only on the question and
# options, so each is measured once and cached on disk.
PRIOR_FLOOR = 0.05
NULL_CONTEXT = "N/A"
_PRIORS = {}


# --- canonical decisions ---------------------------------------------------
# Production and eval_decide.py use THESE exact strings. Calibration priors
# and the measured accuracy are tied to the wording — edit one and you must
# re-run the eval, or the numbers in CLAUDE.md no longer describe the code.

ROUTE = {
    "local": "something on this machine — a file, folder, program, setting, "
             "hardware or running service",
    "web": "information from the internet — news, weather, forums, guides, "
           "software versions, facts that change",
    "jobs": "Riadh's job search — postings, the apply queue, CVs, applications, "
            "which companies can hire him",
    "personal": "a fact about Riadh himself that he told Johan earlier",
    "compute": "a calculation, or today's date or time",
    "talk": "nothing to look up — a greeting, thanks, an opinion, or general "
            "knowledge Johan already has",
}
ROUTE_Q = "What does this message need?"

DONE_Q = "Did this tool result get what the step was trying to get?"
SUPPORT_Q = "Is every fact in the answer backed by the tool result?"
RISKY_Q = "Would running this command change or delete anything on the machine?"

# Options describe the two situations rather than saying "yes"/"no" — the
# same rule that fixed routing: describe what is true, don't name a label.
DONE_OPTS = {"yes": "the result contains what the step was looking for",
             "no": "the result is an error, empty, or not what was asked for"}
SUPPORT_OPTS = {"yes": "every fact and number in the answer appears in the tool result",
                "no": "the answer states something the tool result does not say, "
                      "or gets a fact or number wrong"}
RISKY_OPTS = {"yes": "it deletes, stops, overwrites, uploads or otherwise changes something",
              "no": "it only reads or displays information"}

# Content-free input per kind, for calibration: same shape as a real case.
NULLS = {"route": "Message: N/A", "step_done": "Goal: N/A\nTool result: N/A",
         "supported": "Tool result: N/A\nAnswer: N/A", "risky": "Command: N/A",
         "multi_step": "Message: N/A"}

RESEARCH_Q = "Is Riadh asking Johan to research something in depth?"
RESEARCH_OPTS = {"yes": "he explicitly asks for research, a deep dive, or to "
                        "look into something properly across many sources",
                 "no": "an ordinary question, or a quick lookup"}
NULLS["research_intent"] = "Message: N/A"

MULTI_Q = "How many separate lookups does answering this message take?"
MULTI_OPTS = {"yes": "several lookups in a row, where a later one needs the "
                     "result of an earlier one",
              "no": "one lookup or action, or none at all"}


def _cfg():
    """Chat-model settings, read lazily so this module never imports the
    engine at load time (the engine will import this one)."""
    ja = sys.modules.get("johan_assistant")
    if ja is None:
        import johan_assistant as ja  # noqa: F811
    return ja


@dataclass
class Decision:
    choice: str                      # the winning option KEY
    probs: dict                      # key -> normalised probability
    margin: float                    # top minus runner-up
    coverage: float                  # raw mass on valid letters (0-1)
    confident: bool
    ms: int
    raw: list = field(default_factory=list, repr=False)

    @property
    def p(self):
        return self.probs[self.choice]


def _normalise_options(options):
    """Accept a list of strings or a dict {key: description}."""
    if isinstance(options, dict):
        items = list(options.items())
    else:
        items = [(o, o) for o in options]
    if not 2 <= len(items) <= len(LETTERS):
        raise ValueError(f"decide() needs 2-{len(LETTERS)} options, got {len(items)}")
    return items


def _letter_of(token):
    """Map a returned token to an option letter, or None.

    Tokens arrive as 'A', ' A', 'A)', '**A', 'a' depending on the model's
    mood. Anything else ('The', '<think>') is mass the model put on NOT
    answering, which is what coverage measures."""
    t = token.strip().strip("*()[].:").upper()
    return t if len(t) == 1 and t in LETTERS else None


def _score_once(question, items, context, system):
    cfg = _cfg()
    lines = [f"{LETTERS[i]}) {desc}" for i, (_, desc) in enumerate(items)]
    prompt = ((context.strip() + "\n\n") if context else "") + \
        f"{question.strip()}\n\nOptions:\n" + "\n".join(lines) + \
        "\n\nAnswer with one letter."
    r = requests.post(cfg.OLLAMA_CHAT, json={
        "model": cfg.MODEL, "stream": False, "think": False,
        # Same num_ctx and keep_alive as the chat: anything else reloads the
        # model (measured 8-12 s) and evicts the conversation's KV cache.
        "keep_alive": cfg.KEEP_ALIVE,
        "logprobs": True, "top_logprobs": len(LETTERS),
        "options": {"num_predict": 1, "temperature": 0, "num_ctx": cfg.NUM_CTX},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }, timeout=120)
    r.raise_for_status()
    body = r.json()
    top = (body.get("logprobs") or [{}])[0].get("top_logprobs", [])
    mass = [0.0] * len(items)
    for t in top:
        letter = _letter_of(t.get("token", ""))
        if letter is not None and LETTERS.index(letter) < len(items):
            mass[LETTERS.index(letter)] += math.exp(t["logprob"])
    return mass, top


def _prior_path():
    return _cfg().ROOT / "brain/decide-priors.json"


def _prior(question, items, system, null_context):
    """The model's lean on this question with no real input. Cached."""
    import hashlib
    key = hashlib.sha1(json.dumps([question, items, system, null_context,
                                   _cfg().MODEL]).encode()).hexdigest()[:16]
    if not _PRIORS:
        try:
            _PRIORS.update(json.loads(_prior_path().read_text()))
        except Exception:
            pass
    if key not in _PRIORS:
        mass, _ = _score_once(question, items, null_context, system)
        s = sum(mass)
        _PRIORS[key] = [m / s for m in mass] if s > 0 else [1 / len(items)] * len(items)
        try:
            _prior_path().write_text(json.dumps(_PRIORS))
        except Exception:
            pass
    return _PRIORS[key]


def decide(question, options, context="", *, kind="general", debias=False,
           calibrate=True, null_context=NULL_CONTEXT,
           threshold=DEFAULT_THRESHOLD, system=DECIDE_SYSTEM, log=True):
    """Pick one of `options`. Returns a Decision.

    `debias=True` scores the options in original AND reversed order and
    averages. Off by default because it measured WORSE (84% vs 88% on
    eval_decide.py) and the second call is slow: it shares the long context
    prefix with the first and then diverges, which on this hybrid model costs
    ~1 s instead of ~0.1 s. Keep context FIRST in every prompt for the same
    reason — context-first measured 110 ms, context-last 1,044 ms."""
    items = _normalise_options(options)
    started = time.monotonic()
    orders = [list(range(len(items)))]
    if debias:
        orders.append(orders[0][::-1])

    total = [0.0] * len(items)
    coverages, raws = [], []
    for order in orders:
        mass, raw = _score_once(question, [items[i] for i in order], context, system)
        raws.append(raw)
        coverages.append(sum(mass))
        for pos, idx in enumerate(order):
            total[idx] += mass[pos]

    coverage = sum(coverages) / len(coverages)
    if calibrate:
        prior = _prior(question, items, system, null_context)
        total = [x / max(pr, PRIOR_FLOOR) for x, pr in zip(total, prior)]
    s = sum(total)
    probs_list = [x / s for x in total] if s > 0 else [1 / len(items)] * len(items)
    ranked = sorted(range(len(items)), key=lambda i: -probs_list[i])
    top, second = ranked[0], ranked[1]
    probs = {items[i][0]: round(probs_list[i], 4) for i in range(len(items))}
    d = Decision(
        choice=items[top][0], probs=probs,
        margin=round(probs_list[top] - probs_list[second], 4),
        coverage=round(coverage, 4),
        confident=probs_list[top] >= threshold and coverage >= MIN_COVERAGE,
        ms=int((time.monotonic() - started) * 1000), raw=raws)
    if log:
        _log(kind, question, items, d, context)
    return d


def check(question, context="", *, yes="yes", no="no", kind="check",
          threshold=DEFAULT_THRESHOLD, **kw):
    """Yes/no question. Returns (p_yes, Decision).

    Pass `yes`/`no` as descriptions of the two situations — "the answer only
    states what the tool result says" beats a bare "yes". Asked as lettered
    options, not "answer yes or no": the native form put under a quarter of
    the model's probability on either word (it wants to start with prose)."""
    d = decide(question, {"yes": yes, "no": no}, context, kind=kind,
               threshold=threshold, **kw)
    return d.probs["yes"], d


def score_each(items, question, context="", *, kind="score", **kw):
    """Score every item independently with a yes/no question — for ranking
    lists longer than 20 (CV bullets, search results). `question` may contain
    `{item}`. Returns [(p_yes, item), ...] sorted best first.

    Shared context goes FIRST in the prompt so Ollama's prompt cache can reuse
    it across items; only the item text changes at the end."""
    scored = []
    for it in items:
        q = question.format(item=it) if "{item}" in question else f"{question}\n\n{it}"
        p, _ = check(q, context, kind=kind, **kw)
        scored.append((p, it))
    return sorted(scored, key=lambda x: -x[0])


def warm():
    """Load the model with the chat's settings, cheaply."""
    try:
        decide("Warm-up.", ["ready", "not ready"], debias=False, log=False)
    except Exception:
        pass


def _log(kind, question, items, d, context="", detail=None):
    """Fire-and-forget, like tool_calls: telemetry must never break a turn.
    Low-confidence rows are what self-improvement reads later — which is why
    the CONTEXT is stored: the first version logged only the (constant)
    question, and two verifier rewrites in an eval could not be inspected."""
    try:
        cfg = _cfg()
        conn = cfg._conn()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """insert into decisions
                   (session_id, model, kind, question, options, probs, choice,
                    p_top, margin, coverage, confident, duration_ms, eval_run,
                    context, detail)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (cfg.SESSION_ID, cfg.MODEL, kind, question[:2000],
                 json.dumps([k for k, _ in items]), json.dumps(d.probs),
                 d.choice, d.p, d.margin, d.coverage, d.confident, d.ms,
                 getattr(cfg, "EVAL_RUN", None), (context or "")[:4000],
                 json.dumps(detail) if detail else None))
        conn.close()
    except Exception as e:      # noqa: BLE001
        print(f"[decide] could not log decision: {type(e).__name__}", file=sys.stderr)


def log_rewrite(draft, fixed, decision, context=""):
    """Record a verifier rewrite with both versions, so it can be judged."""
    _log("rewrite", SUPPORT_Q, [("yes", ""), ("no", "")], decision, context,
         detail={"draft": draft[:3000], "fixed": fixed[:3000]})


_SENTENCE_SPLIT = None


def drop_unbacked(text, evidence, *, kind="claim"):
    """Check EVERY sentence of `text` against `evidence`; drop the ones the
    calibrated checker is confident are not backed. Returns (kept, dropped).

    Whole-answer checks were measured too coarse twice: a research report's
    invented "Q-E-R-T-Y" and an application draft's invented "we solved it
    with structured async updates and decision logs" both passed, because
    one long answer against truncated evidence reads as mostly supported.
    One sentence at a time is a short, precise context."""
    import re as _re
    global _SENTENCE_SPLIT
    if _SENTENCE_SPLIT is None:
        _SENTENCE_SPLIT = _re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
    ev = (evidence or "")[:1400]
    kept, dropped = [], []
    for sent in _SENTENCE_SPLIT.split((text or "").strip()):
        if not sent.strip():
            continue
        try:
            _, dd = check(SUPPORT_Q, f"Tool result: {ev}\nAnswer: {sent}",
                          yes=SUPPORT_OPTS["yes"], no=SUPPORT_OPTS["no"],
                          kind=kind, null_context=NULLS["supported"])
        except Exception:
            kept.append(sent)
            continue
        (dropped if dd.choice == "no" and dd.confident else kept).append(sent)
    return " ".join(kept).strip(), dropped
