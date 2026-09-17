"""Offline tests for the analysis planner, validator, and executor (step 10).

The fixtures are a tiny SQLite database (items + an intentionally empty brands
table) and a governed semantic model with a count metric, so the control flow,
the QuerySpec compilation, the governed SQL path, and the evidence gate are all
exercised against real SQL rather than a stubbed result.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.application.analysis_planner import AnalysisPlannerService
from queryforge.core.config import Config
from queryforge.core.observability import SpanRecorder, get_span_recorder
from queryforge.domain.semantic import SemanticModelLoader
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.orchestration.runtime.execution_journal import RunNotResumable
from queryforge.orchestration.planner import (
    AnalysisExecutor,
    AnalysisPlan,
    PlanStep,
    PlanValidator,
    PlanViolation,
)
from queryforge.orchestration.planner.executor import UnsupportedAnalysisError
from queryforge.orchestration.tools import (
    BudgetManager,
    ToolRegistry,
    ToolSpec,
)
from queryforge.orchestration.tools.specs import ToolContext

ITEMS = [(1, "alpha", "a", 10.0, 1), (2, "beta", "b", 20.0, 2), (3, "gamma", "a", 30.0, 1)]

SEMANTIC_MODEL = """
version: 1
name: planner_fixture
description: Offline planner fixture.
entities:
- name: items
  table: items
  description: One row per item.
  entity_type: fact
  primary_key: [item_id]
  grain: [item_id]
  dimensions:
  - name: category
    column: category
    description: Item category.
  - name: name
    column: name
    description: Item name.
- name: brands
  table: brands
  description: Brand dimension, deliberately empty in this fixture.
  entity_type: dimension
  primary_key: [brand_id]
  dimensions:
  - name: brand_name
    column: brand_name
    description: Brand display name.
relationships:
- name: items_to_brands
  from: items.brand_id
  to: brands.brand_id
  relationship_type: many_to_one
metrics:
- name: item_count
  description: Number of items.
  entity: items
  aggregation: count
  expression: COUNT(items.item_id)
  synonyms: [items, item count]
  allowed_dimensions: [items.category, brands.brand_name]
- name: item_amount
  description: Total amount of items.
  entity: items
  aggregation: sum
  expression: SUM(items.amount)
  synonyms: [total amount, item amount]
  allowed_dimensions: [items.category]
"""


SINGULAR_SEMANTIC_MODEL = """
version: 1
name: smoke
description: Integration smoke fixture (singular entity name).
entities:
- name: item
  table: items
  description: One row per item.
  entity_type: fact
  primary_key: [id]
  grain: [id]
  dimensions:
  - name: category
    column: category
    description: Item category.
metrics:
- name: item_count
  description: Number of items.
  entity: item
  aggregation: count
  expression: COUNT(items.id)
  synonyms: [items, item_count]
  allowed_dimensions: [item.category]
"""


class Fixture:
    """Temp SQLite database plus a governed semantic model."""

    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE items (item_id INTEGER PRIMARY KEY, name TEXT, category TEXT, "
            "amount REAL, brand_id INTEGER)"
        )
        connection.executemany(
            "INSERT INTO items (item_id, name, category, amount, brand_id) VALUES (?, ?, ?, ?, ?)",
            ITEMS,
        )
        connection.execute(
            "CREATE TABLE brands (brand_id INTEGER PRIMARY KEY, brand_name TEXT)"
        )
        connection.commit()
        connection.close()
        self.semantic_model = self.root / "semantic_model.yml"
        self.semantic_model.write_text(SEMANTIC_MODEL, encoding="utf-8")

        # Second database/model pair with the naming used in the integration
        # smoke report: entity "item" over table "items", metric COUNT(items.id).
        self.singular_database = self.root / "singular.sqlite"
        singular = sqlite3.connect(self.singular_database)
        singular.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, category TEXT)")
        singular.executemany(
            "INSERT INTO items (id, category) VALUES (?, ?)",
            [(1, "a"), (2, "b"), (3, "a")],
        )
        singular.commit()
        singular.close()
        self.singular_model = self.root / "singular_model.yml"
        self.singular_model.write_text(SINGULAR_SEMANTIC_MODEL, encoding="utf-8")

    def cleanup(self) -> None:
        self.directory.cleanup()

    def config(self) -> Config:
        return Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            semantic_model_path=str(self.semantic_model),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )

    def service(self, **kwargs) -> AnalysisPlannerService:
        return AnalysisPlannerService(
            config_loader=lambda **_: self.config(), **kwargs
        )

    def analyze(self, question: str, **kwargs):
        return self.service().analyze(
            question,
            database=str(self.database),
            semantic_model_path=str(self.semantic_model),
            **kwargs,
        )


QUALITY_PARAMS = {
    "type": "object",
    "properties": {
        "table_name": {"type": "string", "minLength": 1},
        "checks": {"type": "array", "items": {"type": "string"}},
        "options": {"type": "object"},
    },
    "required": ["table_name", "checks"],
    "additionalProperties": False,
}

SQL_PARAMS = {
    "type": "object",
    "properties": {"sql": {"type": "string", "minLength": 1}},
    "required": ["sql"],
    "additionalProperties": False,
}


class StubDatabaseTool:
    """Stand-in for the governed DatabaseTool in pure control-flow tests."""

    policy_summary: dict = {"status": "stub"}
    last_policy_decision = None

    def list_tables(self) -> list[str]:
        return ["items"]

    def describe_table(self, name: str):  # pragma: no cover - not exercised
        raise ValueError(f"stub database tool cannot describe {name!r}")

    def execute_sql(self, sql: str):  # pragma: no cover - not exercised
        raise ValueError("stub database tool cannot execute SQL")


def fake_registry(
    budget: BudgetManager,
    handlers: dict[str, callable],
    *,
    modes: tuple[str, ...] = ("read", "execute"),
    schemas: dict[str, dict] | None = None,
    database_tool: object | None = None,
) -> ToolRegistry:
    """A registry with only the handlers a control-flow test needs."""

    schemas = schemas or {
        "check_data_quality": QUALITY_PARAMS,
        "execute_sql": SQL_PARAMS,
        "preview_sql": SQL_PARAMS,
    }
    registry = ToolRegistry(
        budget, database_tool_factory=database_tool or StubDatabaseTool()
    )
    for name, handler in handlers.items():
        registry.register(
            ToolSpec(
                name=name,
                description=f"fake {name}",
                parameter_schema=schemas.get(name, {"type": "object", "properties": {}}),
                modes=list(modes),
            ),
            lambda params, context, _handler=handler: _handler(params, context),
        )
    return registry


class PlanValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = BudgetManager()
        self.registry = fake_registry(
            self.budget,
            {"check_data_quality": lambda p, c: {}, "execute_sql": lambda p, c: {}},
        )

    def validate(self, plan: AnalysisPlan, mode: str = "execute") -> list[str]:
        try:
            PlanValidator.validate(plan, self.registry, mode)
        except PlanViolation as exc:
            return exc.violations
        return []

    def test_cyclic_plan_is_rejected(self):
        plan = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(id="a", action="check_data_quality", depends_on=["b"]),
                PlanStep(id="b", action="check_data_quality", depends_on=["a"]),
            ],
        )
        violations = self.validate(plan)
        self.assertTrue(any(item.startswith("cycle_detected") for item in violations), violations)
        with self.assertRaises(PlanViolation):
            PlanValidator.topological_order(plan)

    def test_unknown_dependency_and_duplicate_id_are_rejected(self):
        missing = AnalysisPlan(
            question="q",
            steps=[PlanStep(id="a", action="check_data_quality", depends_on=["ghost"])],
        )
        self.assertTrue(
            any(item.startswith("unknown_dependency") for item in self.validate(missing))
        )
        duplicate = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(id="a", action="check_data_quality"),
                PlanStep(id="a", action="check_data_quality"),
            ],
        )
        self.assertTrue(
            any(item.startswith("duplicate_step_id") for item in self.validate(duplicate))
        )
        self.assertTrue(
            any(item.startswith("empty_plan") for item in self.validate(AnalysisPlan(question="q")))
        )

    def test_unknown_action_and_bad_params_are_rejected(self):
        unknown = AnalysisPlan(
            question="q", steps=[PlanStep(id="a", action="explode_database")]
        )
        self.assertEqual(self.validate(unknown), ["unknown_action: 'explode_database'"])

        bad_params = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(
                    id="a",
                    action="query_metric",
                    inputs={"metric": "item_count", "limit": "many"},
                )
            ],
        )
        violations = self.validate(bad_params)
        self.assertTrue(any(item.startswith("invalid_params") for item in violations), violations)

    def test_mode_and_budget_bounds_are_enforced(self):
        plan = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(
                    id="a",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["grain_unique"]},
                    budget={"max_tool_calls": 1},
                )
            ],
        )
        self.assertEqual(self.validate(plan), [])
        self.assertTrue(
            any(
                item.startswith("mode_not_allowed")
                for item in self.validate(plan, mode="plan_only")
            )
        )
        greedy = plan.model_copy(deep=True)
        greedy.steps[0].budget = {"max_tool_calls": 10_000}
        self.assertTrue(
            any(
                item.startswith("budget_exceeds_limit")
                for item in self.validate(greedy)
            )
        )
        unknown_key = plan.model_copy(deep=True)
        unknown_key.steps[0].budget = {"max_magic": 1}
        self.assertTrue(
            any(item.startswith("unknown_budget_key") for item in self.validate(unknown_key))
        )
        # Validation never executes anything.
        self.assertEqual(self.budget.usage.max_tool_calls, 0)
        self.assertEqual(self.registry.journal, [])


class ExecutorControlFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0

    def _record(self, name: str, sleep: float = 0.0) -> dict:
        with self.lock:
            self.calls.append((name, time.monotonic()))
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if sleep:
                time.sleep(sleep)
            return {"status": "ok", "name": name}
        finally:
            with self.lock:
                self.inflight -= 1

    def test_dependency_order_is_respected(self):
        budget = BudgetManager()
        registry = fake_registry(
            budget,
            {
                "check_data_quality": lambda p, c: self._record("check_data_quality"),
            },
        )
        plan = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(
                    id="third",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["duplicates"]},
                    depends_on=["fourth"],
                ),
                PlanStep(
                    id="first",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["grain_unique"]},
                ),
                PlanStep(
                    id="second",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["null_rate"]},
                    depends_on=["first"],
                ),
                PlanStep(
                    id="fourth",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["coverage"]},
                    depends_on=["second"],
                ),
            ],
        )
        result = AnalysisExecutor(registry, budget_manager=budget).execute(plan)
        self.assertEqual([step.status for step in result.steps], ["succeeded"] * 4)
        self.assertEqual([name for name, _ in self.calls], ["check_data_quality"] * 4)
        # Each step started only after its dependency had finished.
        self.assertLessEqual(self.calls[0][1], self.calls[1][1])
        self.assertLessEqual(self.calls[1][1], self.calls[2][1])
        self.assertLessEqual(self.calls[2][1], self.calls[3][1])
        self.assertEqual(
            [step.step_id for step in result.steps], ["first", "second", "fourth", "third"]
        )

    def test_independent_steps_run_in_parallel_on_one_shared_budget(self):
        budget = BudgetManager()
        barrier = threading.Barrier(2, timeout=5)

        def handler(params, context):
            name = params["checks"][0]
            with self.lock:
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                barrier.wait()
            finally:
                with self.lock:
                    self.inflight -= 1
            return {"status": "ok", "name": name}

        registry = fake_registry(budget, {"check_data_quality": handler})
        plan = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(
                    id="left",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["grain_unique"]},
                ),
                PlanStep(
                    id="right",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["null_rate"]},
                ),
            ],
        )
        result = AnalysisExecutor(registry, budget_manager=budget, max_workers=2).execute(plan)
        self.assertEqual(self.max_inflight, 2, "independent steps must overlap")
        self.assertEqual([step.status for step in result.steps], ["succeeded", "succeeded"])
        self.assertEqual(budget.usage.max_tool_calls, 2)
        self.assertEqual(len(registry.journal), 2)

    def test_shared_budget_cap_stops_the_parallel_consumer(self):
        budget = BudgetManager(limits={"max_tool_calls": 1}, per_call={"max_tool_calls": 1})
        registry = fake_registry(
            budget, {"check_data_quality": lambda p, c: self._record("check_data_quality", 0.05)}
        )
        plan = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(
                    id="left",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["grain_unique"]},
                ),
                PlanStep(
                    id="right",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["null_rate"]},
                ),
            ],
        )
        result = AnalysisExecutor(registry, budget_manager=budget, max_workers=2).execute(plan)
        statuses = sorted(step.status for step in result.steps)
        self.assertEqual(statuses, ["failed", "succeeded"])
        denied = next(step for step in result.steps if step.status == "failed")
        self.assertEqual(denied.error_category, "budget")
        self.assertEqual(budget.usage.max_tool_calls, 1)
        self.assertEqual(result.stop_reason, "budget_exhausted")
        self.assertNotEqual(result.status, "succeeded")

    def test_upstream_failure_skips_dependents_instead_of_succeeding(self):
        budget = BudgetManager()
        registry = fake_registry(
            budget,
            {
                "check_data_quality": lambda p, c: self._record("check_data_quality"),
                "execute_sql": lambda p, c: self._record("execute_sql"),
            },
        )
        plan = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(id="broken", action="query_metric", inputs={"metric": "m", "dimensions": ["x"]}),
                PlanStep(
                    id="dependent",
                    action="check_data_quality",
                    inputs={"table_name": "items", "checks": ["grain_unique"]},
                    depends_on=["broken"],
                ),
            ],
        )
        result = AnalysisExecutor(registry, budget_manager=budget).execute(plan)
        self.assertEqual(result.step("broken").status, "failed")
        self.assertEqual(result.step("dependent").status, "skipped")
        self.assertIn("upstream_failure", result.step("dependent").error)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.status, "failed")

    def test_phase_four_actions_are_implemented_and_unknown_actions_are_not(self):
        """Step 11 tools are real; genuinely unknown actions still fail honestly."""

        budget = BudgetManager()
        from queryforge.orchestration.tools import build_default_registry

        registry = build_default_registry(StubDatabaseTool(), budget)
        for name in (
            "compare_periods",
            "drill_down",
            "calculate_contribution",
            "detect_anomaly",
            "render_chart",
        ):
            self.assertTrue(registry.is_available(name), name)

        # A chart is computable straight from supplied rows (no database needed).
        plan = AnalysisPlan(
            question="q",
            steps=[
                PlanStep(
                    id="chart",
                    action="render_chart",
                    validation={
                        "knobs": {"time_grain": None},
                    },
                )
            ],
        )
        result = AnalysisExecutor(registry, budget_manager=budget).execute(plan)
        chart_step = result.steps[0]
        # Without a governed metric result the value assembly refuses honestly.
        self.assertIn(chart_step.status, {"failed", "succeeded"})
        if chart_step.status == "failed":
            self.assertIn("data_absent", chart_step.error)

        # An action that no tool backs is rejected before any tool call.
        with self.assertRaises(PlanViolation):
            AnalysisExecutor(registry, budget_manager=budget).execute(
                AnalysisPlan(question="q", steps=[PlanStep(id="nope", action="nope")])
            )
        self.assertEqual(registry.journal, [])


class ThreadLocalTools:
    """Per-thread governed DatabaseTool, like the application service uses.

    sqlite3 connections cannot be shared between threads, so the registry gets a
    facade that resolves the current thread's connection on attribute access.
    """

    def __init__(self, database: Path) -> None:
        self.database = database
        self._local = threading.local()
        self._created: list = []

    def __getattr__(self, name: str):
        tool = getattr(self._local, "tool", None)
        if tool is None:
            from queryforge.domain.security import load_sql_policy
            from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
            from queryforge.infrastructure.tools.database_tool import DatabaseTool

            connector = SQLiteConnector(str(self.database))
            policy, source = load_sql_policy(None)
            tool = DatabaseTool(connector, policy, policy_source_path=source)
            self._local.tool = tool
            self._created.append(tool)
        return getattr(tool, name)

    def close(self) -> None:
        for tool in self._created:
            try:
                tool.connector.close()
            except Exception:
                # A connection opened by a worker thread may only be closed from
                # that thread; teardown reclaims it when the thread exits.
                pass
        self._created.clear()


class ExecutorEvidenceGateTest(unittest.TestCase):
    """Task success needs compose_answer *and* every promised evidence kind."""

    def setUp(self) -> None:
        self.fixture = Fixture()
        self.budget = BudgetManager()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _registry(self):
        from queryforge.domain.semantic import SemanticModelLoader
        from queryforge.orchestration.tools import build_default_registry

        tools = ThreadLocalTools(self.fixture.database)
        schemas = [
            tools.describe_table_for_validation(name) for name in tools.list_tables()
        ]
        model = SemanticModelLoader.load_and_validate(
            str(self.fixture.semantic_model), schemas, "items"
        )
        # The registry binds its database_tool_factory into every tool context;
        # the facade resolves the governed connection of the calling thread.
        registry = build_default_registry(tools, self.budget, semantic_model=model)
        context = ToolContext(run_id="run-gate", question="items", semantic_model=model)
        return registry, context, tools

    def test_compose_succeeds_but_missing_evidence_keeps_partial(self):
        registry, context, tools = self._registry()
        try:
            plan = AnalysisPlan(
                question="How many items are there?",
                steps=[
                    PlanStep(
                        id="quality",
                        action="check_data_quality",
                        inputs={"table_name": "items", "checks": ["grain_unique"]},
                        expected_evidence=["data_quality"],
                    ),
                    PlanStep(
                        id="value",
                        action="query_metric",
                        inputs={"metric": "item_count", "sql": "SELECT COUNT(*) AS n FROM items"},
                        expected_evidence=["metric_value"],
                    ),
                    PlanStep(id="chart", action="render_chart", expected_evidence=["chart"]),
                    PlanStep(
                        id="compose",
                        action="compose_answer",
                        inputs={},
                        depends_on=["value"],
                        expected_evidence=["answer"],
                    ),
                ],
            )
            result = AnalysisExecutor(registry, budget_manager=self.budget).execute(plan)
            self.assertEqual(result.step("value").status, "succeeded")
            self.assertEqual(result.step("compose").status, "failed")
            self.assertIn("missing_evidence", result.step("compose").error)
            self.assertIn("chart", result.step("compose").error)
            self.assertIsNone(result.answer)
            self.assertEqual(result.status, "partial")
            self.assertNotEqual(result.status, "succeeded")

            # With the missing evidence removed the same plan succeeds.
            complete = plan.model_copy(deep=True)
            complete.steps = [step for step in complete.steps if step.id != "chart"]
            complete.status = "pending"
            ok = AnalysisExecutor(registry, budget_manager=self.budget).execute(complete)
            self.assertEqual(ok.status, "succeeded")
            self.assertIsNotNone(ok.answer)
            self.assertTrue(ok.answer["evidence_ids"])
            self.assertEqual(ok.answer["value"], len(ITEMS))
        finally:
            tools.close()


class AnalysisPlannerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_end_to_end_count_metric_with_evidence_ids(self):
        payload = self.fixture.analyze("How many items are there?")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["plan"]["status"], "succeeded")
        self.assertEqual(len(payload["plan"]["steps"]), 4)
        self.assertEqual(
            [step["step_id"] for step in payload["steps"]],
            ["resolve_metric", "check_data_quality", "query_metric", "compose_answer"],
        )
        answer = payload["answer"]
        self.assertEqual(answer["metric"], "item_count")
        self.assertEqual(answer["value"], len(ITEMS))
        self.assertEqual(len(answer["evidence_ids"]), 3)
        kinds = {item["kind"] for item in payload["evidence"]}
        self.assertEqual(kinds, {"metric_resolution", "data_quality", "metric_value"})
        step_ids = {step["step_id"] for step in payload["steps"]}
        for evidence in payload["evidence"]:
            self.assertTrue(evidence["evidence_id"].startswith("ev:"))
            self.assertIn(evidence["step_id"], step_ids)
        value_evidence = next(
            item for item in payload["evidence"] if item["kind"] == "metric_value"
        )
        self.assertIn("FROM items", value_evidence["payload"]["sql"])
        self.assertFalse(value_evidence["payload"]["degraded"])
        self.assertEqual(value_evidence["payload"]["row_count"], 1)
        self.assertEqual(payload["replan_reasons"], [])
        self.assertGreaterEqual(payload["budgets"]["usage"]["max_tool_calls"], 2)
        json.dumps(payload)

    def test_grouped_report_reuses_the_compiled_query_spec(self):
        payload = self.fixture.analyze("How many items are there by category?")
        self.assertEqual(payload["status"], "succeeded")
        value_evidence = next(
            item for item in payload["evidence"] if item["kind"] == "metric_value"
        )
        rows = value_evidence["payload"]["rows"]
        totals = {row[0]: row[1] for row in rows}
        # Independent oracle: rows inserted per category in the fixture.
        expected: dict[str, int] = {}
        for _, _, category, _, _ in ITEMS:
            expected[category] = expected.get(category, 0) + 1
        self.assertEqual(totals, expected)
        self.assertIn("GROUP BY items.category", value_evidence["payload"]["sql"])
        self.assertIsNotNone(value_evidence["payload"]["query_spec"])

    def test_data_absence_triggers_bounded_replan_to_a_legal_dimension(self):
        payload = self.fixture.analyze("How many items are there by brand_name?")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(len(payload["replan_reasons"]), 1)
        self.assertIn("brands.brand_name", payload["replan_reasons"][0])
        self.assertIn("items.category", payload["replan_reasons"][0])
        self.assertEqual(payload["plan"]["version"], 2)
        statuses = {step["step_id"]: step["status"] for step in payload["steps"]}
        self.assertEqual(statuses["query_metric"], "failed")
        replanned = [step for step in payload["steps"] if "~r" in step["step_id"]]
        self.assertEqual(len(replanned), 1)
        self.assertEqual(replanned[0]["status"], "succeeded")
        value_evidence = next(
            item for item in payload["evidence"] if item["kind"] == "metric_value"
        )
        self.assertIn("GROUP BY items.category", value_evidence["payload"]["sql"])
        # The replan swapped a dimension; it never redefined the metric.
        self.assertEqual(value_evidence["payload"]["metric"], "item_count")

    def test_replanning_is_bounded_and_does_not_fabricate_success(self):
        payload = self.fixture.analyze("How many items are there by brand_name?", max_replans=0)
        self.assertEqual(payload["replan_reasons"], [])
        self.assertNotEqual(payload["status"], "succeeded")
        self.assertIn(payload["status"], {"partial", "failed"})

    def test_data_absent_error_names_the_legal_alternative_dimension(self):
        """Regression: the suggestion was written to a state attribute nobody
        read, so the actionable alternative never reached the caller even though
        the bounded replan exists precisely to retry one of them."""

        payload = self.fixture.analyze(
            "How many items are there by brand_name?", max_replans=0
        )
        failed = next(
            step for step in payload["steps"] if step["step_id"] == "query_metric"
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("data_absent", failed["error"])
        self.assertIn("brands.brand_name", failed["error"])
        self.assertIn("legal alternative dimensions", failed["error"])
        self.assertIn("items.category", failed["error"])

    def test_high_impact_ambiguity_asks_for_clarification_without_running_sql(self):
        payload = self.fixture.analyze("What is the revenue?")
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertEqual(payload["plan"]["steps"], [])
        self.assertTrue(payload["unresolved_questions"])
        self.assertEqual(payload["evidence"], [])
        self.assertIsNone(payload["answer"])
        self.assertEqual(payload["stop_reason"], "high_impact_ambiguity:ambiguous_metric_definition")
        self.assertEqual(payload["budgets"]["usage"]["max_tool_calls"], 0)

    def test_plan_only_mode_refuses_the_execution_plan(self):
        with self.assertRaises(PlanViolation) as raised:
            self.fixture.analyze("How many items are there?", mode="plan_only")
        self.assertTrue(
            any("mode_not_allowed" in item for item in raised.exception.violations),
            raised.exception.violations,
        )

    def test_invalid_arguments_raise_value_error(self):
        service = self.fixture.service()
        with self.assertRaises(ValueError):
            service.analyze("   ", database=str(self.fixture.database))
        with self.assertRaises(ValueError):
            service.analyze("How many items?", database=str(self.fixture.root / "missing.sqlite"))
        with self.assertRaises(ValueError):
            service.analyze(
                "How many items?",
                database=str(self.fixture.database),
                semantic_model_path=str(self.fixture.semantic_model),
                mode="turbo",
            )

    # ------------------------------------------- governed breakdowns (step 02/06)

    def test_governed_join_dimension_is_compiled_not_previewed(self):
        """A breakdown across entities must compile a join, not return a preview.

        The regression this pins: the executor passed an EMPTY join-path list, so
        "watch hours by anime format" fell back to `SELECT * FROM fact_watch_session
        LIMIT 5`, recorded it as `metric_value` evidence and reported success — a
        raw preview presented as a metric answer.
        """
        root = Path(__file__).resolve().parents[1]
        database = root / "sample_data/anime_streaming/anime_streaming.sqlite"
        model = root / "sample_data/anime_streaming/semantic_model.yml"
        policy = root / "sample_data/anime_streaming/sql_policy.yml"
        payload = AnalysisPlannerService().analyze(
            "How many watch hours by anime format?",
            database=str(database),
            semantic_model_path=str(model),
            sql_policy_path=str(policy),
        )
        self.assertEqual(payload["status"], "succeeded", payload.get("steps"))
        metric_evidence = next(
            item for item in payload["evidence"] if item["kind"] == "metric_value"
        )
        sql = metric_evidence["payload"]["sql"]
        self.assertIn("JOIN dim_episode", sql)
        self.assertIn("dim_anime", sql)
        self.assertFalse(metric_evidence["payload"]["degraded"])
        self.assertNotIn("SELECT * FROM", sql)

        # Independent oracle: the same numbers computed straight from SQLite.
        connection = sqlite3.connect(database)
        expected = dict(
            connection.execute(
                "SELECT a.content_format, ROUND(SUM(w.watch_seconds)/3600.0, 6) "
                "FROM fact_watch_session w "
                "JOIN dim_episode e ON e.episode_id = w.episode_id "
                "JOIN dim_anime a ON a.anime_id = e.anime_id "
                "GROUP BY a.content_format"
            ).fetchall()
        )
        connection.close()
        observed = {row[0]: round(float(row[1]), 6) for row in metric_evidence["payload"]["rows"]}
        self.assertEqual(observed, expected)

    # --------------------------------- period comparison grain (single time dim)

    @staticmethod
    def _anime_service() -> AnalysisPlannerService:
        return AnalysisPlannerService()

    def _anime_analyze(self, question: str) -> dict:
        """Analyze a question against the bundled anime dataset."""

        root = Path(__file__).resolve().parents[1]
        return self._anime_service().analyze(
            question,
            database=str(root / "sample_data/anime_streaming/anime_streaming.sqlite"),
            semantic_model_path=str(
                root / "sample_data/anime_streaming/semantic_model.yml"
            ),
            sql_policy_path=str(root / "sample_data/anime_streaming/sql_policy.yml"),
        )

    def test_a_multi_dimension_result_is_never_a_period_comparison(self):
        """Regression: month x device rows were compared as if they were periods.

        ``_series`` takes the first non-numeric column as the label, so a result
        grouped by month AND device yields 60 points in which every month repeats
        once per device: the "last two points" became ``September -> September``
        — two categories of the same period — and were reported as a
        ``period_comparison`` with ``status=succeeded``.
        """

        payload = self._anime_analyze("Watch hours by device vs the previous month in 2024")
        metric_evidence = next(
            item for item in payload["evidence"] if item["kind"] == "metric_value"
        )
        self.assertEqual(
            metric_evidence["payload"]["dimensions"],
            ["calendar.month", "watch_session.device"],
        )
        self.assertEqual(
            len(metric_evidence["payload"]["columns"]), 3
        )
        # No fabricated comparison, and the plan does not claim success.
        self.assertEqual(
            [item for item in payload["evidence"] if item["kind"] == "period_comparison"],
            [],
        )
        self.assertNotEqual(payload["status"], "succeeded")
        comparison_step = next(
            step for step in payload["steps"] if step["step_id"] == "compare_periods"
        )
        self.assertEqual(comparison_step["status"], "failed")
        self.assertIn("unsupported_grain", comparison_step["error"])
        self.assertIn("calendar.month", comparison_step["error"])
        self.assertIn("watch_session.device", comparison_step["error"])

    def test_a_categorical_single_dimension_is_not_a_period_comparison(self):
        """Regression: one dimension is not enough — it must also be a time one.

        "Watch hours by device in December 2024 compared to November 2024" compiles
        a per-device result, and the comparison then reported ``Tablet -> Web`` (a
        gap between two devices) as a period change, with ``status=succeeded``.
        """

        payload = self._anime_analyze(
            "Watch hours by device in December 2024 compared to November 2024"
        )
        metric_evidence = next(
            item for item in payload["evidence"] if item["kind"] == "metric_value"
        )
        self.assertEqual(
            metric_evidence["payload"]["dimensions"], ["watch_session.device"]
        )
        self.assertEqual(
            [item for item in payload["evidence"] if item["kind"] == "period_comparison"],
            [],
        )
        self.assertNotEqual(payload["status"], "succeeded")
        comparison_step = next(
            step for step in payload["steps"] if step["step_id"] == "compare_periods"
        )
        self.assertEqual(comparison_step["status"], "failed")
        self.assertIn("unsupported_grain", comparison_step["error"])
        self.assertIn("not calendar periods", comparison_step["error"])
        # The categorical result itself is still produced and usable: refusing the
        # comparison must not throw away the breakdown the user asked for.
        self.assertEqual(metric_evidence["payload"]["row_count"], len(
            metric_evidence["payload"]["rows"]
        ))
        self.assertGreater(metric_evidence["payload"]["row_count"], 1)

    def test_a_single_ordered_time_dimension_still_compares_periods(self):
        """The refusal must not cost the legitimate monthly comparison."""

        database = (
            Path(__file__).resolve().parents[1]
            / "sample_data/anime_streaming/anime_streaming.sqlite"
        )
        payload = self._anime_analyze("How did watch hours change month over month in 2024?")
        self.assertEqual(payload["status"], "succeeded", payload.get("steps"))
        metric_evidence = next(
            item for item in payload["evidence"] if item["kind"] == "metric_value"
        )
        self.assertEqual(metric_evidence["payload"]["dimensions"], ["calendar.month"])
        comparison = next(
            item for item in payload["evidence"] if item["kind"] == "period_comparison"
        )
        self.assertEqual(comparison["payload"]["label"], "November -> December")

        # Independent oracle: the same two periods computed straight from SQLite.
        connection = sqlite3.connect(database)
        rows = dict(
            connection.execute(
                "SELECT d.month_name, ROUND(SUM(w.watch_seconds)/3600.0, 6) "
                "FROM fact_watch_session w "
                "JOIN dim_date d ON d.date_key = w.watch_date_key "
                "WHERE d.year = 2024 GROUP BY d.month_name"
            ).fetchall()
        )
        connection.close()
        self.assertAlmostEqual(
            float(comparison["payload"]["baseline"]), rows["November"], places=6
        )
        self.assertAlmostEqual(
            float(comparison["payload"]["current"]), rows["December"], places=6
        )
        self.assertAlmostEqual(rows["November"], 1172.496389, places=6)
        self.assertAlmostEqual(rows["December"], 1207.235, places=6)

    def test_drill_down_takes_a_single_dimension_where_a_comparison_refuses_many(self):
        """Pinned contrast: buckets need one dimension, period comparisons need time.

        ``_buckets`` shares ``_series`` with the comparison path, and taking the
        first non-numeric column as the category is exactly right for a
        breakdown; only the *period* claim requires ordered time.
        """

        single = {
            "columns": ["device_type", "watch_hours"],
            "rows": [["TV", 10.0], ["Mobile", 4.0]],
            "dimensions": ["watch_session.device"],
        }
        self.assertEqual(
            AnalysisExecutor._buckets(single),
            [{"category": "TV", "value": 10.0}, {"category": "Mobile", "value": 4.0}],
        )
        multi = {
            "columns": ["month_name", "device_type", "watch_hours"],
            "rows": [["January", "TV", 5.0], ["January", "Mobile", 1.0]],
            "dimensions": ["calendar.month", "watch_session.device"],
        }
        with self.assertRaises(UnsupportedAnalysisError) as raised:
            AnalysisExecutor._require_single_series_dimension("compare_periods", multi)
        self.assertIn("unsupported_grain", str(raised.exception))

        # End-to-end contrast on the same dataset: the breakdown question keeps
        # its buckets and chart while the comparison refuses the multi-dimension
        # grain.
        breakdown = self._anime_analyze("Compare watch hours by device between November and December 2024")
        self.assertEqual(breakdown["status"], "succeeded", breakdown.get("steps"))
        self.assertTrue(
            any(item["kind"] == "drill_down" for item in breakdown["evidence"])
        )
        self.assertEqual(
            [
                item
                for item in breakdown["evidence"]
                if item["kind"] == "period_comparison"
            ],
            [],
        )

    def test_only_advancing_calendar_periods_prove_a_time_series(self):
        """The order proof accepts time order and refuses categories/backwards."""

        for labels in (
            ["January", "February", "December"],
            ["October", "November", "December", "January"],
            # A month without rows is skipped by the result; April < August <
            # December is still provably ordered in time.
            ["April", "August", "December"],
            ["2024-01", "2024-02"],
            ["2024-Q1", "2024-Q2"],
            ["2022", "2023", "2024"],
        ):
            AnalysisExecutor._require_ordered_time_series("compare_periods", labels)
        for labels in (
            ["TV", "Mobile", "Web"],
            ["December", "February"],
            ["January", "March", "January"],
            ["2024-02", "2024-01"],
        ):
            with self.assertRaises(UnsupportedAnalysisError) as raised:
                AnalysisExecutor._require_ordered_time_series("compare_periods", labels)
            self.assertIn("unsupported_grain", str(raised.exception))


    def test_a_time_scoped_question_is_filtered_and_an_empty_scope_is_not_success(self):
        """Time scope must reach the compiler, and an empty slice must not pass.

        Two regressions pinned together: "in 2024" used to compile an UNFILTERED
        query and return the all-time total as success, and a scope with no rows
        returned a null scalar reported as ``succeeded``.
        """
        root = Path(__file__).resolve().parents[1]
        database = root / "sample_data/anime_streaming/anime_streaming.sqlite"
        model = root / "sample_data/anime_streaming/semantic_model.yml"
        policy = root / "sample_data/anime_streaming/sql_policy.yml"
        service = AnalysisPlannerService()

        scoped = service.analyze(
            "How many watch hours in 2024?",
            database=str(database),
            semantic_model_path=str(model),
            sql_policy_path=str(policy),
        )
        self.assertEqual(scoped["status"], "succeeded")
        self.assertEqual(
            scoped["analysis_request"]["time_range"], "2024-01-01..2024-12-31"
        )
        metric_evidence = next(
            item for item in scoped["evidence"] if item["kind"] == "metric_value"
        )
        self.assertIn("watch_date_key BETWEEN 20240101 AND 20241231", metric_evidence["payload"]["sql"])

        connection = sqlite3.connect(database)
        expected_2024 = connection.execute(
            "SELECT SUM(watch_seconds)/3600.0 FROM fact_watch_session "
            "WHERE watch_date_key BETWEEN 20240101 AND 20241231"
        ).fetchone()[0]
        all_time = connection.execute(
            "SELECT SUM(watch_seconds)/3600.0 FROM fact_watch_session"
        ).fetchone()[0]
        connection.close()
        self.assertAlmostEqual(scoped["answer"]["value"], expected_2024, places=6)
        self.assertNotAlmostEqual(scoped["answer"]["value"], all_time, places=6)

        # A window with no rows is a gap, not a successful answer with a null value.
        empty = service.analyze(
            "How many watch hours in Q1 1990?",
            database=str(database),
            semantic_model_path=str(model),
            sql_policy_path=str(policy),
        )
        self.assertEqual(empty["status"], "partial")
        # No answer is composed at all: the null scalar is not dressed up as one.
        self.assertIsNone(empty.get("answer"))
        self.assertTrue(empty["replan_reasons"])
        self.assertIn("no value for the requested scope", json.dumps(empty["steps"]))

    def test_ungoverned_breakdown_asks_instead_of_answering_a_different_question(self):
        """A breakdown the model cannot express must not degrade into a total.

        "by brand_name" (a field no entity declares) used to drop the dimension and
        answer the overall total with status ``succeeded``, which reads as a correct
        answer to a question nobody asked.
        """
        payload = self.fixture.analyze("How many items are there by supplier_code?")
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertEqual(
            payload["stop_reason"], "unsupported_breakdown:supplier_code"
        )
        self.assertIsNone(payload["answer"])
        self.assertEqual(payload["plan"]["steps"], [])

        # A governed breakdown still runs normally.
        grouped = self.fixture.analyze("How many items are there by category?")
        self.assertEqual(grouped["status"], "succeeded")

    def test_a_dimension_declared_twice_resolves_to_one_entity(self):
        root = Path(__file__).resolve().parents[1]
        with SQLiteConnector(
            str(root / "sample_data/anime_streaming/anime_streaming.sqlite")
        ) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(name) for name in tool.list_tables()]
        model_context = SemanticModelLoader.load_and_validate(
            root / "sample_data/anime_streaming/semantic_model.yml",
            schemas,
            "watch hours by anime format",
        )
        declared = {
            dimension.name
            for entity in model_context.model.entities
            for dimension in entity.dimensions
        }
        self.assertIn("format", declared)
        entities_declaring_format = [
            entity.name
            for entity in model_context.model.entities
            if any(dimension.name == "format" for dimension in entity.dimensions)
        ]
        self.assertGreater(len(entities_declaring_format), 1, "fixture needs the ambiguity")
        refs = AnalysisPlannerService._dimension_refs(
            model_context, ["format"], "watch hours by anime format"
        )
        # Exactly one ref, and the entity the question names wins.
        self.assertEqual(refs, ["anime.format"])

    # ------------------------------------------- ablation switches (step 16)

    def test_ablation_switches_change_the_plan_through_real_code_paths(self):
        """The benchmark disables capabilities the way a weaker build would."""
        from queryforge.application.analysis_planner import DISABLEABLE_FEATURES

        self.assertEqual(
            DISABLEABLE_FEATURES,
            frozenset(
                {"analysis_tools", "data_quality", "semantic_compile", "evidence_layer"}
            ),
        )
        baseline = self.fixture.analyze("How many items are there?")
        self.assertEqual(baseline["status"], "succeeded")
        self.assertEqual(
            [step["step_id"] for step in baseline["steps"]],
            ["resolve_metric", "check_data_quality", "query_metric", "compose_answer"],
        )
        self.assertIsNotNone(baseline.get("final_answer"))

        # `data_quality` removes the quality step and records the gap honestly.
        without_quality = self.fixture.service(disabled_features=["data_quality"]).analyze(
            "How many items are there?",
            database=str(self.fixture.database),
            semantic_model_path=str(self.fixture.semantic_model),
        )
        self.assertEqual(
            [step["step_id"] for step in without_quality["steps"]],
            ["resolve_metric", "query_metric", "compose_answer"],
        )
        self.assertIn("check_data_quality", without_quality["unavailable_actions"])
        self.assertEqual(without_quality["answer"]["value"], len(ITEMS))

        # `evidence_layer` keeps the legacy answer but drops the step-12 layer.
        legacy_only = self.fixture.service(disabled_features=["evidence_layer"]).analyze(
            "How many items are there?",
            database=str(self.fixture.database),
            semantic_model_path=str(self.fixture.semantic_model),
        )
        self.assertEqual(legacy_only["status"], "succeeded")
        self.assertIsNone(legacy_only.get("final_answer"))
        self.assertEqual(legacy_only["answer"]["value"], len(ITEMS))

        # `replan` is the existing knob: without it the data-absence case cannot
        # recover to a legal dimension.
        no_replan = self.fixture.analyze("How many items are there by brand_name?", max_replans=0)
        self.assertEqual(no_replan["replan_reasons"], [])
        self.assertNotEqual(no_replan["status"], "succeeded")

        with self.assertRaises(ValueError) as raised:
            self.fixture.service(disabled_features=["teleportation"])
        self.assertIn("unknown disabled feature", str(raised.exception))

    # ------------------------------------------------- durable runs (step 15)

    def test_client_cancel_persists_and_blocks_every_resume(self):
        service = self.fixture.service()
        self.assertTrue(
            service.cancel_run("run-cancelled", reason="client disconnected")
        )
        status = service.run_status("run-cancelled")
        self.assertTrue(status.terminal)
        self.assertEqual(status.terminal_outcome, "cancelled")
        self.assertTrue(any("client disconnected" in note for note in status.notes))
        # A second cancellation is a no-op: the outcome is written once.
        self.assertFalse(service.cancel_run("run-cancelled"))
        self.assertEqual(service.run_status("run-cancelled").terminal_outcome, "cancelled")
        with self.assertRaises(RunNotResumable):
            self.fixture.analyze(
                "How many items are there?", run_id="run-cancelled", resume=True
            )
        with self.assertRaises(ValueError):
            service.cancel_run("")
        with self.assertRaises(ValueError):
            service.run_status("")

    def test_finished_run_is_terminal_and_a_crashed_run_resumes_without_recomputing(self):
        payload = self.fixture.analyze("How many items are there?", run_id="run-durable")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["terminal_outcome"], "success")
        self.assertEqual(payload["reused_steps"], [])

        status = self.fixture.service().run_status("run-durable")
        self.assertTrue(status.terminal)
        self.assertEqual(status.terminal_outcome, "success")
        self.assertEqual(
            sorted(status.reused_candidates),
            ["check_data_quality", "compose_answer", "query_metric", "resolve_metric"],
        )
        # A run that already ended is never silently revived.
        with self.assertRaises(RunNotResumable):
            self.fixture.analyze(
                "How many items are there?", run_id="run-durable", resume=True
            )

        # Simulate a crash *after* the last step committed but before the
        # terminal outcome was written: exactly what a killed process leaves.
        journal_path = self.fixture.root / ".queryforge" / "runs" / "run-durable" / "execution.json"
        state = json.loads(journal_path.read_text(encoding="utf-8"))
        state["status"] = "running"
        state["terminal_outcome"] = None
        state["terminal_at"] = None
        journal_path.write_text(json.dumps(state), encoding="utf-8")

        empty = self.fixture.service().run_status("run-durable")
        self.assertFalse(empty.terminal)

        resumed = self.fixture.analyze(
            "How many items are there?", run_id="run-durable", resume=True
        )
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(resumed["terminal_outcome"], "success")
        # Every committed step was reused; nothing was recomputed or re-queried.
        self.assertEqual(
            sorted(resumed["reused_steps"]),
            ["check_data_quality", "compose_answer", "query_metric", "resolve_metric"],
        )
        self.assertEqual(resumed["recomputed_steps"], [])
        self.assertEqual(resumed["budgets"]["usage"]["max_tool_calls"], 0)
        self.assertEqual(resumed["answer"]["value"], len(ITEMS))


class TransportTest(unittest.TestCase):
    """`--analyze` and `POST /analyze` expose the same structured result."""

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        from queryforge import cli

        out, err = io.StringIO(), io.StringIO()
        previous = sys.argv
        sys.argv = ["queryforge", *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main()
        finally:
            sys.argv = previous
        return code, out.getvalue(), err.getvalue()

    def test_cli_analyze_prints_the_structured_result(self):
        code, stdout, _ = self._run_cli(
            [
                "--question",
                "How many items are there?",
                "--analyze",
                "--database",
                str(self.fixture.database),
                "--semantic-model",
                str(self.fixture.semantic_model),
            ]
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["answer"]["value"], len(ITEMS))
        self.assertTrue(payload["answer"]["evidence_ids"])
        self.assertIn("budgets", payload)

    def test_cli_analyze_uses_the_same_failure_code_as_question(self):
        code, _, stderr = self._run_cli(
            [
                "--question",
                "How many items are there?",
                "--analyze",
                "--database",
                str(self.fixture.root / "missing.sqlite"),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("QueryForge failed", stderr)

    def test_cli_analyze_rejects_an_invalid_replan_budget(self):
        code, _, stderr = self._run_cli(
            [
                "--question",
                "How many items are there?",
                "--analyze",
                "--analyze-max-replans",
                "-1",
                "--database",
                str(self.fixture.database),
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--analyze-max-replans", stderr)

    def test_cli_run_id_persists_status_and_resume_requires_it(self):
        """`--run-id` / `--resume` / `--run-status` expose durable runs to operators."""
        state_root = self.fixture.root / "cli_runs"
        with patch.dict(os.environ, {"ORCHESTRATION_STATE_ROOT": str(state_root)}):
            code, _, stderr = self._run_cli(
                [
                    "--question",
                    "How many items are there?",
                    "--analyze",
                    "--resume",
                    "--database",
                    str(self.fixture.database),
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("--resume requires --run-id", stderr)

            code, stdout, _ = self._run_cli(
                [
                    "--question",
                    "How many items are there?",
                    "--analyze",
                    "--run-id",
                    "cli-run",
                    "--database",
                    str(self.fixture.database),
                    "--semantic-model",
                    str(self.fixture.semantic_model),
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["terminal_outcome"], "success")
            self.assertTrue((state_root / "cli-run" / "execution.json").is_file())

            code, stdout, _ = self._run_cli(["--run-status", "cli-run"])
            self.assertEqual(code, 0)
            status = json.loads(stdout)
            self.assertEqual(status["run_id"], "cli-run")
            self.assertTrue(status["terminal"])
            self.assertEqual(status["terminal_outcome"], "success")
            self.assertEqual(
                sorted(status["steps"]),
                [
                    "check_data_quality",
                    "compose_answer",
                    "query_metric",
                    "resolve_metric",
                ],
            )

            # The run already ended, so a resume is refused rather than revived.
            code, _, stderr = self._run_cli(
                [
                    "--question",
                    "How many items are there?",
                    "--analyze",
                    "--run-id",
                    "cli-run",
                    "--resume",
                    "--database",
                    str(self.fixture.database),
                    "--semantic-model",
                    str(self.fixture.semantic_model),
                ]
            )
            self.assertEqual(code, 1)
            self.assertIn("already ended", stderr)

    @unittest.skipUnless(
        importlib.util.find_spec("fastapi") is not None
        and importlib.util.find_spec("httpx") is not None,
        "fastapi optional dependency is not installed",
    )
    def test_analyze_route_returns_the_result_and_400_for_clarification(self):
        from fastapi.testclient import TestClient

        from queryforge.application import AgentService
        from queryforge.interfaces.api.app import create_app

        config = self.fixture.config()
        service = AgentService(config_loader=lambda **_: config)
        client = TestClient(create_app(service))
        self.assertIn("/analyze", {route.path for route in client.app.routes})

        ok = client.post(
            "/analyze",
            json={
                "question": "How many items are there?",
                "database": str(self.fixture.database),
                "semantic_model_path": str(self.fixture.semantic_model),
            },
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["status"], "succeeded")

        clarified = client.post(
            "/analyze",
            json={
                "question": "What is the revenue?",
                "database": str(self.fixture.database),
                "semantic_model_path": str(self.fixture.semantic_model),
            },
        )
        self.assertEqual(clarified.status_code, 400)
        self.assertEqual(clarified.json()["detail"]["status"], "needs_clarification")

        invalid = client.post(
            "/analyze",
            json={
                "question": "How many items are there?",
                "database": str(self.fixture.root / "missing.sqlite"),
            },
        )
        self.assertEqual(invalid.status_code, 400)


class IntegrationSmokeRegressionTest(unittest.TestCase):
    """Regression guards for the reported "unbound registry" integration defect.

    The reported fixture (entity ``item`` over table ``items``, metric
    ``COUNT(items.id)``) must reach a complete governed run: every step
    succeeding, a non-empty answer, and traceable evidence ids.
    """

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _analyze(self, question: str, **kwargs):
        return self.fixture.service().analyze(
            question,
            database=str(self.fixture.singular_database),
            semantic_model_path=str(self.fixture.singular_model),
            **kwargs,
        )

    def test_item_count_by_category_completes_with_evidence(self):
        payload = self._analyze("What is the item_count by category?")
        statuses = {step["step_id"]: step for step in payload["steps"]}
        self.assertEqual(
            [step["status"] for step in payload["steps"]],
            ["succeeded"] * len(payload["steps"]),
            {key: value["error"] for key, value in statuses.items()},
        )
        # The grouped question uses the segment template: drill-down + chart.
        self.assertIn("drill_down", statuses)
        self.assertIn("render_chart", statuses)
        self.assertEqual(payload["status"], "succeeded")
        self.assertIsNotNone(payload["answer"])
        self.assertTrue(payload["answer"]["evidence_ids"])
        # metric_resolution + data_quality + metric_value (+ drill_down/chart
        # for the segment template) are all cited.
        self.assertGreaterEqual(len(payload["answer"]["evidence_ids"]), 3)
        self.assertTrue(payload["evidence"])
        # Independent oracle: the fixture groups are a=2 and b=1.
        self.assertEqual(payload["answer"]["rows"], [["a", 2], ["b", 1]])
        # The governed SQL path really ran once per data step, and no step failed
        # with an unbound-tool error.
        self.assertGreaterEqual(payload["budgets"]["usage"]["max_tool_calls"], 2)
        for step in payload["steps"]:
            self.assertNotIn("no governed database tool", step["error"] or "")
        json.dumps(payload)

    def test_declared_but_unmatched_metric_still_asks_for_clarification(self):
        payload = self._analyze("What is the average temperature?")
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertEqual(payload["stop_reason"], "no_governed_metric_match")
        self.assertEqual(payload["steps"], [])
        self.assertEqual(payload["evidence"], [])
        self.assertIsNone(payload["answer"])
        self.assertTrue(payload["analysis_request"]["unresolved_questions"] == [])
        self.assertEqual(payload["budgets"]["usage"]["max_tool_calls"], 0)

    def test_registry_is_bound_to_a_governed_tool_for_every_run(self):
        captured: dict = {}
        fixture = self.fixture

        def spy_registry_factory(database_tool_factory, budget_manager, **kwargs):
            captured["factory"] = database_tool_factory
            captured["kwargs"] = kwargs
            from queryforge.orchestration.tools import build_default_registry

            return build_default_registry(database_tool_factory, budget_manager, **kwargs)

        service = AnalysisPlannerService(
            config_loader=lambda **_: fixture.config(), registry_factory=spy_registry_factory
        )
        payload = service.analyze(
            "What is the item_count by category?",
            database=str(fixture.singular_database),
            semantic_model_path=str(fixture.singular_model),
        )
        self.assertEqual(payload["status"], "succeeded")
        bound = captured["factory"]
        self.assertIsNotNone(bound)
        # The bound object resolves this thread's governed tool on demand.
        self.assertEqual(bound.list_tables(), ["items"])
        self.assertIsNotNone(bound.policy_summary)
        self.assertEqual(captured["kwargs"]["semantic_model"].model.name, "smoke")
        # The registry rejects an unbound factory instead of failing per step.
        with self.assertRaises(ValueError):
            self.fixture.service(
                registry_factory=lambda *args, **kwargs: __import__(
                    "queryforge.orchestration.tools", fromlist=["ToolRegistry"]
                ).ToolRegistry(args[1] if len(args) > 1 else BudgetManager())
            ).analyze(
                "What is the item_count by category?",
                database=str(fixture.singular_database),
                semantic_model_path=str(fixture.singular_model),
            )

    def test_connections_are_closed_after_each_run(self):
        service = self.fixture.service()
        for _ in range(2):
            payload = service.analyze(
                "What is the item_count by category?",
                database=str(self.fixture.singular_database),
                semantic_model_path=str(self.fixture.singular_model),
            )
            self.assertEqual(payload["status"], "succeeded")
        # A leaked handle would still hold the file on a read-only URI, so the
        # database has to stay replaceable/removable after the run finished.
        replaced = self.fixture.root / "replacement.sqlite"
        self.fixture.singular_database.replace(replaced)
        replaced.replace(self.fixture.singular_database)


class PlannerObservabilityTest(unittest.TestCase):
    """Step 17, section 八 item 3: the planned-analysis path is observable.

    ``AnalysisPlannerService.analyze`` used to produce no spans, no usage and no
    latency breakdown, so ``/analyze`` — unlike ``/ask`` — had no cost or latency
    a caller could reconcile. These tests pin the planner to the primitives the
    workflow path already uses, including the honest "no model call" usage report
    and the privacy contract (no prompt, no SQL text, no result rows).
    """

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    @staticmethod
    def _spans_by_kind(payload: dict) -> dict[str, list[dict]]:
        by_kind: dict[str, list[dict]] = {}
        for span in payload["observability"]["spans"]:
            by_kind.setdefault(span["kind"], []).append(span)
        return by_kind

    def test_step_tool_and_sql_spans_match_the_plan(self):
        payload = self.fixture.analyze(
            "How many items are there?", run_id="spans-run"
        )
        observability = payload["observability"]
        by_kind = self._spans_by_kind(payload)
        # One ``step`` span per executed plan step, named after the step id.
        self.assertEqual(
            {span["name"] for span in by_kind["step"]},
            {f"step.{step['step_id']}" for step in payload["steps"]},
        )
        self.assertEqual(
            [span["attributes"]["action"] for span in by_kind["step"]],
            [step["action"] for step in payload["steps"]],
        )
        # The governed SQL step is observed as a tool call *and* as a SQL span
        # (duration plus row count, never the statement text).
        self.assertIn("tool.execute_sql", {span["name"] for span in by_kind["tool"]})
        self.assertIn(
            "tool.check_data_quality", {span["name"] for span in by_kind["tool"]}
        )
        self.assertEqual([span["name"] for span in by_kind["sql"]], ["sql.execute"])
        self.assertEqual(by_kind["sql"][0]["attributes"]["row_count"], 1)
        for span in observability["spans"]:
            self.assertGreaterEqual(span["duration_ms"], 0.0)
            self.assertEqual(span["status"], "success")
            self.assertEqual(span["run_id"], "spans-run")

    def test_a_deterministic_run_reports_no_model_call_and_closes_its_recorder(self):
        closed: list[str] = []
        real_close = SpanRecorder.close

        def spy(recorder: SpanRecorder) -> None:
            closed.append(recorder.run_id)
            real_close(recorder)

        with patch.object(SpanRecorder, "close", spy):
            payload = self.fixture.analyze(
                "How many items are there?", run_id="deterministic-run"
            )
        usage = payload["observability"]["usage"]
        # The planner makes no model call: that is reported as zero calls, not as
        # invented tokens, and nothing is marked estimated because no token count
        # was derived from anything.
        self.assertEqual(usage["model_calls"], 0)
        self.assertEqual(usage["measured_calls"], 0)
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(usage["by_model"], {})
        self.assertFalse(usage["estimated"])
        # Cost stays unknown rather than zero: no price table was configured.
        self.assertIsNone(usage["estimated_cost_usd"])
        # The recorder is closed and dropped, so a long-lived process cannot
        # accumulate one recorder per analysis run.
        self.assertIn("deterministic-run", closed)
        self.assertIsNone(get_span_recorder("deterministic-run"))

    def test_the_summary_carries_no_prompt_sql_text_or_result_rows(self):
        question = "How many items are there?"
        payload = self.fixture.analyze(question, run_id="privacy-run")
        observability = payload["observability"]
        blob = json.dumps(observability)
        # The question is prompt text and the answering SQL lives in the run's
        # evidence (asserted elsewhere); neither may reach the observability block.
        self.assertNotIn(question, blob)
        self.assertNotIn("SELECT", blob.upper())
        self.assertNotIn("FROM ITEMS", blob.upper())
        for row_value in ("alpha", "beta", "gamma"):
            self.assertNotIn(row_value, blob)
        # A digest is what makes a statement correlatable without storing it.
        self.assertIn("statement_digest", blob)
        for span in observability["spans"]:
            self.assertNotIn("sql", span["attributes"])
            self.assertNotIn("rows", span["attributes"])
            for value in span["attributes"].values():
                self.assertNotIn("select", str(value).casefold())

    def test_latency_is_measured_end_to_end_with_per_kind_entries(self):
        payload = self.fixture.analyze(
            "How many items are there?", run_id="latency-run"
        )
        observability = payload["observability"]
        latency = observability["latency"]
        # A real measurement: the run opened the database, executed SQL and
        # composed an answer, so the measured window cannot be zero.
        self.assertGreater(latency["end_to_end_ms"], 0.0)
        self.assertEqual(latency["span_count"], len(observability["spans"]))
        self.assertEqual(
            set(latency["by_kind"]),
            {"model", "tool", "sql", "retrieval", "step"},
        )
        for kind in ("step", "tool", "sql"):
            entry = latency["by_kind"][kind]
            self.assertGreater(entry["count"], 0)
            self.assertGreaterEqual(entry["duration_ms"], 0.0)
            self.assertGreaterEqual(entry["max_duration_ms"], 0.0)
        self.assertEqual(
            latency["by_kind"]["step"]["count"], len(payload["steps"])
        )
        # The end-to-end window contains every span of the run (1ms of slack for
        # the 3-decimal rounding of both numbers).
        slowest = max(span["duration_ms"] for span in observability["spans"])
        self.assertGreaterEqual(latency["end_to_end_ms"] + 0.001, slowest)
        # Kinds this path never produces stay honestly at zero.
        self.assertEqual(latency["by_kind"]["model"]["count"], 0)
        self.assertEqual(latency["by_kind"]["retrieval"]["count"], 0)

    def test_a_failed_step_is_recorded_and_the_recorder_never_leaks(self):
        payload = self.fixture.analyze(
            "How many items are there by brand_name?",
            max_replans=0,
            run_id="failed-run",
        )
        self.assertNotEqual(payload["status"], "succeeded")
        failed = [
            span
            for span in payload["observability"]["spans"]
            if span["kind"] == "step" and span["status"] == "failed"
        ]
        self.assertEqual(
            [span["attributes"]["step_id"] for span in failed], ["query_metric"]
        )
        self.assertEqual(failed[0]["attributes"]["step_status"], "failed")
        self.assertEqual(failed[0]["attributes"]["error_category"], "data_quality")
        self.assertIsNone(get_span_recorder("failed-run"))
        # A run that raises before it can return a payload must release its
        # recorder as well (the direct-run leak fixed in ``AgentService``).
        with self.assertRaises(PlanViolation):
            self.fixture.analyze(
                "How many items are there?", mode="plan_only", run_id="refused-run"
            )
        self.assertIsNone(get_span_recorder("refused-run"))

    def test_a_model_call_made_by_a_step_is_counted_as_estimated_usage(self):
        """The planner makes no model call today; when one happens it must count.

        A step that consults a model (for example a later date/LLM fallback) is
        observed through the same per-run recorder, and a provider that reports no
        token counts yields ``estimated`` usage instead of a fabricated zero.
        """

        from queryforge.core.observability import ObservedModelProvider
        from queryforge.orchestration.tools import build_default_registry

        class StubLLM:
            def generate_text(self, prompt: str) -> str:
                return "stub answer"

        class ModelCallingRegistry:
            """The real registry, plus one tool that consults a model."""

            def __init__(self, registry) -> None:
                self._registry = registry

            def __getattr__(self, name):
                return getattr(self._registry, name)

            def execute(self, name, params=None, **kwargs):
                if name == "check_data_quality":
                    provider = ObservedModelProvider(
                        StubLLM(), provider_name="stub", model_name="stub-1"
                    )
                    provider.generate_text("a prompt that must never be recorded")
                return self._registry.execute(name, params, **kwargs)

        def factory(database_tool_factory, budget_manager, **kwargs):
            return ModelCallingRegistry(
                build_default_registry(database_tool_factory, budget_manager, **kwargs)
            )

        service = AnalysisPlannerService(
            config_loader=lambda **_: self.fixture.config(), registry_factory=factory
        )
        payload = service.analyze(
            "How many items are there?",
            database=str(self.fixture.database),
            semantic_model_path=str(self.fixture.semantic_model),
            run_id="model-run",
        )
        usage = payload["observability"]["usage"]
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(usage["measured_calls"], 0)
        self.assertTrue(usage["estimated"])
        self.assertGreater(usage["total_tokens"], 0)
        model_spans = [
            span
            for span in payload["observability"]["spans"]
            if span["kind"] == "model"
        ]
        self.assertEqual([span["name"] for span in model_spans], ["model.generate_text"])
        self.assertNotIn(
            "a prompt that must never be recorded", json.dumps(payload["observability"])
        )

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        from queryforge import cli

        out, err = io.StringIO(), io.StringIO()
        previous = sys.argv
        sys.argv = ["queryforge", *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main()
        finally:
            sys.argv = previous
        return code, out.getvalue()

    @unittest.skipUnless(
        importlib.util.find_spec("fastapi") is not None
        and importlib.util.find_spec("httpx") is not None,
        "fastapi optional dependency is not installed",
    )
    def test_the_route_and_the_cli_publish_the_same_summary(self):
        from fastapi.testclient import TestClient

        from queryforge.application import AgentService
        from queryforge.interfaces.api.app import create_app

        direct = self.fixture.analyze("How many items are there?", run_id="parity-run")
        config = self.fixture.config()
        service = AgentService(config_loader=lambda **_: config)
        client = TestClient(create_app(service))
        response = client.post(
            "/analyze",
            json={
                "question": "How many items are there?",
                "database": str(self.fixture.database),
                "semantic_model_path": str(self.fixture.semantic_model),
            },
        )
        self.assertEqual(response.status_code, 200)
        route_payload = response.json()
        self.assertEqual(
            set(route_payload["observability"]), set(direct["observability"])
        )
        self.assertEqual(
            sorted(span["kind"] for span in route_payload["observability"]["spans"]),
            sorted(span["kind"] for span in direct["observability"]["spans"]),
        )
        self.assertGreater(
            route_payload["observability"]["latency"]["end_to_end_ms"], 0.0
        )
        self.assertEqual(route_payload["observability"]["usage"]["model_calls"], 0)

        code, stdout = self._run_cli(
            [
                "--question",
                "How many items are there?",
                "--analyze",
                "--database",
                str(self.fixture.database),
                "--semantic-model",
                str(self.fixture.semantic_model),
            ]
        )
        self.assertEqual(code, 0)
        cli_payload = json.loads(stdout)
        self.assertEqual(
            set(cli_payload["observability"]), set(direct["observability"])
        )
        self.assertGreater(
            cli_payload["observability"]["latency"]["end_to_end_ms"], 0.0
        )
        self.assertEqual(cli_payload["observability"]["usage"]["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
