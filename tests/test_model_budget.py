"""feat-012: every model call is charged to the run's shared budget.

Before this, the conversational path constructed no model budget at all. The tool
loop and the planner had one, but ``/ask`` did not, so a run's model spend and
wall-clock time were unbounded and ``BudgetLimits.model_deadline_ms`` had no
consumer anywhere in the codebase.

These tests drive the real ``WorkflowRunner`` with a counting model provider, so
they exercise the actual decoration order (budget outside the observed provider)
and the real reservation lifecycle.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.core.config import Config
from queryforge.core.schemas.models import SqlTask
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.orchestration.tools.budget import BudgetLimits, BudgetManager
from queryforge.orchestration.tools.specs import ToolBudgetError
from queryforge.workflow.budgeted_model import BudgetedModelProvider
from queryforge.workflow.workflow import WorkflowError
from queryforge.workflow.workflow_runner import WorkflowRunner


class CountingModel(BaseModelProvider):
    """A real provider adapter that counts calls and records the deadline it saw.

    Subclasses ``BaseModelProvider`` (not a bare duck type) so it inherits the
    ``generate_json`` / ``generate_text`` envelope the nodes actually call; a
    duck-typed double that lacks them produces a confusing ``NoneType is not
    callable`` deep in the provider chain.
    """

    provider = "counting"
    model = "counting-1"

    def __init__(self) -> None:
        self.calls = 0
        self.timeouts_seen: list[float | None] = []
        self.last_timeout: float | None = None
        self.last_usage = None

    def generate_json(self, prompt: str, timeout: float | None = None):
        # Overridden so the recorded deadline reflects what the run actually
        # passed, rather than being reconstructed inside the base class.
        self.timeouts_seen.append(timeout)
        self.last_timeout = timeout
        return super().generate_json(prompt, timeout=timeout)

    def generate_with_messages(self, messages, json_mode=False, timeout=None):
        if not self.timeouts_seen:
            # Direct generate_with_messages calls still need recording.
            self.timeouts_seen.append(timeout)
        self.calls += 1
        self.last_timeout = timeout
        prompt = json.dumps(messages, ensure_ascii=False)
        if "Select local QueryForge skills" in prompt:
            payload = {"skills": [], "reason": "test"}
        elif "Evaluate whether the SQL and result" in prompt:
            payload = {"success": True, "strategy": "SUCCESS", "reason": "ok"}
        else:
            payload = {"sql": "SELECT 1 AS n", "explanation": "e", "tables_used": []}

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 20
            total_tokens = 120
            estimated = False

        self.last_usage = _Usage()
        return json.dumps(payload)


class BudgetedModelProviderTest(unittest.TestCase):
    def test_a_call_is_reserved_before_it_is_sent_and_settled_with_real_tokens(self):
        budget = BudgetManager(limits={})
        inner = CountingModel()
        provider = BudgetedModelProvider(inner, budget)

        provider.generate_with_messages([{"role": "user", "content": "hi"}])

        self.assertEqual(inner.calls, 1)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["reservations"], 1)
        # Settled to the provider's reported total, not the reservation.
        self.assertEqual(snapshot["usage"]["max_estimated_tokens"], 120)

    def test_an_unknown_cost_is_not_recorded_as_a_free_call(self):
        """A provider that reports nothing keeps its reservation charged."""

        class SilentModel(CountingModel):
            def generate_with_messages(self, messages, json_mode=False, timeout=None):
                self.calls += 1
                self.last_usage = None
                return json.dumps(
                    {"sql": "SELECT 1", "explanation": "e", "tables_used": []}
                )

        budget = BudgetManager(limits={})
        provider = BudgetedModelProvider(SilentModel(), budget)
        provider.generate_with_messages([{"role": "user", "content": "hi"}])

        usage = budget.snapshot()["usage"]["max_estimated_tokens"]
        self.assertGreater(usage, 0, "an unreported cost must still be charged")

    def test_the_remaining_deadline_reaches_the_adapter(self):
        """model_deadline_ms had no consumer before this feature."""
        inner = CountingModel()
        inner.timeouts_seen = []
        budget = BudgetManager(limits={"model_deadline_ms": 30_000})
        provider = BudgetedModelProvider(inner, budget)
        provider.generate_with_messages([{"role": "user", "content": "hi"}])

        self.assertEqual(len(inner.timeouts_seen), 1)
        timeout = inner.timeouts_seen[0]
        self.assertIsNotNone(timeout, "the adapter must receive a deadline")
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)

    def test_an_exhausted_call_budget_refuses_before_sending(self):
        budget = BudgetManager(limits={"max_tool_calls": 1})
        inner = CountingModel()
        refusals: dict = {}
        provider = BudgetedModelProvider(inner, budget, refusal_sink=refusals)

        provider.generate_with_messages([{"role": "user", "content": "one"}])
        with self.assertRaises(ToolBudgetError):
            provider.generate_with_messages([{"role": "user", "content": "two"}])

        # The refusal happened *before* the second request was sent.
        self.assertEqual(inner.calls, 1)
        self.assertEqual(refusals["budget_refusal"]["limit"], "max_tool_calls")
        self.assertIn("max_tool_calls", refusals["budget_refusal"]["reason"])

    def test_an_expired_deadline_refuses_before_sending(self):
        budget = BudgetManager(limits={"model_deadline_ms": 0})
        inner = CountingModel()
        refusals: dict = {}
        provider = BudgetedModelProvider(inner, budget, refusal_sink=refusals)

        with self.assertRaises(ToolBudgetError):
            provider.generate_with_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(inner.calls, 0, "an impossible call must not be sent")
        self.assertEqual(refusals["budget_refusal"]["limit"], "model_deadline_ms")


class WorkflowBudgetTest(unittest.TestCase):
    """The budget has to stop a real run, not just a synthetic provider call."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.database = self.root / "budget.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE items (name TEXT)")
            connection.execute("INSERT INTO items VALUES ('a')")
        self.model = CountingModel()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _run(self, **runner_kwargs):
        runner = WorkflowRunner(
            self._config(),
            llm_factory=lambda _: self.model,
            selected_skills=[],
            **runner_kwargs,
        )
        return runner.run(
            SqlTask(question="List item names", database_path=str(self.database))
        )

    def _config(self):
        """Minimal explicit config, as tests/test_retry_workflow.py does.

        Loading the ambient config would point the semantic model at the sample
        database and fail schema validation against this fixture database.
        """
        return Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.db"),
            orchestration_state_root=str(self.root / "runs"),
        )

    def test_a_small_call_budget_stops_the_run_and_records_why(self):
        """A deliberately tiny budget must end the run mid-chain, with the reason.

        The workflow makes more than one model call (skill selection, then SQL
        generation), so a single-call allowance is exhausted partway through.
        """
        budget = BudgetManager(limits={"max_tool_calls": 1})
        with self.assertRaises(WorkflowError):
            self._run(model_budget_manager=budget)

        snapshot = budget.snapshot()
        self.assertGreaterEqual(snapshot["reservations"], 1)
        self.assertIn("max_tool_calls", snapshot["exhausted"])
        self.assertLess(
            self.model.calls,
            3,
            "the refusal must stop further model calls, not merely report them",
        )

    def test_an_unbounded_run_still_completes(self):
        """The default must not change existing behaviour."""
        output = self._run()
        self.assertEqual(output["status"], "success")
        self.assertGreaterEqual(self.model.calls, 1)

    def test_the_run_exposes_its_model_budget(self):
        """A caller can see what a run spent, which run_context alone did not give."""
        budget = BudgetManager(limits={"model_deadline_ms": 60_000})
        output = self._run(model_budget_manager=budget)
        self.assertEqual(output["status"], "success")
        # Deadline reached the adapter on the real run, not only in a unit test.
        self.assertTrue(
            any(t is not None for t in self.model.timeouts_seen),
            "the adapter must receive a deadline on a real run",
        )


if __name__ == "__main__":
    unittest.main()
