import tempfile
import unittest
from pathlib import Path
from typing import Any

from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, SqlTask
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.infrastructure.tools.reference_sql_tool import ReferenceSqlTool


SAMPLE_ROOT = Path("sample_data/anime_streaming").resolve()
DATABASE = SAMPLE_ROOT / "anime_streaming.sqlite"
SEMANTIC_MODEL = SAMPLE_ROOT / "semantic_model.yml"

CASES = {
    "What are watch hours by anime format?": {
        "sql": (
            "SELECT a.content_format, ROUND(SUM(w.watch_seconds) / 3600.0, 2) "
            "AS watch_hours FROM fact_watch_session w "
            "JOIN dim_episode e ON w.episode_id = e.episode_id "
            "JOIN dim_anime a ON e.anime_id = a.anime_id "
            "GROUP BY a.content_format ORDER BY watch_hours DESC"
        ),
        "tables_used": ["fact_watch_session", "dim_episode", "dim_anime"],
    },
    "What is completion rate by device?": {
        "sql": (
            "SELECT device_type, ROUND(CAST(SUM(completed_flag) AS REAL) / "
            "NULLIF(COUNT(*), 0), 4) AS completion_rate "
            "FROM fact_watch_session GROUP BY device_type "
            "ORDER BY completion_rate DESC"
        ),
        "tables_used": ["fact_watch_session"],
    },
    "Which studios have the highest average audience rating?": {
        "sql": (
            "SELECT s.studio_name, ROUND(AVG(r.score), 2) AS average_rating "
            "FROM fact_rating r JOIN dim_anime a ON r.anime_id = a.anime_id "
            "JOIN dim_studio s ON a.studio_id = s.studio_id "
            "GROUP BY s.studio_name ORDER BY average_rating DESC LIMIT 10"
        ),
        "tables_used": ["fact_rating", "dim_anime", "dim_studio"],
    },
}


class ReferenceQueryLLM:
    def generate_json(self, prompt: str) -> dict[str, Any]:
        if "Select local QueryForge skills" in prompt:
            return {
                "skills": [],
                "reason": "The governed anime semantic model provides the domain context.",
            }
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The offline reference SQL answers the anime analytics question.",
                "suggested_fix": None,
            }
        for question, query in CASES.items():
            if f"User question:\n{question}\n" in prompt:
                return {**query, "explanation": "Offline anime reference query."}
        raise AssertionError("Question was not present in the generated prompt")


class SampleIntegrationTest(unittest.TestCase):
    def test_merch_question_gets_exact_database_value_hint(self) -> None:
        question = "Show merchandise GMV for Figure products."
        context = Context(
            task=SqlTask(question=question, database_path=str(DATABASE))
        )
        with SQLiteConnector(str(DATABASE)) as connector:
            result = SchemaLinkingNode(
                DatabaseTool(connector), semantic_model_path=str(SEMANTIC_MODEL)
            ).execute(context)
        context.reference_examples = ReferenceSqlTool(str(DATABASE)).find_similar(
            "Which anime titles drive the most merchandise GMV?"
        )
        self.assertTrue(result.success)
        hints = {
            (hint.table_name, hint.column_name): hint.values
            for hint in context.value_hints
        }
        self.assertIn(
            "Figure",
            hints[("dim_merch_product", "product_category")],
        )
        prompt = GenSqlNode._build_prompt(context)
        self.assertIn("Figure", prompt)
        self.assertIn("Many-to-many", prompt)
        self.assertIn("Similar validated question-to-SQL examples", prompt)

    def test_three_anime_questions_complete_full_workflow(self) -> None:
        self.assertTrue(DATABASE.is_file())
        history_directory = tempfile.TemporaryDirectory()
        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline-reference",
            llm_base_url=None,
            database_path=str(DATABASE),
            api_key_env_names=("OPENAI_API_KEY",),
            history_db_path=str(Path(history_directory.name) / "history.sqlite"),
            semantic_model_path=str(SEMANTIC_MODEL),
        )
        runner = WorkflowRunner(config, llm_factory=lambda _: ReferenceQueryLLM())

        for question in CASES:
            with self.subTest(question=question):
                output = runner.run(
                    SqlTask(question=question, database_path=str(DATABASE))
                )
                for field in (
                    "question",
                    "relevant_tables",
                    "sql",
                    "explanation",
                    "rows",
                    "row_count",
                ):
                    self.assertIn(field, output)
                self.assertEqual(output["question"], question)
                self.assertEqual(output["skills_used"], ["sql_best_practices"])
                self.assertEqual(output["skill_selection"]["mode"], "auto")
                self.assertEqual(output["reflection"]["strategy"], "SUCCESS")
                self.assertEqual(len(output["sql_attempt_history"]), 1)
                self.assertGreater(output["row_count"], 0)
        history_directory.cleanup()


if __name__ == "__main__":
    unittest.main()
