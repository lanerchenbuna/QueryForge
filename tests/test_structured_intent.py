"""Offline tests for the typed analysis intent, date windows, and clarifications."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from queryforge.workflow.node.date_parser_node import DateParserNode
from queryforge.core.config import Config
from queryforge.core.schemas.models import Context, SqlTask
from queryforge.domain.analysis import (
    DEFAULT_TIMEZONE,
    AnalysisRequest,
    apply_patch,
    detect_comparison_baseline,
    detect_time_grain,
    is_high_impact_ambiguity,
)
from queryforge.application import AgentOptions, AgentService


TODAY = date(2025, 5, 15)


class StructuredIntentLLM:
    """Deterministic provider: no model ever sees a real network call."""

    def generate_json(self, prompt: str) -> dict:
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


class StructuredIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT, region TEXT, category TEXT)")
        connection.execute("INSERT INTO items VALUES ('alpha', 'East', 'books')")
        connection.commit()
        connection.close()
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def service(self) -> AgentService:
        return AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: StructuredIntentLLM(),
        )

    def options(
        self,
        run_id: str,
        *,
        session_id: str | None = None,
    ) -> AgentOptions:
        return AgentOptions(
            database=str(self.database),
            skills=[],
            run_id=run_id,
            session_id=session_id,
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )

    def analysis_payload(self, output: dict) -> dict:
        state_path = Path(output["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        reference = next(
            artifact
            for artifact in state["artifacts"]
            if artifact["artifact_type"] == "analysis_request"
        )
        document = json.loads(
            (state_path.parent / reference["path"]).read_text(encoding="utf-8")
        )
        return document["payload"]

    @staticmethod
    def session_document(output: dict) -> dict:
        return json.loads(Path(output["session"]["path"]).read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ typed contract

    def test_legacy_artifact_coercion_produces_typed_request(self) -> None:
        payload = {
            "question": "Show revenue by region for last month",
            "goal": "Show revenue by region for last month",
            "metrics": ["revenue"],
            "dimensions": ["region"],
            "filters": ["region = East", {"expression": "status = 'paid'"}],
            "time_range": {
                "reference_date": "2025-05-15",
                "source": "rule",
                "ranges": [
                    {"expression": "last month", "start_date": "2025-04-01", "end_date": "2025-04-30"}
                ],
            },
            "grain": "region",
            "limit": "5",
            "clarification_reasons": ["Ranking was requested without a dimension."],
            "ambiguities": ["missing_ranking_dimension"],
            "assumptions": ["Use the matched metric."],
            "status": "warning",
        }
        request = AnalysisRequest.model_validate_artifact(payload)
        self.assertEqual(request.intent, "ask_sql")
        self.assertEqual(request.metric_ids, ["revenue"])
        self.assertEqual(request.dimensions, ["region"])
        self.assertEqual(
            request.filters,
            [{"expression": "region = East"}, {"expression": "status = 'paid'"}],
        )
        self.assertEqual(request.time_range, "2025-04-01..2025-04-30")
        self.assertEqual(request.timezone, DEFAULT_TIMEZONE)
        self.assertEqual(request.top_n, 5)
        self.assertEqual(request.unresolved_questions, ["missing_ranking_dimension"])
        self.assertEqual(request.assumptions, ["Use the matched metric."])
        self.assertEqual(request.status, "warning")

    def test_artifact_coercion_never_raises_on_hostile_payloads(self) -> None:
        for payload in (
            None,
            {},
            {"metrics": {"weird": {"nested": 1}}, "filters": [None, 3], "time_range": 7},
            {"status": "degraded", "clarifications": ["not-an-object"]},
        ):
            with self.subTest(payload=payload):
                request = AnalysisRequest.model_validate_artifact(payload)
                self.assertIsInstance(request, AnalysisRequest)
        degraded = AnalysisRequest.model_validate_artifact({"status": "degraded"})
        self.assertEqual(degraded.status, "warning")

    # ------------------------------------------------------------------ rule-based patches

    def test_apply_patch_updates_dimensions_filters_topn_and_baseline(self) -> None:
        base = AnalysisRequest(
            metric_ids=["order_count"],
            dimensions=["region"],
            filters=[{"expression": "status = 'paid'"}],
        )
        added, reason = apply_patch("by product category", base)
        self.assertEqual(reason, "add_dimension")
        self.assertEqual(added.dimensions, ["region", "product category"])
        self.assertEqual(base.dimensions, ["region"])  # previous request untouched

        replaced, reason = apply_patch("by warehouse instead", base)
        self.assertEqual(reason, "replace_dimension")
        self.assertEqual(replaced.dimensions, ["warehouse"])

        removed, reason = apply_patch("remove region", base)
        self.assertEqual(reason, "remove_dimension_or_metric")
        self.assertEqual(removed.dimensions, [])

        filtered, reason = apply_patch("only include East", base)
        self.assertEqual(reason, "add_filter")
        self.assertIn({"expression": "East"}, filtered.filters)
        self.assertIn({"expression": "status = 'paid'"}, filtered.filters)

        ranked, reason = apply_patch("top 5", base)
        self.assertEqual(reason, "set_ranking")
        self.assertEqual(ranked.top_n, 5)

        compared, reason = apply_patch("compared to last year", base)
        self.assertEqual(reason, "set_comparison_baseline")
        self.assertEqual(compared.comparison_baseline, "previous_year")

        windowed, reason = apply_patch("last 3 months", base)
        self.assertEqual(reason, "set_time_range")
        self.assertEqual(windowed.time_range, "last 3 months")

        grain, reason = apply_patch("by month", base)
        self.assertEqual(reason, "add_time_dimension")
        self.assertEqual(grain.time_grain, "monthly")
        self.assertEqual(grain.dimensions, ["region"])

    def test_baseline_span_is_not_reused_as_the_analysis_window(self) -> None:
        patched, reason = apply_patch("vs last month", AnalysisRequest())
        self.assertEqual(reason, "set_comparison_baseline")
        self.assertEqual(patched.comparison_baseline, "previous_month")
        self.assertIsNone(patched.time_range)

    def test_patch_never_invents_a_governed_metric_id(self) -> None:
        patched, reason = apply_patch("also include order count", AnalysisRequest(metric_ids=["revenue"]))
        self.assertEqual(reason, "add_metric")
        self.assertEqual(patched.metric_ids, ["revenue"])
        self.assertTrue(
            any("order count" in item for item in patched.unresolved_questions),
            patched.unresolved_questions,
        )

    def test_non_integer_top_n_is_ignored(self) -> None:
        patched, reason = apply_patch("top many", AnalysisRequest())
        self.assertIsNone(reason)
        self.assertIsNone(patched.top_n)

    def test_reference_without_prior_context_asks_instead_of_guessing(self) -> None:
        patched, reason = apply_patch("还是那个，换成上月", AnalysisRequest())
        self.assertEqual(patched.metric_ids, [])
        self.assertEqual(patched.dimensions, [])
        self.assertEqual(patched.time_range, "last month")
        self.assertIn("resolve_reference", reason or "")
        self.assertTrue(
            any("prior request" in item for item in patched.unresolved_questions)
        )

    # ------------------------------------------------------------------ ambiguity + grain

    def test_high_impact_ambiguity_names_the_missing_definition(self) -> None:
        self.assertEqual(
            is_high_impact_ambiguity("Show total revenue by region", AnalysisRequest()),
            ["ambiguous_metric_definition"],
        )
        self.assertEqual(
            is_high_impact_ambiguity(
                "How many active users did we have", AnalysisRequest()
            ),
            ["ambiguous_active_user_definition"],
        )
        self.assertEqual(
            is_high_impact_ambiguity(
                "Show revenue growth", AnalysisRequest(metric_ids=["revenue"])
            ),
            ["missing_comparison_baseline"],
        )
        # A matched governed metric with an explicit baseline is not ambiguous.
        self.assertEqual(
            is_high_impact_ambiguity(
                "Show revenue yoy", AnalysisRequest(metric_ids=["revenue"])
            ),
            [],
        )
        # Generic "users growth" phrasing is not a metric definition.
        self.assertEqual(
            is_high_impact_ambiguity("Show top users growth", AnalysisRequest()), []
        )

    def test_grain_and_baseline_detection_is_token_based(self) -> None:
        self.assertEqual(detect_time_grain("Show revenue by month"), "monthly")
        self.assertIsNone(detect_time_grain("Show revenue for last month"))
        self.assertEqual(detect_comparison_baseline("Show revenue yoy"), "same_period_last_year")
        self.assertEqual(detect_comparison_baseline("Show revenue mom"), "previous_period")
        self.assertEqual(detect_comparison_baseline("Compare to Q1 2024"), "vs Q1 2024")
        self.assertIsNone(detect_comparison_baseline("Show revenue"))

    # ------------------------------------------------------------------ date windows

    def test_two_explicit_dates_join_into_one_inclusive_range(self) -> None:
        single = DateParserNode.parse_rules("Show orders from 2026-01-01 to 2026-01-05", TODAY)
        self.assertEqual(len(single), 1)
        self.assertEqual((single[0].start_date, single[0].end_date), ("2026-01-01", "2026-01-05"))

        chinese = DateParserNode.parse_rules("订单 2026-01-01 至 2026-01-05", TODAY)
        self.assertEqual(len(chinese), 1)
        self.assertEqual((chinese[0].start_date, chinese[0].end_date), ("2026-01-01", "2026-01-05"))

        ranges, explicit_merge = DateParserNode.resolve_rules(
            "Show orders from 2026-01-01 to 2026-01-05", TODAY
        )
        self.assertTrue(explicit_merge)
        self.assertEqual(len(ranges), 1)

        # Unrelated explicit dates stay separate points.
        separate = DateParserNode.parse_rules("Compare 2025-01-01 with 2025-03-05", TODAY)
        self.assertEqual(len(separate), 2)

    def test_reversed_explicit_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DateParserNode.parse_rules("Show orders from 2026-01-05 to 2026-01-01", TODAY)
        result = DateParserNode(today_provider=lambda: TODAY).execute(
            Context(
                task=SqlTask(
                    question="Show orders from 2026-01-05 to 2026-01-01",
                    database_path="x.sqlite",
                )
            )
        )
        self.assertFalse(result.success)
        self.assertIn("reversed", result.error or "")

    def test_window_semantics_label_calendar_rolling_and_merge(self) -> None:
        node = DateParserNode(today_provider=lambda: TODAY)

        months = Context(task=SqlTask(question="Show revenue for last 3 months", database_path="x.sqlite"))
        node.execute(months)
        self.assertEqual(
            (months.date_context.ranges[0].start_date, months.date_context.ranges[0].end_date),
            ("2025-03-01", "2025-05-15"),
        )
        self.assertEqual(months.task_context["date_window"]["mode"], "calendar")
        self.assertFalse(months.task_context["date_window"]["explicit_merge"])
        self.assertIn("calendar months", months.date_context.note)

        rolling = Context(task=SqlTask(question="Show 最近 90 天滚动 revenue", database_path="x.sqlite"))
        node.execute(rolling)
        self.assertEqual(rolling.date_context.ranges[0].start_date, "2025-02-15")
        self.assertEqual(rolling.task_context["date_window"]["mode"], "rolling")

        merged = Context(
            task=SqlTask(question="Show orders from 2026-01-01 to 2026-01-05", database_path="x.sqlite")
        )
        node.execute(merged)
        self.assertEqual(len(merged.date_context.ranges), 1)
        self.assertTrue(merged.task_context["date_window"]["explicit_merge"])

        empty = Context(task=SqlTask(question="How many items are there?", database_path="x.sqlite"))
        node.execute(empty)
        self.assertFalse(empty.task_context["date_window"]["resolved"])
        self.assertEqual(empty.task_context["date_window"]["mode"], "calendar")

    def test_relative_counts_reject_out_of_range_values(self) -> None:
        for question in ("last 0 days", "last 999999 months"):
            with self.subTest(question=question):
                result = DateParserNode(today_provider=lambda: TODAY).execute(
                    Context(task=SqlTask(question=question, database_path="x.sqlite"))
                )
                self.assertFalse(result.success)

    # ------------------------------------------------------------------ agent integration

    def test_analysis_artifact_adds_typed_fields_and_keeps_raw_keys(self) -> None:
        output = self.service().ask(
            "List item names by month", self.options("intent_artifact")
        )
        payload = self.analysis_payload(output)
        for raw_key in (
            "question",
            "goal",
            "objective",
            "metrics",
            "metric_mappings",
            "dimensions",
            "dimension_mappings",
            "filters",
            "sort_by",
            "date_context",
            "time_range",
            "ordering",
            "limit",
            "grain",
            "target_grain",
            "clarification_needed",
            "clarifications",
            "clarification_reasons",
            "ambiguities",
            "assumptions",
            "status",
            "is_followup",
            "rewritten_from",
            "followup_reason",
        ):
            with self.subTest(key=raw_key):
                self.assertIn(raw_key, payload)
        self.assertEqual(payload["timezone"], DEFAULT_TIMEZONE)
        self.assertEqual(payload["time_grain"], "monthly")
        self.assertIsNone(payload["comparison_baseline"])
        self.assertEqual(payload["metric_ids"], [])
        self.assertEqual(payload["unresolved_questions"], payload["ambiguities"])
        typed = AnalysisRequest.model_validate_artifact(payload)
        self.assertEqual(typed.time_grain, "monthly")
        self.assertEqual(typed.status, "valid")

    def test_revenue_without_definition_blocks_and_records_clarification(self) -> None:
        service = self.service()
        output = service.ask(
            "Show total revenue by region for last month",
            self.options("intent_blocked", session_id="intent_session"),
        )
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(output["agent_team"]["blocked_phase"], "analysis")
        self.assertEqual(output["delivery_report"]["status"], "degraded")
        self.assertNotIn("sql", output)

        persisted = self.session_document(output)
        turn = persisted["history"][-1]
        self.assertEqual(turn["status"], "blocked")
        self.assertIsNone(turn["sql"])
        aspects = [item["aspect"] for item in turn["needs_clarification"]]
        self.assertIn("ambiguous_metric_definition", aspects)
        self.assertTrue(
            all(item["severity"] == "high" for item in turn["needs_clarification"])
        )
        self.assertEqual(
            [item["aspect"] for item in persisted["pending_clarifications"]],
            ["ambiguous_metric_definition"],
        )
        self.assertEqual(
            turn["analysis_request"]["status"],
            "blocked",
        )
        self.assertEqual(output["session"]["pending_clarifications"], persisted["pending_clarifications"])

    def test_blocked_intent_is_resumed_and_patched_by_the_next_turn(self) -> None:
        service = self.service()
        service.ask(
            "Show total revenue by region for last month",
            self.options("intent_resume_one", session_id="resume_session"),
        )
        second = service.ask(
            "by category",
            self.options("intent_resume_two", session_id="resume_session"),
        )
        self.assertEqual(second["status"], "success")
        persisted = self.session_document(second)
        self.assertEqual(persisted["turn_count"], 2)
        resumed = persisted["history"][-1]["analysis_request"]
        # The blocked intent survives the clarification round trip...
        self.assertTrue(resumed["time_range"])
        self.assertTrue(resumed["unresolved_questions"])
        # ...and the follow-up is applied as a patch on top of it.
        self.assertIn("category", resumed["dimensions"])
        self.assertEqual(resumed["status"], "valid")
        self.assertEqual(persisted["pending_clarifications"], [])

    def test_followup_turn_persists_the_structured_patch(self) -> None:
        service = self.service()
        first = service.ask(
            "List item names", self.options("intent_first", session_id="followup_session")
        )
        second = service.ask(
            "by month", self.options("intent_month", session_id="followup_session")
        )
        self.assertTrue(second["session"]["is_followup"])
        first_document = self.session_document(first)
        self.assertEqual(first_document["history"][-1]["analysis_request"]["dimensions"], [])
        persisted = self.session_document(second)
        request = persisted["history"][-1]["analysis_request"]
        self.assertEqual(request["time_grain"], "monthly")
        self.assertEqual(request["timezone"], DEFAULT_TIMEZONE)
        self.assertEqual(request["intent"], "ask_sql")
        self.assertEqual(request["unresolved_questions"], [])
        self.assertEqual(persisted["history"][-1]["needs_clarification"], [])
        self.assertEqual(persisted["pending_clarifications"], [])

    def test_simple_questions_do_not_gain_clarifications(self) -> None:
        output = self.service().ask("List item names", self.options("intent_simple"))
        payload = self.analysis_payload(output)
        self.assertEqual(payload["status"], "valid")
        self.assertFalse(payload["clarification_needed"])
        self.assertEqual(payload["clarifications"], [])
        self.assertEqual(payload["unresolved_questions"], [])


if __name__ == "__main__":
    unittest.main()
