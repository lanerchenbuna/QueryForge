"""Atomic shared budgets for tool calls, SQL time, and result size.

Step 09 requires that every expensive call *reserves* budget before it starts and
*settles* the real cost afterwards, that parallel callers sharing one
:class:`BudgetManager` can never exceed the cap, and that SQLite statements are
actually interrupted when their deadline passes instead of being relabelled
"timeout" after the fact.

The manager keeps one global limit set plus a stricter per-call limit set.
``reserve`` is the only mutating entry point and it is guarded by a
``threading.Lock``, so two threads competing for the last remaining call cannot
both win.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from queryforge.orchestration.tools.specs import ToolBudgetError

#: Keys of a budget limit set.
BUDGET_KEYS: tuple[str, ...] = (
    "max_tool_calls",
    "max_sql_duration_ms",
    "model_deadline_ms",
    "max_output_rows",
    "max_output_bytes",
    "max_estimated_tokens",
)


@dataclass(frozen=True)
class BudgetLimits:
    """A validated limit set (global or per call)."""

    max_tool_calls: int = 64
    max_sql_duration_ms: float = 120_000.0
    model_deadline_ms: float = 120_000.0
    max_output_rows: int = 1_000
    max_output_bytes: int = 2_000_000
    max_estimated_tokens: int = 100_000

    def __post_init__(self) -> None:
        for key in BUDGET_KEYS:
            value = getattr(self, key)
            if value is None or float(value) < 0:
                raise ValueError(f"budget limit {key} must be zero or greater")

    def merged(self, overrides: Mapping[str, Any] | None) -> "BudgetLimits":
        """Return a copy with the recognised overrides applied."""

        if not overrides:
            return self
        unknown = sorted(set(overrides) - set(BUDGET_KEYS))
        if unknown:
            raise ValueError(f"unknown budget limit(s): {', '.join(unknown)}")
        return replace(
            self,
            **{
                key: int(value)
                if key in {"max_tool_calls", "max_output_rows"}
                else float(value)
                for key, value in overrides.items()
            },
        )

    def as_dict(self) -> dict[str, float]:
        return {key: getattr(self, key) for key in BUDGET_KEYS}


#: Permissive default used when a caller does not supply limits, so existing
#: behaviour (for example the bounded tool loop) is preserved by construction.
DEFAULT_LIMITS = BudgetLimits()

#: Per-call caps applied on top of the global limits.
DEFAULT_PER_CALL = BudgetLimits(
    max_tool_calls=1,
    max_sql_duration_ms=30_000.0,
    model_deadline_ms=120_000.0,
    max_output_rows=1_000,
    max_output_bytes=2_000_000,
    max_estimated_tokens=20_000,
)


@dataclass
class BudgetUsage:
    """Consumed budget so far."""

    max_tool_calls: float = 0.0
    max_sql_duration_ms: float = 0.0
    max_output_rows: float = 0.0
    max_output_bytes: float = 0.0
    max_estimated_tokens: float = 0.0
    model_deadline_ms: float = 0.0

    def copy(self) -> "BudgetUsage":
        return BudgetUsage(**self.__dict__)

    def as_dict(self) -> dict[str, float]:
        return {key: getattr(self, key) for key in BUDGET_KEYS}

    def add(self, key: str, amount: float) -> None:
        setattr(self, key, getattr(self, key) + float(amount))


@dataclass
class BudgetReservation:
    """A held reservation; settle it with the real cost when the call returns."""

    manager: "BudgetManager"
    reserved: dict[str, float]
    call_id: str | None = None
    tool: str | None = None
    category: str = "tool"
    _settled: bool = field(default=False, repr=False)

    @property
    def settled(self) -> bool:
        return self._settled

    def settle(self, **actual: float) -> BudgetUsage:
        """Release the unused part of the reservation and charge the real cost."""

        if self._settled:
            return self.manager.usage
        return self.manager._settle(self, actual)

    def __enter__(self) -> "BudgetReservation":
        return self

    def __exit__(self, *_: object) -> None:
        if not self._settled:
            self.settle()


class BudgetManager:
    """Thread-safe global + per-call budget with atomic reservation."""

    def __init__(
        self,
        limits: BudgetLimits | Mapping[str, Any] | None = None,
        *,
        per_call: BudgetLimits | Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        started_at: float | None = None,
    ) -> None:
        self.limits = _coerce_limits(limits, DEFAULT_LIMITS)
        self.per_call = _coerce_limits(per_call, DEFAULT_PER_CALL)
        self._clock = clock
        self._started_at = self._clock() if started_at is None else float(started_at)
        self._lock = threading.Lock()
        self._usage = BudgetUsage()
        self._reservations = 0
        self._exhausted: list[str] = []

    # -------------------------------------------------------------- inspection

    @property
    def usage(self) -> BudgetUsage:
        with self._lock:
            return self._usage.copy()

    @property
    def reservations(self) -> int:
        with self._lock:
            return self._reservations

    @property
    def exhausted_limits(self) -> list[str]:
        with self._lock:
            return list(self._exhausted)

    def remaining(self, key: str) -> float:
        """Remaining allowance for one budget key (never negative)."""

        if key not in BUDGET_KEYS:
            raise ValueError(f"unknown budget limit: {key}")
        with self._lock:
            return max(
                0.0, float(getattr(self.limits, key)) - float(getattr(self._usage, key))
            )

    def restore(self, usage: Mapping[str, Any] | None) -> list[str]:
        """Re-apply a previously persisted usage snapshot.

        A resumed run must inherit what the earlier attempt already spent,
        otherwise the budget boundary resets on every resume and a run can spend
        its whole allowance again after a crash. Returns the keys that were
        restored, so a caller can report what carried over rather than assuming.

        Values are clamped to the configured limits: a snapshot that exceeds the
        current limits (because they were tightened) must not leave the manager in
        a state where ``remaining`` is negative and nothing can run.
        """

        if not usage:
            return []
        applied: list[str] = []
        with self._lock:
            for key in BUDGET_KEYS:
                if key not in usage:
                    continue
                try:
                    amount = float(usage[key])
                except (TypeError, ValueError):
                    continue
                if amount <= 0:
                    continue
                limit = float(getattr(self.limits, key))
                # Preserve the spent amount, but never above the limit: the point
                # is that the allowance is consumed, not that it is invalid.
                setattr(self._usage, key, min(amount, limit) if limit > 0 else amount)
                applied.append(key)
            if applied:
                self._reservations += 1
        return applied

    def snapshot(self) -> dict[str, Any]:
        """Serialize limits, usage, and remaining budget for a result payload."""

        with self._lock:
            usage = self._usage.copy()
            limits = self.limits.as_dict()
        used = usage.as_dict()
        return {
            "limits": limits,
            "per_call": self.per_call.as_dict(),
            "usage": used,
            "remaining": {key: max(0.0, limits[key] - used[key]) for key in BUDGET_KEYS},
            "reservations": self.reservations,
            "exhausted": self.exhausted_limits,
            "model_deadline_seconds": round(self.deadline_seconds(), 3),
        }

    # ---------------------------------------------------------------- deadline

    def deadline_seconds(self, now: float | None = None) -> float:
        """Seconds left before the model deadline (never negative)."""

        current = self._clock() if now is None else float(now)
        elapsed_ms = (current - self._started_at) * 1000.0
        return max(0.0, (float(self.limits.model_deadline_ms) - elapsed_ms) / 1000.0)

    def sql_deadline_at(self, *, now: float | None = None) -> float:
        """Absolute clock value bounding one SQL statement."""

        current = self._clock() if now is None else float(now)
        sql_seconds = float(self.per_call.max_sql_duration_ms) / 1000.0
        return current + min(sql_seconds, self.deadline_seconds(now=current))

    def install_sql_deadline_handler(
        self,
        connection: Any,
        deadline: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> "SqlDeadlineGuard":
        """Install a SQLite progress handler that interrupts at ``deadline``."""

        return install_sql_deadline_handler(
            connection,
            self.sql_deadline_at() if deadline is None else deadline,
            clock or self._clock,
        )

    # ----------------------------------------------------------------- reserve

    def reserve(
        self,
        *,
        category: str = "tool",
        calls: int = 1,
        estimated_tokens: float = 0,
        output_rows: float = 0,
        output_bytes: float = 0,
        sql_duration_ms: float = 0,
        per_call: Mapping[str, Any] | None = None,
        require_remaining: Iterable[str] | None = None,
        call_id: str | None = None,
        tool: str | None = None,
    ) -> BudgetReservation:
        """Atomically reserve budget, or raise :class:`ToolBudgetError`.

        ``require_remaining`` names cumulative caps (for example the SQL duration
        budget) that must still have headroom even though this call only
        *charges* them at settle time.
        """

        requested = {
            "max_tool_calls": float(calls),
            "max_sql_duration_ms": float(sql_duration_ms),
            "max_output_rows": float(output_rows),
            "max_output_bytes": float(output_bytes),
            "max_estimated_tokens": float(estimated_tokens),
        }
        call_limits = self.per_call.merged(per_call) if per_call else self.per_call
        with self._lock:
            if self.deadline_seconds(now=self._clock()) <= 0:
                self._note_exhausted("model_deadline_ms")
                raise ToolBudgetError(
                    "budget exhausted: model deadline exceeded "
                    f"(model_deadline_ms={self.limits.model_deadline_ms})",
                    limit="model_deadline_ms",
                )
            for key in require_remaining or ():
                if key not in BUDGET_KEYS:
                    raise ValueError(f"unknown budget limit: {key}")
                if float(getattr(self.limits, key)) - float(getattr(self._usage, key)) <= 1e-9:
                    self._note_exhausted(key)
                    raise ToolBudgetError(
                        f"budget exhausted: {key} has no remaining allowance "
                        f"(category={category})",
                        limit=key,
                    )
            for key, amount in requested.items():
                if amount <= 0:
                    continue
                remaining = float(getattr(self.limits, key)) - float(
                    getattr(self._usage, key)
                )
                if amount > remaining + 1e-9:
                    self._note_exhausted(key)
                    raise ToolBudgetError(
                        f"budget exhausted: {key} requires {amount} but only "
                        f"{max(0.0, remaining)} remains (category={category})",
                        limit=key,
                    )
                if key != "max_sql_duration_ms":
                    per_call_limit = float(getattr(call_limits, key))
                    if amount > per_call_limit + 1e-9:
                        self._note_exhausted(key)
                        raise ToolBudgetError(
                            f"budget exhausted: single call exceeds per-call {key} "
                            f"({amount} > {per_call_limit}, category={category})",
                            limit=key,
                        )
            for key, amount in requested.items():
                if amount:
                    self._usage.add(key, amount)
            self._reservations += 1
            return BudgetReservation(
                manager=self,
                reserved=requested,
                call_id=call_id,
                tool=tool,
                category=category,
            )

    def charge(self, **kwargs: Any) -> BudgetUsage:
        """Reserve and immediately settle (accounting for a finished call)."""

        reserved_keys = {
            "max_tool_calls",
            "max_sql_duration_ms",
            "max_output_rows",
            "max_output_bytes",
            "max_estimated_tokens",
        }
        actual = {key: kwargs.pop(key) for key in list(kwargs) if key in reserved_keys}
        reservation = self.reserve(**kwargs)
        return reservation.settle(**actual)

    # ---------------------------------------------------------------- internal

    def _settle(
        self, reservation: BudgetReservation, actual: Mapping[str, float]
    ) -> BudgetUsage:
        unknown = sorted(set(actual) - set(BUDGET_KEYS))
        if unknown:
            raise ValueError(f"unknown budget limit(s): {', '.join(unknown)}")
        with self._lock:
            for key, amount in actual.items():
                cost = max(0.0, float(amount))
                reserved = float(reservation.reserved.get(key, 0.0))
                if cost > reserved:
                    # Charge the difference without ever going negative; an
                    # overrunning call is recorded so the loop can stop honestly.
                    self._usage.add(key, cost - reserved)
                    reservation.reserved[key] = cost
                else:
                    self._usage.add(key, -(reserved - cost))
                    reservation.reserved[key] = cost
            for key, reserved in reservation.reserved.items():
                if reserved <= 0:
                    continue
                if float(getattr(self._usage, key)) >= float(
                    getattr(self.limits, key)
                ) - 1e-9:
                    self._note_exhausted(key)
            reservation._settled = True
            return self._usage.copy()

    def _note_exhausted(self, key: str) -> None:
        if key not in self._exhausted:
            self._exhausted.append(key)


def _coerce_limits(
    value: BudgetLimits | Mapping[str, Any] | None, default: BudgetLimits
) -> BudgetLimits:
    if value is None:
        return default
    if isinstance(value, BudgetLimits):
        return value
    if isinstance(value, Mapping):
        return default.merged(value)
    raise ValueError("budget limits must be a BudgetLimits or a mapping")


@dataclass
class SqlDeadlineGuard:
    """Removable SQLite progress-handler deadline (a no-op when unsupported)."""

    restore: Callable[[], None]
    installed: bool = False

    def __call__(self) -> None:
        self.restore()


def install_sql_deadline_handler(
    connection: Any,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> SqlDeadlineGuard:
    """Interrupt a long-running SQLite statement once ``deadline`` passes.

    Returns a guard whose ``restore()`` removes the handler again.  When the
    connection cannot install a progress handler the guard is a no-op, which is
    the documented limit for non-SQLite/external providers (see step 09).
    """

    setter = getattr(connection, "set_progress_handler", None)
    if connection is not None and setter is None and hasattr(connection, "interrupt"):
        import threading
        stopped = threading.Event()
        def watch():
            while not stopped.wait(0.01):
                if clock() >= deadline:
                    connection.interrupt()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        def restore():
            stopped.set()
            watcher.join()
        return SqlDeadlineGuard(restore=restore, installed=True)
    if connection is None or setter is None:
        return SqlDeadlineGuard(restore=_noop, installed=False)

    def handler() -> int:
        return 1 if clock() >= deadline else 0

    try:  # pragma: no cover - depends on the sqlite3 build
        setter(handler, 10_000)
    except Exception:  # pragma: no cover - defensive
        return SqlDeadlineGuard(restore=_noop, installed=False)

    def restore() -> None:
        try:
            setter(None, 0)
        except Exception:  # pragma: no cover - defensive
            pass

    return SqlDeadlineGuard(restore=restore, installed=True)


def _noop() -> None:
    return None


def connection_for(database_tool: Any) -> Any:
    """Return the underlying SQLite connection of a governed DatabaseTool."""

    return getattr(getattr(database_tool, "connector", None), "_connection", None)


__all__ = [
    "BUDGET_KEYS",
    "BudgetLimits",
    "BudgetManager",
    "BudgetReservation",
    "BudgetUsage",
    "DEFAULT_LIMITS",
    "DEFAULT_PER_CALL",
    "SqlDeadlineGuard",
    "connection_for",
    "install_sql_deadline_handler",
]
