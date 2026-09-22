"""Resumable run control: plan persistence, resume decisions, status (step 15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from queryforge.orchestration.runtime.execution_journal import (
    ExecutionJournal,
    RunNotResumable,
)

PLAN_FILENAME = "plan.json"
STATE_FILENAME = "state.json"

#: Terminal ``TaskStatus`` values of the orchestration state file, mapped onto the
#: journal's terminal-outcome vocabulary. Two durable records describe the same
#: run (``state.json`` written by the orchestrator/streaming cancel path and
#: ``execution.json`` written by the step journal), so a run that ended in either
#: one has ended.
_STATE_TERMINAL_OUTCOMES: dict[str, str] = {
    "completed": "success",
    "blocked": "blocked",
    "failed": "failed",
    "cancelled": "cancelled",
}


class ResumeDecision(BaseModel):
    """What a resume will do with one step."""

    step_id: str
    action: str
    decision: Literal["reuse", "recompute", "skip"]
    reason: str


class RunStatus(BaseModel):
    """Operator-facing status of a persisted run."""

    run_id: str
    terminal: bool
    terminal_outcome: str | None = None
    plan_id: str = ""
    plan_version: int = 1
    steps: dict[str, str] = Field(default_factory=dict)
    reused_candidates: list[str] = Field(default_factory=list)
    lease_conflicts: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class RunResumer:
    """Owns the durable artifacts of one run directory.

    Layout::

        <state_root>/<run_id>/execution.json   # journal (owner of per-step truth)
        <state_root>/<run_id>/plan.json        # the plan a resume replays
        <state_root>/<run_id>/state.json       # run state (orchestrator/cancel path)

    The journal stays the owner of the *per-step* truth, but it is not the only
    durable record of a run: the streaming cancel path persists ``state.json``
    and, for a run that never opened a journal, writes nothing else. Terminality
    and resumability therefore consult both records, so a cancelled (or blocked,
    or completed) run can never be revived by a resume just because its journal
    was never created. Fixing this in the reader — instead of having the cancel
    path manufacture an empty journal — keeps non-durable runs journal-free and
    leaves every existing journal behaviour (and its tests) untouched.
    """

    def __init__(self, state_root: str | Path, run_id: str) -> None:
        self.state_root = Path(state_root).expanduser()
        self.run_id = run_id
        self.run_dir = self.state_root / run_id

    # ------------------------------------------------------------------ journal

    @property
    def journal(self) -> ExecutionJournal:
        return ExecutionJournal(self.run_dir, run_id=self.run_id)

    # -------------------------------------------------------------------- plan

    @property
    def plan_path(self) -> Path:
        return self.run_dir / PLAN_FILENAME

    def save_plan(self, plan: Any) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.plan_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.plan_path)
        return self.plan_path

    def load_plan(self) -> Any | None:
        """Load the persisted plan, or None when this run has no plan yet."""
        if not self.plan_path.is_file():
            return None
        from queryforge.orchestration.planner.plan import AnalysisPlan

        try:
            payload = json.loads(self.plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return AnalysisPlan.model_validate(payload)
        except Exception:
            return None

    # ------------------------------------------------------------------ status

    @property
    def state_path(self) -> Path:
        return self.run_dir / STATE_FILENAME

    def persisted_state_status(self) -> str | None:
        """The status recorded in the run's ``state.json``, when it is readable.

        A missing, unreadable or malformed state file reports ``None``: this
        reader decides whether a run may be resumed, so an unreadable document
        must never be mistaken for a terminal one (the journal still applies).
        """

        if not self.state_path.is_file():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        status = payload.get("status")
        return str(status) if isinstance(status, str) and status else None

    def persisted_terminal_outcome(self) -> str | None:
        """Terminal outcome recorded by the run state, in journal vocabulary.

        Returns ``None`` while the run state is absent or non-terminal, so this is
        a pure addition to the journal's answer, never a replacement.
        """

        return _STATE_TERMINAL_OUTCOMES.get(self.persisted_state_status() or "")

    def status(self) -> RunStatus:
        journal = self.journal
        steps = {
            step_id: record.status for step_id, record in journal.journal.steps.items()
        }
        terminal = journal.journal.terminal()
        terminal_outcome = journal.journal.terminal_outcome
        notes = list(journal.journal.notes)
        if not terminal:
            persisted = self.persisted_terminal_outcome()
            if persisted is not None:
                terminal = True
                terminal_outcome = persisted
                notes.append(
                    "run state recorded the terminal status "
                    f"{self.persisted_state_status()!r} before the journal did"
                )
        return RunStatus(
            run_id=self.run_id,
            terminal=terminal,
            terminal_outcome=terminal_outcome,
            plan_id=journal.journal.plan_id,
            plan_version=journal.journal.plan_version,
            steps=steps,
            reused_candidates=sorted(
                step_id
                for step_id, record in journal.journal.steps.items()
                if record.reusable()
            ),
            lease_conflicts=sorted(
                step_id
                for step_id, record in journal.journal.steps.items()
                if record.lease is not None
            ),
            budget=journal.budget(),
            notes=notes,
        )

    # ------------------------------------------------------------------ resume

    def resume_decisions(self, plan: Any) -> list[ResumeDecision]:
        """Compute, per step, whether a resume reuses or recomputes it."""
        journal = self.journal
        decisions: list[ResumeDecision] = []
        reusable: dict[str, bool] = {}
        for step in plan.steps:
            fingerprint = journal.fingerprint_step(
                step.action, dict(step.inputs or {}), plan_version=plan.version
            )
            record = journal.reusable_step(step.id, fingerprint)
            upstream_ok = all(reusable.get(item, False) for item in step.depends_on)
            if record is not None and upstream_ok:
                reusable[step.id] = True
                decisions.append(
                    ResumeDecision(
                        step_id=step.id,
                        action=step.action,
                        decision="reuse",
                        reason="recorded success with a matching input fingerprint",
                    )
                )
                continue
            reusable[step.id] = False
            if record is not None and not upstream_ok:
                reason = "an upstream step must be recomputed; downstream work is invalidated"
            elif journal.step(step.id) is None:
                reason = "no recorded attempt for this step"
            else:
                existing = journal.step(step.id)
                reason = (
                    f"previous status was {existing.status}"
                    if existing is not None
                    else "unknown"
                )
                if existing is not None and not existing.outcome_certain:
                    reason += " and its outcome is uncertain"
            decisions.append(
                ResumeDecision(
                    step_id=step.id,
                    action=step.action,
                    decision="recompute",
                    reason=reason,
                )
            )
        return decisions

    def assert_resumable(self) -> None:
        """Refuse to restart a run that already reached a terminal outcome.

        Both durable records are consulted: the execution journal *and* the run
        state file, because the streaming cancel path (and any run that never
        opted into the journal) records its outcome only in ``state.json``.
        """

        journal = self.journal
        if journal.journal.terminal():
            raise RunNotResumable(
                f"run {self.run_id!r} already ended as "
                f"{journal.journal.terminal_outcome!r}"
            )
        persisted = self.persisted_terminal_outcome()
        if persisted is not None:
            raise RunNotResumable(
                f"run {self.run_id!r} already ended as {persisted!r} "
                f"(recorded in {STATE_FILENAME} as "
                f"{self.persisted_state_status()!r})"
            )

    # -------------------------------------------------------------- cancellation

    def cancel(self, reason: str = "cancelled by the caller") -> bool:
        """Persist a cancellation so a later resume can never revive the run.

        A cancelled run is terminal: the outcome is written once and every
        subsequent attempt to resume it is rejected, which is what makes a
        client disconnect (an SSE stream closing) safe to act on. Returns
        ``False`` when the run had already ended — in the journal *or* in its
        persisted run state, so a late cancel cannot relabel a blocked, failed or
        completed run either.
        """

        if self.persisted_terminal_outcome() is not None:
            return False
        return self.journal.mark_cancelled(reason)


__all__ = ["PLAN_FILENAME", "STATE_FILENAME", "ResumeDecision", "RunResumer", "RunStatus"]
