import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as cli
from queryforge.workflow.workflow import WorkflowError
from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.agents.entry_router import EntryRouterAgent, TaskRoute
from queryforge.orchestration.orchestrator.pipeline_registry import (
    pipeline_for,
    register_pipeline,
)
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.orchestrator.orchestrator import OrchestratorAgent
from queryforge.orchestration.schemas import RoutingDecision, TaskState, TaskStatus
from queryforge.core.config import Config
from queryforge.core.schemas.models import (
    Context,
    ExecutionResult,
    ReflectionResult,
    SQLContext,
    SqlTask,
)
from queryforge.domain.semantic import (
    SemanticEntity,
    SemanticMetric,
    SemanticModel,
    SemanticModelContext,
)
from queryforge.application import AgentOptions, AgentService


class RecordingRunner:
    def __init__(self, config, **kwargs):
        self.config = config
        self.kwargs = kwargs
        self.calls = []

    def run(self, task):
        self.calls.append(task)
        run_id = self.kwargs["run_id_factory"]()
        return {
            "status": "success",
            "run_id": run_id,
            "question": task.question,
            "sql": "SELECT name FROM items",
            "explanation": "List item names.",
            "columns": ["name"],
            "rows": [["alpha"]],
            "row_count": 1,
        }


class AgentTeamRouterOrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('alpha')")
        connection.commit()
        connection.close()
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
        )
        self.runners = []

    def tearDown(self):
        self.directory.cleanup()

    def runner_factory(self, config, **kwargs):
        runner = RecordingRunner(config, **kwargs)
        self.runners.append(runner)
        return runner

    def service(self):
        return AgentService(
            config_loader=lambda **_: self.config,
            runner_factory=self.runner_factory,
        )

    def real_service(self):
        class RoleAgentLLM:
            def generate_json(inner_self, prompt):
                if "Evaluate whether the SQL and result" in prompt:
                    return {
                        "success": True,
                        "strategy": "SUCCESS",
                        "reason": "The result answers the question.",
                        "suggested_fix": None,
                    }
                return {
                    "sql": "SELECT name FROM items ORDER BY name",
                    "explanation": "List item names.",
                    "tables_used": ["items"],
                }

        return AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: RoleAgentLLM(),
        )

    def options(self, **overrides):
        values = {
            "database": str(self.database),
            "skills": [],
            "run_id": "qf_agent_team_test",
            "orchestration_state_root": str(self.root / ".queryforge" / "runs"),
        }
        values.update(overrides)
        return AgentOptions(**values)

    def test_router_classifies_required_task_types_without_execution_dependencies(self):
        router = EntryRouterAgent()
        cases = {
            "Show total sales by month": "ask_sql",
            "Review SQL: SELECT * FROM items": "sql_review",
            "SQL error: no such column": "troubleshoot_sql",
            "Explain result from the previous run": "explain_result",
            "Build report for monthly sales": "build_report",
            "List tables in this database": "metadata_query",
            "...": "unknown",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                decision = router.route(question, "cli")
                self.assertEqual(decision.task_type, expected)
                self.assertEqual(decision.entrypoint, "cli")

    def test_router_cjk_markers_are_whitespace_insensitive(self):
        """D-3: the Chinese marker tables were internally inconsistent.

        ``route`` normalises whitespace with ``" ".join(text.split())``, which
        collapses runs but cannot remove a single space inside a Chinese phrase.
        ``_SQL_REVIEW_MARKERS`` / ``_TROUBLESHOOT_MARKERS`` therefore spelled each
        Chinese marker twice ("审核sql" and "审核 sql") while ``_REPORT_MARKERS``,
        ``_METADATA_MARKERS`` and ``_EXPLAIN_MARKERS`` listed only the unspaced
        form — so "生成 报告" fell through to the default ``ask_sql`` while
        "审核 sql" worked. Marker matching is now whitespace-tolerant for every
        marker, so no table can drift again.
        """
        router = EntryRouterAgent()
        cases = {
            "生成报告": "build_report",
            "生成 报告": "build_report",
            "生成  报告": "build_report",
            "制作 报告": "build_report",
            "生成 看板": "build_report",
            "表结构": "metadata_query",
            "表 结构": "metadata_query",
            "指标 列表": "metadata_query",
            "有哪些表": "metadata_query",
            "为什么": "explain_result",
            "为 什么": "explain_result",
            "解释 结果": "explain_result",
            "审核sql": "sql_review",
            "审核 sql": "sql_review",
            "审 核 sql": "sql_review",
            "修复 sql": "troubleshoot_sql",
            "sql 报错": "troubleshoot_sql",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(router.route(question).task_type, expected)

    def test_router_latin_markers_still_classify_as_before(self):
        """The whitespace-tolerant matcher must not disturb Latin behaviour."""
        router = EntryRouterAgent()
        cases = {
            "generate report": "build_report",
            "create report": "build_report",
            "dashboard": "build_report",
            "show tables": "metadata_query",
            "database schema": "metadata_query",
            "explain the output": "explain_result",
            "review sql": "sql_review",
            "fix this sql": "troubleshoot_sql",
            "how many orders were placed": "ask_sql",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(router.route(question).task_type, expected)

    def test_a_keyword_inside_an_identifier_does_not_route_the_request(self):
        """E-08: "report" matched "sales_report" and hijacked the whole request.

        Marker matching is substring-based, so a bare keyword appearing inside a
        table or column name was enough to classify the request: "Show the first 10
        rows of sales_report" became a report-building task, and the run skipped
        straight to report generation instead of answering the question. ASCII
        markers now match on a word boundary; CJK is unaffected because it has no
        such boundary.
        """
        router = EntryRouterAgent()
        cases = {
            "Show the first 10 rows of sales_report": "ask_sql",
            "显示 sales_report 的前 10 行": "ask_sql",
            "List rows from a metadata_table": "ask_sql",
            "How many rows are in the explain_log": "ask_sql",
            # Genuine report requests must still route as before.
            "build report for monthly sales": "build_report",
            "generate report": "build_report",
            "dashboard": "build_report",
            "生成报告": "build_report",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(router.route(question).task_type, expected)

    def test_router_assigns_simple_and_complex_execution_profiles(self):
        router = EntryRouterAgent()
        simple = router.route("List item names")
        complex_request = router.route(
            "Compare monthly revenue by region and product category with top ranking"
        )
        self.assertEqual(simple.complexity_profile, "simple")
        self.assertEqual(simple.complexity_score, 0)
        self.assertEqual(complex_request.complexity_profile, "complex")
        self.assertGreaterEqual(complex_request.complexity_score, 2)
        self.assertTrue(complex_request.complexity_reasons)

    def test_sql_review_checks_explicit_governed_metric_aliases(self):
        semantic = SemanticModelContext(
            source_path="test.yml",
            model=SemanticModel(
                name="test",
                entities=[SemanticEntity(name="item", table="items")],
                metrics=[
                    SemanticMetric(
                        name="revenue",
                        description="Total amount.",
                        entity="item",
                        aggregation="sum",
                        expression="SUM(items.amount)",
                    )
                ],
            ),
        )
        from queryforge.orchestration.agents.sql_review import SQLReviewAgent
        import sqlglot

        payload, findings = SQLReviewAgent._semantic_findings(
            sqlglot.parse_one("SELECT COUNT(*) AS revenue FROM items"),
            semantic,
        )
        self.assertTrue(payload["checked"])
        self.assertEqual(payload["matched_metrics"], [])
        self.assertEqual(findings[0]["rule"], "semantic_metric_contract")

    def test_router_and_pipeline_support_deployment_specific_task_types(self):
        register_pipeline(
            "data_contract_check",
            ("product_analyst", "schema_architect", "delivery"),
        )
        router = EntryRouterAgent(
            extra_routes=(
                TaskRoute(
                    task_type="data_contract_check",
                    markers=("validate data contract",),
                    priority=100,
                ),
            )
        )
        decision = router.route("Validate data contract for retail")
        self.assertEqual(decision.task_type, "data_contract_check")
        self.assertEqual(
            pipeline_for(decision.task_type),
            ("product_analyst", "schema_architect", "delivery"),
        )

    def test_auto_complex_profile_enables_the_tool_loop_but_not_extra_candidates(self):
        """Complexity routing boosts the tool loop only.

        It used to also raise parallel_candidates to 2. A controlled ablation over
        identical inputs found zero accuracy benefit for the second candidate with
        ~20% higher p50 latency (docs/evaluation_baselines.md), and the boost was
        often wasted anyway because the tool loop short-circuits the candidate
        node. Candidates are now opt-in via the caller.
        """
        service = self.service()
        service.ask("List item names", self.options(run_id="simple_profile"))
        self.assertFalse(self.runners[-1].kwargs["tool_loop_enabled"])
        self.assertEqual(self.runners[-1].kwargs["parallel_candidates"], 1)

        service.ask(
            "Compare monthly revenue by region and product category with top ranking",
            self.options(run_id="complex_profile"),
        )
        self.assertTrue(self.runners[-1].kwargs["tool_loop_enabled"])
        # Not raised implicitly — see the docstring.
        self.assertEqual(self.runners[-1].kwargs["parallel_candidates"], 1)

        service.ask(
            "List item names",
            self.options(run_id="forced_simple", complexity_mode="simple"),
        )
        self.assertFalse(self.runners[-1].kwargs["tool_loop_enabled"])
        self.assertEqual(self.runners[-1].kwargs["parallel_candidates"], 1)

        service.ask(
            "List item names",
            self.options(run_id="forced_complex", complexity_mode="complex"),
        )
        self.assertTrue(self.runners[-1].kwargs["tool_loop_enabled"])
        self.assertEqual(self.runners[-1].kwargs["parallel_candidates"], 1)

    def test_an_explicit_candidate_count_is_still_honoured(self):
        """The opt-in path must survive: a caller that wants candidates gets them."""
        service = self.service()
        service.ask(
            "Compare monthly revenue by region and product category with top ranking",
            self.options(run_id="explicit_candidates", parallel_candidates=2),
        )
        self.assertEqual(self.runners[-1].kwargs["parallel_candidates"], 2)
        self.assertTrue(self.runners[-1].kwargs["tool_loop_enabled"])

    def test_pipeline_is_declarative_and_contains_expected_ask_sql_roles(self):
        self.assertEqual(
            pipeline_for("ask_sql"),
            ("analysis", "candidate", "execution", "completion", "delivery"),
        )
        self.assertEqual(
            pipeline_for("metadata_query"),
            ("analysis", "delivery"),
        )
        self.assertEqual(
            pipeline_for("sql_review"),
            ("analysis", "candidate", "review", "completion", "delivery"),
        )

    def test_default_service_path_always_uses_orchestrator(self):
        output = self.service().ask(
            "List item names",
            self.options(entrypoint="api"),
        )
        self.assertEqual(output["rows"], [["alpha"]])
        self.assertEqual(output["run_id"], "qf_agent_team_test")
        self.assertEqual(output["agent_team"]["task_type"], "ask_sql")
        self.assertEqual(len(self.runners[0].calls), 1)
        self.assertIn("analysis_hook", self.runners[0].kwargs)
        self.assertIn("candidate_hook", self.runners[0].kwargs)
        self.assertIn("completion_hook", self.runners[0].kwargs)

        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["classification"]["entrypoint"], "api")
        self.assertEqual(state["workflow_run_id"], "qf_agent_team_test")
        self.assertEqual(state["completed_phases"], ["routing", "delivery"])
        self.assertTrue((state_path.parent / "artifacts").is_dir())
        self.assertTrue((state_path.parent / "logs").is_dir())
        artifact_types = [item["artifact_type"] for item in state["artifacts"]]
        self.assertEqual(artifact_types, ["routing_decision", "delivery_report"])

    def test_sql_review_uses_static_review_path_without_workflow_runner(self):
        output = self.service().ask(
            "Review SQL: SELECT name FROM items",
            self.options(entrypoint="mcp"),
        )
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["agent_team"]["task_type"], "sql_review")
        self.assertEqual(self.runners, [])
        self.assertFalse(output["executed"])
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact_types = [artifact["artifact_type"] for artifact in state["artifacts"]]
        self.assertIn("review_report", artifact_types)
        self.assertIn("sql_candidate", artifact_types)
        self.assertIn("governance_report", artifact_types)

    def test_metadata_query_uses_fast_path_without_workflow_runner(self):
        output = self.service().ask(
            "List tables in this database",
            self.options(entrypoint="api"),
        )
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["agent_team"]["task_type"], "metadata_query")
        self.assertEqual(self.runners, [])
        self.assertIn("metadata", output)
        self.assertEqual(
            [table["name"] for table in output["metadata"]["tables"]],
            ["items"],
        )
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact_types = [artifact["artifact_type"] for artifact in state["artifacts"]]
        self.assertIn("schema_plan", artifact_types)
        self.assertIn("knowledge_context", artifact_types)
        self.assertNotIn("sql_candidate", artifact_types)

    def test_real_workflow_emits_role_artifacts_at_integrated_lifecycle_points(self):
        output = self.real_service().ask(
            "List item names",
            self.options(entrypoint="api"),
        )
        self.assertEqual(output["rows"], [["alpha"]])
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact_types = [artifact["artifact_type"] for artifact in state["artifacts"]]
        self.assertEqual(state["pending_phases"], [])
        for phase in (
            "analysis",
            "candidate",
            "execution",
            "completion",
            "delivery",
        ):
            self.assertIn(phase, state["completed_phases"])
        for artifact_type in (
            "analysis_request",
            "knowledge_context",
            "schema_plan",
            "sql_candidate",
            "governance_report",
            "qa_report",
            "visualization_artifact",
            "ops_report",
            "delivery_report",
        ):
            self.assertIn(artifact_type, artifact_types)
        self.assertLess(
            artifact_types.index("schema_plan"),
            artifact_types.index("sql_candidate"),
        )
        self.assertLess(
            artifact_types.index("governance_report"),
            artifact_types.index("qa_report"),
        )
        governance_ref = next(
            item for item in state["artifacts"]
            if item["artifact_type"] == "governance_report"
        )
        governance = json.loads(
            (state_path.parent / governance_ref["path"]).read_text(encoding="utf-8")
        )
        self.assertTrue(governance["payload"]["allowed"])
        self.assertTrue(governance["payload"]["checked_before_execution"])
        qa_ref = next(
            item for item in state["artifacts"] if item["artifact_type"] == "qa_report"
        )
        qa = json.loads(
            (state_path.parent / qa_ref["path"]).read_text(encoding="utf-8")
        )
        self.assertTrue(qa["payload"]["passed"])
        analysis_ref = next(
            item for item in state["artifacts"]
            if item["artifact_type"] == "analysis_request"
        )
        analysis = json.loads(
            (state_path.parent / analysis_ref["path"]).read_text(encoding="utf-8")
        )
        self.assertNotIn("sql", analysis["payload"])
        self.assertFalse(any(name.startswith("todo_") for name in artifact_types))

    def test_ambiguous_analysis_warning_is_exposed_in_agent_metadata(self):
        output = self.real_service().ask(
            "Show top item names",
            self.options(entrypoint="api"),
        )
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        analysis_ref = next(
            item for item in state["artifacts"]
            if item["artifact_type"] == "analysis_request"
        )
        analysis = json.loads(
            (state_path.parent / analysis_ref["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(analysis_ref["status"], "warning")
        self.assertTrue(analysis["payload"]["clarification_needed"])
        self.assertIn("missing_ranking_dimension", analysis["payload"]["ambiguities"])
        self.assertTrue(output["agent_team"]["warnings"])

    def test_schema_plan_contains_decision_fields(self):
        output = self.real_service().ask(
            "List item names",
            self.options(entrypoint="api"),
        )
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        schema_ref = next(
            item for item in state["artifacts"]
            if item["artifact_type"] == "schema_plan"
        )
        schema_plan = json.loads(
            (state_path.parent / schema_ref["path"]).read_text(encoding="utf-8")
        )
        payload = schema_plan["payload"]
        self.assertIn("primary_tables", payload)
        self.assertIn("recommended_fields", payload)
        self.assertIn("risks", payload)
        self.assertEqual(payload["primary_tables"][0]["table_name"], "items")

    def test_blocked_analysis_stops_before_sql_generation(self):
        output = self.real_service().ask(
            ".",
            self.options(entrypoint="api"),
        )
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(output["agent_team"]["blocked_phase"], "analysis")
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["blocked_phase"], "analysis")
        artifact_types = [artifact["artifact_type"] for artifact in state["artifacts"]]
        self.assertIn("analysis_request", artifact_types)
        self.assertNotIn("sql_candidate", artifact_types)
        self.assertEqual(output["delivery_report"]["status"], "degraded")

    def test_artifact_schema_failure_degrades_without_blocking_emit(self):
        class BrokenAnalysisAgent(RoleAgent):
            agent_name = "BrokenAnalysisAgent"
            artifact_type = "analysis_request"

        state = TaskState(
            run_id="artifact_schema_degrade",
            entrypoint="test",
            classification=RoutingDecision(
                task_type="ask_sql",
                entrypoint="test",
                confidence=1,
                reason="test",
                pipeline="ask_sql",
            ),
            status="running",
        )
        store = AgentTeamStateStore(self.root / ".queryforge" / "runs")
        store.initialize(state)
        reference = BrokenAnalysisAgent(store).emit(
            state,
            {"objective": "missing required question"},
        )
        self.assertEqual(reference.status, "degraded")
        artifact = json.loads(
            (store.run_dir(state.run_id) / reference.path).read_text(encoding="utf-8")
        )
        self.assertIn("artifact_schema_error", artifact["payload"])

    def test_cancelled_run_state_round_trips_through_the_reader(self):
        """A cancelled run must be readable: `cancelled` is part of TaskStatus."""
        from queryforge.application.agent_service import persist_cancelled_outcome

        state = TaskState(
            run_id="cancel_round_trip",
            entrypoint="test",
            classification=RoutingDecision(
                task_type="ask_sql",
                entrypoint="test",
                confidence=1,
                reason="test",
                pipeline="ask_sql",
            ),
            status="running",
        )
        root = self.root / ".queryforge" / "runs"
        store = AgentTeamStateStore(root)
        store.initialize(state)

        persisted = persist_cancelled_outcome(
            state_root=root, run_id=state.run_id, reason="client disconnected"
        )
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["status"], "cancelled")

        # The document the streaming cancel path wrote validates back into the
        # schema a reader uses, instead of failing on an unknown status.
        loaded = store.load_state(state.run_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, "cancelled")
        self.assertEqual(loaded.current_phase, "cancelled")
        self.assertEqual(loaded.last_error, "client disconnected")
        self.assertIn("cancelled", TaskStatus.__args__)
        # A second cancel never rewrites the terminal outcome.
        self.assertIsNone(
            persist_cancelled_outcome(
                state_root=root, run_id=state.run_id, reason="late cancel"
            )
        )
        self.assertEqual(store.load_state(state.run_id).status, "cancelled")
        # A run that was never persisted reads as "no state", not as an error.
        self.assertIsNone(store.load_state("missing_run"))

    def test_a_governance_blocked_run_is_not_rewritten_by_a_late_cancel(self):
        """H7: `blocked` is a terminal outcome, so a cancel must not replace it."""
        from queryforge.application.agent_service import persist_cancelled_outcome

        root = self.root / ".queryforge" / "runs"
        store = AgentTeamStateStore(root)

        def blocked_state(run_id: str) -> TaskState:
            return TaskState(
                run_id=run_id,
                entrypoint="test",
                classification=RoutingDecision(
                    task_type="ask_sql",
                    entrypoint="test",
                    confidence=1,
                    reason="test",
                    pipeline="ask_sql",
                ),
                status="blocked",
                current_phase="governance",
                blocked_phase="governance",
                blocked_reason="governance policy blocked the query",
                last_error=(
                    "column 'dim_user.email' is outside the allowed column scope"
                ),
            )

        state = blocked_state("blocked_then_cancelled")
        store.initialize(state)
        before = json.loads(
            (store.run_dir(state.run_id) / "state.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(
            persist_cancelled_outcome(
                state_root=root,
                run_id=state.run_id,
                reason="Client disconnected.",
            )
        )
        after = json.loads(
            (store.run_dir(state.run_id) / "state.json").read_text(encoding="utf-8")
        )
        # The governance outcome and its error text survive the disconnect: the
        # run ended as blocked and stays blocked.
        self.assertEqual(after, before)
        loaded = store.load_state(state.run_id)
        self.assertEqual(loaded.status, "blocked")
        self.assertEqual(
            loaded.last_error,
            "column 'dim_user.email' is outside the allowed column scope",
        )
        self.assertEqual(loaded.blocked_reason, "governance policy blocked the query")
        self.assertEqual(loaded.current_phase, "governance")

        # The same first-writer-wins rule covers every terminal status; a run that
        # is still running is the only one a cancellation may claim.
        for status in ("completed", "failed", "cancelled", "needs_clarification"):
            with self.subTest(status=status):
                terminal = blocked_state(f"terminal_{status}")
                terminal.status = status
                terminal.last_error = f"the run ended as {status}"
                store.initialize(terminal)
                self.assertIsNone(
                    persist_cancelled_outcome(
                        state_root=root,
                        run_id=terminal.run_id,
                        reason="late cancel",
                    )
                )
                reloaded = store.load_state(terminal.run_id)
                self.assertEqual(reloaded.status, status)
                self.assertEqual(reloaded.last_error, f"the run ended as {status}")

        running = blocked_state("still_running")
        running.status = "running"
        store.initialize(running)
        persisted = persist_cancelled_outcome(
            state_root=root, run_id=running.run_id, reason="client disconnected"
        )
        self.assertIsNotNone(persisted)
        self.assertEqual(store.load_state(running.run_id).status, "cancelled")

    def test_a_recorded_gate_block_is_not_overwritten_by_a_stale_result_status(self):
        """E-02 regression: the QA gate's block must survive the delivery path.

        The completion hook runs *after* ``ReflectiveWorkflow.run()`` has already
        returned its output, so the workflow's result dict can still say "success"
        while a gate has blocked the run. The orchestrator used to re-derive the
        status from that stale dict and write ``completed``, leaving ``state.json``
        saying ``blocked`` (with a recorded reason) and the caller being told the
        run succeeded.

        This test drives the real gate and the real derivation rather than
        asserting the mapping in isolation.
        """
        from queryforge.core.outcomes import derive_outcome
        from queryforge.orchestration.agents.data_qa import DataQAAgent
        from queryforge.orchestration.orchestrator.orchestrator import (
            _DELIVERY_STATUS_FOR_OUTCOME,
            _TASK_STATUS_FOR_OUTCOME,
        )

        root = self.root / ".queryforge" / "runs"
        store = AgentTeamStateStore(root)
        state = TaskState(
            run_id="gate_block_survives_delivery",
            entrypoint="test",
            classification=RoutingDecision(
                task_type="ask_sql",
                entrypoint="test",
                confidence=1,
                reason="test",
                pipeline="ask_sql",
            ),
            status="running",
        )
        store.initialize(state)

        # A QA report the deterministic gate must treat as blocking.
        DataQAAgent(store).emit(
            state,
            {
                "passed": False,
                "row_count": 3,
                "columns": ["a"],
                "row_count_consistent": False,
                "answers_question": False,
                "empty_result": False,
                "issues": [
                    {
                        "rule": "row_count_mismatch",
                        "severity": "error",
                        "reason": "row_count does not match the returned rows",
                    }
                ],
                "quality_checks": [],
                "quality_status": "skipped",
                "reflection": None,
                "retry_recommendation": "REGENERATE",
                "sql_attempts": [],
            },
        )

        orchestrator = OrchestratorAgent(store)
        context = Context(
            task=SqlTask(question="q", database_path="/tmp/x.sqlite"),
            run_id=state.run_id,
            sql_context=SQLContext(sql="SELECT 1", explanation="e", tables_used=[]),
            execution_result=ExecutionResult(
                columns=["a"], rows=[[1], [2]], row_count=3
            ),
            reflection_result=ReflectionResult(
                success=True, strategy="SUCCESS", reason="ok"
            ),
        )
        context.final_output = {"status": "success", "run_id": state.run_id}
        state.pending_phases = ["completion"]
        orchestrator._completion_hook(state)(context)

        self.assertEqual(state.blocked_reason, "qa_report found severe data quality issue")
        self.assertEqual(state.status, "blocked")

        # The workflow still reports its earlier, pre-hook status.
        stale_result = {"status": "success", "run_id": state.run_id}
        outcome = derive_outcome(
            result_status=stale_result.get("status"),
            blocked_reason=state.blocked_reason,
        )
        self.assertEqual(outcome, "blocked")
        self.assertEqual(_TASK_STATUS_FOR_OUTCOME[outcome], "blocked")
        # A block is reported as degraded delivery, never as success.
        self.assertEqual(_DELIVERY_STATUS_FOR_OUTCOME[outcome], "degraded")

    def test_run_context_carries_identity_and_versions(self):
        """feat-008: one object holds run identity and the versions it depends on."""
        from queryforge.core.schemas.models import RunContext

        context = Context(
            task=SqlTask(question="q", database_path="/tmp/x.sqlite"),
            run_id="qf_identity",
        )
        context.task_context["retrieval_scope"] = {
            "domain_id": "retail",
            "data_version": "2024-01",
        }
        run_context = RunContext.from_context(
            context, semantic_version="7", entrypoint="api"
        )
        self.assertEqual(run_context.run_id, "qf_identity")
        self.assertEqual(run_context.domain_id, "retail")
        self.assertEqual(run_context.data_version, "2024-01")
        self.assertEqual(run_context.semantic_version, "7")
        self.assertEqual(run_context.entrypoint, "api")
        # An undeclared version stays None: "not declared" is not "unchanged".
        self.assertIsNone(run_context.policy_version)

    def test_needs_clarification_is_a_terminal_status_that_round_trips(self):
        """D-2: a clarification stop must be persistable and not overwritten.

        Before this, a NEED_USER_REVIEW verdict raised and produced no payload;
        ``needs_clarification`` was also absent from ``TaskStatus``, so persisting
        it would have failed validation on the next read.
        """
        from queryforge.application.agent_service import persist_cancelled_outcome

        root = self.root / ".queryforge" / "runs"
        store = AgentTeamStateStore(root)
        state = TaskState(
            run_id="clarify_round_trip",
            entrypoint="test",
            classification=RoutingDecision(
                task_type="ask_sql",
                entrypoint="test",
                confidence=1,
                reason="test",
                pipeline="ask_sql",
            ),
            status="needs_clarification",
            current_phase="clarification",
        )
        store.initialize(state)
        self.assertIn("needs_clarification", TaskStatus.__args__)

        loaded = store.load_state("clarify_round_trip")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, "needs_clarification")
        self.assertEqual(loaded.current_phase, "clarification")

        # A late disconnect must not relabel a question as a cancellation.
        self.assertIsNone(
            persist_cancelled_outcome(
                state_root=root,
                run_id="clarify_round_trip",
                reason="client disconnected",
            )
        )
        still = store.load_state("clarify_round_trip")
        self.assertEqual(still.status, "needs_clarification")

    def test_plan_uses_integrated_analysis_candidate_governance_and_ops(self):
        output = self.real_service().plan(
            "List item names",
            self.options(entrypoint="api"),
        )
        self.assertEqual(output["status"], "planned")
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact_types = [artifact["artifact_type"] for artifact in state["artifacts"]]
        self.assertNotIn("execute_sql", output["agent_team"]["pipeline"])
        self.assertNotIn("data_qa", output["agent_team"]["pipeline"])
        self.assertNotIn("visualization", output["agent_team"]["pipeline"])
        for artifact_type in (
            "analysis_request",
            "knowledge_context",
            "schema_plan",
            "sql_candidate",
            "governance_report",
            "ops_report",
            "delivery_report",
        ):
            self.assertIn(artifact_type, artifact_types)
        self.assertNotIn("qa_report", artifact_types)

    def test_explain_result_emits_explanation_report(self):
        output = self.real_service().ask(
            "Explain result for item names",
            self.options(entrypoint="api"),
        )
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["agent_team"]["task_type"], "explain_result")
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact_types = [artifact["artifact_type"] for artifact in state["artifacts"]]
        self.assertIn("explanation_report", artifact_types)
        self.assertNotIn("visualization_artifact", artifact_types)

    def test_build_report_emits_static_report_artifact(self):
        output = self.real_service().ask(
            "Build report for item names",
            self.options(entrypoint="api"),
        )
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["agent_team"]["task_type"], "build_report")
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        report_ref = next(
            artifact for artifact in state["artifacts"]
            if artifact["artifact_type"] == "report_artifact"
        )
        self.assertEqual(report_ref["status"], "valid")

    def test_governance_denial_is_persisted_before_sql_execution(self):
        policy = self.root / "strict.yml"
        policy.write_text(
            """version: 1
name: strict_agent_team
allowed_tables: [items]
allowed_columns:
  items: [name]
require_limit: true
max_limit: 10
""",
            encoding="utf-8",
        )
        with self.assertRaises(WorkflowError):
            self.real_service().ask(
                "List item names",
                self.options(
                    entrypoint="api",
                    sql_policy_path=str(policy),
                ),
            )
        state_path = (
            self.root / ".queryforge" / "runs" / "qf_agent_team_test" / "state.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact_types = [artifact["artifact_type"] for artifact in state["artifacts"]]
        self.assertIn("governance_report", artifact_types)
        self.assertNotIn("qa_report", artifact_types)
        governance_ref = next(
            item for item in state["artifacts"]
            if item["artifact_type"] == "governance_report"
        )
        governance = json.loads(
            (state_path.parent / governance_ref["path"]).read_text(encoding="utf-8")
        )
        self.assertFalse(governance["payload"]["allowed"])
        self.assertEqual(governance["payload"]["blocking_rule"], "unbounded_result")

    def test_cli_no_longer_exposes_agent_team_feature_flags(self):
        with patch(
            "sys.argv",
            [
                "queryforge",
                "--question",
                "List item names",
            ],
        ):
            arguments = cli.build_parser().parse_args()
        self.assertFalse(hasattr(arguments, "agent_team"))
        self.assertFalse(hasattr(arguments, "show_agent_plan"))

    def test_cli_exposes_conversation_session_options(self):
        with patch(
            "sys.argv",
            [
                "queryforge",
                "--question",
                "List item names",
                "--session-id",
                "analysis_1",
                "--reset-session",
            ],
        ):
            arguments = cli.build_parser().parse_args()
        self.assertEqual(arguments.session_id, "analysis_1")
        self.assertTrue(arguments.reset_session)
        self.assertFalse(arguments.new_session)


if __name__ == "__main__":
    unittest.main()
