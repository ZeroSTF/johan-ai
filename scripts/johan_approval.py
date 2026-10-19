"""Approval inside the conversation, the way Claude Code asks.

A change Johan wants to make (`propose_action`, `propose_edit`) used to be
staged in the F4 tab while the turn ended, so Johan never learned whether the
install worked and a two-step job ("install X, then make it the default")
had to be planned blind. Now, when someone can answer, the turn PAUSES: the
UI shows an approval card, his choice comes back here, the change runs and
is verified, and the model gets the real result in the same turn.

`approver` is a ContextVar so it reaches tools that run in their own thread
(johan_interrupt.run_stoppable copies the context). With no approver — the
eval harness, one-shot `johan "…"` (nobody can wait
mid-turn) — `ask()` returns None and the change is staged as before.

A decision is {"choice": "yes" | "always" | "no", "reason": str}.
"""

import contextlib
import contextvars

_APPROVER = contextvars.ContextVar("johan_approver", default=None)

YES, ALWAYS, NO = "yes", "always", "no"


def set_approver(fn):
    """fn(kind, item) -> decision, blocking until he answers."""
    _APPROVER.set(fn)


@contextlib.contextmanager
def approver(fn):
    token = _APPROVER.set(fn)
    try:
        yield
    finally:
        _APPROVER.reset(token)


def can_ask():
    """Is someone there to answer right now?"""
    return _APPROVER.get() is not None


def ask(kind, item):
    """Ask him about `item` ("action" or "edit"). None = nobody to ask."""
    fn = _APPROVER.get()
    if fn is None:
        return None
    try:
        d = fn(kind, item) or {}
    except Exception as e:          # noqa: BLE001 - a broken card must not run anything
        return {"choice": NO, "reason": f"approval failed: {type(e).__name__}: {e}"}
    choice = d.get("choice")
    if choice not in (YES, ALWAYS, NO):
        choice = NO
    return {"choice": choice, "reason": str(d.get("reason") or "")}
