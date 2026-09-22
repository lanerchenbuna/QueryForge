"""Offline tests for the data-domain publication service (step 03)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from queryforge.application.publish_service import PublishError, PublishService
from queryforge.core.config import Config
from queryforge.domain.domains import DomainResolver
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector

CSV = "id,name\n1,alpha\n2,beta\n"
CSV_CHANGED = "id,name\n1,alpha\n2,beta\n3,gamma\n"

CONTRACT = {
    "entity": "items",
    "description": "Items catalog.",
    "owner": "data-platform",
    "reviewed_by": "tester",
    "sensitivity": "internal",
    "grain": ["id"],
    "primaryKey": ["id"],
    "dimensions": [
        {"name": "id", "column": "id"},
        {"name": "name", "column": "name"},
    ],
    "metrics": [
        {
            "name": "item_count",
            "description": "Number of items.",
            "aggregation": "count",
            "expression": "COUNT(items.id)",
        }
    ],
}


class PublishServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.root / "unused.sqlite"),
            history_db_path=str(self.root / "history.db"),
            orchestration_state_root=str(self.root / "runs"),
            domain_registry_path=str(self.root / "domains-registry.json"),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def service(self) -> PublishService:
        return PublishService(config_loader=lambda **_: self.config)

    def test_publish_builds_a_queryable_registered_version(self):
        result = self.service().publish(
            domain_id="retail",
            files=[("items.csv", CSV.encode())],
            contract=CONTRACT,
        )
        self.assertEqual(result.status, "published")
        self.assertEqual(result.domain_id, "retail")
        context = DomainResolver.from_config(self.config).resolve("retail")
        self.assertEqual(context.data_version, result.data_version)
        self.assertEqual(context.database_path, result.database_path)
        self.assertTrue(Path(context.semantic_model_path or "").is_file())
        with SQLiteConnector(result.database_path) as connector:
            rows = connector.execute_sql(
                "SELECT id, name FROM items ORDER BY id"
            ).rows
        self.assertEqual(rows, [[1, "alpha"], [2, "beta"]])
        self.assertEqual(result.assets[0]["status"], "success")

    def test_failed_publish_preserves_previous_version(self):
        service = self.service()
        first = service.publish(
            domain_id="retail",
            files=[("items.csv", CSV.encode())],
            contract=CONTRACT,
        )
        invalid_contract = dict(CONTRACT)
        invalid_contract.pop("dimensions")
        with self.assertRaises(PublishError):
            service.publish(
                domain_id="retail",
                files=[("items.csv", CSV_CHANGED.encode())],
                contract=invalid_contract,
            )
        context = DomainResolver.from_config(self.config).resolve("retail")
        self.assertEqual(context.data_version, first.data_version)

    def test_fingerprint_changes_with_file_content(self):
        service = self.service()
        first = service.publish(
            domain_id="retail",
            files=[("items.csv", CSV.encode())],
            contract=CONTRACT,
        )
        second = service.publish(
            domain_id="retail",
            files=[("items.csv", CSV_CHANGED.encode())],
            contract=CONTRACT,
        )
        self.assertNotEqual(first.schema_fingerprint, second.schema_fingerprint)
        self.assertNotEqual(first.data_version, second.data_version)

    def test_validation_rejects_bad_inputs(self):
        service = self.service()
        with self.assertRaises(PublishError):
            service.publish(
                domain_id="BAD DOMAIN!",
                files=[("items.csv", CSV.encode())],
                contract=CONTRACT,
            )
        with self.assertRaises(PublishError):
            service.publish(
                domain_id="retail",
                files=[("items.txt", b"nope")],
                contract=CONTRACT,
            )
        with self.assertRaises(PublishError):
            service.publish(
                domain_id="retail",
                files=[("items.csv", b"x" * (25 * 1024 * 1024 + 1))],
                contract=CONTRACT,
            )
        contract = dict(CONTRACT)
        contract.pop("reviewed_by")
        with self.assertRaises(PublishError):
            service.publish(
                domain_id="retail",
                files=[("items.csv", CSV.encode())],
                contract=contract,
            )
        with self.assertRaises(PublishError):
            service.publish(domain_id="retail", files=[], contract=CONTRACT)

    def test_unknown_domain_is_not_published_but_resolver_stays_empty(self):
        resolver = DomainResolver.from_config(self.config)
        self.assertEqual(resolver.list_domains(), [])
        with self.assertRaises(Exception):
            resolver.resolve("missing")


if __name__ == "__main__":
    unittest.main()
