"""A stand-in model for the offline tests: no server, no GPU.

`install(handler)` replaces johan_llm.chat. Each call hands `handler` the
request as one dict — {"model", "messages", "tools", "format", "logprobs",
"top_logprobs", "stream", "options"} — and the handler returns a reply shaped
{"message": {...}, "logprobs": [{"top_logprobs": [...]}]} (both optional).
Streaming callers get the content through on_delta, as from the real thing.

Importing this module also forbids starting a real model in this process:
the tests used to reach the real server through stubs Ollama's removal made
dead, and swapped his model out from under a running OpenCode session.
"""
import os

os.environ["JOHAN_NO_MODEL_START"] = "1"

import johan_llm  # noqa: E402

_REAL = johan_llm.chat


def install(handler):
    def chat(messages, *, model=None, tools=None, schema=None, stream=False, on_delta=None,
             max_tokens=None, temperature=0.3, logprobs=0, seed=None, timeout=180, **_old):
        import johan_assistant as ja
        payload = {"model": model or ja.MODEL, "messages": messages, "tools": tools,
                   "format": schema, "stream": bool(on_delta or stream),
                   "logprobs": bool(logprobs), "top_logprobs": logprobs,
                   "options": {"temperature": temperature, "num_predict": max_tokens,
                               "seed": seed}}
        reply = handler(payload) or {}
        msg = reply.get("message") or {"role": "assistant", "content": ""}
        if on_delta and msg.get("content"):
            on_delta(msg["content"])
        return johan_llm._result(reply)
    johan_llm.chat = chat


def uninstall():
    johan_llm.chat = _REAL
