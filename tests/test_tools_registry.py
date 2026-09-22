"""Offline tests for the typed tool registry, budgets, and execution boundary.

Covers step 09 acceptance points: parameter validation and refusal before
execution, permission/mode denial, explicit truncation, typed error
categorization, atomic shared budget under concurrency, unreachable
(unimplemented) tools, and the bounded tool loop's migrated dispatch.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from queryforge.core.schemas.models import Context, SqlTask
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.data_quality_tool import DataQualityTool
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.orchestration.tools import (
    BUDGET_KEYS,
    PLACEHOLDER_TOOLS,
    BudgetLimits,
    BudgetManager,
    SqlDeadlineGuard,
    ToolBudgetError,
    ToolCall,
    ToolContext,
    ToolDenied,
    ToolObservation,
    ToolRegistry,
    ToolSpec,
    ToolUnavailable,
    build_default_registry,
    install_sql_deadline_handler,
    validate_params,
)


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Fixture:
    """Small deterministic SQLite fixture shared by the registry tests."""

    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE items (item_id INTEGER PRIMARY KEY, name TEXT, category TEXT, amount REAL)"
        )
        connection.executemany(
            "INSERT INTO items (item_id, name, category, amount) VALUES (?, ?, ?, ?)",
            [
                (1, "alpha", "a", 10.0),
                (2, "beta", "b", 20.0),
                (3, "gamma", "a", 30.0),
                (4, "delta", None, 40.0),
            ],
        )
        connection.commit()
        connection.close()

    def database_tool(self, **kwargs) -> DatabaseTool:
        return DatabaseTool(SQLiteConnector(str(self.database)), **kwargs)

    def cleanup(self) -> None:
        self.directory.cleanup()


class SpecProtocolTest(unittest.TestCase):
    def test_tool_spec_and_records_validate_their_contract(self):
        spec = ToolSpec(
            name="demo",
            description="demo tool",
            parameter_schema={"type": "object", "properties": {"name": {"type": "string"}}},
            output_schema={"type": "object"},
            permissions=["demo:read"],
            modes=["read", "execute", "plan_only"],
            idempotent=True,
            budget_category="read",
        )
        self.assertTrue(spec.permits("plan_only"))
        self.assertFalse(spec.permits("other"))
        call = ToolCall(tool="demo", params={"name": "x"})
        self.assertEqual(call.status, "pending")
        self.assertTrue(call.id.startswith("tc_"))
        observation = ToolObservation(tool="demo", params={"name": "x"}, result={"ok": True})
        self.assertTrue(observation.ok)
        self.assertEqual(observation.observation_payload()["ok"], True)

    def test_param_validation_rejects_bad_shapes(self):
        schema = {
            "type": "object",
            "properties": {
                "table_name": {"type": "string"},
                "limit": {"type": "integer"},
                "mode": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["table_name"],
            "additionalProperties": False,
        }
        self.assertEqual(
            validate_params("demo", schema, {"table_name": "t", "limit": 5})["limit"], 5
        )
        for bad in (
            {},
            {"table_name": 1},
            {"table_name": "t", "limit": "many"},
            {"table_name": "t", "extra": 1},
            {"table_name": "t", "mode": "z"},
            "not-an-object",
        ):
            with self.assertRaises(ToolDenied):
                validate_params("demo", schema, bad)


class RegistryExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _Fixture()
        self.database_tool = self.fixture.database_tool()
        self.budget = BudgetManager()
        self.registry = build_default_registry(self.database_tool, self.budget)

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_callable_database_tool_factory_resolves_per_call(self):
        """A callable factory is resolved per call, never mistaken for a tool."""

        calls: list[int] = []

        def factory(context=None):  # noqa: ARG001 - bound with and without a context
            calls.append(1)
            return self.database_tool

        registry = build_default_registry(factory, BudgetManager())
        observation = registry.execute("list_tables", {})
        self.assertEqual(observation.status, "succeeded")
        self.assertEqual(observation.result["tables"], ["items"])
        self.assertEqual(len(calls), 1, "the factory resolves once for this call")

        other = self.fixture.database_tool()
        try:
            preferred = registry.execute(
                "list_tables",
                {},
                context=ToolContext(run_id="r", database_tool=other),
            )
            self.assertEqual(preferred.status, "succeeded")
            self.assertEqual(
                len(calls), 1, "a caller-provided tool must win over the factory"
            )
        finally:
            other.connector.close()

        empty = build_default_registry(None)
        failed = empty.execute("list_tables", {})
        self.assertNotEqual(failed.status, "succeeded")
        self.assertEqual(failed.call.status, "failed")

    def test_metadata_and_sql_tools_are_governed_and_traceable(self):
        tables = self.registry.execute("list_tables", {})
        self.assertEqual(tables.status, "succeeded")
        self.assertEqual(tables.result["tables"], ["items"])
        self.assertIsNotNone(tables.call)
        self.assertEqual(tables.call.status, "succeeded")
        self.assertEqual(tables.call.observation_ref, f"obs:{tables.call_id}")

        described = self.registry.execute("describe_table", {"table_name": "items"})
        self.assertEqual(
            [column["name"] for column in described.result["table"]["columns"]],
            ["item_id", "name", "category", "amount"],
        )

        executed = self.registry.execute(
            "execute_sql",
            {
                "sql": "SELECT category, COUNT(*) AS n FROM items "
                "WHERE category IS NOT NULL GROUP BY category ORDER BY category"
            },
        )
        self.assertEqual(executed.result["row_count"], 2)
        self.assertEqual(executed.result["rows"], [["a", 2], ["b", 1]])
        self.assertEqual(executed.result["policy_decision"]["allowed"], True)
        self.assertGreaterEqual(executed.estimated_tokens, 1)

    def test_param_failure_denies_before_the_handler_runs(self):
        calls: list[dict] = []

        def handler(params: dict, context: ToolContext) -> dict:
            calls.append(params)
            return {"ok": True}

        self.registry.register(
            ToolSpec(
                name="strict",
                description="requires an integer count",
                parameter_schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
                modes=["read", "execute"],
            ),
            handler,
        )
        denied = self.registry.execute("strict", {"count": "many"})
        self.assertEqual(denied.status, "denied")
        self.assertIn("invalid_tool_params", denied.call.error)
        self.assertEqual(denied.call.error_category, "unknown")
        self.assertEqual(calls, [])
        self.assertEqual(self.budget.usage.max_tool_calls, 0)

        missing = self.registry.execute("strict", {})
        self.assertEqual(missing.status, "denied")
        self.assertEqual(calls, [])

        accepted = self.registry.execute("strict", {"count": 3})
        self.assertEqual(accepted.status, "succeeded")
        self.assertEqual(calls, [{"count": 3}])

    def test_unsafe_sql_is_a_permission_error_and_sqlite_errors_are_typed(self):
        unsafe = self.registry.execute("execute_sql", {"sql": "DROP TABLE items"})
        self.assertEqual(unsafe.status, "failed")
        self.assertEqual(unsafe.error_category, "permission")

        unknown_table = self.registry.execute("execute_sql", {"sql": "SELECT * FROM missing"})
        self.assertEqual(unknown_table.error_category, "identifier")

        unknown_column = self.registry.execute("describe_table", {"table_name": "missing"})
        self.assertEqual(unknown_column.status, "failed")
        self.assertEqual(unknown_column.error_category, "identifier")

    def test_plan_only_allows_metadata_and_denies_sql_execution(self):
        for tool, params in (
            ("list_tables", {}),
            ("describe_table", {"table_name": "items"}),
        ):
            observation = self.registry.execute(tool, params, mode="plan_only")
            self.assertEqual(observation.status, "succeeded", (tool, observation.call.error))

        for tool, params in (
            ("execute_sql", {"sql": "SELECT * FROM items"}),
            ("preview_sql", {"sql": "SELECT * FROM items"}),
            ("execute_sql_preview", {"sql": "SELECT * FROM items"}),
            ("check_data_quality", {"table_name": "items", "checks": ["grain_unique"]}),
        ):
            denied = self.registry.execute(tool, params, mode="plan_only")
            self.assertEqual(denied.status, "denied", tool)
            self.assertEqual(denied.error_category, "permission", tool)
            self.assertIn("plan_only", denied.call.error)

        # The mode gate really ran nothing: only the metadata calls were billed.
        self.assertEqual(self.budget.usage.max_tool_calls, 2)
        self.assertEqual(self.budget.usage.max_sql_duration_ms, 0)

    def test_permissions_and_domain_scope_are_enforced(self):
        context = ToolContext(run_id="run-1", granted_permissions=frozenset({"demo:read"}))
        self.registry.register(
            ToolSpec(
                name="secret",
                description="needs a stronger permission",
                parameter_schema={"type": "object", "properties": {}},
                permissions=["demo:admin"],
                modes=["read", "execute"],
            ),
            lambda params, ctx: {"ok": True},
        )
        denied = self.registry.execute("secret", {}, context=context)
        self.assertEqual(denied.status, "denied")
        self.assertEqual(denied.error_category, "permission")

        granted = ToolContext(run_id="run-2", granted_permissions=frozenset({"demo:admin"}))
        self.assertEqual(self.registry.execute("secret", {}, context=granted).status, "succeeded")

        # A caller that declares permissions must declare SQL execution too.
        sql_context = ToolContext(run_id="run-3", granted_permissions=frozenset({"demo:read"}))
        blocked = self.registry.execute(
            "execute_sql", {"sql": "SELECT * FROM items"}, context=sql_context
        )
        self.assertEqual(blocked.error_category, "permission")

        scoped = ToolContext(run_id="run-4", domain_id="domain_a")
        mismatch = self.registry.execute(
            "list_tables", {"domain_id": "domain_b"}, context=scoped
        )
        self.assertEqual(mismatch.status, "denied")
        self.assertIn("domain_b", mismatch.call.error)

    def test_unknown_tools_are_denied_and_step11_tools_are_implemented(self):
        """Genuinely unknown tools are denied; step 11 tools are real (step 11)."""

        unknown = self.registry.execute("no_such_tool", {})
        self.assertEqual(unknown.status, "denied")
        self.assertEqual(unknown.error_category, "unknown")

        # Step 11 replaced the declared placeholders with real handlers, so the
        # planner's `is_available` gate must now report them as implemented.
        self.assertEqual(PLACEHOLDER_TOOLS, ())
        for tool in (
            "compare_periods",
            "drill_down",
            "calculate_contribution",
            "detect_anomaly",
            "render_chart",
        ):
            self.assertTrue(self.registry.has(tool), tool)
            self.assertTrue(self.registry.is_available(tool), tool)

        # A real computation runs without any governed connection.
        comparison = self.registry.execute(
            "compare_periods", {"current": 80, "baseline": 100}
        )
        self.assertEqual(comparison.status, "succeeded")
        self.assertEqual(comparison.result["delta"], -20)
        self.assertAlmostEqual(comparison.result["relative_change"], -0.2)

        # A call with no usable inputs is refused as unsupported, not silently passed.
        empty = self.registry.execute("compare_periods", {})
        self.assertIn(empty.status, {"denied", "failed"})

    def test_results_over_row_and_byte_caps_are_explicitly_truncated(self):
        budget = BudgetManager(
            limits={"max_output_rows": 2, "max_output_bytes": 260},
            per_call={"max_output_rows": 2, "max_output_bytes": 260},
        )
        registry = build_default_registry(self.fixture.database_tool(), budget)
        observation = registry.execute("execute_sql", {"sql": "SELECT * FROM items"})
        self.assertEqual(observation.status, "succeeded")
        self.assertTrue(observation.truncated)
        self.assertIn(observation.truncation["reason"], {"max_output_rows", "max_output_bytes"})
        payload = observation.observation_payload()
        self.assertTrue(payload["truncated"])
        returned = payload.get("rows") or []
        self.assertLessEqual(len(returned), 2)
        self.assertLess(observation.truncation["limit"] + 1, 2**31)

        # A payload that is too large even without rows is cut, not passed on.
        big_budget = BudgetManager(limits={"max_output_bytes": 200}, per_call={"max_output_bytes": 200})
        big_registry = build_default_registry(self.fixture.database_tool(), big_budget)
        big_registry.register(
            ToolSpec(
                name="big_text",
                description="returns one huge string",
                parameter_schema={"type": "object", "properties": {}},
                modes=["read", "execute"],
            ),
            lambda params, ctx: {"text": "x" * 5_000, "columns": ["text"]},
        )
        wide = big_registry.execute("big_text", {})
        self.assertTrue(wide.truncated)
        self.assertEqual(wide.truncation["reason"], "max_output_bytes")
        self.assertIn("truncated_json_prefix", wide.result)
        self.assertNotIn("text", wide.result)

    def test_budget_exhaustion_is_typed_and_stops_further_calls(self):
        budget = BudgetManager(limits={"max_tool_calls": 2}, per_call={"max_tool_calls": 1})
        registry = build_default_registry(self.fixture.database_tool(), budget)
        self.assertEqual(registry.execute("list_tables", {}).status, "succeeded")
        self.assertEqual(registry.execute("list_tables", {}).status, "succeeded")
        refused = registry.execute("list_tables", {})
        self.assertEqual(refused.status, "denied")
        self.assertEqual(refused.error_category, "budget")
        self.assertIsInstance(refused.call, ToolCall)
        self.assertEqual(budget.usage.max_tool_calls, 2)

    def test_data_quality_tool_is_wrapped_not_reimplemented(self):
        observation = self.registry.execute(
            "check_data_quality",
            {"table_name": "items", "checks": ["grain_unique", "null_rate"]},
        )
        self.assertEqual(observation.status, "succeeded")
        self.assertEqual(observation.result["table"], "items")
        # null_rate warns because "category" is a quarter NULL in this fixture.
        self.assertEqual(observation.result["status"], "warning")
        self.assertEqual(
            observation.result["counts"], {"ok": 1, "warning": 1, "error": 0, "unknown": 0}
        )
        self.assertEqual(observation.result["requested_checks"], ["grain_unique", "null_rate"])

        bad_option = self.registry.execute(
            "check_data_quality",
            {"table_name": "items", "checks": ["grain_unique"], "options": {"nope": 1}},
        )
        self.assertEqual(bad_option.status, "failed")
        self.assertIn("nope", bad_option.call.error)

        # Same implementation as the step-08 tool, not a second code path.
        report = DataQualityTool(self.fixture.database_tool()).check(
            "items", ["grain_unique", "null_rate"]
        )
        self.assertEqual(report.status, observation.result["status"])


class BudgetAtomicityTest(unittest.TestCase):
    def test_two_threads_cannot_exceed_the_last_remaining_allowance(self):
        manager = BudgetManager(
            limits={"max_tool_calls": 1}, per_call={"max_tool_calls": 1}
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                reservation = manager.reserve(category="sql")
            except ToolBudgetError:
                with lock:
                    outcomes.append("refused")
                return
            time.sleep(0.02)
            reservation.settle(max_tool_calls=1)
            with lock:
                outcomes.append("granted")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(outcomes), ["granted", "refused"])
        self.assertEqual(manager.usage.max_tool_calls, 1)
        self.assertEqual(manager.remaining("max_tool_calls"), 0)
        self.assertGreaterEqual(manager.remaining("max_estimated_tokens"), 0)

    def test_parallel_reservations_never_exceed_a_shared_cap(self):
        manager = BudgetManager(limits={"max_tool_calls": 12}, per_call={"max_tool_calls": 4})
        granted: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(5):
                try:
                    reservation = manager.reserve(category="tool")
                except ToolBudgetError:
                    continue
                reservation.settle(max_tool_calls=1, max_output_rows=1)
                with lock:
                    granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(granted), 12)
        self.assertEqual(manager.usage.max_tool_calls, 12)
        self.assertEqual(manager.remaining("max_tool_calls"), 0)

    def test_settle_releases_unused_reservation_and_deadline_is_computed(self):
        manager = BudgetManager(
            limits={
                "max_tool_calls": 5,
                "max_estimated_tokens": 100,
                "model_deadline_ms": 60_000,
            },
            clock=lambda: 0.0,
        )
        self.assertAlmostEqual(manager.deadline_seconds(), 60.0, places=3)
        reservation = manager.reserve(category="sql", estimated_tokens=60, sql_duration_ms=0)
        reservation.settle(max_estimated_tokens=10, max_sql_duration_ms=250)
        self.assertEqual(manager.usage.max_estimated_tokens, 10)
        self.assertEqual(manager.usage.max_sql_duration_ms, 250)
        self.assertAlmostEqual(manager.remaining("max_estimated_tokens"), 90)

        expired = BudgetManager(
            limits={"model_deadline_ms": 1_000}, clock=lambda: 5.0, started_at=0.0
        )
        self.assertEqual(expired.deadline_seconds(), 0.0)
        with self.assertRaises(ToolBudgetError):
            expired.reserve()

    def test_sql_deadline_handler_is_removable(self):
        connection = sqlite3.connect(":memory:")
        guard = install_sql_deadline_handler(connection, time.monotonic() - 1)
        self.assertIsInstance(guard, SqlDeadlineGuard)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c"
            )
        guard.restore()
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        connection.close()

        # A connection that cannot install a handler degrades to a no-op guard.
        class NoHandler:
            pass

        noop = install_sql_deadline_handler(NoHandler(), time.monotonic())
        self.assertFalse(noop.installed)
        noop.restore()

        self.assertEqual(set(BudgetLimits().as_dict()), set(BUDGET_KEYS))
        self.assertEqual(EmptyParams().model_dump(), {})


class ToolLoopRegistryIntegrationTest(unittest.TestCase):
    def test_tool_loop_records_typed_calls_on_task_context(self):
        from queryforge.workflow.node.tool_loop_node import ToolLoopNode

        class LoopLLM:
            def __init__(self, actions: list[dict]) -> None:
                self.actions = list(actions)

            def generate_json(self, prompt: str) -> dict:
                if self.actions:
                    return self.actions.pop(0)
                return {
                    "action": "final_answer",
                    "params": {"sql": "SELECT name FROM items", "explanation": "names"},
                }

        fixture = _Fixture()
        try:
            context = Context(
                task=SqlTask(question="list names", database_path=str(fixture.database)),
                run_id="run-tool-loop",
            )
            budget = BudgetManager()
            node = ToolLoopNode(
                LoopLLM(
                    [
                        {"action": "list_tables", "params": {}},
                        {"action": "execute_sql_preview", "params": {"sql": "SELECT name FROM items"}},
                        {"action": "final_answer", "params": {"sql": "SELECT name FROM items"}},
                    ]
                ),
                fixture.database_tool(),
                max_rounds=3,
                budget_manager=budget,
            )
            result = node.execute(context)
            self.assertTrue(result.success)
            self.assertEqual(context.tool_loop_status, "completed")

            recorded = context.task_context["tool_calls"]
            self.assertEqual(len(recorded), 3)
            self.assertEqual(recorded[0]["call"]["tool"], "list_tables")
            self.assertEqual(recorded[0]["call"]["run_id"], "run-tool-loop")
            self.assertEqual(recorded[0]["observation"]["status"], "succeeded")
            self.assertEqual(recorded[1]["call"]["tool"], "execute_sql_preview")
            self.assertEqual(recorded[2]["status"], "succeeded")
            self.assertEqual(recorded[2]["local"], True)
            # Every registry call is journaled and billed once.
            self.assertEqual(budget.usage.max_tool_calls, 2)
            self.assertEqual(len(node.registry.journal), 2)
            json.dumps(context.task_context["tool_calls"])

            invalid_context = Context(
                task=SqlTask(question="list names", database_path=str(fixture.database)),
                run_id="run-invalid",
            )
            invalid = ToolLoopNode(LoopLLM([{"action": "drop_table", "params": {}}]), fixture.database_tool())
            invalid.execute(invalid_context)
            self.assertEqual(invalid_context.tool_loop_exit_reason, "invalid_action")
            self.assertEqual(invalid_context.tool_loop_status, "error")
            self.assertEqual(
                invalid_context.task_context["tool_calls"][0]["observation"]["status"],
                "denied",
            )
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
