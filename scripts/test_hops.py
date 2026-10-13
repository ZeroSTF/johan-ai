"""The final hop must always produce text, and never execute tool calls.

Ollama lifts tool calls out of this model's raw output even when `tools` is
left out of the request — tool_calls rows exist at hop 8, the tools-off hop —
so "the final hop disables tools" was a comment, not a guarantee. Model
stubbed; no GPU.

    python test_hops.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja   # noqa: E402

results = {}


def call(name="run_command"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": {"command": "uptime"}}}]}


def run(model, max_hops=3):
    """Run the loop with `model(payload_kwargs)` standing in for Ollama."""
    sent, executed = [], []
    real = (ja.call_model, ja.execute_tool, ja._reader.set_question)

    def fake_call_model(messages, on_delta=None, no_tools=False, should_stop=None, tools=None):
        sent.append({"no_tools": no_tools, "last_user": messages[-1].get("content", "")})
        return model(len(sent), no_tools)

    def fake_execute(c, seen, hop, q, should_stop=None):
        executed.append(hop)
        return c["function"]["name"], {}, {"output": f"up {hop} hours"}, '{"output": "up"}'
    ja.call_model, ja.execute_tool = fake_call_model, fake_execute
    ja._reader.set_question = lambda q: None
    try:
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "check my uptime and kernel"}]
        out = ja.run_turn_loop(msgs, max_hops=max_hops)
    finally:
        ja.call_model, ja.execute_tool, ja._reader.set_question = real
    return out, sent, executed, msgs


# 1. a model that ALWAYS returns tool calls, even when no tools were offered
out, sent, executed, msgs = run(lambda n, no_tools: call())
results["never returns the 'unreachable' string"] = "unreachable" not in out
results["answer is real text"] = bool(out.strip()) and "ran out of steps" in out
results["final-hop tool calls are never executed"] = executed == [0, 1]
results["final hop and its retry both sent without tools"] = [s["no_tools"] for s in sent] == [False, False, True, True]
results["the retry carries the answer-now note"] = "No more tools" in sent[-1]["last_user"]
results["the note is not kept in history"] = not any("No more tools" in (m.get("content") or "") for m in msgs)
results["the code summary names what ran"] = "run_command" in out

# 2. the retry answers properly -> that answer is used
out, sent, executed, msgs = run(lambda n, no_tools: call() if n <= 3 else
                                {"role": "assistant", "content": "Uptime is 2 hours."})
results["a good retry answer is returned"] = out == "Uptime is 2 hours."
results["…and stored as the last assistant message"] = msgs[-1]["content"] == "Uptime is 2 hours."

# 3. empty text on the final hop also gets the retry
out, sent, executed, msgs = run(lambda n, no_tools: call() if not no_tools else
                                ({"role": "assistant", "content": ""} if n == 3 else
                                 {"role": "assistant", "content": "Done."}))
results["empty final text is retried"] = out == "Done."

# 4. the normal path is untouched: a text answer on hop 0 costs one call
out, sent, executed, msgs = run(lambda n, no_tools: {"role": "assistant", "content": "hi"})
results["normal answer: one call, no retry"] = (out, len(sent)) == ("hi", 1)

for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
