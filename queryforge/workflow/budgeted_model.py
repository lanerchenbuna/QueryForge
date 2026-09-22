"""Bound every model call by the run's shared budget and deadline.

The conversational path had no model budget at all. ``BudgetLimits`` declared
``model_deadline_ms`` and ``max_estimated_tokens``, and ``BudgetManager`` could
enforce both, but nothing on the ``/ask`` path ever constructed a manager or called
``reserve`` for a model request: only the tool loop and the planner did. The result
was that a run's model spend and wall-clock time were unbounded, and
``model_deadline_ms`` had no consumer anywhere.

This module closes that gap with a provider decorator rather than with changes at
each of the seven decision points, so every model call is covered by construction
instead of by remembering to add a call site.

Design notes:

* A call **reserves** before the request is sent and **settles** with the tokens the
  provider actually reported. Reserving first is what makes the cap real: a
  reservation that cannot be satisfied raises before any spend happens.
* The remaining deadline is published through
  :func:`queryforge.core.observability.model_deadline`, so adapters can bound the
  HTTP request instead of discovering the overrun afterwards.
* A refusal is recorded on the run context, because "the run stopped" and "the run
  stopped because the budget ran out" are different reports.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from queryforge.core.observability import (
    _call_with_optional_timeout,
    current_model_deadline,
    model_deadline,
)
from queryforge.orchestration.tools.budget import BudgetManager
from queryforge.orchestration.tools.specs import ToolBudgetError


class BudgetedModelProvider:
    """Provider decorator that charges every model call to a shared budget.

    Wraps the observed provider, so both the span and the budget see the call.
    """

    #: Tokens reserved before a call, reconciled to the reported total afterwards.
    #:
    #: A first version reserved the *whole* per-call cap (20k by default) on every
    #: call, so three model calls exhausted the 100k global allowance and every
    #: workflow run failed with a budget refusal — the enforcement was real but the
    #: accounting was wrong. An expectation is reserved and then settled to the
    #: truth; the per-call cap still applies as a hard ceiling on any single call.
    DEFAULT_EXPECTED_TOKENS = 4_000.0

    def __init__(
        self,
        provider: Any,
        budget: BudgetManager,
        *,
        refusal_sink: dict[str, Any] | None = None,
        expected_tokens: float | None = None,
    ) -> None:
        self._provider = provider
        self._budget = budget
        self._refusal_sink = refusal_sink
        self._expected_tokens = float(
            self.DEFAULT_EXPECTED_TOKENS
            if expected_tokens is None
            else expected_tokens
        )
        self.provider = getattr(provider, "provider", None)
        self.model = getattr(provider, "model", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    @contextmanager
    def _charged(self, *, entry_point: str):
        """Reserve budget, publish the deadline, settle with reported usage.

        One implementation for every entry point. An earlier version overrode only
        ``generate_with_messages`` and re-implemented ``generate_json`` on top of
        it, which silently bypassed each provider's own ``generate_json`` — every
        adapter and test double that customises it stopped being called.
        """

        try:
            reservation = self._budget.reserve(
                category="model",
                calls=1,
                estimated_tokens=self._expected_tokens,
                require_remaining=("model_deadline_ms",),
            )
        except ToolBudgetError as exc:
            self._record_refusal(exc)
            raise
        try:
            with model_deadline(self._budget.deadline_seconds()):
                yield
        except Exception:
            # A failed call still consumed its reservation (and may have been
            # billed), so settle at the reserved amount rather than releasing it.
            reservation.settle(max_estimated_tokens=self._expected_tokens)
            raise
        usage = self._last_usage()
        actual: dict[str, float] = {}
        if usage is not None:
            tokens = int(getattr(usage, "total_tokens", 0) or 0)
            if tokens > 0:
                actual["max_estimated_tokens"] = float(tokens)
        reservation.settle(**actual)

    # ------------------------------------------------------- entry points
    #
    # The nodes call ``generate_json`` (and occasionally ``generate_text``), not
    # ``generate_with_messages``. Each entry point therefore charges the budget and
    # then delegates to the *inner* provider's same method, so a provider that
    # customises one of them keeps its behaviour.

    def generate_with_messages(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        **_ignored: Any,
    ) -> str:
        with self._charged(entry_point="generate_with_messages"):
            return self._provider.generate_with_messages(
                messages,
                json_mode=json_mode,
                timeout=current_model_deadline(),
            )

    def generate_json(self, prompt: str) -> dict[str, Any]:
        with self._charged(entry_point="generate_json"):
            return _call_with_optional_timeout(
                self._provider.generate_json, prompt, current_model_deadline()
            )

    def generate_text(self, prompt: str) -> str:
        with self._charged(entry_point="generate_text"):
            return _call_with_optional_timeout(
                self._provider.generate_text, prompt, current_model_deadline()
            )

    # ------------------------------------------------------------------ helpers

    def _last_usage(self) -> Any:
        inner = getattr(self._provider, "_provider", None)
        return getattr(inner, "last_usage", None) or getattr(
            self._provider, "last_usage", None
        )

    def _record_refusal(self, exc: ToolBudgetError) -> None:
        if self._refusal_sink is None:
            return
        self._refusal_sink["budget_refusal"] = {
            "limit": getattr(exc, "limit", None),
            "reason": str(exc),
            "usage": self._budget.snapshot().get("usage"),
            "deadline_exhausted": current_model_deadline() is not None
            and float(current_model_deadline() or 0) <= 0,
        }
