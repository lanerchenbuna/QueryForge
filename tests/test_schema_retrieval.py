"""Step-05 tests: deterministic schema retrieval (recall, rank, prune, evidence)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.core.schemas.models import Context, SqlTask
from queryforge.domain.semantic import (
    SchemaRetriever,
    SemanticModelLoader,
)
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.node.metric_search_node import MetricSearchNode
from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANIME_ROOT = PROJECT_ROOT / "sample_data/anime_streaming"
ANIME_DATABASE = ANIME_ROOT / "anime_streaming.sqlite"
ANIME_SEMANTIC_MODEL = ANIME_ROOT / "semantic_model.yml"

FILLER_TABLES = tuple(f"zz_extra_{index:02d}" for index in range(1, 57))
DIM_PRODUCT_ATTRIBUTES = tuple(f"attr_{index:02d}" for index in range(1, 41))

WIDE_SEMANTIC_MODEL = """version: 1
name: wide_fixture
entities:
  - name: order
    table: fact_orders
    entity_type: fact
    primary_key: [order_id]
    grain: [order_id]
    hidden_columns: [internal_note]
    dimensions:
      - name: status
        column: status
  - name: product
    table: dim_product
    primary_key: [product_id]
    dimensions:
      - name: category
        column: product_category
  - name: region
    table: zzz_dim_region
    primary_key: [region_id]
    dimensions:
      - name: name
        column: region_name
relationships:
  - name: orders_to_product
    from: fact_orders.product_id
    to: dim_product.product_id
    relationship_type: many_to_one
  - name: products_to_region
    from: dim_product.region_id
    to: zzz_dim_region.region_id
    relationship_type: many_to_one
join_paths:
  - name: orders_to_region_via_product
    from_entity: order
    to_entity: region
    relationships: [orders_to_product, products_to_region]
metrics:
  - name: order_revenue
    description: Paid order revenue.
    entity: order
    aggregation: sum
    expression: SUM(fact_orders.amount_usd)
    synonyms: [order revenue, revenue]
    default_filters: ["fact_orders.status = 'paid'"]
    allowed_dimensions: [product.category, region.name]
    time_field: fact_orders.order_date
"""


def build_wide_database(path: Path) -> None:
    """Create a 60-table fixture with a wide table and a sorted-last bridge table."""
    attributes = ", ".join(f"{name} TEXT" for name in DIM_PRODUCT_ATTRIBUTES)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE fact_orders ("
        "order_id INTEGER PRIMARY KEY, "
        "product_id INTEGER NOT NULL REFERENCES dim_product(product_id), "
        "region_id INTEGER NOT NULL REFERENCES zzz_dim_region(region_id), "
        "status TEXT NOT NULL, amount_usd REAL NOT NULL, "
        "order_date TEXT NOT NULL, internal_note TEXT)"
    )
    connection.execute(
        "CREATE TABLE dim_product ("
        "product_id INTEGER PRIMARY KEY, product_name TEXT, "
        f"product_category TEXT, region_id INTEGER REFERENCES zzz_dim_region(region_id), {attributes})"
    )
    connection.execute(
        "CREATE TABLE zzz_dim_region (region_id INTEGER PRIMARY KEY, region_name TEXT)"
    )
    connection.execute(
        "CREATE TABLE dim_supplier (supplier_id INTEGER PRIMARY KEY, "
        "supplier_name TEXT, supplier_tier TEXT)"
    )
    for table in FILLER_TABLES:
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload TEXT)")
    connection.execute(
        "INSERT INTO zzz_dim_region VALUES (1, 'North')"
    )
    connection.execute(
        "INSERT INTO dim_product (product_id, product_name, product_category, region_id) "
        "VALUES (1, 'Widget', 'Hardware', 1)"
    )
    connection.execute(
        "INSERT INTO dim_supplier VALUES (1, 'Acme', 'gold')"
    )
    connection.execute(
        "INSERT INTO fact_orders VALUES (1, 1, 1, 'paid', 12.5, '2025-01-01', 'internal')"
    )
    connection.commit()
    connection.close()


class WideSchemaRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "wide.sqlite"
        build_wide_database(self.database)
        self.model_path = self.root / "semantic.yml"
        self.model_path.write_text(WIDE_SEMANTIC_MODEL, encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def schemas_and_model(self, question: str):
        with SQLiteConnector(str(self.database)) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(name) for name in tool.list_tables()]
        self.assertEqual(len(schemas), 60)
        semantic_model = SemanticModelLoader.load_and_validate(
            self.model_path, schemas, question
        )
        return schemas, semantic_model

    def test_required_column_closure_keeps_metric_join_and_time_columns(self):
        question = "What is order revenue by region name?"
        schemas, semantic_model = self.schemas_and_model(question)
        result = SchemaRetriever().retrieve(
            schemas, question, semantic_model=semantic_model
        )
        self.assertEqual(result.mode, "semantic")
        selected = {schema.table_name: schema for schema in result.selected_tables}
        # metric base + bridge + requested dimension entity are all required.
        self.assertEqual(
            set(result.required_tables),
            {"fact_orders", "dim_product", "zzz_dim_region"},
        )
        order_columns = {column.name for column in selected["fact_orders"].columns}
        self.assertTrue(
            {"amount_usd", "status", "order_date", "order_id", "product_id"}.issubset(
                order_columns
            )
        )
        product_columns = {column.name for column in selected["dim_product"].columns}
        self.assertTrue(
            {"product_id", "product_category", "region_id"}.issubset(product_columns)
        )
        region_columns = {column.name for column in selected["zzz_dim_region"].columns}
        self.assertIn("region_name", region_columns)

    def test_hidden_columns_are_pruned_and_recorded(self):
        question = "What is order revenue by product category?"
        schemas, semantic_model = self.schemas_and_model(question)
        result = SchemaRetriever().retrieve(
            schemas, question, semantic_model=semantic_model
        )
        selected = {schema.table_name: schema for schema in result.selected_tables}
        order_columns = {column.name for column in selected["fact_orders"].columns}
        self.assertNotIn("internal_note", order_columns)
        self.assertIn("internal_note", result.omitted_columns["fact_orders"])
        self.assertIn("internal_note", result.evidence["omitted_columns"]["fact_orders"])
        selection = result.selection_by_table()["fact_orders"]
        self.assertIn("internal_note", selection.omitted_columns)
        self.assertTrue(selection.required)

    def test_table_and_column_budget_are_respected_without_alphabetical_truncation(self):
        question = "What is order revenue by region name?"
        schemas, semantic_model = self.schemas_and_model(question)
        result = SchemaRetriever(max_tables=50).retrieve(
            schemas, question, semantic_model=semantic_model
        )
        self.assertLessEqual(len(result.selected_tables), 50)
        names = set(result.selected_table_names)
        self.assertTrue(
            {"fact_orders", "dim_product", "zzz_dim_region"}.issubset(names)
        )
        # the required bridge table sorts after every filler table and survives.
        self.assertIn("zzz_dim_region", names)
        self.assertTrue(result.omitted_tables)
        self.assertTrue(
            any(table in FILLER_TABLES for table in result.omitted_tables)
        )
        self.assertFalse(
            set(result.required_tables) & set(result.omitted_tables)
        )
        self.assertTrue(result.evidence["budget_respected"])

        pruned = SchemaRetriever(max_columns_per_table=5).retrieve(
            schemas, question, semantic_model=semantic_model
        )
        product = {
            schema.table_name: schema for schema in pruned.selected_tables
        }["dim_product"]
        kept = {column.name for column in product.columns}
        self.assertLessEqual(len(kept), 5)
        self.assertTrue({"product_id", "product_category", "region_id"}.issubset(kept))
        self.assertLess(len(kept), len(DIM_PRODUCT_ATTRIBUTES))

    def test_question_term_recall_without_semantic_hits_does_not_fabricate_matches(self):
        question = "How many supplier tier records are there?"
        schemas, semantic_model = self.schemas_and_model(question)
        result = SchemaRetriever().retrieve(
            schemas, question, semantic_model=semantic_model
        )
        self.assertEqual(result.mode, "lexical_fallback")
        self.assertIn("dim_supplier", result.selected_table_names)
        selection = result.selection_by_table()["dim_supplier"]
        self.assertIn("question_terms", selection.recalled_by)
        self.assertFalse(
            any(
                reason.startswith("semantic_") or reason.startswith("metric_base")
                for reason in selection.recalled_by
            )
        )
        self.assertIn("dim_supplier", result.evidence["question_terms"]["overlap_tables"])

    def test_passthrough_without_semantic_model_returns_full_schema(self):
        question = "What is order revenue by region name?"
        schemas, _ = self.schemas_and_model(question)
        result = SchemaRetriever().retrieve(schemas, question, semantic_model=None)
        self.assertEqual(result.mode, "passthrough")
        self.assertEqual(result.selected_table_names, [s.table_name for s in schemas])
        order = next(
            schema for schema in result.selected_tables if schema.table_name == "fact_orders"
        )
        self.assertIn("internal_note", {column.name for column in order.columns})
        self.assertEqual(result.omitted_tables, [])
        self.assertEqual(result.omitted_columns, {})
        self.assertEqual(result.evidence["mode"], "passthrough")

    def test_linking_node_records_evidence_and_metric_requirements(self):
        question = "What is order revenue by region name?"
        context = Context(
            task=SqlTask(question=question, database_path=str(self.database))
        )
        with SQLiteConnector(str(self.database)) as connector:
            result = SchemaLinkingNode(
                DatabaseTool(connector),
                semantic_model_path=str(self.model_path),
            ).execute(context)
        self.assertTrue(result.success, result.error)
        evidence = context.task_context["schema_retrieval"]
        self.assertEqual(evidence["mode"], "semantic")
        self.assertEqual(
            evidence["selected_table_names"],
            [schema.table_name for schema in context.relevant_tables],
        )
        self.assertEqual(
            evidence["selected_count"], len(context.relevant_tables)
        )
        self.assertIn("omitted_tables", evidence)
        self.assertGreater(evidence["omitted_columns_count"], 0)

        metric_result = MetricSearchNode().execute(context)
        self.assertTrue(metric_result.success, metric_result.error)
        requirements = context.task_context["schema_retrieval"]["metric_requirements"]
        self.assertEqual(requirements["metrics"], ["order_revenue"])
        self.assertTrue(
            {"fact_orders", "dim_product", "zzz_dim_region"}.issubset(
                set(requirements["tables"])
            )
        )
        self.assertEqual(requirements["missing_tables"], [])
        self.assertEqual(requirements["requested_dimensions"], ["region.name"])
        self.assertIn(
            "orders_to_region_via_product",
            {path["name"] for path in requirements["join_paths"]},
        )

    def test_context_schema_keeps_hidden_columns_but_prompt_does_not(self):
        question = "What is order revenue by product category?"
        context = Context(
            task=SqlTask(question=question, database_path=str(self.database))
        )
        with SQLiteConnector(str(self.database)) as connector:
            result = SchemaLinkingNode(
                DatabaseTool(connector),
                semantic_model_path=str(self.model_path),
            ).execute(context)
        self.assertTrue(result.success, result.error)
        order = next(
            schema
            for schema in context.relevant_tables
            if schema.table_name == "fact_orders"
        )
        # governance-hidden columns stay part of the physical context schema...
        self.assertIn("internal_note", {column.name for column in order.columns})
        # ...but never reach the generation prompt.
        prompt = GenSqlNode._build_prompt(context)
        schema_block = prompt.split("Current SQLite schema (authoritative):", 1)[1].split(
            "Validated semantic model", 1
        )[0]
        self.assertNotIn("internal_note", schema_block)


class AnimeSampleRetrievalTest(unittest.TestCase):
    """Step 05-N1: the canonical anime question still yields its required tables."""

    def setUp(self) -> None:
        self.assertTrue(ANIME_DATABASE.is_file())

    def test_multi_hop_question_recalls_bridge_table_and_dimension(self):
        question = "What are watch hours by anime format?"
        with SQLiteConnector(str(ANIME_DATABASE)) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(name) for name in tool.list_tables()]
        semantic_model = SemanticModelLoader.load_and_validate(
            ANIME_SEMANTIC_MODEL, schemas, question
        )
        result = SchemaRetriever().retrieve(
            schemas, question, semantic_model=semantic_model
        )
        names = set(result.selected_table_names)
        self.assertTrue(
            {"fact_watch_session", "dim_episode", "dim_anime"}.issubset(names)
        )
        self.assertIn("watches_to_anime_via_episode", result.evidence["join_paths"])
        for unrelated in ("fact_merch_order_item", "dim_merch_product", "fact_merch_order"):
            self.assertNotIn(unrelated, names)
        self.assertLess(len(names), len(schemas))

    def test_linking_node_and_history_scope_stay_consistent(self):
        context = Context(
            task=SqlTask(
                question="What are watch hours by anime format?",
                database_path=str(ANIME_DATABASE),
            )
        )
        with SQLiteConnector(str(ANIME_DATABASE)) as connector:
            result = SchemaLinkingNode(
                DatabaseTool(connector),
                semantic_model_path=str(ANIME_SEMANTIC_MODEL),
            ).execute(context)
        self.assertTrue(result.success, result.error)
        loaded = {schema.table_name for schema in context.relevant_tables}
        self.assertTrue(
            {"fact_watch_session", "dim_episode", "dim_anime"}.issubset(loaded)
        )
        prompt = GenSqlNode._build_prompt(context)
        schema_block = prompt.split("Current SQLite schema (authoritative):", 1)[1].split(
            "Validated semantic model", 1
        )[0]
        for table in ("fact_watch_session", "dim_episode", "dim_anime"):
            self.assertIn(table, schema_block)
        self.assertNotIn("fact_merch_order_item", schema_block)

    def test_hidden_viewer_email_is_pruned_from_retrieval_selection(self):
        question = "List viewer email"
        with SQLiteConnector(str(ANIME_DATABASE)) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(name) for name in tool.list_tables()]
        semantic_model = SemanticModelLoader.load_and_validate(
            ANIME_SEMANTIC_MODEL, schemas, question
        )
        result = SchemaRetriever().retrieve(
            schemas, question, semantic_model=semantic_model
        )
        users = next(
            schema for schema in result.selected_tables if schema.table_name == "dim_user"
        )
        self.assertNotIn("email", {column.name for column in users.columns})
        self.assertIn("email", result.omitted_columns["dim_user"])


if __name__ == "__main__":
    unittest.main()
