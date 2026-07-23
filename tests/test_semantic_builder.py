import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from queryforge.domain.semantic import SemanticModel, discover_semantic_model
from queryforge.domain.semantic.builder import SemanticModelBuilder


class SemanticModelBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "analytics.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE dim_region (
                    region_id INTEGER PRIMARY KEY,
                    region_name TEXT NOT NULL
                );
                CREATE TABLE dim_customer (
                    customer_id INTEGER PRIMARY KEY,
                    region_id INTEGER NOT NULL,
                    email TEXT,
                    segment TEXT,
                    FOREIGN KEY (region_id) REFERENCES dim_region(region_id)
                );
                CREATE TABLE fact_order (
                    order_id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL,
                    order_date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    completed_flag INTEGER NOT NULL,
                    FOREIGN KEY (customer_id) REFERENCES dim_customer(customer_id)
                );
                INSERT INTO dim_region VALUES (1, 'East');
                INSERT INTO dim_customer VALUES (10, 1, 'viewer@example.test', 'Premium');
                INSERT INTO fact_order VALUES (100, 10, '2026-01-01', 25.5, 1);
                """
            )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_fresh_build_infers_structure_metrics_pii_and_safe_join_path(self) -> None:
        output = self.root / "semantic_model.yml"
        result = SemanticModelBuilder(self.database, owner="analytics").build(output)

        self.assertTrue(result.published)
        self.assertTrue(output.is_file())
        self.assertTrue(result.report_path.is_file())
        payload = yaml.safe_load(output.read_text(encoding="utf-8"))
        model = SemanticModel.model_validate(payload)
        entities = {entity.name: entity for entity in model.entities}
        self.assertEqual(entities["order"].entity_type, "fact")
        self.assertEqual(entities["order"].effective_grain, ["order_id"])
        self.assertEqual(entities["customer"].hidden_columns, ["email"])
        self.assertEqual(len(model.relationships), 2)
        self.assertTrue(
            any(
                path.from_entity == "order" and path.to_entity == "region"
                for path in model.join_paths
            )
        )
        metric_names = {metric.name for metric in model.metrics}
        self.assertIn("order_count", metric_names)
        self.assertIn("total_amount", metric_names)
        self.assertIn("completed_rate", metric_names)
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["contract"]["blocking_failures"], 0)
        self.assertGreater(len(report["inference_evidence"]), 0)
        self.assertGreater(len(report["review_items"]), 0)

    def test_incremental_build_preserves_curated_metric_and_records_columns(self) -> None:
        output = self.root / "semantic_model.yml"
        existing = self.root / "curated.yml"
        existing.write_text(
            """version: 1
name: curated_orders
entities:
  - name: region
    table: dim_region
    primary_key: [region_id]
    dimensions:
      - {name: region, column: region_name}
  - name: customer
    table: dim_customer
    primary_key: [customer_id]
    hidden_columns: [email]
    dimensions:
      - {name: segment, column: segment}
  - name: order
    table: fact_order
    entity_type: fact
    grain: [order_id]
    dimensions:
      - {name: order_date, column: order_date}
relationships:
  - name: orders_to_customers
    from: fact_order.customer_id
    to: dim_customer.customer_id
metrics:
  - name: governed_revenue
    description: Revenue approved by Finance.
    entity: order
    aggregation: sum
    expression: SUM(fact_order.amount)
""",
            encoding="utf-8",
        )
        result = SemanticModelBuilder(self.database).build(
            output,
            existing_model_path=existing,
        )

        self.assertTrue(result.published)
        model = SemanticModel.model_validate(
            yaml.safe_load(output.read_text(encoding="utf-8"))
        )
        self.assertEqual(model.name, "curated_orders")
        self.assertEqual(
            [metric.name for metric in model.metrics],
            ["governed_revenue"],
        )
        order = next(entity for entity in model.entities if entity.name == "order")
        self.assertIn("amount", order.expected_columns)
        self.assertEqual(
            [dimension.name for dimension in order.dimensions],
            ["order_date"],
        )

    def test_contract_failure_keeps_draft_and_does_not_publish(self) -> None:
        broken = self.root / "broken.sqlite"
        with sqlite3.connect(broken) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                CREATE TABLE dim_parent (
                    parent_id INTEGER PRIMARY KEY
                );
                CREATE TABLE fact_child (
                    child_id INTEGER PRIMARY KEY,
                    parent_id INTEGER,
                    FOREIGN KEY (parent_id) REFERENCES dim_parent(parent_id)
                );
                INSERT INTO fact_child VALUES (1, 999);
                """
            )
        output = self.root / "broken_semantic.yml"
        result = SemanticModelBuilder(broken).build(output)

        self.assertFalse(result.contract_passed)
        self.assertFalse(result.published)
        self.assertFalse(output.exists())
        self.assertTrue(result.draft_path.is_file())
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertGreater(report["contract"]["blocking_failures"], 0)

    def test_database_local_semantic_model_is_discovered(self) -> None:
        sibling = self.root / "semantic_model.yml"
        sibling.write_text("name: placeholder\n", encoding="utf-8")
        self.assertEqual(
            discover_semantic_model(self.database),
            str(sibling.resolve()),
        )
        explicit = self.root / "explicit.yml"
        self.assertEqual(
            discover_semantic_model(self.database, explicit),
            str(explicit),
        )


if __name__ == "__main__":
    unittest.main()
