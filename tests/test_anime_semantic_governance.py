import tempfile
import unittest
from pathlib import Path

import yaml

from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.node.execute_sql_node import ExecuteSqlNode
from queryforge.workflow.node.metric_search_node import MetricSearchNode
from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, SQLContext, SqlTask
from queryforge.domain.semantic import SemanticModelLoader
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from sample.generate_anime_streaming import (
    EXPECTED_COUNTS,
    TABLES,
    build_database,
    export_csv,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANIME_ROOT = PROJECT_ROOT / "sample_data/anime_streaming"
DATABASE = ANIME_ROOT / "anime_streaming.sqlite"
SEMANTIC_MODEL = ANIME_ROOT / "semantic_model.yml"


def governed_context(question: str) -> Context:
    state = Context(task=SqlTask(question=question, database_path=str(DATABASE)))
    with SQLiteConnector(str(DATABASE)) as connector:
        result = SchemaLinkingNode(
            DatabaseTool(connector), semantic_model_path=str(SEMANTIC_MODEL)
        ).execute(state)
    if not result.success:
        raise AssertionError(result.error)
    return state


ANIME_CASES = {
    "What are watch hours by anime format?": {
        "metric": "watch_hours",
        "dimension": "anime.format",
        "sql": (
            "SELECT a.content_format, "
            "ROUND(SUM(w.watch_seconds) / 3600.0, 2) AS watch_hours "
            "FROM fact_watch_session w "
            "JOIN dim_episode e ON w.episode_id = e.episode_id "
            "JOIN dim_anime a ON e.anime_id = a.anime_id "
            "GROUP BY a.content_format ORDER BY a.content_format"
        ),
        "rows": [
            ["Movie", 10837.24],
            ["ONA", 10782.36],
            ["OVA", 10767.53],
            ["Series", 10796.36],
        ],
    },
    "What is completion rate by device?": {
        "metric": "completion_rate",
        "dimension": "watch_session.device",
        "sql": (
            "SELECT device_type, ROUND(CAST(SUM(completed_flag) AS REAL) / "
            "NULLIF(COUNT(*), 0), 6) AS completion_rate "
            "FROM fact_watch_session GROUP BY device_type ORDER BY device_type"
        ),
        "rows": [
            ["Console", 0.170867],
            ["Mobile", 0.168667],
            ["TV", 0.168233],
            ["Tablet", 0.170833],
            ["Web", 0.1657],
        ],
    },
    "What is average rating by anime format?": {
        "metric": "average_rating",
        "dimension": "anime.format",
        "sql": (
            "SELECT a.content_format, ROUND(AVG(r.score), 4) AS average_rating "
            "FROM fact_rating r JOIN dim_anime a ON r.anime_id = a.anime_id "
            "GROUP BY a.content_format ORDER BY a.content_format"
        ),
        "rows": [
            ["Movie", 7.3592],
            ["ONA", 7.3602],
            ["OVA", 7.3686],
            ["Series", 7.3769],
        ],
    },
    "What is merch GMV by product category?": {
        "metric": "merch_gmv",
        "dimension": "merch_product.category",
        "sql": (
            "SELECT p.product_category, ROUND(SUM(i.net_amount_usd), 2) AS merch_gmv "
            "FROM fact_merch_order_item i JOIN dim_merch_product p "
            "ON i.product_id = p.product_id "
            "GROUP BY p.product_category ORDER BY p.product_category"
        ),
        "rows": [
            ["Accessory", 736868.78],
            ["Apparel", 701419.83],
            ["Blu-ray", 762975.7],
            ["Figure", 723228.44],
            ["Poster", 724358.02],
        ],
    },
    "What is merch GMV by anime format?": {
        "metric": "merch_gmv",
        "dimension": "anime.format",
        "sql": (
            "SELECT a.content_format, ROUND(SUM(i.net_amount_usd), 2) AS merch_gmv "
            "FROM fact_merch_order_item i JOIN dim_merch_product p "
            "ON i.product_id = p.product_id "
            "JOIN dim_anime a ON p.anime_id = a.anime_id "
            "GROUP BY a.content_format ORDER BY a.content_format"
        ),
        "rows": [
            ["Movie", 875685.7],
            ["ONA", 899619.88],
            ["OVA", 905999.04],
            ["Series", 757176.78],
        ],
    },
}


class AnimeLLM:
    def __init__(self) -> None:
        self.gen_prompts = []

    def generate_json(self, prompt):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "Use governed anime platform semantics."}
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "Metric, dimension, and governed Join Path are preserved.",
                "suggested_fix": None,
            }
        self.gen_prompts.append(prompt)
        for question, case in ANIME_CASES.items():
            if f"User question:\n{question}\n" in prompt:
                return {
                    "sql": case["sql"],
                    "explanation": "Use the governed metric and declared relationship.",
                    "tables_used": [],
                }
        raise AssertionError("No governed anime case matched")


class AnimeSemanticGovernanceTest(unittest.TestCase):
    def test_generator_is_deterministic_and_exports_all_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "anime.sqlite"
            counts = build_database(database)
            export_csv(database, root / "tables")
            self.assertEqual(counts, EXPECTED_COUNTS)
            self.assertEqual(sum(counts.values()), 370762)
            for table in TABLES:
                self.assertTrue((root / "tables" / f"{table}.csv").is_file())

    def test_semantic_model_covers_diverse_entities_relationships_and_metrics(self):
        with SQLiteConnector(str(DATABASE)) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(table) for table in tool.list_tables()]
        context = SemanticModelLoader.load_and_validate(
            SEMANTIC_MODEL,
            schemas,
            "What are watch hours by anime format?",
        )
        self.assertEqual(len(context.model.entities), 15)
        self.assertEqual(len(context.model.relationships), 30)
        self.assertEqual(len(context.model.join_paths), 7)
        self.assertEqual(len(context.model.metrics), 11)
        self.assertTrue(
            all(
                relationship.effective_contract.enforcement == "physical_fk"
                for relationship in context.model.relationships
            )
        )
        self.assertTrue(
            all(entity.effective_grain for entity in context.model.entities)
        )
        self.assertIn(
            "anime_to_prequel",
            {relationship.name for relationship in context.model.relationships},
        )
        self.assertIn(
            "follows_to_followed_user",
            {relationship.name for relationship in context.model.relationships},
        )

    def test_cross_entity_dimension_uses_explicit_multi_hop_path(self):
        state = governed_context("What are watch hours by anime format?")
        result = MetricSearchNode().execute(state)
        self.assertTrue(result.success)
        self.assertEqual(state.metric_matches[0].metric.name, "watch_hours")
        self.assertEqual(state.metric_requested_dimensions, ["anime.format"])
        self.assertEqual(
            state.metric_join_paths[0].name, "watches_to_anime_via_episode"
        )
        prompt = GenSqlNode._build_prompt(state)
        self.assertIn("watches_to_episode", prompt)
        self.assertIn("episodes_to_anime", prompt)

    def test_reverse_many_to_many_genre_path_is_blocked_as_fanout(self):
        state = governed_context("What are watch hours by genre name?")
        result = MetricSearchNode().execute(state)
        self.assertFalse(result.success)
        self.assertIn("Fan-out risk", result.error or "")
        self.assertIn("one_to_many", result.error or "")
        self.assertIn("anime_genres_to_anime", result.error or "")

    def test_explicit_safe_three_hop_path_is_resolved(self):
        state = governed_context("What are watch hours by studio name?")
        result = MetricSearchNode().execute(state)
        self.assertTrue(result.success, result.error)
        path = state.metric_join_paths[0]
        self.assertEqual(path.name, "watches_to_studio_via_episode_anime")
        self.assertEqual(
            path.relationships,
            ["watches_to_episode", "episodes_to_anime", "anime_to_studio"],
        )
        self.assertEqual(
            path.tables,
            ["fact_watch_session", "dim_episode", "dim_anime", "dim_studio"],
        )
        self.assertTrue(path.safe)
        self.assertTrue(path.explicit)

    def test_invalid_physical_cardinality_contract_fails_at_load_time(self):
        payload = yaml.safe_load(SEMANTIC_MODEL.read_text(encoding="utf-8"))
        payload["relationships"][0]["from"] = "dim_episode.episode_id"
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "invalid_contract.yml"
            model_path.write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
            with SQLiteConnector(str(DATABASE)) as connector:
                tool = DatabaseTool(connector)
                schemas = [tool.describe_table(table) for table in tool.list_tables()]
            with self.assertRaisesRegex(
                ValueError, "requires physical FK dim_episode.episode_id"
            ):
                SemanticModelLoader.load_and_validate(
                    model_path, schemas, "What are watch hours?"
                )

    def test_execution_guard_blocks_llm_added_rating_fanout(self):
        state = governed_context("What are watch hours?")
        self.assertTrue(MetricSearchNode().execute(state).success)
        state.sql_context = SQLContext(
            sql=(
                "SELECT SUM(w.watch_seconds) FROM fact_watch_session w "
                "JOIN dim_episode e ON w.episode_id = e.episode_id "
                "JOIN dim_anime a ON e.anime_id = a.anime_id "
                "JOIN fact_rating r ON a.anime_id = r.anime_id"
            ),
            explanation="Unsafe model-added one-to-many rating join.",
            tables_used=[
                "fact_watch_session",
                "dim_episode",
                "dim_anime",
                "fact_rating",
            ],
        )
        with SQLiteConnector(str(DATABASE)) as connector:
            result = ExecuteSqlNode(DatabaseTool(connector)).execute(state)
        self.assertFalse(result.success)
        self.assertIn("Fan-out execution guard", result.error or "")
        self.assertIn("one_to_many", result.error or "")
        self.assertIsNone(state.execution_result)

    def test_execution_guard_requires_every_explicit_path_table(self):
        state = governed_context("What is merch GMV by anime format?")
        self.assertTrue(MetricSearchNode().execute(state).success)
        state.sql_context = SQLContext(
            sql=(
                "SELECT dim_anime.content_format, "
                "SUM(fact_merch_order_item.net_amount_usd) "
                "FROM fact_merch_order_item, dim_anime "
                "GROUP BY dim_anime.content_format"
            ),
            explanation="Missing the governed product bridge table.",
            tables_used=["fact_merch_order_item", "dim_anime"],
        )
        with SQLiteConnector(str(DATABASE)) as connector:
            result = ExecuteSqlNode(DatabaseTool(connector)).execute(state)
        self.assertFalse(result.success)
        self.assertIn("omitted required table", result.error or "")
        self.assertIn("dim_merch_product", result.error or "")

    def test_hidden_user_email_is_not_visible_to_gen_sql(self):
        state = governed_context("List viewer email")
        prompt = GenSqlNode._build_prompt(state)
        schema_block = prompt.split("Current SQLite schema (authoritative):", 1)[1].split(
            "Validated semantic model", 1
        )[0]
        self.assertNotIn('"name": "email"', schema_block)

    def test_real_join_queries_return_stable_governed_results(self):
        llm = AnimeLLM()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                llm_provider="openai",
                llm_api_key=None,
                llm_model="offline-anime",
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
            for question, case in ANIME_CASES.items():
                with self.subTest(question=question):
                    output = runner.run(
                        SqlTask(question=question, database_path=str(DATABASE))
                    )
                    self.assertEqual(output["rows"], case["rows"])
                    self.assertEqual(
                        output["metric_search"]["matches"][0]["metric"]["name"],
                        case["metric"],
                    )
                    self.assertEqual(
                        output["metric_search"]["requested_dimensions"],
                        [case["dimension"]],
                    )
        self.assertTrue(all("relationships" in prompt for prompt in llm.gen_prompts))


if __name__ == "__main__":
    unittest.main()
