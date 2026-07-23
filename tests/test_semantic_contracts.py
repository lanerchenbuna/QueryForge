import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.domain.semantic import SemanticContractValidator, SemanticModel


class SemanticContractValidatorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "contracts.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE customers (
                    customer_id INTEGER,
                    email TEXT,
                    age INTEGER
                );
                CREATE TABLE orders (
                    order_id INTEGER,
                    customer_id INTEGER,
                    amount REAL
                );
                INSERT INTO customers VALUES (1, 'ada@example.test', 30);
                INSERT INTO customers VALUES (2, NULL, 31);
                INSERT INTO orders VALUES (101, 1, 10.0);
                INSERT INTO orders VALUES (102, 99, -1.0);
                """
            )

    def tearDown(self):
        self.directory.cleanup()

    def test_contract_metadata_and_physical_checks_detect_operational_failures(self):
        model = SemanticModel.model_validate(
            {
                "name": "contracts",
                "entities": [
                    {
                        "name": "customer",
                        "table": "customers",
                        "primary_key": ["customer_id"],
                        "expected_columns": ["customer_id", "email", "age", "missing"],
                        "allow_additive_columns": False,
                        "owner": "customer-data",
                        "sla": "P1D",
                        "refresh_frequency": "daily",
                        "sensitivity": "confidential",
                        "contract_version": "2.0",
                        "dimensions": [
                            {
                                "name": "email",
                                "column": "email",
                                "owner": "customer-data",
                                "sla": "P1D",
                                "refresh_frequency": "daily",
                                "sensitivity": "confidential",
                                "contract_version": "2.0",
                                "quality_rules": [
                                    {
                                        "rule": "null_rate",
                                        "max_null_rate": 0.0,
                                    },
                                    {"rule": "unique"},
                                ],
                            }
                        ],
                    },
                    {
                        "name": "order",
                        "table": "orders",
                        "entity_type": "fact",
                        "grain": ["order_id"],
                        "expected_columns": ["order_id", "customer_id", "amount"],
                        "owner": "sales-data",
                        "sla": "PT1H",
                        "refresh_frequency": "hourly",
                        "sensitivity": "internal",
                        "dimensions": [
                            {
                                "name": "amount",
                                "column": "amount",
                                "quality_rules": [
                                    {"rule": "range", "minimum": 0},
                                ],
                            }
                        ],
                    },
                ],
                "relationships": [
                    {
                        "name": "orders_to_customers",
                        "from": "orders.customer_id",
                        "to": "customers.customer_id",
                    }
                ],
                "join_paths": [
                    {
                        "name": "orders_to_customer",
                        "from_entity": "order",
                        "to_entity": "customer",
                        "relationships": ["orders_to_customers"],
                        "owner": "sales-data",
                        "sla": "PT1H",
                        "refresh_frequency": "hourly",
                        "sensitivity": "internal",
                        "contract_version": "2.0",
                        "quality_rules": [
                            {
                                "rule": "foreign_key",
                                "column": "orders.customer_id",
                                "referenced_table": "customers",
                                "referenced_column": "customer_id",
                            }
                        ],
                    }
                ],
                "metrics": [
                    {
                        "name": "revenue",
                        "description": "Governed order amount.",
                        "entity": "order",
                        "aggregation": "sum",
                        "expression": "SUM(orders.amount)",
                        "owner": "sales-data",
                        "sla": "PT1H",
                        "refresh_frequency": "hourly",
                        "sensitivity": "internal",
                        "contract_version": "2.0",
                        "quality_rules": [
                            {
                                "rule": "range",
                                "column": "amount",
                                "minimum": 0,
                            }
                        ],
                    }
                ],
            }
        )

        report = SemanticContractValidator.validate(model, self.database)
        self.assertFalse(report.passed)
        failures = {(check.subject, check.rule) for check in report.blocking_failures}
        self.assertIn(("customer", "schema_drift"), failures)
        self.assertIn(("customer.email", "null_rate"), failures)
        self.assertIn(("order.amount", "range"), failures)
        self.assertIn(("revenue", "range"), failures)
        self.assertIn(("orders_to_customers", "foreign_key"), failures)
        self.assertIn(
            ("orders_to_customer:orders_to_customers", "foreign_key"),
            failures,
        )
        self.assertEqual(model.metrics[0].owner, "sales-data")
        self.assertEqual(model.join_paths[0].contract_version, "2.0")
        self.assertEqual(model.entities[0].dimensions[0].sensitivity, "confidential")

    def test_operational_contract_passes_after_data_is_reconciled(self):
        model = SemanticModel.model_validate(
            {
                "name": "valid_contracts",
                "entities": [
                    {
                        "name": "customer",
                        "table": "customers",
                        "primary_key": ["customer_id"],
                        "expected_columns": ["customer_id", "email", "age"],
                        "allow_additive_columns": False,
                    },
                    {
                        "name": "order",
                        "table": "orders",
                        "entity_type": "fact",
                        "grain": ["order_id"],
                        "expected_columns": ["order_id", "customer_id", "amount"],
                        "dimensions": [
                            {
                                "name": "amount",
                                "column": "amount",
                                "quality_rules": [
                                    {"rule": "range", "minimum": 0},
                                ],
                            }
                        ],
                    },
                ],
                "relationships": [
                    {
                        "name": "orders_to_customers",
                        "from": "orders.customer_id",
                        "to": "customers.customer_id",
                    }
                ],
            }
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE customers SET email = 'ben@example.test' WHERE customer_id = 2"
            )
            connection.execute("UPDATE orders SET customer_id = 2, amount = 1 WHERE order_id = 102")

        report = SemanticContractValidator.validate(model, self.database)
        self.assertTrue(report.passed)
        self.assertGreater(report.summary()["passed_checks"], 0)


if __name__ == "__main__":
    unittest.main()
