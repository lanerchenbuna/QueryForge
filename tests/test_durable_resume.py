"""Offline tests for durable run resume, leases, and terminal invariants (step 15)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from queryforge.core.config import Config
from queryforge.orchestration.planner import plan as plan_module
from queryforge.orchestration.planner.executor import AnalysisExecutor
from queryforge.orchestration.planner.plan import AnalysisPlan, PlanStep, PlanViolation
from queryforge.orchestration.runtime.execution_journal import (
    ExecutionJournal,
    IdempotencyClass,
    IDEMPOTENCY_POLICY,
    RunNotResumable,
    idempotency_class_for,
)
from queryforge.orchestration.runtime.resume import RunResumer
from queryforge.orchestration.tools.budget import BudgetManager
from queryforge.orchestration.tools.registry import ToolRegistry, build_default_registry
from queryforge.orchestration.tools.specs import ToolSpec

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


def _registry() -> ToolRegistry:
    registry = build_default_registry(None, BudgetManager())
    # A deterministic tool that succeeds and returns the supplied value.
    registry.register(
        ToolSpec(
            name="echo_step",
            description="returns its input",
            parameter_schema={
                "type": "object",
                "properties": {"value": {}},
                "additionalProperties": False,
            },
            modes=["execute"],
            budget_category="compute",
        ),
        lambda params, context=None: {"echo": params.get("value")},
    )
    return registry


def _plan_only_registry() -> ToolRegistry:
    """The same tool, but no longer permitted in ``execute`` mode (15-S1)."""
    registry = build_default_registry(None, BudgetManager())
    registry.register(
        ToolSpec(
            name="echo_step",
            description="returns its input, planning mode only",
            parameter_schema={
                "type": "object",
                "properties": {"value": {}},
                "additionalProperties": False,
            },
            modes=["plan_only"],
            budget_category="compute",
        ),
        lambda params, context=None: {"echo": params.get("value")},
    )
    return registry


def _counting_registry(counter: list[int]) -> ToolRegistry:
    registry = build_default_registry(None, BudgetManager())

    def handler(params, context=None):
        counter.append(1)
        return {"echo": params.get("value")}

    registry.register(
        ToolSpec(
            name="echo_step",
            description="counts its calls",
            parameter_schema={
                "type": "object",
                "properties": {"value": {}},
                "additionalProperties": False,
            },
            modes=["execute"],
            budget_category="compute",
        ),
        handler,
    )
    return registry


def _plan(version: int = 1, value: int = 1) -> AnalysisPlan:
    """A two-step data plan plus the answer step a real plan always carries.

    ``echo_step`` records its evidence under the ``echo_step`` kind, so the
    steps promise exactly that kind; the trailing ``compose_answer`` step is what
    lets the executor reach ``succeeded`` rather than ``partial``.
    """

    return AnalysisPlan(
        plan_id="plan_resume",
        question="q",
        version=version,
        steps=[
            PlanStep(
                id="first",
                action="echo_step",
                inputs={"value": value},
                expected_evidence=["echo_step"],
            ),
            PlanStep(
                id="second",
                action="echo_step",
                inputs={"value": value * 1000},
                depends_on=["first"],
                expected_evidence=["echo_step"],
            ),
            PlanStep(
                id="answer",
                action="compose_answer",
                depends_on=["first", "second"],
            ),
        ],
    )


class ExecutionJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.directory.name) / "runs" / "qf_resume"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_terminal_outcome_is_immutable_and_cancellation_is_persisted(self):
        journal = ExecutionJournal(self.run_dir, run_id="qf_resume")
        self.assertTrue(journal.resumable())
        self.assertTrue(journal.mark_terminal("success"))
        # A late writer must not rewrite the outcome.
        self.assertFalse(journal.mark_terminal("failed"))
        self.assertEqual(journal.journal.terminal_outcome, "success")
        self.assertFalse(journal.resumable())
        reloaded = ExecutionJournal(self.run_dir, run_id="qf_resume")
        self.assertEqual(reloaded.journal.terminal_outcome, "success")

        cancelled_dir = Path(self.directory.name) / "runs" / "qf_cancel"
        cancelled = ExecutionJournal(cancelled_dir, run_id="qf_cancel")
        self.assertTrue(cancelled.mark_cancelled("client disconnected"))
        self.assertEqual(cancelled.journal.terminal_outcome, "cancelled")
        # A cancelled run is never revived by a resume.
        self.assertFalse(cancelled.resumable())

    def test_lease_ownership_and_expiry(self):
        clock = [100.0]
        journal = ExecutionJournal(
            self.run_dir, run_id="qf_resume", clock=lambda: clock[0]
        )
        plan = _plan()
        journal.register_plan(plan)
        lease = journal.acquire_lease("first", owner="worker-a", ttl_seconds=30)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.expires_at, 130.0)
        # A second worker cannot claim a live lease.
        self.assertIsNone(journal.acquire_lease("first", owner="worker-b"))
        # The owner may renew its own lease, which pushes the deadline out from
        # the moment of the renewal call.
        clock[0] += 10
        renewed = journal.acquire_lease("first", owner="worker-a", ttl_seconds=30)
        self.assertEqual(renewed.expires_at, 140.0)
        clock[0] += 29
        # The renewed lease is still live: a live worker must not be preempted.
        self.assertEqual(journal.expire_leases(), [])
        self.assertIsNone(journal.acquire_lease("first", owner="worker-b"))
        clock[0] += 11
        self.assertEqual(journal.expire_leases(), ["first"])
        takeover = journal.acquire_lease("first", owner="worker-b", ttl_seconds=30)
        self.assertIsNotNone(takeover)
        # A lease a crashed worker left behind expires; a released one disappears
        # immediately and is never reported as expired.
        journal.release_lease("first", owner="worker-b")
        self.assertIsNone(journal.journal.steps["first"].lease)
        self.assertEqual(journal.expire_leases(), [])
        # Only the current owner may release a lease.
        journal.acquire_lease("first", owner="worker-a")
        journal.release_lease("first", owner="worker-b")
        self.assertIsNotNone(journal.journal.steps["first"].lease)

    def test_fingerprint_change_resets_a_completed_step(self):
        journal = ExecutionJournal(self.run_dir, run_id="qf_resume")
        journal.register_plan(_plan(value=1))
        journal.begin_attempt("first", action="echo_step", fingerprint="fp1")
        journal.record_success("first", outputs={"echo": 1})
        self.assertIsNotNone(journal.reusable_step("first", "fp1"))

        journal.register_plan(_plan(value=2))  # inputs changed
        record = journal.step("first")
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.attempt, 0)
        self.assertIsNone(journal.reusable_step("first", "fp1"))

    def test_uncertain_outcomes_are_not_reused(self):
        journal = ExecutionJournal(self.run_dir, run_id="qf_resume")
        journal.register_plan(_plan())
        self.assertEqual(
            idempotency_class_for("query_metric"), IdempotencyClass.PURE_QUERY
        )
        self.assertFalse(
            IDEMPOTENCY_POLICY[IdempotencyClass.ASSET_PUBLISH]["safe_to_repeat"]
        )
        journal.begin_attempt("first", action="echo_step", fingerprint="fp")
        journal.record_failure("first", error="connection reset", status="uncertain")
        record = journal.step("first")
        self.assertFalse(record.outcome_certain)
        self.assertIsNone(journal.reusable_step("first", "fp"))

    def test_legacy_state_migration_is_idempotent(self):
        state = self.run_dir / "state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "task_id": "task_legacy",
                    "artifacts": [
                        {"artifact_type": "analysis_request", "path": "artifacts/001_analysis_request.json"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        journal = ExecutionJournal(self.run_dir, run_id="qf_resume")
        before = state.read_bytes()
        notes = journal.migrate_legacy_state()
        self.assertTrue(any("migrated" in note for note in notes))
        self.assertEqual(journal.step("analysis_request").status, "succeeded")
        # The rollback path is the untouched legacy file plus the new journal.
        self.assertTrue(any("rollback path" in note for note in notes))
        self.assertEqual(state.read_bytes(), before)
        # A migrated record is never reused: nothing proves its inputs are
        # unchanged, so it is reported and recomputed instead.
        self.assertIsNotNone(journal.step("analysis_request"))
        self.assertIsNone(
            journal.reusable_step(
                "analysis_request",
                journal.fingerprint_step(
                    "analysis_request", {"question": "q"}, plan_version=1
                ),
            )
        )
        again = journal.migrate_legacy_state()
        self.assertTrue(any("skipped" in note for note in again))
        self.assertEqual(state.read_bytes(), before)


    def test_artifact_references_are_persisted_with_the_step(self):
        journal = ExecutionJournal(self.run_dir, run_id="qf_resume")
        journal.register_plan(_plan())
        journal.begin_attempt("first", action="echo_step", fingerprint="fp")
        journal.record_success(
            "first",
            outputs={
                "chart": {"chart_type": "table"},
                "artifacts": [{"path": "artifacts/001_chart.json"}, "artifacts/002.csv"],
                "report_path": "reports/001.md",
                "unrelated": "artifacts/never.json",
            },
        )
        record = journal.step("first")
        self.assertEqual(
            record.artifact_refs,
            ["artifacts/001_chart.json", "artifacts/002.csv", "reports/001.md"],
        )
        # The reference list survives a reload, which is what a resume reads.
        reloaded = ExecutionJournal(self.run_dir, run_id="qf_resume")
        self.assertEqual(
            reloaded.step("first").artifact_refs,
            ["artifacts/001_chart.json", "artifacts/002.csv", "reports/001.md"],
        )

class ResumeExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "runs"
        self.run_id = "qf_resume_run"
        # The plan validator only accepts known actions; register a controlled test
        # action so the deterministic tool below can flow through the executor.
        self._saved_actions = plan_module.PLAN_ACTIONS
        plan_module.PLAN_ACTIONS = plan_module.PLAN_ACTIONS + ("echo_step",)
        plan_module.ACTION_TOOL_MAP["echo_step"] = "echo_step"

    def tearDown(self) -> None:
        plan_module.PLAN_ACTIONS = self._saved_actions
        plan_module.ACTION_TOOL_MAP.pop("echo_step", None)
        self.directory.cleanup()

    def _journal(self) -> ExecutionJournal:
        return ExecutionJournal(self.root / self.run_id, run_id=self.run_id)

    def test_resume_reuses_completed_steps_and_recomputes_the_rest(self):
        plan = _plan()
        journal = self._journal()
        journal.register_plan(plan)
        # Simulate a crash after the first step committed.
        journal.begin_attempt("first", action="echo_step", fingerprint=journal.fingerprint_step("echo_step", {"value": 1}, plan_version=1))
        journal.record_success("first", outputs={"echo": 1}, evidence_ids=["ev:first"])

        counter: list[int] = []
        executor = AnalysisExecutor(
            _counting_registry(counter), budget_manager=BudgetManager(), journal=journal
        )
        result = executor.execute(plan)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.reused_steps, ["first"])
        self.assertEqual(result.recomputed_steps, ["second", "answer"])
        self.assertEqual(len(counter), 1, "only the unfinished step was executed")
        self.assertEqual(result.terminal_outcome, "success")
        self.assertFalse(self._journal().resumable())

    def test_changed_inputs_invalidate_downstream_reuse(self):
        plan = _plan()
        journal = self._journal()
        journal.register_plan(plan)
        for step in plan.steps:
            fingerprint = journal.fingerprint_step(
                step.action, dict(step.inputs), plan_version=plan.version
            )
            journal.begin_attempt(step.id, action=step.action, fingerprint=fingerprint)
            if step.action == "compose_answer":
                journal.record_success(step.id, outputs={"answer": {"question": "q"}})
            else:
                journal.record_success(step.id, outputs={"echo": step.inputs["value"]})

        counter: list[int] = []
        changed = _plan(value=2)  # every step's inputs changed
        executor = AnalysisExecutor(
            _counting_registry(counter), budget_manager=BudgetManager(), journal=journal
        )
        result = executor.execute(changed)
        self.assertEqual(result.reused_steps, [])
        self.assertEqual(sorted(result.recomputed_steps), ["answer", "first", "second"])
        self.assertEqual(len(counter), 2)

    def test_terminal_runs_are_not_resumed(self):
        plan = _plan()
        journal = self._journal()
        executor = AnalysisExecutor(
            _registry(), budget_manager=BudgetManager(), journal=journal
        )
        executor.execute(plan)
        self.assertTrue(journal.journal.terminal())

        with self.assertRaises(RunNotResumable):
            AnalysisExecutor(
                _registry(), budget_manager=BudgetManager(), journal=journal
            ).execute(plan)

        resumer = RunResumer(self.root, self.run_id)
        resumer.assert_resumable() if False else None
        with self.assertRaises(RunNotResumable):
            resumer.assert_resumable()

    def test_resume_re_authorises_before_reusing_a_persisted_success(self):
        """A recorded success is not a standing permission (15-S1)."""
        plan = _plan()
        journal = self._journal()
        journal.register_plan(plan)
        for step in plan.steps:
            journal.begin_attempt(
                step.id,
                action=step.action,
                fingerprint=journal.fingerprint_step(
                    step.action, dict(step.inputs), plan_version=plan.version
                ),
            )
            journal.record_success(
                step.id,
                outputs=(
                    {"answer": {"question": "q"}}
                    if step.action == "compose_answer"
                    else {"echo": step.inputs["value"]}
                ),
            )
        self.assertEqual(
            [record.status for record in journal.journal.steps.values()],
            ["succeeded"] * 3,
        )

        # The registry the resumed run builds no longer permits the tool in
        # execution mode, so the plan is rejected before any step is reused.
        with self.assertRaises(PlanViolation) as raised:
            AnalysisExecutor(
                _plan_only_registry(), budget_manager=BudgetManager(), journal=journal
            ).execute(plan)
        self.assertTrue(
            any("mode_not_allowed" in item for item in raised.exception.violations),
            raised.exception.violations,
        )
        self.assertEqual(journal.journal.steps["first"].status, "succeeded")
        self.assertEqual(journal.journal.steps["first"].attempt, 1)

        # Defence in depth for callers that skip validation: the reuse decision
        # itself re-checks authorisation and reports why it refused.
        result = AnalysisExecutor(
            _plan_only_registry(),
            budget_manager=BudgetManager(),
            journal=journal,
        ).execute(plan, validate=False)
        self.assertEqual(result.reused_steps, [])
        self.assertEqual(sorted(result.reuse_denied), ["answer", "first", "second"])
        self.assertIn("not authorised", result.reuse_denied["first"])
        self.assertIn("not authorised", result.reuse_denied["second"])
        self.assertIn("upstream", result.reuse_denied["answer"])
        self.assertTrue(result.recomputed_steps)
        self.assertNotEqual(result.status, "succeeded")

    def test_an_uncertain_side_effect_is_never_silently_repeated(self):
        """15-E1: an unknown-outcome publish blocks instead of running twice."""
        plan = _plan()
        journal = self._journal()
        journal.register_plan(plan)
        # The planner has no side-effecting action today, so the durable record is
        # marked as an asset publication explicitly: this is what a crash during a
        # publish step looks like to the executor.
        journal.step("first").idempotency_class = IdempotencyClass.ASSET_PUBLISH
        journal.begin_attempt(
            "first",
            action="echo_step",
            fingerprint=journal.fingerprint_step(
                "echo_step", {"value": 1}, plan_version=plan.version
            ),
        )
        journal.record_failure("first", error="worker killed", status="uncertain")
        record = journal.step("first")
        self.assertEqual(record.idempotency_class, IdempotencyClass.ASSET_PUBLISH)
        self.assertFalse(record.outcome_certain)
        self.assertFalse(IDEMPOTENCY_POLICY[IdempotencyClass.ASSET_PUBLISH]["safe_to_repeat"])

        counter: list[int] = []
        result = AnalysisExecutor(
            _counting_registry(counter), budget_manager=BudgetManager(), journal=journal
        ).execute(plan)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.stop_reason, "blocked")
        self.assertIn("side_effect_not_repeatable", result.step("first").error)
        self.assertEqual(counter, [], "the side-effecting tool was not called twice")
        self.assertEqual(journal.step("first").attempt, 1)

        # An operator who verified the external state can force the resume.
        forced: list[int] = []
        retry = _plan()
        result = AnalysisExecutor(
            _counting_registry(forced),
            budget_manager=BudgetManager(),
            journal=ExecutionJournal(self.root / self.run_id, run_id=self.run_id),
            force_resume=True,
        ).execute(retry)
        self.assertNotEqual(result.status, "blocked")
        # With the operator's confirmation the plan runs again from the blocked step.
        self.assertEqual(len(forced), 2)

    def test_lease_conflict_blocks_a_second_worker(self):
        plan = _plan()
        journal = self._journal()
        journal.register_plan(plan)
        journal.acquire_lease("first", owner="worker-a", ttl_seconds=600)

        worker_b = ExecutionJournal(self.root / self.run_id, run_id=self.run_id)
        result = AnalysisExecutor(
            _registry(),
            budget_manager=BudgetManager(),
            journal=worker_b,
            worker_id="worker-b",
        ).execute(plan)
        self.assertEqual(result.lease_conflicts, ["first"])
        self.assertEqual(result.stop_reason, "lease_conflict")
        self.assertNotEqual(result.status, "succeeded")

    def test_cancellation_is_persisted_and_never_reported_as_success(self):
        plan = _plan()
        journal = self._journal()
        result = AnalysisExecutor(
            _registry(),
            budget_manager=BudgetManager(),
            journal=journal,
            cancel_check=lambda: True,
        ).execute(plan)
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(result.terminal_outcome, "cancelled")
        self.assertNotEqual(result.status, "succeeded")
        self.assertFalse(self._journal().resumable())

    def test_run_status_reports_reusable_and_terminal_state(self):
        plan = _plan()
        journal = self._journal()
        executor = AnalysisExecutor(
            _registry(), budget_manager=BudgetManager(), journal=journal
        )
        executor.execute(plan)
        status = RunResumer(self.root, self.run_id).status()
        self.assertTrue(status.terminal)
        self.assertEqual(status.terminal_outcome, "success")
        self.assertEqual(
            status.steps,
            {"first": "succeeded", "second": "succeeded", "answer": "succeeded"},
        )
        self.assertEqual(status.reused_candidates, ["answer", "first", "second"])
        resumer = RunResumer(self.root, self.run_id)
        decisions = resumer.resume_decisions(plan)
        self.assertTrue(all(item.decision == "reuse" for item in decisions))

    def test_resume_decisions_after_a_partial_run(self):
        plan = _plan()
        journal = self._journal()
        journal.register_plan(plan)
        fingerprint = journal.fingerprint_step("echo_step", {"value": 1}, plan_version=1)
        journal.begin_attempt("first", action="echo_step", fingerprint=fingerprint)
        journal.record_success("first", outputs={"echo": 1})
        decisions = RunResumer(self.root, self.run_id).resume_decisions(plan)
        by_step = {item.step_id: item for item in decisions}
        self.assertEqual(by_step["first"].decision, "reuse")
        self.assertEqual(by_step["second"].decision, "recompute")


class PersistedRunStateTest(unittest.TestCase):
    """H8: the run state file is a second source of truth for resumability.

    A run cancelled while it had no journal (the streaming disconnect path writes
    ``state.json`` only) used to look fully resumable, so ``--resume`` revived a
    run the user had stopped.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "runs"
        self.run_id = "qf_state_only"
        self.run_dir = self.root / self.run_id
        self.run_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write_state(self, status: str, **extra: object) -> None:
        (self.run_dir / "state.json").write_text(
            json.dumps({"run_id": self.run_id, "status": status, **extra}),
            encoding="utf-8",
        )

    def test_cancelled_run_without_a_journal_is_terminal(self):
        from queryforge.application.agent_service import persist_cancelled_outcome

        persisted = persist_cancelled_outcome(
            state_root=self.root,
            run_id=self.run_id,
            reason="Client disconnected.",
        )
        self.assertIsNotNone(persisted)
        # The cancel path deliberately does not manufacture a journal for a run
        # that never opted into durable execution.
        self.assertFalse((self.run_dir / "execution.json").is_file())

        resumer = RunResumer(self.root, self.run_id)
        status = resumer.status()
        self.assertTrue(status.terminal)
        self.assertEqual(status.terminal_outcome, "cancelled")
        self.assertTrue(any("state" in note for note in status.notes), status.notes)
        with self.assertRaises(RunNotResumable):
            resumer.assert_resumable()
        # Reading the status never creates the journal it did not find.
        self.assertFalse((self.run_dir / "execution.json").is_file())
        # A second cancel is a no-op and stays journal-free.
        self.assertFalse(resumer.cancel("late cancel"))
        self.assertFalse((self.run_dir / "execution.json").is_file())
        self.assertEqual(resumer.status().terminal_outcome, "cancelled")

    def test_every_terminal_run_state_blocks_a_resume(self):
        for status, outcome in (
            ("completed", "success"),
            ("blocked", "blocked"),
            ("failed", "failed"),
            ("cancelled", "cancelled"),
        ):
            with self.subTest(status=status):
                self.write_state(status)
                resumer = RunResumer(self.root, self.run_id)
                self.assertTrue(resumer.status().terminal)
                self.assertEqual(resumer.status().terminal_outcome, outcome)
                with self.assertRaises(RunNotResumable):
                    resumer.assert_resumable()

    def test_non_terminal_or_unreadable_state_leaves_the_run_resumable(self):
        self.write_state("running")
        resumer = RunResumer(self.root, self.run_id)
        self.assertFalse(resumer.status().terminal)
        resumer.assert_resumable()
        # An unreadable state file must never be mistaken for a terminal one: the
        # journal still decides, and nothing here says the run has ended.
        (self.run_dir / "state.json").write_text("{ not json", encoding="utf-8")
        self.assertIsNone(resumer.persisted_state_status())
        self.assertFalse(resumer.status().terminal)
        resumer.assert_resumable()
        (self.run_dir / "state.json").unlink()
        resumer.assert_resumable()

    def test_a_terminal_state_file_is_never_relabelled_by_a_cancel(self):
        self.write_state("completed")
        resumer = RunResumer(self.root, self.run_id)
        self.assertFalse(resumer.cancel("late cancel"))
        self.assertFalse((self.run_dir / "execution.json").is_file())
        self.assertEqual(resumer.persisted_state_status(), "completed")


class DurableRunNetworkControlTest(unittest.TestCase):
    """M5: durable runs are inspectable and cancellable over REST."""

    class Service:
        """Transport double: the routes only need a config loader."""

        def __init__(self, config: Config) -> None:
            self.config_loader = lambda **_: config

    class RecordingPlanner:
        """Planner double that records what the route hands to ``analyze``."""

        instances: list["DurableRunNetworkControlTest.RecordingPlanner"] = []

        def __init__(self, **kwargs) -> None:
            self.init_kwargs = kwargs
            self.calls: list[tuple[str, dict]] = []
            DurableRunNetworkControlTest.RecordingPlanner.instances.append(self)

        def analyze(self, question: str, **kwargs) -> dict:
            self.calls.append((question, kwargs))
            return {"status": "succeeded", "run_id": kwargs.get("run_id")}

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        database = self.root / "items.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.commit()
        connection.close()
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / "runs"),
        )
        self.RecordingPlanner.instances = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def client(self, config: Config | None = None):
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        return TestClient(create_app(self.Service(config or self.config)))

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_cancel_and_status_routes_control_a_durable_run(self):
        client = self.client()
        cancelled = client.post(
            "/analyze/runs/qf_net/cancel", params={"reason": "client disconnected"}
        )
        self.assertEqual(cancelled.status_code, 200)
        body = cancelled.json()
        self.assertEqual(body["run_id"], "qf_net")
        self.assertTrue(body["cancelled"])
        self.assertTrue(body["status"]["terminal"])
        self.assertEqual(body["status"]["terminal_outcome"], "cancelled")
        self.assertTrue(
            any("client disconnected" in note for note in body["status"]["notes"])
        )
        # The cancel really reached the durable record of the run.
        self.assertTrue(
            (self.root / "runs" / "qf_net" / "execution.json").is_file()
        )

        status = client.get("/analyze/runs/qf_net")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["terminal"])
        self.assertEqual(status.json()["terminal_outcome"], "cancelled")

        # A second cancel reports that it lost the race instead of rewriting it.
        again = client.post("/analyze/runs/qf_net/cancel")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["cancelled"])

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_run_control_routes_respect_the_transport_api_key(self):
        secured = replace(self.config, api_key="net-secret")
        client = self.client(secured)
        self.assertEqual(client.get("/analyze/runs/qf_net").status_code, 401)
        self.assertEqual(client.post("/analyze/runs/qf_net/cancel").status_code, 401)
        allowed = client.get(
            "/analyze/runs/qf_net", headers={"X-API-Key": "net-secret"}
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertFalse(allowed.json()["terminal"])

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_analyze_request_threads_the_durable_run_options(self):
        with patch(
            "queryforge.interfaces.api.app.AnalysisPlannerService",
            self.RecordingPlanner,
        ):
            client = self.client()
            response = client.post(
                "/analyze",
                json={
                    "question": "How many items are there?",
                    "run_id": "qf_net",
                    "resume": True,
                    "force_resume": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.RecordingPlanner.instances[-1].calls[0][0], "How many items are there?")
        kwargs = self.RecordingPlanner.instances[-1].calls[0][1]
        self.assertEqual(kwargs["run_id"], "qf_net")
        self.assertTrue(kwargs["resume"])
        self.assertTrue(kwargs["force_resume"])
        # The route names its transport, so the planner applies the /ask allowlist.
        self.assertEqual(kwargs["entrypoint"], "api")

    def test_run_id_cannot_escape_the_orchestration_state_root(self):
        """A network-supplied run id becomes a directory name, so it is confined."""
        from queryforge.application.analysis_planner import AnalysisPlannerService

        planner = AnalysisPlannerService(config_loader=lambda **_: self.config)
        for run_id in ("../escape", "..", "a/b", ""):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    planner.run_status(run_id)
                with self.assertRaises(ValueError):
                    planner.cancel_run(run_id)
        with self.assertRaises(ValueError):
            planner.analyze(
                "How many items are there?",
                run_id="../escape",
                database=str(self.config.database_path),
            )
        # Nothing was written outside the run root.
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.root.parent / "escape").exists())

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_analyze_route_rejects_an_unsafe_run_id(self):
        client = self.client()
        rejected = client.post(
            "/analyze",
            json={"question": "How many items are there?", "run_id": "../escape"},
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertFalse((self.root / "escape").exists())
        # A percent-encoded traversal reaches the route as a path parameter and is
        # refused by the same run-id rule (HTTP 400), never used as a directory.
        cancel = client.post("/analyze/runs/%2E%2E/cancel")
        self.assertEqual(cancel.status_code, 400)
        self.assertFalse((self.root / "execution.json").exists())


if __name__ == "__main__":
    unittest.main()
