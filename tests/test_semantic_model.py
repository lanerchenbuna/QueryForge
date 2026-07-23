import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, SqlTask
from queryforge.domain.semantic import SemanticModelLoader
from queryforge.infrastructure.tools.database_tool import DatabaseTool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "sample_data/anime_streaming"
SAMPLE_DATABASE = SAMPLE_ROOT / "anime_streaming.sqlite"
SAMPLE_SEMANTIC_MODEL = SAMPLE_ROOT / "semantic_model.yml"


class SemanticWorkflowLLM:
    def __init__(self) -> None:
        self.gen_prompt = ""

    def generate_json(self, prompt):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "Semantic model is sufficient."}
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "Declared multi-hop join answers the question.",
                "suggested_fix": None,
            }
        self.gen_prompt = prompt
        return {
            "sql": (
                "SELECT s.studio_name, ROUND(AVG(r.score), 2) AS average_rating "
                "FROM fact_rating r JOIN dim_anime a ON r.anime_id = a.anime_id "
                "JOIN dim_studio s ON a.studio_id = s.studio_id "
                "GROUP BY s.studio_name ORDER BY AVG(r.score) DESC LIMIT 1"
            ),
            "explanation": "Use rating-to-anime and anime-to-studio relationships.",
            "tables_used": ["fact_rating", "dim_anime", "dim_studio"],
        }


class SemanticModelTest(unittest.TestCase):
    def test_sample_model_validates_and_matches_business_synonyms(self):
        with SQLiteConnector(str(SAMPLE_DATABASE)) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(table) for table in tool.list_tables()]
        context = SemanticModelLoader.load_and_validate(
            SAMPLE_SEMANTIC_MODEL,
            schemas,
            "Show anime title, studio country, and content format.",
        )
        mappings = {
            (match.semantic_name, match.table, match.column)
            for match in context.matches
        }
        self.assertIn(("title", "dim_anime", "title"), mappings)
        self.assertIn(("format", "dim_anime", "content_format"), mappings)
        self.assertIn(("country", "dim_studio", "country"), mappings)
        relationship_names = {item.name for item in context.model.relationships}
        self.assertIn("anime_to_studio", relationship_names)
        self.assertIn("anime_genres_to_genre", relationship_names)

    def test_invalid_physical_column_fails_before_sql_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "semantic.yml"
            model_path.write_text(
                """version: 1
name: invalid
entities:
  - name: item
    table: items
    dimensions:
      - name: missing
        column: does_not_exist
""",
                encoding="utf-8",
            )
            database = Path(directory) / "items.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE items (id INTEGER, name TEXT)")
            connection.commit()
            connection.close()
            state = Context(
                task=SqlTask(question="List missing", database_path=str(database))
            )
            with SQLiteConnector(str(database)) as connector:
                result = SchemaLinkingNode(
                    DatabaseTool(connector), semantic_model_path=str(model_path)
                ).execute(state)
        self.assertFalse(result.success)
        self.assertIn("items.does_not_exist", result.error or "")
        self.assertIsNone(state.sql_context)

    def test_hidden_columns_remain_physical_but_are_absent_from_gen_prompt(self):
        state = Context(
            task=SqlTask(
                question="Show viewer region",
                database_path=str(SAMPLE_DATABASE),
            )
        )
        with SQLiteConnector(str(SAMPLE_DATABASE)) as connector:
            result = SchemaLinkingNode(
                DatabaseTool(connector),
                semantic_model_path=str(SAMPLE_SEMANTIC_MODEL),
            ).execute(state)
        self.assertTrue(result.success)
        users = next(
            schema for schema in state.relevant_tables if schema.table_name == "dim_user"
        )
        self.assertIn("email", {column.name for column in users.columns})
        prompt = GenSqlNode._build_prompt(state)
        schema_block = prompt.split("Current SQLite schema (authoritative):", 1)[1].split(
            "Validated semantic model", 1
        )[0]
        self.assertNotIn('"name": "email"', schema_block)
        self.assertIn('"name": "region"', prompt)
        self.assertIn('"column": "region"', prompt)

    def test_full_workflow_uses_declared_join_and_exposes_semantic_status(self):
        llm = SemanticWorkflowLLM()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                llm_provider="openai",
                llm_api_key=None,
                llm_model="offline-semantic",
                llm_base_url=None,
                database_path=str(SAMPLE_DATABASE),
                history_db_path=str(Path(directory) / "history.sqlite"),
                semantic_model_path=str(SAMPLE_SEMANTIC_MODEL),
            )
            output = WorkflowRunner(
                config,
                llm_factory=lambda _: llm,
                selected_skills=[],
            ).run(
                SqlTask(
                    question="Which studio has the highest average audience rating?",
                    database_path=str(SAMPLE_DATABASE),
                )
            )
        self.assertEqual(output["rows"], [["Studio T-20", 7.44]])
        self.assertEqual(output["semantic_model"]["status"], "active")
        self.assertEqual(output["semantic_model"]["name"], "anime_streaming")
        self.assertIn("ratings_to_anime", llm.gen_prompt)
        self.assertIn("anime_to_studio", llm.gen_prompt)

    def test_no_model_preserves_original_schema_linking_behavior(self):
        state = Context(
            task=SqlTask(
                question="How many anime titles?",
                database_path=str(SAMPLE_DATABASE),
            )
        )
        with SQLiteConnector(str(SAMPLE_DATABASE)) as connector:
            result = SchemaLinkingNode(DatabaseTool(connector)).execute(state)
        self.assertTrue(result.success)
        self.assertIsNone(state.semantic_model)
        prompt = GenSqlNode._build_prompt(state)
        self.assertNotIn("Validated semantic model", prompt)
        self.assertNotIn("Semantic model rules", prompt)


if __name__ == "__main__":
    unittest.main()
