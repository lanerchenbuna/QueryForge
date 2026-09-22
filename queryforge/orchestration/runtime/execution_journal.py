"""Durable per-step execution journal for resumable analysis runs (step 15).

The journal is the source of truth for *what already happened* in a run:
step input fingerprints, attempts, artifact/evidence references, lease
ownership, and one immutable terminal outcome. It deliberately records
uncertainty instead of pretending exactly-once semantics: an external call
that was interrupted is marked ``outcome_certain=False`` so a resume never
silently repeats a side effect whose result is unknown.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


LOGGER = logging.getLogger("queryforge.execution_journal")


JOURNAL_SCHEMA_VERSION = "1.0"

TERMINAL_OUTCOMES = ("success", "partial", "blocked", "failed", "cancelled")


class RunNotResumable(ValueError):
    """Raised when a run already reached a terminal state and must not restart."""


class IdempotencyClass(str, Enum):
    """How safely an action may be repeated after an interruption."""

    PURE_QUERY = "pure_query"
    MODEL_CALL = "model_call"
    ARTIFACT_WRITE = "artifact_write"
    ASSET_PUBLISH = "asset_publish"


#: Retry/重复执行策略：纯查询可自由重试；模型调用重试会产生费用且结果不确定；
#: 产物写入按幂等键安全；资产发布绝不盲目重试，必须先核对发布清单。
IDEMPOTENCY_POLICY: dict[IdempotencyClass, dict[str, Any]] = {
    IdempotencyClass.PURE_QUERY: {
        "safe_to_repeat": True,
        "max_attempts": 3,
        "requires_verification": False,
    },
    IdempotencyClass.MODEL_CALL: {
        "safe_to_repeat": True,
        "max_attempts": 2,
        "requires_verification": True,
    },
    IdempotencyClass.ARTIFACT_WRITE: {
        "safe_to_repeat": True,
        "max_attempts": 3,
        "requires_verification": False,
    },
    IdempotencyClass.ASSET_PUBLISH: {
        "safe_to_repeat": False,
        "max_attempts": 1,
        "requires_verification": True,
    },
}

#: Plan actions mapped to their idempotency class.
ACTION_IDEMPOTENCY: dict[str, IdempotencyClass] = {
    "resolve_metric": IdempotencyClass.PURE_QUERY,
    "check_data_quality": IdempotencyClass.PURE_QUERY,
    "query_metric": IdempotencyClass.PURE_QUERY,
    "compare_periods": IdempotencyClass.PURE_QUERY,
    "drill_down": IdempotencyClass.PURE_QUERY,
    "calculate_contribution": IdempotencyClass.PURE_QUERY,
    "detect_anomaly": IdempotencyClass.PURE_QUERY,
    "render_chart": IdempotencyClass.ARTIFACT_WRITE,
    "compose_answer": IdempotencyClass.ARTIFACT_WRITE,
}


def idempotency_class_for(action: str) -> IdempotencyClass:
    return ACTION_IDEMPOTENCY.get(action, IdempotencyClass.MODEL_CALL)


class StepLease(BaseModel):
    """One worker's exclusive claim on a step."""

    owner: str
    token: str
    acquired_at: float
    expires_at: float


class StepRecord(BaseModel):
    """Durable state of one plan step."""

    step_id: str
    action: str
    idempotency_class: IdempotencyClass
    input_fingerprint: str
    status: Literal[
        "pending", "running", "succeeded", "failed", "blocked", "cancelled", "uncertain"
    ] = "pending"
    attempt: int = 0
    lease: StepLease | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    plan_version: int = 1
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    error_category: str | None = None
    outcome_certain: bool = True

    def reusable(self) -> bool:
        return self.status == "succeeded" and self.outcome_certain


class RunJournal(BaseModel):
    """The persisted journal of one run."""

    schema_version: str = JOURNAL_SCHEMA_VERSION
    run_id: str
    plan_id: str = ""
    plan_version: int = 1
    status: Literal["running", "terminal"] = "running"
    terminal_outcome: str | None = None
    terminal_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    budget: dict[str, Any] = Field(default_factory=dict)
    #: Data / semantic / policy versions this run was recorded against. Persisted so
    #: a resume recomputes steps whose inputs now resolve differently.
    versions: dict[str, str] = Field(default_factory=dict)
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def terminal(self) -> bool:
        return self.status == "terminal"


class ExecutionJournal:
    """Atomic, thread-safe journal persisted beside the run's state file."""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        run_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir).expanduser()
        self.clock = clock
        self._utc_now = utc_now or _utc_now
        self._lock = threading.RLock()
        self._run_id = run_id or self.run_dir.name
        self._journal = self._load_or_create()
        #: The version set this journal binds to. Set by ``register_plan`` and read
        #: by every later fingerprint computation so a resume cannot recompute a
        #: step against a different version than it was originally recorded under.
        self.versions: dict[str, Any] = dict(self._journal.versions)

    # ------------------------------------------------------------------ paths

    @property
    def path(self) -> Path:
        return self.run_dir / "execution.json"

    @property
    def journal(self) -> RunJournal:
        return self._journal

    # ----------------------------------------------------------------- loading

    def _load_or_create(self) -> RunJournal:
        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                journal = RunJournal.model_validate(payload)
                if journal.schema_version != JOURNAL_SCHEMA_VERSION:
                    journal.notes.append(
                        f"journal schema {journal.schema_version} upgraded to "
                        f"{JOURNAL_SCHEMA_VERSION}"
                    )
                    journal.schema_version = JOURNAL_SCHEMA_VERSION
                return journal
            except Exception as exc:  # corrupt journal must not crash the run
                journal = RunJournal(run_id=self._run_id)
                journal.notes.append(f"journal was unreadable and was restarted: {exc}")
                journal.created_at = self._utc_now()
                return journal
        journal = RunJournal(run_id=self._run_id)
        journal.created_at = self._utc_now()
        return journal

    def save(self) -> Path:
        with self._lock:
            self._journal.updated_at = self._utc_now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(self._journal.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
            return self.path

    # ------------------------------------------------------------------ plan

    #: Version dimensions a step fingerprint must bind, beyond the action itself.
    #:
    #: A fingerprint that covers only ``action``/``inputs``/``plan_version`` says
    #: "the same request" while ignoring the data and definitions it ran against, so
    #: a resume after the database or the semantic model changed would reuse a
    #: result computed over different inputs. ``DomainContext`` and ``RunContext``
    #: already carry all three versions; this is where they start mattering.
    FINGERPRINT_VERSIONS: tuple[str, ...] = (
        "data_version",
        "semantic_version",
        "policy_version",
    )

    @classmethod
    def fingerprint_step(
        cls,
        action: str,
        inputs: dict[str, Any],
        *,
        plan_version: int,
        versions: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "action": action,
            "inputs": inputs,
            "plan_version": plan_version,
        }
        if versions:
            # Only the declared dimensions, and only when actually supplied: an
            # undeclared version is "unknown", which is different from "unchanged",
            # so it must not silently equal a known value.
            declared = {
                key: str(versions[key])
                for key in cls.FINGERPRINT_VERSIONS
                if versions.get(key) not in (None, "")
            }
            if declared:
                payload["versions"] = declared
        return hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8")
        ).hexdigest()

    def register_plan(
        self, plan: Any, *, versions: dict[str, Any] | None = None
    ) -> list[str]:
        """Register (or refresh) every step of ``plan``; returns changed step ids.

        A step whose fingerprint changed (new inputs or a new plan version) is
        reset to ``pending`` so a resume recomputes it instead of reusing a
        stale artifact.
        """
        changed: list[str] = []
        with self._lock:
            self._journal.plan_id = str(getattr(plan, "plan_id", "") or self._journal.plan_id)
            version = int(getattr(plan, "version", 1) or 1)
            self._journal.plan_version = max(self._journal.plan_version, version)
            for step in getattr(plan, "steps", []):
                fingerprint = self.fingerprint_step(
                    step.action,
                    dict(step.inputs or {}),
                    plan_version=version,
                    versions=versions,
                )
                existing = self._journal.steps.get(step.id)
                if existing is None:
                    self._journal.steps[step.id] = StepRecord(
                        step_id=step.id,
                        action=step.action,
                        idempotency_class=idempotency_class_for(step.action),
                        input_fingerprint=fingerprint,
                        plan_version=version,
                    )
                    changed.append(step.id)
                    continue
                if existing.input_fingerprint != fingerprint:
                    existing.input_fingerprint = fingerprint
                    existing.status = "pending"
                    existing.attempt = 0
                    existing.outputs = {}
                    existing.evidence_ids = []
                    existing.artifact_refs = []
                    existing.error = None
                    existing.error_category = None
                    existing.outcome_certain = True
                    existing.plan_version = version
                    changed.append(step.id)
            if versions:
                self._journal.versions = {
                    key: str(versions[key])
                    for key in self.FINGERPRINT_VERSIONS
                    if versions.get(key) not in (None, "")
                }
                self.versions = dict(self._journal.versions)
            self.save()
        return changed

    # ----------------------------------------------------------------- records

    def step(self, step_id: str) -> StepRecord | None:
        return self._journal.steps.get(step_id)

    def reusable_step(self, step_id: str, fingerprint: str) -> StepRecord | None:
        record = self._journal.steps.get(step_id)
        if record is None or not record.reusable():
            return None
        if record.input_fingerprint != fingerprint:
            return None
        return record

    def begin_attempt(
        self,
        step_id: str,
        *,
        action: str,
        fingerprint: str,
        budget: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            record = self._journal.steps.get(step_id)
            if record is None:
                record = StepRecord(
                    step_id=step_id,
                    action=action,
                    idempotency_class=idempotency_class_for(action),
                    input_fingerprint=fingerprint,
                )
                self._journal.steps[step_id] = record
            record.input_fingerprint = fingerprint
            record.status = "running"
            record.attempt += 1
            record.started_at = self._utc_now()
            record.finished_at = None
            record.budget = dict(budget or {})
            self.save()
            return record.attempt

    def record_success(
        self,
        step_id: str,
        *,
        evidence_ids: list[str] | None = None,
        artifact_refs: list[str] | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        """Mark a step succeeded and persist what a resume needs to reuse it.

        Artifact references are lifted from the step outputs when the caller did
        not pass them explicitly, so a resumed run knows which durable files the
        step already produced instead of writing them twice.
        """
        with self._lock:
            record = self._journal.steps.get(step_id)
            if record is None:
                return
            record.status = "succeeded"
            record.outcome_certain = True
            record.evidence_ids = list(evidence_ids or [])
            payload = dict(outputs or {})
            record.artifact_refs = list(
                artifact_refs if artifact_refs is not None else _artifact_refs(payload)
            )
            record.outputs = payload
            record.error = None
            record.error_category = None
            record.finished_at = self._utc_now()
            self.save()

    def record_failure(
        self,
        step_id: str,
        *,
        error: str,
        error_category: str | None = None,
        status: Literal["failed", "blocked", "cancelled", "uncertain"] = "failed",
    ) -> None:
        with self._lock:
            record = self._journal.steps.get(step_id)
            if record is None:
                return
            record.status = status
            record.error = error
            record.error_category = error_category
            record.finished_at = self._utc_now()
            # An interrupted external call may already have had an effect:
            # mark it uncertain rather than assuming it did not happen.
            if status == "uncertain" or record.idempotency_class in {
                IdempotencyClass.MODEL_CALL,
                IdempotencyClass.ASSET_PUBLISH,
            }:
                record.outcome_certain = False
            self.save()

    # ------------------------------------------------------- cross-instance lock
    #
    # The in-process ``threading.RLock`` only serialises access within one
    # ``ExecutionJournal`` object. Two instances built on the same run directory —
    # a second worker, or a resumed process — each keep their own in-memory
    # snapshot, so both could pass the "is this lease still live?" check against a
    # stale copy and both write their own lease: measured, worker A and worker B
    # both acquired the same step. ``os.replace`` makes each write atomic but
    # provides no mutual exclusion between the writers.
    #
    # A file lock does. Same approach as the data-asset builder lock: an advisory
    # ``fcntl.flock`` on a sidecar file, degrading to a logged no-op where
    # ``fcntl`` is unavailable.

    @property
    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _cross_instance_lock(self) -> Any:
        """Serialise a read-modify-write against other instances of this run."""

        try:
            import fcntl
        except ImportError:  # pragma: no cover - platform dependent
            LOGGER.warning(
                "fcntl unavailable; lease exclusion between journal instances is "
                "not enforced on this platform (%s)",
                self.path,
            )
            yield
            return
        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def reload(self) -> None:
        """Refresh the in-memory snapshot from disk.

        Called while holding the cross-instance lock, so a read-modify-write cycle
        decides on the state another instance actually committed rather than on
        the copy this instance loaded at construction time.
        """

        with self._lock:
            self._journal = self._load_or_create()

    # ------------------------------------------------------------------ leases

    def acquire_lease(
        self, step_id: str, *, owner: str, ttl_seconds: float = 60.0
    ) -> StepLease | None:
        """Claim a step for one worker; returns None when another lease is live.

        The claim is decided under a cross-instance file lock and on a freshly
        reloaded snapshot, so a second worker cannot win the same step by checking
        its own stale copy. Measured before this: two instances in the same
        process both acquired the same step.
        """

        now = self.clock()
        with self._cross_instance_lock():
            # Decide on what is actually on disk, not on what this instance loaded
            # when it was constructed.
            self.reload()
            with self._lock:
                record = self._journal.steps.get(step_id)
                if record is None:
                    return None
                lease = record.lease
                if (
                    lease is not None
                    and lease.expires_at > now
                    and lease.owner != owner
                ):
                    LOGGER.info(
                        "lease_denied run_id=%s step=%s held_by=%s expires_in=%.1fs",
                        self._journal.run_id,
                        step_id,
                        lease.owner,
                        lease.expires_at - now,
                    )
                    return None
                token = hashlib.sha256(
                    f"{self._journal.run_id}:{step_id}:{owner}:{now}".encode("utf-8")
                ).hexdigest()[:16]
                record.lease = StepLease(
                    owner=owner,
                    token=token,
                    acquired_at=now,
                    expires_at=now + ttl_seconds,
                )
                self.save()
                return record.lease

    def release_lease(self, step_id: str, *, owner: str) -> None:
        with self._lock:
            record = self._journal.steps.get(step_id)
            if record is None or record.lease is None:
                return
            if record.lease.owner == owner:
                record.lease = None
                self.save()

    def expire_leases(self) -> list[str]:
        """Drop expired leases so a crashed worker cannot block a resume."""
        now = self.clock()
        expired: list[str] = []
        with self._lock:
            for step_id, record in self._journal.steps.items():
                if record.lease is not None and record.lease.expires_at <= now:
                    record.lease = None
                    expired.append(step_id)
            if expired:
                self.save()
        return expired

    # ------------------------------------------------------------------ budget

    def record_budget(self, budget: dict[str, Any]) -> None:
        with self._lock:
            self._journal.budget = dict(budget)
            self.save()

    def budget(self) -> dict[str, Any]:
        return dict(self._journal.budget)

    # ---------------------------------------------------------------- terminal

    def mark_terminal(self, outcome: str) -> bool:
        """Record the single terminal outcome; later calls are recorded, not applied."""
        if outcome not in TERMINAL_OUTCOMES:
            raise ValueError(f"unknown terminal outcome {outcome!r}")
        with self._lock:
            if self._journal.terminal():
                if self._journal.terminal_outcome != outcome:
                    self._journal.notes.append(
                        f"ignored late terminal '{outcome}'; run already ended as "
                        f"{self._journal.terminal_outcome}"
                    )
                    self.save()
                return False
            self._journal.status = "terminal"
            self._journal.terminal_outcome = outcome
            self._journal.terminal_at = self._utc_now()
            self.save()
            return True

    def mark_cancelled(self, reason: str = "cancelled by client") -> bool:
        """Record a cancellation once; an already-terminal run is left untouched."""
        with self._lock:
            if self._journal.terminal():
                return False
            self._journal.notes.append(f"cancellation persisted: {reason}")
            self.save()
        return self.mark_terminal("cancelled")

    def resumable(self) -> bool:
        """A cancelled or completed run is never silently revived."""
        return not self._journal.terminal()

    # --------------------------------------------------------------- migration

    def migrate_legacy_state(self, state_path: str | Path | None = None) -> list[str]:
        """Best-effort upgrade of a pre-journal run directory.

        The migration is additive and one-directional: the legacy ``state.json``
        is read but never modified, so it stays in place as the rollback path
        (deleting ``execution.json`` restores the pre-journal behaviour).
        Migrated records carry the ``legacy`` fingerprint, which never matches a
        computed step fingerprint — a legacy artifact is therefore reported but
        never silently reused, because nothing proves its inputs are unchanged.
        Returns the list of notes added; repeated calls are idempotent.
        """
        state_file = Path(state_path) if state_path else self.run_dir / "state.json"
        notes: list[str] = []
        with self._lock:
            if self._journal.steps:
                return ["journal already has step records; migration skipped"]
            if state_file.is_file():
                try:
                    payload = json.loads(state_file.read_text(encoding="utf-8"))
                except Exception as exc:
                    payload = None
                    notes.append(f"legacy state unreadable: {exc}")
                if isinstance(payload, dict):
                    self._journal.plan_id = str(payload.get("task_id") or "")
                    for artifact in payload.get("artifacts") or []:
                        if not isinstance(artifact, dict):
                            continue
                        step_id = str(artifact.get("artifact_type") or "artifact")
                        record = self._journal.steps.get(step_id) or StepRecord(
                            step_id=step_id,
                            action=str(artifact.get("artifact_type") or "artifact"),
                            idempotency_class=IdempotencyClass.ARTIFACT_WRITE,
                            input_fingerprint="legacy",
                        )
                        record.status = "succeeded"
                        record.artifact_refs = [str(artifact.get("path") or "")]
                        record.started_at = record.started_at or self._utc_now()
                        record.finished_at = self._utc_now()
                        self._journal.steps[step_id] = record
                    notes.append(
                        f"migrated {len(self._journal.steps)} legacy artifact record(s) "
                        "as reusable steps"
                    )
                    notes.append(
                        f"legacy state retained unchanged as the rollback path: {state_file}"
                    )
            else:
                notes.append("no legacy state.json found; nothing to migrate")
            self._journal.notes.extend(notes)
            self.save()
        return notes


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


#: Output keys whose value names a durable file a step produced.
_ARTIFACT_KEYS = ("artifact_path", "artifact_paths", "artifacts", "report_path", "chart_path")


def _artifact_refs(outputs: dict[str, Any]) -> list[str]:
    """Collect the durable file references a step reported in its outputs.

    Deliberately narrow: only well-known artifact keys are read, and only string
    values (or dicts carrying a ``path``) count, so a random payload field is
    never mistaken for a produced artifact.
    """
    refs: list[str] = []
    for key in _ARTIFACT_KEYS:
        value = outputs.get(key)
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                refs.append(candidate)
            elif isinstance(candidate, dict):
                path = candidate.get("path") or candidate.get("artifact_path")
                if isinstance(path, str) and path:
                    refs.append(path)
    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            unique.append(ref)
    return unique
