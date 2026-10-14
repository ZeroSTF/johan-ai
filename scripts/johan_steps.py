"""The step engine: plan, do one step at a time, check each, verify the answer.

Why: the free-form loop hands a 4B model nine hops and hopes. Its measured
failures were long-chain failures — eight hops lost to one error, rummaging
in the wrong folder, answering from nothing, stating a number the tool never
returned. This engine keeps the model on one short job at a time and lets
code, via the calibrated decision layer (johan_decide.py), make the control
decisions:

  1. PLAN    decide() whether the message needs several DEPENDENT lookups.
             If so the model writes <= 4 steps; otherwise the ordinary loop.
  2. STEP    each step is its own instruction, <= 3 hops. After every tool
             result, decide() "did this get what the step needed?" — yes
             moves on immediately.
  3. ANSWER  written from what the steps found.
  4. VERIFY  decide() "is every fact backed by the tool results?" A
             confident "no" triggers one rewrite, which replaces the
             streamed draft on screen (both UIs render the returned reply).

THE CONSTRAINT THAT SHAPED THIS (measured, see CLAUDE.md): on this hybrid
model only EXTENDING a conversation is cheap. Changing anything near the
start of the prompt — a different tool schema, a fresh context per step —
reprocesses everything, sometimes on a ~225 tok/s slow path (1,721 tokens:
7.6 s). The first version shortlisted tools and gave each step a fresh
context; it took 58-66 s for questions the loop answers in 4-20 s. So:
  - the system prompt and the FULL tool schema never change
  - plan, steps and answer are appended to the SAME conversation
  - decisions are separate small calls, which were measured not to evict
    the conversation's cache
Tool shortlisting was dropped rather than fixed: tool choice already scores
24/24, so it had nothing to win.

Same signature as run_turn_loop, so every caller works unchanged. Selected
with johan_assistant.ENGINE = "steps".
"""

import json
import re

import johan_assistant as ja
import johan_decide as jd

STEP_HOPS = 3
MAX_STEPS = 4
# Evidence shown to the verifier. It re-sends this as fresh text, and fresh
# text is the slow path on this model (~225 tok/s): 2,400 chars measured
# ~3 s per check. 1,400 keeps the check near ~1.5 s; answers cite early
# lines of a result far more often than late ones.
EVIDENCE_CHARS = 1400
PER_RESULT_CHARS = 700

PLAN_PROMPT = (
    "[Johan, internal] Before answering, break my request into at most {n} "
    "short steps. Each step is ONE lookup or action you can do with a tool. "
    "Write only the numbered steps, one per line, no explanations, and no "
    "step for writing the answer.")

STEP_PROMPT = (
    "[Johan, internal] Do step {k} of {total} now: {step}\n"
    "Use a tool. Stop as soon as a result gives what this step needs.")

ANSWER_PROMPT = (
    "[Johan, internal] All steps are done. Now answer my original question "
    "using only what the tool results above show: {q}\n"
    "If something was not found, say so plainly instead of guessing. Short "
    "and direct.")

FIX_PROMPT = (
    "[Johan, internal] Check that answer against the tool results above. "
    "Rewrite it so it states only facts and numbers that appear in those "
    "results, exactly as they appear, and says plainly what was not found. "
    "Your own ideas, advice and suggestions may stay — only facts and numbers "
    "must come from the results. "
    "He never saw the first version: don't mention it, corrections or mistakes. "
    "Keep my language and your normal tone. "
    "Reply with the corrected answer only.")

_ANSWER_STEP = re.compile(r"\b(answer|respond|reply|summari[sz]e|tell (him|riadh|me|the user)|report)\b", re.I)
_LOOKUP_VERB = re.compile(r"\b(search|find|read|run|check|look|query|list|get|fetch|open|count|compare)\b", re.I)


def _parse_plan(text):
    steps = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?:\d+[.)]|[-*•])\s*(.+?)\s*$", line)
        if not m:
            continue
        step = m.group(1).strip()
        # "Answer the user" is not a lookup; the engine writes the answer.
        if _ANSWER_STEP.search(step) and not _LOOKUP_VERB.search(step):
            continue
        steps.append(step)
    return steps[:MAX_STEPS]


def _compact(result):
    """The part of a tool result worth showing the step checker."""
    if isinstance(result, dict):
        if result.get("error"):
            return f"error: {result['error']}"
        if "output" in result:
            return result["output"] or "(no output)"
    try:
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


def _shown_by_code(content):
    """find_images results: the pictures, titles and sources are put on his
    screen by code, so the answer is only a caption list. Checking it against
    them measured 3/3 false alarms ("History, Height, & Facts" in a page title
    read as a claim) and each rewrite made a word-for-word answer worse."""
    return content.startswith('{"count": ') and '"pictures": [' in content[:40]


def _failed_only(content):
    """A result that is nothing but an error carries no facts to check
    against: "what skills do you have?" + one failed call was judged
    "not backed" and a correct self-description was rewritten into nonsense."""
    try:
        d = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(d, dict) and set(d) <= {"error", "blocked", "hint", "seconds"}


def _evidence(messages, start):
    """Successful tool results added to the conversation since `start`."""
    parts = [m["content"][:PER_RESULT_CHARS] for m in messages[start:]
             if m.get("role") == "tool" and m.get("content")
             and not _shown_by_code(m["content"]) and not _failed_only(m["content"])]
    return "\n".join(parts)[:EVIDENCE_CHARS]


def _verify_and_fix(messages, start, answer, should_stop=None):
    """If the answer is confidently NOT backed by this turn's tool results,
    rewrite it once. Mutates `messages` so the rewrite replaces the draft.
    Returns the answer to show."""
    evidence = _evidence(messages, start)
    if not answer or not evidence or (should_stop and should_stop()):
        return answer
    try:
        # Measured 2026-10-13: asking "backed by the tool result OR what he
        # said" stopped false rewrites of chat-sourced facts but lost two real
        # catches (90% -> 80% on eval_decide supported). Kept tool-only; the
        # UI now SAYS when an answer was corrected instead of silently swapping.
        _, d = jd.check(jd.SUPPORT_Q, f"Tool result: {evidence}\nAnswer: {answer}",
                        yes=jd.SUPPORT_OPTS["yes"], no=jd.SUPPORT_OPTS["no"],
                        kind="supported", null_context=jd.NULLS["supported"])
    except Exception:
        return answer          # the checker failing must never cost the answer
    if not (d.choice == "no" and d.confident):
        return answer
    # Extends the same conversation (cheap), then the internal exchange is
    # folded away so history reads as one question, one answer.
    fix = ja.call_model(messages + [{"role": "user", "content": FIX_PROMPT}],
                        no_tools=True, should_stop=should_stop)
    fixed = (fix.get("content") or "").strip()
    if not fixed:
        return answer
    jd.log_rewrite(answer, fixed, d, f"Tool result: {evidence}")
    messages[-1] = {"role": "assistant", "content": fixed}
    ja.LAST_CORRECTED[:] = [True]      # the UIs say so instead of silently swapping text
    return fixed


def _is_multi_step(q):
    try:
        _, d = jd.check(jd.MULTI_Q, f"Message: {q}",
                        yes=jd.MULTI_OPTS["yes"], no=jd.MULTI_OPTS["no"],
                        kind="multi_step", null_context=jd.NULLS["multi_step"])
        return d.choice == "yes" and d.confident
    except Exception:
        return False


def run_turn_steps(messages, max_hops=9, on_tool=None, on_delta=None,
                   should_stop=None):
    q = ja._last_user_message(messages)
    start = len(messages)

    # Greetings: the loop already answers them with no tools at all.
    if ja._is_trivial(q, messages) or not _is_multi_step(q):
        answer = ja.run_turn_loop(messages, max_hops=max_hops, on_tool=on_tool,
                                  on_delta=on_delta, should_stop=should_stop)
        if answer and not answer.startswith("(interrupted"):
            answer = _verify_and_fix(messages, start, answer, should_stop)
        return answer

    # 1. PLAN — appended to the same conversation, no tools.
    messages.append({"role": "user", "content": PLAN_PROMPT.format(n=MAX_STEPS)})
    plan = ja.call_model(messages, no_tools=True, should_stop=should_stop)
    if plan.pop("interrupted", False):
        return "(interrupted)"
    messages.append({"role": "assistant", "content": plan.get("content", "")})
    steps = _parse_plan(plan.get("content", ""))

    if len(steps) < 2:
        # Not really a chain after all: answer normally, same conversation.
        messages.append({"role": "user",
                         "content": f"[Johan, internal] Go ahead and answer: {q}"})
        answer = ja.run_turn_loop(messages, max_hops=max_hops, on_tool=on_tool,
                                  on_delta=on_delta, should_stop=should_stop)
        if answer and not answer.startswith("(interrupted"):
            answer = _verify_and_fix(messages, start, answer, should_stop)
        return answer

    # 2. STEPS
    seen = {}
    for k, step in enumerate(steps, 1):
        if should_stop and should_stop():
            return "(interrupted)"
        messages.append({"role": "user", "content": STEP_PROMPT.format(
            k=k, total=len(steps), step=step)})
        for hop in range(STEP_HOPS):
            msg = ja.call_model(messages, should_stop=should_stop)
            if msg.pop("interrupted", False):
                return "(interrupted)"
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                break                      # it reported back in prose; move on
            got_it = False
            for call in calls:
                name, args, res, payload = ja.execute_tool(call, seen, hop, q, should_stop)
                messages.append({"role": "tool", "content": payload})
                ja.notify_tool(on_tool, name, args, res)
                try:
                    _, dd = jd.check(
                        jd.DONE_Q, f"Goal: {step}\nTool result: {_compact(res)[:1500]}",
                        yes=jd.DONE_OPTS["yes"], no=jd.DONE_OPTS["no"],
                        kind="step_done", null_context=jd.NULLS["step_done"])
                    got_it = got_it or dd.choice == "yes"
                except Exception:
                    got_it = got_it or not (isinstance(res, dict) and res.get("error"))
            if got_it:
                break

    # 3. ANSWER — streamed, no tools.
    messages.append({"role": "user", "content": ANSWER_PROMPT.format(q=q)})
    final = ja.call_model(messages, on_delta=on_delta, no_tools=True,
                          should_stop=should_stop)
    if final.pop("interrupted", False):
        if final.get("content"):
            messages.append({"role": "assistant", "content": final["content"]})
        return final.get("content", "") or "(interrupted)"
    answer = (final.get("content") or "").strip()
    messages.append({"role": "assistant", "content": answer})

    # 4. VERIFY
    return _verify_and_fix(messages, start, answer, should_stop)
