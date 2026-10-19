"""Ask-with-options, plan-first and the live checklist — the engine side.

Three features, one module, so the shared files (engine, TUI) each
get only a few lines of glue:

  ASK        a tool, `ask(question, options)`, that ENDS the turn. The UIs show
             the options as choices; the pick is sent as his next message.
             The risk is a 4B over-asking on clear requests, which is slower
             than just doing it — so the tool description and the one prompt
             line both say "never on a clear request", and eval_tools.py has
             clear-request cases that must not call it.

  PLAN       for clearly big requests (several actions, or changing the
             machine in more than one way) Johan writes 2-6 one-line steps and
             he approves, edits or cancels BEFORE anything runs.
             Cost rule (CLAUDE.md, step 2): only extending a conversation is
             cheap, and a different tool schema between calls is a slow path.
             So:
               - normal messages pay nothing: a word-boundary pre-filter in
                 code decides whether to even ask (`prefilter`), then ONE
                 calibrated decide() check (~0.1-0.5 s, context first)
               - the plan is ONE model call that sends the SAME system prompt
                 and the SAME tool schema as the chat (prefix cache reused),
                 with `format` = a JSON schema. Measured 2026-09-13: format
                 plus tools switches tool calling off and forces the schema,
                 which broke the chat back then and is exactly what a plan
                 call wants
               - the plan is APPENDED to the conversation verbatim (the exact
                 generated JSON, so the KV cache is not invalidated), then the
                 approval is appended, then the ordinary loop runs and follows it

  CHECKLIST  each plan step names the tool it expects to use. `Progress`
             ticks steps off as tool calls complete, in order, with no model
             call per step (see Progress for the exact rule and why).
"""

import json
import re
import sys

import requests

INTERNAL = "[Johan, internal]"

# ============================================================== ask

ASK_TOOL = {"type": "function", "function": {
    "name": "ask",
    "description": "Ask him to pick one of 2-4 options (shown as buttons). Use it instead "
                   "of asking in text when a request is missing what to do it with. "
                   "Ends your turn. Never on a clear request — just do it.",
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}}},
        "required": ["question", "options"]}}}

MAX_OPTIONS = 4


def _clean_options(options):
    """Accept what a 4B actually sends: a list, or one string with newlines,
    '|' or ';' between options. Strips '1.'/'-' prefixes, drops duplicates."""
    if isinstance(options, str):
        options = re.split(r"\s*(?:\n|\||;)\s*", options)
    out = []
    for o in options or []:
        o = re.sub(r"^\s*(?:\d+[.)]|[-*•]|[A-Da-d][.)])\s*", "", str(o)).strip()
        if o and o.lower() not in (x.lower() for x in out):
            out.append(o[:120])
    return out


def ask(question="", options=None):
    """The tool. Validates and echoes; the ENGINE ends the turn on it."""
    q = (question or "").strip()
    opts = _clean_options(options)
    if not q:
        return {"error": "ask needs a `question`"}
    if len(opts) < 2:
        return {"error": "ask needs 2-4 distinct `options`. If only one reading "
                         "makes sense, don't ask — just do it."}
    return {"asked": True, "question": q, "options": opts[:MAX_OPTIONS]}


def render_ask(res):
    """What goes into history (and is shown if a UI has no choice widget):
    the question with the options numbered, so "2" or the option text both
    make sense to the model next turn."""
    lines = [res["question"], ""] + [f"{i}. {o}" for i, o in enumerate(res["options"], 1)]
    return "\n".join(lines)


def ended_by_ask(messages, start=0):
    """The asked-question result, if this turn (messages[start:]) ended on
    the ask tool; else None. Stateless: read back from the history, so it is
    safe across TUI worker threads."""
    if not messages or messages[-1].get("role") != "assistant":
        return None
    last = messages[-1].get("content")
    for m in reversed(messages[max(start, 0):-1]):
        if m.get("role") != "tool":
            break                    # only the final hop's results count
        try:
            res = json.loads(m.get("content") or "")
        except ValueError:
            continue
        if isinstance(res, dict) and res.get("asked") and last == render_ask(res):
            return res
    return None


# --- when the model asks in prose anyway ----------------------------------
# Measured while tuning: the prompt line and tool description did not stop
# the 4B's habit of LOOKING first ("change my wallpaper" -> gsettings get)
# and then asking in prose ("Une image spécifique ? Un lien web ?"). Worse,
# the answer verifier then judged that question "not backed by the tool
# result" and rewrote it into "no information about your preferences was
# found" — the question was destroyed. So code catches it: a reply whose
# last paragraph is a question offering a choice is checked once
# (calibrated), and if it really is waiting on his pick it is re-asked
# through the tool, appended to the same conversation (cheap path).

_CHOICE = re.compile(
    r"\b(or|ou|which|what|quel|quelle|quels|quelles|lequel|laquelle|"
    r"dis-moi|tell me|choose|pick)\b", re.I)

CLARIFY_Q = "Is Johan's reply waiting for Riadh to choose or say something before it can do what he asked?"
CLARIFY_OPTS = {"yes": "it cannot go on until he picks an option or says which or what",
                "no": "it answered or did the task; a question at the end is only an "
                      "optional offer"}
CLARIFY_NULL = "Message: N/A\nReply: N/A"
TO_ASK_PROMPT = (f"{INTERNAL} Ask me that again with the ask tool: the question "
                 "and 2-4 short options.")


def looks_like_choice_question(answer):
    """Free, code-only gate: last paragraph is a question that offers a
    choice. Whole words only."""
    paras = [p for p in re.split(r"\n\s*\n", (answer or "").strip()) if p.strip()]
    if not paras:
        return False
    tail = paras[-1]
    if len(paras) > 1 and re.match(r"^\s*(\d+[.)]|[-*•])\s", tail):
        tail = paras[-2] + "\n" + tail          # "which one?\n\n1. a\n2. b"
    return "?" in tail and bool(_CHOICE.search(tail))


def reask_as_tool(messages, q, answer, *, call_model, execute, on_tool=None,
                  decide=None, should_stop=None):
    """If `answer` (already appended as messages[-1]) is a prose question
    waiting on his choice, re-ask it via the ask tool. `q` is his message.
    Returns the ask result (messages then end with render_ask) or None
    (messages untouched)."""
    if not looks_like_choice_question(answer) or \
            (messages[-1].get("content") or "") != answer:
        return None
    try:
        if decide is None:
            import johan_decide as jd
            decide = jd.decide
        d = decide(CLARIFY_Q, CLARIFY_OPTS, f"Message: {q}\nReply: {answer[-600:]}",
                   kind="clarify", null_context=CLARIFY_NULL)
        if not (d.choice == "yes" and d.confident):
            return None
        trial = messages + [{"role": "user", "content": TO_ASK_PROMPT}]
        msg = call_model(trial, should_stop=should_stop)
    except Exception:
        return None
    msg.pop("interrupted", None)
    call = next((c for c in msg.get("tool_calls") or []
                 if c.get("function", {}).get("name") == "ask"), None)
    if not call:
        return None
    name, args, res, payload = execute(call)
    if not (isinstance(res, dict) and res.get("asked")):
        return None
    if on_tool:
        try:
            on_tool(name, args, res)
        except Exception:
            pass
    # The prose question is replaced, like the verifier replaces a draft,
    # so history reads as one question with its options.
    messages[-1] = {"role": "assistant", "content": "", "tool_calls": [call]}
    messages.extend([{"role": "tool", "content": payload},
                     {"role": "assistant", "content": render_ask(res)}])
    return res


def ask_result_of(results):
    """The first successful ask among a hop's (name, result) pairs."""
    for name, res in results:
        if name == "ask" and isinstance(res, dict) and res.get("asked"):
            return res
    return None


# ============================================================== plan trigger

# Questions are answered, not planned: "how do I set up docker?" wants an
# explanation. A request phrased politely ("can you set up…") is still a
# request, so can/could/would/please are NOT on this list.
_QUESTION = re.compile(
    r"^\s*(?:(?:can|could|would|will) you\s+|please\s+)?"
    r"(what|what's|whats|how|why|which|who|where|when|is|are|was|were|does|"
    r"do|did|should|explain|tell me (about|why|how|what)|show me how|teach me)\b", re.I)
# Writing about something is not doing it: "tidy up this paragraph: we set
# up the server…" got a plan card (the check said yes, confidently).
_WRITING = re.compile(r"\b(paragraph|sentence|text|email|e-mail|message|essay|letter|"
                      r"post|tweet|wording|grammar)\b", re.I)

# Verbs whose job is inherently several steps.
_BIG = re.compile(
    r"\b(set\s?up|setup|configure|reconfigure|migrate|organi[sz]e|reorgani[sz]e|"
    r"clean\s?up|tidy\s?up|back\s?up|backup|deploy|automate|refactor|overhaul|"
    r"reinstall)\b(?!\.\w)", re.I)          # "setup" yes, "setup.py" no

# Verbs, for counting distinct actions in one message. Whole words only —
# the substring version of this check is the bug class this project keeps
# shipping ("java" in "JavaScript", "25" in "2025", "erp" in "enterprise").
# CHANGE verbs do something; LOOK verbs only read. Several lookups need no
# approval — the loop does them fine — so a plan needs at least one change.
_CHANGE = re.compile(
    r"\b(install|uninstall|remove|delete|trash|move|rename|copy|create|make|"
    r"build|write|draft|download|update|upgrade|add|change|fix|enable|disable|"
    r"restart|stop|start|kill|close|convert|compress|extract|mark|apply|"
    r"replace|edit|set)\b(?!\.\w)", re.I)
_LOOK = re.compile(
    r"\b(find|search|check|compare|summari[sz]e|list|read|open|run|look)\b(?!\.\w)", re.I)
_JOIN = re.compile(r"\band\b|,|\b(then|after that|afterwards|followed by|finally)\b", re.I)


def prefilter(text):
    """Pure code, microseconds: is this message worth ASKING whether it needs
    a plan? False for every ordinary question, so they pay nothing.

    True when it is a request (not a question) and either uses a verb that
    is inherently several steps ("set up", "clean up", "migrate"), or joins
    two different actions of which at least one changes something ("install
    docker AND add me to the group", "draft CVs for the top 3, then compare")."""
    t = (text or "").strip()
    if len(t) < 12 or t.startswith("/") or _QUESTION.search(t):
        return False
    if _WRITING.search(t.split(":")[0]):
        return False
    if _BIG.search(t):
        return True
    change = {m.group(1).lower() for m in _CHANGE.finditer(t)}
    look = {m.group(1).lower() for m in _LOOK.finditer(t)}
    return bool(change) and len(change | look) >= 2 and bool(_JOIN.search(t))


# The calibrated check. Wording follows decide()'s measured rule: describe
# the situations, never name labels. Priors are tied to these exact strings.
PLAN_Q = "Is this message a big task that takes several separate actions?"
PLAN_OPTS = {"yes": "a task with several separate actions or changes, done one "
                    "after another",
             "no": "one action or one lookup, a question, or just talk"}
PLAN_NULL = "Message: N/A"


def needs_plan(text, decide=None):
    """prefilter, then at most one calibrated decision. Never raises: a
    broken decision layer means no plan, not a broken turn."""
    if not prefilter(text):
        return False
    try:
        if decide is None:
            import johan_decide as jd
            decide = jd.decide
        d = decide(PLAN_Q, PLAN_OPTS, f"Message: {text}", kind="plan_intent",
                   null_context=PLAN_NULL)
        return d.choice == "yes" and d.confident
    except Exception:
        return False


# ============================================================== plan call

PLAN_PROMPT = (
    f"{INTERNAL} Before doing anything, plan my request as 2-6 short steps in "
    "order. Each step: one action under 12 words, and the tool it uses (none "
    "if no tool). Look before changing anything. A step that changes my "
    "machine IS the change, made with propose_action (I approve each one). "
    "No steps for waiting, planning or approving. Don't do anything yet.")

REPLAN_PROMPT = (
    f"{INTERNAL} I want changes to that plan: {{changes}}\n"
    "Rewrite the plan with my changes, same format. Don't do anything yet.")

GO_PROMPT = (
    f"{INTERNAL} Plan approved. Do it now, step by step, in that order — stage "
    "each change with propose_action — then tell me briefly what was done and "
    "what is still waiting for my OK.")

CANCEL_PROMPT = f"{INTERNAL} I cancelled that plan. Don't do any of it."
CANCELLED = "Cancelled — nothing ran."

MAX_STEPS = 6


def plan_schema(tool_names):
    return {"type": "object", "required": ["steps"], "properties": {
        "steps": {"type": "array", "minItems": 2, "maxItems": MAX_STEPS,
                  "items": {"type": "object", "required": ["do", "tool"],
                            "properties": {
                                "do": {"type": "string"},
                                "tool": {"type": "string",
                                         "enum": sorted(tool_names) + ["none"]}}}}}}


def parse_plan(text):
    """Steps from the model's JSON: [{"do": str, "tool": str}], or [] if it
    is not a usable plan (then the turn runs normally)."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    steps = []
    for s in (data.get("steps") if isinstance(data, dict) else None) or []:
        if not isinstance(s, dict):
            continue
        do = " ".join(str(s.get("do") or "").split())
        if do:
            steps.append({"do": do[:140], "tool": str(s.get("tool") or "none")})
    return steps[:MAX_STEPS]


def is_plan_message(m):
    """A plan the model wrote, as stored in history (for replaying a chat)."""
    if m.get("role") != "assistant":
        return False
    c = (m.get("content") or "").lstrip()
    return c.startswith("{") and '"steps"' in c[:20] and bool(parse_plan(c))


def render_plan(steps):
    return "\n".join(f"{i}. {s['do']}" for i, s in enumerate(steps, 1))


def _with_images(messages):
    """Attached pictures as the chat call sends them (same prefix, same pixels)."""
    try:
        import johan_attach
        return johan_attach.ollama_messages(messages)
    except Exception:
        return messages


def _plan_call(messages, cfg):
    """ONE model call. Same model, system text, tool schema, num_ctx and
    keep_alive as call_model, so the conversation's cached prefix is reused;
    `format` turns tool calling off and forces the plan schema."""
    names = [t["function"]["name"] for t in cfg.TOOLS]
    import johan_interrupt as ji     # esc stops it mid-wait
    body = ji.ollama_post(cfg.OLLAMA_CHAT, {
        "model": cfg.MODEL, "messages": _with_images(messages), "tools": cfg.TOOLS,
        "format": plan_schema(names), "think": False,
        "keep_alive": cfg.KEEP_ALIVE,
        "options": {"temperature": 0, "num_ctx": cfg.NUM_CTX, "num_predict": 400}},
        timeout=120)
    return (body.get("message") or {}).get("content") or ""


def make_plan(messages, changes=None, cfg=None, call=None):
    """Append the plan request and the plan to `messages`; return the steps.

    Returns [] (and leaves `messages` exactly as it was) when the model did
    not produce a usable 2+ step plan — the caller then runs the turn
    normally. `changes` re-plans after he typed edits."""
    if cfg is None:
        import johan_assistant as cfg  # noqa: F811
    call = call or _plan_call
    prompt = REPLAN_PROMPT.format(changes=changes) if changes else PLAN_PROMPT
    trial = messages + [{"role": "user", "content": prompt}]
    try:
        content = call(trial, cfg)
    except Exception as e:      # noqa: BLE001 - no plan, not a broken turn
        print(f"[plan] plan call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    steps = parse_plan(content)
    if len(steps) < 2:
        return []
    # The EXACT generated text, not a re-rendering: rewriting it would make
    # the next request diverge from the cached prefix mid-conversation.
    messages.extend([trial[-1], {"role": "assistant", "content": content}])
    return steps


def approve(messages):
    messages.append({"role": "user", "content": GO_PROMPT})


def cancel(messages):
    """Recorded in history so a later "ok do it" can't be read as approval."""
    messages.extend([{"role": "user", "content": CANCEL_PROMPT},
                     {"role": "assistant", "content": CANCELLED}])


# ============================================================== checklist

PENDING, ACTIVE, DONE, FAILED, SKIPPED = "pending", "active", "done", "failed", "skipped"
MARKS = {PENDING: "○", ACTIVE: "▸", DONE: "✓", FAILED: "✗", SKIPPED: "–"}


class Progress:
    """Ticks plan steps off as tool calls complete — no model call per step.

    Rule: each step names the tool it expects. A tool call is attributed to
    the FIRST not-yet-finished step (from the current one on) that expects
    that tool — after the current step has one call that worked, the next
    call of that tool moves on (steps are one action each; a retry after a
    failure stays put); reaching a later step finishes the ones before it. A call
    whose tool no remaining step expects counts as more work on the current
    step. Finished = done if any of its calls worked, failed if all failed,
    skipped if it had none (a `none`-tool step — thinking, telling him — is
    done when passed, since it needs no call). When the turn ends with an
    answer, the active step and any remaining `none` steps are done; steps
    never reached stay skipped. That is honest: a tick means a tool for that
    step actually ran.

    Why not a `step` parameter on every tool, or a step_done tool: both make
    the model spend a hop (a full model call) per step, and a schema change
    touches every chat. Matching by tool in order is free and right whenever
    the model follows its own plan, which it was just told to."""

    def __init__(self, steps):
        self.steps = [dict(s) for s in steps]
        for s in self.steps:
            s.update(state=PENDING, ok=0, failed=0)
        self.cur = 0             # nothing is active until a tool call lands

    def _close(self, i):
        s = self.steps[i]
        if s["ok"]:
            s["state"] = DONE
        elif s["failed"]:
            s["state"] = FAILED
        else:
            s["state"] = DONE if s["tool"] == "none" else SKIPPED

    def on_tool(self, name, failed=False):
        if not self.steps:
            return
        n = len(self.steps)
        # Each planned step is one action, so once the current step has a
        # call that WORKED, the next call to the same tool belongs to the next
        # step expecting it; a retry after a failure stays where it is.
        here = self.cur < n and self.steps[self.cur]["ok"]
        target = next((j for j in range(self.cur + (1 if here else 0), n)
                       if self.steps[j]["tool"] == name), None)
        if target is None:
            target = next((j for j in range(self.cur, n)
                           if self.steps[j]["tool"] == name), min(self.cur, n - 1))
        for j in range(self.cur, target):
            self._close(j)
        self.cur = target
        s = self.steps[target]
        s["state"] = ACTIVE
        s["failed" if failed else "ok"] += 1

    def finish(self, answered=True):
        for j in range(self.cur, len(self.steps)):
            s = self.steps[j]
            if j == self.cur and (s["ok"] or s["failed"]):
                self._close(j)
            elif answered and s["tool"] == "none":
                s["state"] = DONE
            elif s["state"] in (PENDING, ACTIVE):
                s["state"] = SKIPPED
        self.cur = len(self.steps)

    def counts(self):
        done = sum(1 for s in self.steps if s["state"] == DONE)
        return done, len(self.steps)

    def lines(self):
        return [f"{MARKS[s['state']]} {s['do']}" for s in self.steps]

    def text(self):
        done, total = self.counts()
        return f"Plan {done}/{total}\n" + "\n".join(self.lines())


def failed_result(result):
    return isinstance(result, dict) and bool(result.get("error"))
