"""Terminal run outcomes: one vocabulary, one derivation.

Why this module exists
----------------------
Before this, the terminal status of a run was decided in more than one place and
described with more than one vocabulary:

* ``OrchestratorAgent`` derived ``state.status`` from the workflow's result dict.
* ``AnalysisExecutor`` derived its own ``AnalysisExecutionResult.status``.
* ``TaskStatus``, ``PlanStatus`` and ``evolve``'s event outcome set each listed
  their own overlapping values.

That is how defect E-02 happened: the QA gate set ``state.status = "blocked"``,
the orchestrator then re-derived the status from the workflow's *earlier* result
dict and overwrote it to ``"completed"``, so ``state.json`` said one thing while
the caller was told another. Both were "correct" according to their own local
rule.

The fix is structural: a single derivation function, used by every path, plus a
single compatibility mapping onto the transport event vocabulary. Callers record
a terminal outcome once and do not re-derive it from a stale value.
"""

from __future__ import annotations

from typing import Any, Literal


#: The canonical terminal vocabulary. Anything persisted as a run's final status
#: must be one of these.
TerminalOutcome = Literal[
    "succeeded",
    "needs_clarification",
    "blocked",
    "partial",
    "failed",
    "cancelled",
]

TERMINAL_OUTCOMES: tuple[str, ...] = (
    "succeeded",
    "needs_clarification",
    "blocked",
    "partial",
    "failed",
    "cancelled",
)

#: Outcomes that mean the attempt is over, whichever way it ended.
TERMINAL_AND_FINAL: frozenset[str] = frozenset(TERMINAL_OUTCOMES)

#: Outcomes a late cancellation may not overwrite. A cancelled run may only be
#: replaced while it is still running.
NON_REPLACEABLE: frozenset[str] = frozenset(
    {"succeeded", "needs_clarification", "blocked", "partial", "failed", "cancelled"}
)


def normalize_outcome(value: Any) -> TerminalOutcome | None:
    """Map any historical status spelling onto the canonical vocabulary.

    Returns ``None`` when the value is not a terminal outcome (for example
    ``"running"``), so callers can distinguish "not finished" from "finished in
    some way".
    """

    text = str(value or "").strip().lower()
    if not text:
        return None
    return _ALIASES.get(text)


#: Spellings produced by the different layers before this module existed, plus the
#: action vocabulary of :class:`TaskStatus` and the evaluator's outcome set.
_ALIASES: dict[str, TerminalOutcome] = {
    # canonical
    "succeeded": "succeeded",
    "needs_clarification": "needs_clarification",
    "blocked": "blocked",
    "partial": "partial",
    "failed": "failed",
    "cancelled": "cancelled",
    # workflow / task-status spellings
    "success": "succeeded",
    "completed": "succeeded",
    "planned": "succeeded",
    "ok": "succeeded",
    "degraded": "partial",
    "error": "failed",
    "canceled": "cancelled",
}

#: Transport event protocol outcomes. Deliberately smaller than the canonical set:
#: the stream protocol distinguishes only these, and a clarification is reported to
#: a streaming client as a blocked run with a reason.
EVENT_OUTCOMES: dict[str, str] = {
    "succeeded": "success",
    "partial": "partial",
    "needs_clarification": "blocked",
    "blocked": "blocked",
    "failed": "failed",
    "cancelled": "cancelled",
}


def derive_outcome(
    *,
    result_status: Any = None,
    blocked_reason: str | None = None,
    cancelled: bool = False,
) -> TerminalOutcome:
    """Derive the terminal outcome from the signals a run actually produces.

    Precedence is deliberate and total, so no caller has to re-derive anything:

    1. an explicit cancellation wins (the run stopped because the client left);
    2. a recorded gate block wins (a deterministic gate refused the stage and
       recorded why) — this is the case E-02 got wrong;
    3. otherwise the workflow's own result status, normalised;
    4. otherwise the run is treated as succeeded.
    """

    if cancelled:
        return "cancelled"
    if blocked_reason:
        return "blocked"
    normalized = normalize_outcome(result_status)
    if normalized is None:
        # Not a terminal spelling: an unknown or non-terminal value is treated as
        # a success, which matches the historical behaviour of defaulting to
        # "success" while making the decision explicit and testable.
        return "succeeded"
    return normalized


def to_event_outcome(outcome: TerminalOutcome) -> str:
    """Map a canonical outcome onto the streaming event vocabulary."""

    return EVENT_OUTCOMES.get(outcome, "failed")


def is_terminal(value: Any) -> bool:
    return normalize_outcome(value) is not None
