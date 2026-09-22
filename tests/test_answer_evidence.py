"""Offline, deterministic tests for step 12: the evidence layer.

Covers 12-N1, 12-E1, 12-E2, 12-B1, 12-B2, 12-E3, 12-S1, 12-M1 and 12-R1 of
the step-12 evidence-anchored-answer contract.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.domain.analysis.evidence import (
    KIND_CONTRIBUTION,
    KIND_PERIOD_COMPARISON,
    AnswerComposer,
    Evidence,
    EvidenceStore,
    FinalAnswer,
    Finding,
    apply_validation,
    build_execution_evidence,
    causal_language_guard,
    load_evidence_store,
    resolve_number,
    result_findings,
    summarize_completeness,
    validate_answer,
)
from queryforge.core.schemas.models import Context, ExecutionResult, SQLContext, SqlTask
from queryforge.orchestration.agents.report import ReportAgent
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.schemas import RoutingDecision, TaskState
from queryforge.workflow.node.output_node import OutputNode
from queryforge.workflow.report_generator import ReportGenerator


SQL = "SELECT month, SUM(revenue) AS revenue FROM sales GROUP BY month"


def month_rows(values: list[int | None]) -> list[list[object]]:
    return [[f"2026-{index + 1:02d}-01", value] for index, value in enumerate(values)]


class AnswerEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    # -- helpers -----------------------------------------------------------
    def result_evidence(
        self,
        rows: list[list[object]],
        *,
        columns: tuple[str, ...] = ("month", "revenue"),
        row_count: int | None = None,
        completeness: str = "complete",
        version: str = "v2",
        metric: str | None = "revenue",
        dimension: str | None = None,
        series_key: str | None = "month",
    ) -> Evidence:
        return build_execution_evidence(
            sql=SQL,
            source="/tmp/sales.sqlite",
            columns=list(columns),
            rows=rows,
            row_count=row_count,
            version=version,
            grain="month",
            unit="CNY",
            range_={"start": "2026-01-01", "end": "2026-12-31"},
            completeness=completeness,
            metric=metric,
            dimension=dimension,
            series_key=series_key,
        )

    def store_with_result(self, rows: list[list[object]], **kwargs) -> tuple[EvidenceStore, Evidence]:
        store = EvidenceStore()
        evidence = self.result_evidence(rows, **kwargs)
        store.add(evidence)
        return store, evidence

    def context(
        self,
        rows: list[list[object]],
        *,
        columns: tuple[str, ...] = ("month", "revenue"),
        max_rows: int = 50,
        row_count: int | None = None,
    ) -> Context:
        return Context(
            task=SqlTask(question="月度收入趋势如何？", database_path="/tmp/sales.sqlite"),
            run_id="qf_evidence_test",
            sql_context=SQLContext(sql=SQL, explanation="Aggregate revenue by month."),
            execution_result=ExecutionResult(
                columns=list(columns),
                rows=rows,
                row_count=len(rows) if row_count is None else row_count,
            ),
            report_output_dir=str(self.root / "reports"),
        )

    # -- 12-N1 -------------------------------------------------------------
    def test_12_n1_findings_cite_existing_evidence_and_match_numbers(self) -> None:
        store, result = self.store_with_result(month_rows([10, 20, 5]))
        contribution = Evidence(
            kind=KIND_CONTRIBUTION,
            source="analysis_tool.contribution",
            version="v2",
            method="period contribution decomposition",
            refs=[result.id],
            payload={"numbers": {"contribution_delta": -30, "base_total": 65}},
        )
        store.add(contribution)

        findings, limitations = result_findings(store, result.id, metric="revenue")
        findings.append(
            Finding(
                kind=KIND_CONTRIBUTION,
                statement="Channel A explains part of the movement.",
                # A model-typed number must never survive: the composer replaces
                # it with the value found in the cited evidence.
                numbers={"contribution_delta": -999},
                evidence_ids=[contribution.id],
            )
        )
        answer = AnswerComposer(store).compose(
            "月度收入趋势如何？",
            findings,
            limitations=limitations,
            charts=[{"id": "chart_1", "chart_type": "line", "evidence_ids": [result.id]}],
        )

        self.assertFalse(answer.review_required)
        self.assertFalse(answer.degraded)
        self.assertTrue(answer.evidence_ids)
        for finding in answer.findings:
            self.assertTrue(finding.evidence_ids, "every finding must cite evidence")
            for evidence_id in finding.evidence_ids:
                self.assertIn(evidence_id, store.ids())
            payloads = [store.get(item).payload for item in finding.evidence_ids]
            for key, value in finding.numbers.items():
                self.assertIsNotNone(value, f"{key} must be resolved from evidence")
                found, actual = resolve_number(payloads, key)
                self.assertTrue(found, f"{key} must exist in the cited evidence payload")
                self.assertEqual(value, actual)
        self.assertEqual(
            next(
                finding.numbers["contribution_delta"]
                for finding in answer.findings
                if finding.kind == KIND_CONTRIBUTION
            ),
            -30,
        )
        self.assertTrue(
            any("evidence value is used" in item for item in answer.limitations),
            "a declared value that disagrees with evidence must be recorded",
        )
        self.assertEqual(validate_answer(answer, store), [])
        self.assertEqual(answer.charts[0]["evidence_ids"], [result.id])
        self.assertEqual(answer.charts[0]["grain"], "month")
        self.assertEqual(answer.charts[0]["unit"], "CNY")

    # -- 12-E1 -------------------------------------------------------------
    def test_12_e1_fabricated_evidence_id_and_wrong_number_are_flagged(self) -> None:
        store, result = self.store_with_result(month_rows([10, 20, 5]))
        fabricated = Finding(
            kind=KIND_CONTRIBUTION,
            statement="Revenue dropped by 12.",
            numbers={"total_revenue": 12},
            evidence_ids=[result.id, "ev_does_not_exist"],
        )
        answer = FinalAnswer(
            question="月度收入趋势如何？",
            status="success",
            conclusions=["Revenue dropped by 12."],
            findings=[fabricated],
            evidence_ids=[result.id, "ev_does_not_exist"],
        )
        problems = validate_answer(answer, store)
        self.assertTrue(any("ev_does_not_exist" in item for item in problems))
        self.assertTrue(any("disagrees with the cited evidence value" in item for item in problems))

        fixed = apply_validation(answer, problems)
        self.assertTrue(fixed.review_required)
        self.assertFalse(answer.review_required, "validation must not mutate the input answer")
        for problem in problems:
            self.assertIn(problem, fixed.limitations)
        self.assertEqual(fixed.model_dump(mode="json")["status"], "success")

        # The composer drops an unknown id but records it instead of silently
        # publishing the finding as verified.
        composed = AnswerComposer(store).compose("月度收入趋势如何？", [fabricated])
        self.assertEqual(composed.findings[0].evidence_ids, [result.id])
        self.assertTrue(composed.findings[0].review_required)
        self.assertTrue(any("ev_does_not_exist" in item for item in composed.limitations))

    # -- 12-E2 -------------------------------------------------------------
    def test_12_e2_contribution_only_evidence_cannot_assert_causation(self) -> None:
        store, result = self.store_with_result(month_rows([30, 18]))
        contribution = Evidence(
            kind=KIND_CONTRIBUTION,
            source="analysis_tool.contribution",
            refs=[result.id],
            method="period contribution decomposition",
            payload={"numbers": {"contribution_delta": -12}, "campaign": "summer_promo"},
        )
        store.add(contribution)
        causal_finding = Finding(
            kind=KIND_CONTRIBUTION,
            statement="渠道 A 导致收入下降 12。",
            numbers={"contribution_delta": -12},
            evidence_ids=[contribution.id],
        )
        composer = AnswerComposer(store)
        causal_answer = composer.compose("为什么收入下降？", [causal_finding])
        problems = validate_answer(causal_answer, store)
        self.assertTrue(any("asserts causation" in item for item in problems))
        self.assertTrue(any("correlational kinds" in item for item in problems))
        self.assertTrue(apply_validation(causal_answer, problems).review_required)

        neutral_finding = Finding(
            kind=KIND_CONTRIBUTION,
            statement="渠道 A 贡献了 12 个单位的收入变化，与夏季活动同期发生（仅相关，不构成因果证明）。",
            numbers={"contribution_delta": -12},
            evidence_ids=[contribution.id],
        )
        neutral_answer = composer.compose("为什么收入下降？", [neutral_finding])
        self.assertEqual(validate_answer(neutral_answer, store), [])

        self.assertIn("导致", causal_language_guard("渠道 A 导致收入下降") or "")
        self.assertIsNone(causal_language_guard("渠道 A 的贡献与下降同期发生"))
        self.assertIsNone(causal_language_guard("The campaign did not cause the decline"))

        # A declared causal source unlocks causal wording.
        experiment = Evidence(
            kind="experiment",
            source="experiment harness",
            refs=[result.id],
            method="randomized A/B experiment",
            payload={"causal": True, "numbers": {"contribution_delta": -12}},
        )
        store.add(experiment)
        backed = composer.compose(
            "为什么收入下降？",
            [
                Finding(
                    kind=KIND_CONTRIBUTION,
                    statement="渠道 A 导致收入下降 12。",
                    numbers={"contribution_delta": -12},
                    evidence_ids=[contribution.id, experiment.id],
                )
            ],
        )
        self.assertEqual(validate_answer(backed, store), [])

    # -- 12-B1 -------------------------------------------------------------
    def test_12_b1_degenerate_inputs_produce_honest_findings(self) -> None:
        composer_checks = 0
        # Empty result set.
        store, result = self.store_with_result([], row_count=0)
        findings, limitations = result_findings(store, result.id, metric="revenue")
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].degraded)
        self.assertEqual(findings[0].numbers["row_count"], 0)
        # Only the honest counts survive: no aggregate, no top item, no trend.
        self.assertEqual(set(findings[0].numbers), {"row_count", "returned_rows"})
        self.assertNotIn(KIND_PERIOD_COMPARISON, [finding.kind for finding in findings])
        self.assertNotIn("Top ", findings[0].statement)
        self.assertTrue(any("empty" in item.lower() for item in limitations))
        empty_answer = AnswerComposer(store).compose(
            "月度收入趋势如何？", findings, status="partial", limitations=limitations
        )
        self.assertTrue(empty_answer.degraded)
        self.assertEqual(validate_answer(empty_answer, store), [])
        composer_checks += 1

        # All-NULL metric.
        store, result = self.store_with_result(month_rows([None, None]))
        findings, limitations = result_findings(store, result.id, metric="revenue")
        self.assertTrue(findings[0].degraded)
        self.assertIn("NULL", findings[0].statement)
        self.assertNotIn("total_revenue", findings[0].numbers)
        self.assertTrue(any("NULL" in item for item in limitations))

        # Single point: no fabricated trend.
        store, result = self.store_with_result(month_rows([7]))
        findings, limitations = result_findings(store, result.id, metric="revenue")
        self.assertFalse(
            any(finding.kind == KIND_PERIOD_COMPARISON for finding in findings)
        )
        self.assertTrue(any("no trend is reported" in item for item in limitations))
        self.assertEqual(findings[0].numbers["total_revenue"], 7)

        # All-negative values: reported as observed, never as growth.
        store, result = self.store_with_result(month_rows([-10, -20, -5]))
        findings, limitations = result_findings(store, result.id, metric="revenue")
        negative = [
            finding
            for finding in findings
            if "negative" in finding.statement
        ]
        self.assertTrue(negative)
        self.assertEqual(negative[0].numbers["total_revenue"], -35)
        for finding in findings:
            self.assertNotIn("increase", finding.statement)
        answer = AnswerComposer(store).compose(
            "月度收入趋势如何？", findings, status="partial", limitations=limitations
        )
        totals = [
            finding.numbers["total_revenue"]
            for finding in answer.findings
            if "total_revenue" in finding.numbers
        ]
        self.assertTrue(all(value == -35 for value in totals))
        self.assertEqual(validate_answer(answer, store), [])
        self.assertGreaterEqual(composer_checks, 1)

    # -- 12-B2 -------------------------------------------------------------
    def test_12_b2_display_truncation_is_separate_from_analysis_completeness(self) -> None:
        rows = month_rows(list(range(1, 61)))
        context = self.context(rows, max_rows=5)
        store, result = self.store_with_result(rows)
        answer = AnswerComposer(store).compose(
            "月度收入趋势如何？",
            result_findings(store, result.id, metric="revenue")[0],
        )
        artifact = ReportGenerator(
            self.root / "reports", max_rows=5
        ).generate(context, evidence=store, final_answer=answer)
        html = Path(artifact.file_path).read_text(encoding="utf-8")
        manifest = json.loads(Path(artifact.manifest_path).read_text(encoding="utf-8"))

        table = next(section for section in artifact.sections if section.type == "table")
        self.assertEqual(table.id, "results")
        self.assertTrue(table.content["truncated"])
        self.assertTrue(table.content["display_truncated"])
        self.assertEqual(table.content["displayed_rows"], 5)
        self.assertEqual(table.content["analysis_complete"], True)

        completeness = next(
            section for section in artifact.sections if section.id == "completeness"
        )
        self.assertTrue(completeness.content["display_truncated"])
        self.assertEqual(completeness.content["displayed_row_count"], 5)
        self.assertEqual(completeness.content["total_row_count"], 60)
        self.assertTrue(completeness.content["analysis_complete"])
        self.assertEqual(completeness.content["analysis_scope"], "complete_result_set")
        self.assertIn("Showing 5 of 60 rows.", html)
        self.assertIn("Completeness", html)

        # The total comes from the complete evidence, not from the shown subset.
        complete_total = store.get(result.id).payload["total_revenue"]
        self.assertEqual(complete_total, sum(range(1, 61)))
        self.assertNotEqual(complete_total, sum(range(1, 6)))
        traceability = next(
            section
            for section in manifest["sections"]
            if section["id"] == "evidence_traceability"
        )
        self.assertEqual(traceability["content"]["rows"][0][0], result.id)
        self.assertEqual(traceability["content"]["rows"][0][6], "complete")

        # Opposite direction: a complete display never implies a complete analysis.
        truncated_store, truncated_result = self.store_with_result(
            month_rows([10, 20, 5]), completeness="truncated"
        )
        truncated_artifact = ReportGenerator(self.root / "reports2", max_rows=50).generate(
            self.context(month_rows([10, 20, 5]), max_rows=50),
            evidence=truncated_store,
        )
        truncated_html = Path(truncated_artifact.file_path).read_text(encoding="utf-8")
        truncated_completeness = next(
            section
            for section in truncated_artifact.sections
            if section.id == "completeness"
        )
        self.assertFalse(truncated_completeness.content["display_truncated"])
        self.assertFalse(truncated_completeness.content["analysis_complete"])
        self.assertEqual(truncated_completeness.content["analysis_scope"], "truncated")
        self.assertIn("Analysis completeness: degraded", truncated_html)
        self.assertIn(truncated_result.id, truncated_html)

    # -- 12-E3 -------------------------------------------------------------
    def test_12_e3_report_failure_degrades_without_marking_success(self) -> None:
        context = self.context(month_rows([10, 20, 5]))
        OutputNode().execute(context)
        self.assertEqual(context.final_output["status"], "success")
        state = TaskState(
            run_id=context.run_id,
            entrypoint="test",
            classification=RoutingDecision(
                task_type="build_report",
                entrypoint="test",
                confidence=1.0,
                reason="test",
                pipeline="build_report",
            ),
        )
        store = AgentTeamStateStore(self.root / "runs")
        store.initialize(state)
        with patch.object(ReportGenerator, "generate", side_effect=RuntimeError("disk full")):
            reference = ReportAgent(store).run(state, context)
        self.assertEqual(reference.status, "degraded")
        document = json.loads(
            (store.run_dir(state.run_id) / reference.path).read_text(encoding="utf-8")
        )
        self.assertEqual(document["status"], "degraded")
        self.assertIsNone(document["payload"]["file_path"])
        self.assertIn("error", document["payload"])
        # The verified query result stays usable and is not overwritten.
        self.assertEqual(context.final_output["status"], "success")
        self.assertEqual(context.final_output["row_count"], 3)
        self.assertNotIn("report", context.final_output)

        # An unusable evidence payload degrades the evidence section only.
        broken = self.context(month_rows([10, 20, 5]))
        broken.task_context["evidence"] = [{"kind": "sql_result", "refs": ["ev_missing"]}]
        artifact = ReportGenerator(self.root / "reports3").generate(broken)
        broken_html = Path(artifact.file_path).read_text(encoding="utf-8")
        self.assertTrue(broken_html.startswith("<!doctype html>"))
        completeness = next(
            section for section in artifact.sections if section.id == "completeness"
        )
        self.assertTrue(
            any("rejected" in item for item in completeness.content["items"]),
            completeness.content["items"],
        )
        self.assertEqual(broken.task_context["evidence"][0]["refs"], ["ev_missing"])

    # -- 12-S1 -------------------------------------------------------------
    def test_12_s1_script_breakout_stays_inert_in_html(self) -> None:
        payload = "</script><script>alert(1)</script>"
        rows = month_rows([10, 20, 5])
        context = self.context(rows)
        store, result = self.store_with_result(rows)
        hostile = Evidence(
            kind="data_quality",
            source=payload,
            method=payload,
            sql=payload,
            refs=[result.id],
            payload={"note": payload},
        )
        store.add(hostile)
        answer = AnswerComposer(store).compose(
            payload,
            [
                Finding(
                    kind="data_quality",
                    statement=f"Row label {payload} looked odd.",
                    evidence_ids=[hostile.id],
                )
            ],
            assumptions=[payload],
            limitations=[payload],
            gaps=["data_quality"],
        )
        artifact = ReportGenerator(self.root / "reports").generate(
            context, evidence=store, final_answer=answer
        )
        html = Path(artifact.file_path).read_text(encoding="utf-8")
        self.assertNotIn("</script><script>", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;/script&gt;", html)
        # Every opened script tag is still a legitimate, closed tag.
        self.assertEqual(html.count("<script"), html.count("</script>"))

    # -- 12-M1 -------------------------------------------------------------
    def test_12_m1_unknown_or_stale_references_are_rejected(self) -> None:
        store = EvidenceStore()
        with self.assertRaises(ValueError):
            store.add(Evidence(kind="contribution", source="x", refs=["ev_unknown"]))
        self.assertEqual(len(store), 0)

        # Ids are unique across every construction path, and unknown lookups fail
        # loudly so callers cannot treat "no evidence" as "verified".
        unique_store = EvidenceStore()
        duplicate = Evidence(kind="sql_result", source="db", id="ev_fixed")
        unique_store.add(duplicate)
        with self.assertRaises(ValueError):
            unique_store.add(Evidence(kind="sql_result", source="db", id="ev_fixed"))
        with self.assertRaises(ValueError):
            EvidenceStore.from_list([duplicate.model_dump(), duplicate.model_dump()])
        with self.assertRaises(KeyError):
            unique_store.get("ev_fixed_but_absent")
        self.assertEqual(unique_store.ids(), ["ev_fixed"])
        self.assertEqual(len(unique_store.by_kind("sql_result")), 1)
        self.assertEqual(unique_store.by_kind("contribution"), [])
        self.assertTrue(duplicate.id.startswith("ev_") or duplicate.id == "ev_fixed")
        self.assertEqual(len(unique_store), 1)
        self.assertEqual(len(Evidence(kind="contribution", source="x").id), len("ev_") + 16)

        parent = self.result_evidence(month_rows([1, 2, 3]), version="v1")
        store.add(parent)
        stale = Evidence(
            kind=KIND_CONTRIBUTION,
            source="analysis_tool",
            version="v2",
            refs=[parent.id],
            payload={"numbers": {"contribution_delta": -1}},
        )
        with self.assertRaises(ValueError) as error:
            store.add(stale)
        self.assertIn("version", str(error.exception))
        self.assertNotIn(stale.id, store.ids())

        # A rejected payload degrades visibly instead of mixing versions.
        loaded, problem = load_evidence_store(
            [stale.model_dump(mode="json"), parent.model_dump(mode="json")]
        )
        self.assertIsNotNone(problem)
        self.assertIn("version", problem)
        self.assertEqual(loaded.ids(), [parent.id])

        # Reference order inside a payload is irrelevant; dangling refs are not.
        child = Evidence(
            kind=KIND_CONTRIBUTION,
            source="analysis_tool",
            version="v1",
            refs=[parent.id],
            payload={"numbers": {"contribution_delta": -2}},
        )
        ordered, ordered_problem = load_evidence_store(
            [child.model_dump(mode="json"), parent.model_dump(mode="json")]
        )
        self.assertIsNone(ordered_problem)
        self.assertEqual(sorted(ordered.ids()), sorted([parent.id, child.id]))
        self.assertEqual(len(EvidenceStore().all()), 0)

    # -- 12-R1 -------------------------------------------------------------
    def test_12_r1_existing_report_artifact_behaviour_is_preserved(self) -> None:
        context = self.context(month_rows([30, 5]), columns=("category", "total"), max_rows=1)
        context.execution_result.rows = [["books", 30], ["music", 5]]
        context.execution_result.row_count = 2
        context.task.question = "Build report for sales by category"
        artifact = ReportGenerator(self.root / "reports", max_rows=1).generate(context)
        html = Path(artifact.file_path).read_text(encoding="utf-8")
        manifest = json.loads(Path(artifact.manifest_path).read_text(encoding="utf-8"))
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("Result Table", html)
        self.assertIn("Show SQL", html)
        self.assertIn("vegaEmbed", html)
        self.assertIn("<svg", html)
        self.assertIn("chart-fallback", html)
        self.assertTrue(any(section.type == "chart" for section in artifact.sections))
        self.assertGreaterEqual(len(artifact.key_findings), 3)
        self.assertTrue(manifest["sections"])
        table = next(section for section in artifact.sections if section.type == "table")
        self.assertTrue(table.content["truncated"])
        self.assertEqual(table.id, "results")

        # Adding evidence keeps the result table first and the manifest valid.
        store, result = self.store_with_result(month_rows([10, 20, 5]))
        artifact_with_evidence = ReportGenerator(self.root / "reports4", max_rows=2).generate(
            context, evidence=[result.model_dump(mode="json")]
        )
        tables = [section for section in artifact_with_evidence.sections if section.type == "table"]
        self.assertEqual(tables[0].id, "results")
        traceability = next(
            section for section in tables if section.id == "evidence_traceability"
        )
        self.assertEqual(traceability.content["rows"][0][0], result.id)
        self.assertEqual(traceability.content["columns"][0], "Evidence ID")
        self.assertEqual(
            len(traceability.content["rows"]), len(store.ids())
        )

    # -- final JSON contract ----------------------------------------------
    def test_final_json_exposes_evidence_answer_and_completeness(self) -> None:
        context = self.context(month_rows([10, 20, 5]), max_rows=2)
        context.report_max_rows = 2
        store, result = self.store_with_result(month_rows([10, 20, 5]))
        answer = AnswerComposer(store).compose(
            "月度收入趋势如何？",
            result_findings(store, result.id, metric="revenue")[0],
        )
        context.task_context["evidence"] = store.to_list()
        context.task_context["final_answer"] = answer.model_dump(mode="json")
        OutputNode(data_version="v2").execute(context)
        output = context.final_output

        self.assertIn("task_evidence", output)
        self.assertEqual(output["task_evidence"]["keys"], ["evidence", "final_answer"])
        kinds = {item["kind"] for item in output["evidence"]}
        self.assertIn("sql_result", kinds)
        self.assertEqual(output["final_answer"]["status"], "success")
        completeness = output["completeness"]
        self.assertTrue(completeness["display_truncated"])
        self.assertEqual(completeness["displayed_row_count"], 2)
        self.assertEqual(completeness["total_row_count"], 3)
        self.assertTrue(completeness["analysis_complete"])
        self.assertEqual(output["answer_validation"]["problems"], [])
        json.dumps(output, ensure_ascii=False)

        # A stale answer payload is surfaced, never silently published.
        stale_context = self.context(month_rows([10, 20, 5]))
        stale_context.task_context["final_answer"] = {
            "question": "q",
            "status": "success",
            "findings": [
                {
                    "kind": "contribution",
                    "statement": "x",
                    "numbers": {"total_revenue": 999},
                    "evidence_ids": ["ev_missing"],
                }
            ],
        }
        OutputNode().execute(stale_context)
        validation = stale_context.final_output["answer_validation"]
        self.assertTrue(validation["review_required"])
        self.assertTrue(any("ev_missing" in item for item in validation["problems"]))
        self.assertTrue(
            any("ev_missing" in item for item in stale_context.final_output["final_answer"]["limitations"])
        )


class CompletenessHelperTest(unittest.TestCase):
    def test_summarize_completeness_never_derives_one_from_the_other(self) -> None:
        store = EvidenceStore()
        truncated = build_execution_evidence(
            sql=SQL,
            source="db",
            columns=["month", "revenue"],
            rows=[["2026-01-01", 1]],
            row_count=1,
            completeness="truncated",
        )
        store.add(truncated)
        summary = summarize_completeness(
            total_row_count=10,
            displayed_row_count=10,
            evidence=store.all(),
            extra_notes=["note"],
        )
        self.assertFalse(summary["display_truncated"])
        self.assertFalse(summary["analysis_complete"])
        self.assertIn("note", summary["notes"])
        unreported = summarize_completeness(total_row_count=4, evidence=[])
        self.assertFalse(unreported["display_truncated"])
        self.assertTrue(unreported["analysis_complete"])
        self.assertEqual(unreported["analysis_scope"], "unreported")


if __name__ == "__main__":
    unittest.main()


class SemanticValidationPropagationTest(unittest.TestCase):
    """E-11: a semantic verdict must travel with the answer.

    ``SemanticSQLValidator`` reports three states, and only one of them means the
    SQL was *proved* to answer the question. ``unsupported`` (an unlisted SQL
    shape, an unparsable statement) used to reach a log line and nothing else, so a
    run whose business semantics were never checked was delivered exactly like one
    that passed every check.
    """

    def _output(self, task_context):
        from queryforge.core.schemas.models import (
            Context,
            ExecutionResult,
            ReflectionResult,
            SQLContext,
            SqlTask,
        )
        from queryforge.workflow.node.output_node import OutputNode

        context = Context(
            task=SqlTask(question="q", database_path="/tmp/x.sqlite")
        )
        context.task_context.update(task_context)
        context.sql_context = SQLContext(
            sql="SELECT 1", explanation="e", tables_used=[]
        )
        context.execution_result = ExecutionResult(
            columns=["a"], rows=[[1]], row_count=1
        )
        context.reflection_result = ReflectionResult(
            success=True, strategy="SUCCESS", reason="ok"
        )
        OutputNode().execute(context)
        return context.final_output

    def test_a_passed_verdict_is_reported_as_verified(self):
        output = self._output(
            {"semantic_validation": {"status": "passed", "rule_names": []}}
        )
        summary = output["semantic_validation"]
        self.assertEqual(summary["status"], "passed")
        self.assertTrue(summary["verified"])
        # Nothing to warn about, so no completeness note is added.
        notes = output["completeness"].get("notes") or []
        self.assertFalse(any("Business semantics" in str(n) for n in notes))

    def test_unsupported_is_not_verified_and_is_stated_in_the_answer(self):
        output = self._output(
            {
                "semantic_validation": {
                    "status": "unsupported",
                    "unsupported_reason": "SQLite SQL could not be parsed",
                    "rule_names": [],
                }
            }
        )
        summary = output["semantic_validation"]
        self.assertEqual(summary["status"], "unsupported")
        self.assertFalse(summary["verified"])
        self.assertIn("could not be parsed", summary["reason"])
        notes = output["completeness"].get("notes") or []
        self.assertTrue(
            any("Business semantics" in str(n) for n in notes),
            "an unproved verdict must be visible in the completeness record too",
        )

    def test_a_violation_is_not_verified(self):
        output = self._output(
            {"semantic_validation": {"status": "violation", "rule_names": ["fanout"]}}
        )
        summary = output["semantic_validation"]
        self.assertFalse(summary["verified"])
        self.assertEqual(summary["rules"], ["fanout"])

    def test_a_run_with_no_verdict_says_so_rather_than_looking_verified(self):
        """The common case: a follow-up that matched no governed metric."""
        output = self._output({})
        summary = output["semantic_validation"]
        self.assertEqual(summary["status"], "not_run")
        self.assertFalse(summary["verified"])
        self.assertIn("no governed metric", summary["reason"])
