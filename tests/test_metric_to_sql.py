import tempfile
import unittest
from datetime import date
from pathlib import Path

from queryforge.workflow.node.date_parser_node import DateParserNode
from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.node.metric_search_node import MetricSearchNode
from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, SqlTask
from queryforge.infrastructure.tools.database_tool import DatabaseTool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "sample_data/anime_streaming"
DATABASE = SAMPLE_ROOT / "anime_streaming.sqlite"
SEMANTIC_MODEL = SAMPLE_ROOT / "semantic_model.yml"


def metric_context(question: str) -> Context:
    state = Context(task=SqlTask(question=question, database_path=str(DATABASE)))
    DateParserNode(today_provider=lambda: date(2026, 7, 16)).execute(state)
    with SQLiteConnector(str(DATABASE)) as connector:
        result = SchemaLinkingNode(
            DatabaseTool(connector), semantic_model_path=str(SEMANTIC_MODEL)
        ).execute(state)
    if not result.success:
        raise AssertionError(result.error)
    return state


METRIC_CASES = {
    "How many unique viewers are there?": {
        "name": "unique_viewers",
        "aggregation": "count",
        "sql": (
            "SELECT COUNT(DISTINCT fact_watch_session.user_id) AS unique_viewers "
            "FROM fact_watch_session"
        ),
        "expected": 6000,
        "table": "fact_watch_session",
    },
    "What are total watch hours?": {
        "name": "watch_hours",
        "aggregation": "sum",
        "sql": (
            "SELECT SUM(fact_watch_session.watch_seconds) / 3600.0 AS watch_hours "
            "FROM fact_watch_session"
        ),
        "expected": 43183.48638888889,
        "table": "fact_watch_session",
    },
    "What is the completion rate?": {
        "name": "completion_rate",
        "aggregation": "ratio",
        "sql": (
            "SELECT CAST(SUM(fact_watch_session.completed_flag) AS REAL) / "
            "NULLIF(COUNT(*), 0) AS completion_rate FROM fact_watch_session"
        ),
        "expected": 0.16886,
        "table": "fact_watch_session",
    },
}


class MetricWorkflowLLM:
    def __init__(self) -> None:
        self.gen_prompts = []
        self.reflect_prompts = []

    def generate_json(self, prompt):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "Structured metric is authoritative."}
        if "Evaluate whether the SQL and result" in prompt:
            self.reflect_prompts.append(prompt)
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The governed metric expression is present.",
                "suggested_fix": None,
            }
        self.gen_prompts.append(prompt)
        for question, case in METRIC_CASES.items():
            if f"User question:\n{question}\n" in prompt:
                return {
                    "sql": case["sql"],
                    "explanation": f"Use governed metric {case['name']}.",
                    "tables_used": [case["table"]],
                }
        raise AssertionError("No metric case matched the prompt")


class MetricToSQLTest(unittest.TestCase):
    def test_count_sum_and_ratio_metrics_match_deterministically(self):
        for question, expected in METRIC_CASES.items():
            with self.subTest(question=question):
                state = metric_context(question)
                result = MetricSearchNode().execute(state)
                self.assertTrue(result.success)
                self.assertEqual(state.metric_matches[0].metric.name, expected["name"])
                self.assertEqual(
                    state.metric_matches[0].metric.aggregation,
                    expected["aggregation"],
                )

    def test_ordinary_field_question_is_not_treated_as_metric(self):
        state = metric_context("What is the original title of this anime?")
        result = MetricSearchNode().execute(state)
        self.assertTrue(result.success)
        self.assertEqual(state.metric_matches, [])
        self.assertNotIn("Matched structured metrics", GenSqlNode._build_prompt(state))

    def test_fanout_group_dimension_fails_before_generation(self):
        state = metric_context("Show watch hours by genre name")
        result = MetricSearchNode().execute(state)
        self.assertFalse(result.success)
        self.assertIn("Fan-out risk", result.error or "")
        self.assertIn("genre.name", result.error or "")
        self.assertIsNone(state.sql_context)

    def test_allowed_multi_hop_group_dimension_is_recorded(self):
        state = metric_context("Show watch hours by studio name")
        result = MetricSearchNode().execute(state)
        self.assertTrue(result.success)
        self.assertEqual(state.metric_requested_dimensions, ["studio.name"])
        self.assertEqual(
            state.metric_join_paths[0].name,
            "watches_to_studio_via_episode_anime",
        )

    def test_metric_time_field_and_resolved_date_reach_gen_prompt(self):
        state = metric_context("What were watch hours last year?")
        result = MetricSearchNode().execute(state)
        self.assertTrue(result.success)
        prompt = GenSqlNode._build_prompt(state)
        self.assertIn(
            '"time_field": "fact_watch_session.watch_date_key"',
            prompt,
        )
        self.assertIn('"start_date": "2025-01-01"', prompt)
        self.assertIn('"end_date": "2025-12-31"', prompt)

    def test_full_workflow_executes_known_count_sum_ratio_results(self):
        llm = MetricWorkflowLLM()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                llm_provider="openai",
                llm_api_key=None,
                llm_model="offline-metrics",
                llm_base_url=None,
                database_path=str(DATABASE),
                history_db_path=str(Path(directory) / "history.sqlite"),
                semantic_model_path=str(SEMANTIC_MODEL),
            )
            runner = WorkflowRunner(
                config,
                llm_factory=lambda _: llm,
                selected_skills=[],
            )
            for question, case in METRIC_CASES.items():
                with self.subTest(question=question):
                    output = runner.run(
                        SqlTask(question=question, database_path=str(DATABASE))
                    )
                    actual = output["rows"][0][0]
                    if case["aggregation"] in {"ratio", "sum"}:
                        self.assertAlmostEqual(actual, case["expected"], places=10)
                    else:
                        self.assertEqual(actual, case["expected"])
                    self.assertEqual(output["metric_search"]["status"], "matched")
                    self.assertEqual(
                        output["metric_search"]["matches"][0]["metric"]["name"],
                        case["name"],
                    )
        self.assertTrue(
            all("Matched structured metrics" in prompt for prompt in llm.gen_prompts)
        )


if __name__ == "__main__":
    unittest.main()
