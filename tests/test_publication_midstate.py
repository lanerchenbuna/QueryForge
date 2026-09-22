"""Offline tests for interrupted-publication detection and reconciliation (step 15).

A publication batch is atomic across three artifacts: the publish database, the
metadata registry (watermarks + ``semantic_catalog``), and the semantic model
file. These tests simulate a process that died mid-batch and assert that the
builder refuses to publish on top of that residue, that the refusal happens
before any mutation, and that reconciliation is explicit and reviewable.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.data_assets.models import AssetBuildConfig, DataAssetError
from queryforge.data_assets.pipeline import DataAssetBuilder


class PublicationMidStateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.publish_database = self.root / "warehouse.sqlite"
        self.state_root = self.root / "asset_state"
        self.semantic_path = self.root / "semantic.yml"
        self.csv_path = self.root / "watch_events.csv"
        self._write_csv(
            [
                "Event ID,Anime Title,Watched At,Watch Seconds",
                "1,Azure Voyager,2024-01-01,10.5",
                "3,Crimson Horizon,2024-01-04,13",
            ]
        )
        self.builder = DataAssetBuilder(self.publish_database, self.state_root)
        [self.first] = self.builder.build_all(self._config(), self.semantic_path)
        self.assertEqual(self.first.status, "success")

    def tearDown(self):
        self.directory.cleanup()

    # ------------------------------------------------------------------ helpers

    def _write_csv(self, lines: list[str]) -> None:
        self.csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _config(self) -> AssetBuildConfig:
        return AssetBuildConfig.model_validate(
            {
                "semantic_model": {
                    "name": "anime_uploads",
                    "description": "Reviewed anime upload semantics.",
                    "owner": "analytics",
                    "reviewed": True,
                },
                "assets": [
                    {
                        "name": "watch_events",
                        "target_table": "fact_watch_events",
                        "source": {"type": "csv", "path": str(self.csv_path)},
                        "column_aliases": {
                            "Event ID": "event_id",
                            "Anime Title": "anime_title",
                            "Watched At": "watched_at",
                        },
                        "quality": {
                            "required_columns": [
                                "event_id",
                                "anime_title",
                                "watched_at",
                            ],
                            "unique_key": ["event_id"],
                        },
                        "publish_mode": "replace",
                        "semantic": {
                            "entity_name": "watch_events",
                            "entity_type": "fact",
                            "description": "Reviewed anime playback events.",
                            "grain": ["event_id"],
                            "owner": "analytics",
                            "sla": "P1D",
                            "refresh_frequency": "daily",
                            "sensitivity": "internal",
                            "dimensions": ["event_id", "anime_title", "watched_at"],
                        },
                    }
                ],
            }
        )

    def _pending_path(self) -> Path:
        return self.semantic_path.with_suffix(".pending.yml")

    def _published_rows(self) -> list[tuple]:
        with sqlite3.connect(self.publish_database) as connection:
            return connection.execute(
                "SELECT event_id FROM fact_watch_events ORDER BY event_id"
            ).fetchall()

    def _lineage_runs(self) -> set[str]:
        with sqlite3.connect(self.builder.metadata_database) as connection:
            return {
                str(row[0])
                for row in connection.execute("SELECT DISTINCT run_id FROM asset_lineage")
            }

    # -------------------------------------------------------------------- tests

    def test_a_successful_build_leaves_a_clean_state(self):
        report = self.builder.check_publication_state(self.semantic_path)
        self.assertTrue(report.clean, report.blocking_reasons)
        self.assertEqual(report.blocking_reasons, [])
        self.assertEqual(report.pending_semantic_files, [])
        self.assertEqual(report.orphan_checkpoints, [])
        self.assertEqual(report.catalog_mismatches, [])
        # Staging is inspected but is not itself a mid-state: it is the durable
        # staging area the next batch reuses.
        self.assertIn("staging_watch_events", report.staging_tables)
        self.assertTrue(self.semantic_path.is_file())

    def test_leftover_pending_semantic_model_blocks_the_next_build(self):
        self._pending_path().write_text("{}", encoding="utf-8")
        report = self.builder.check_publication_state(self.semantic_path)
        self.assertFalse(report.clean)
        self.assertEqual(
            report.pending_semantic_files, [str(self._pending_path().resolve())]
        )
        self.assertTrue(
            any("pending_semantic_model" in item for item in report.blocking_reasons)
        )

        rows_before = self._published_rows()
        runs_before = self._lineage_runs()
        with self.assertRaises(DataAssetError) as caught:
            self.builder.build_all(self._config(), self.semantic_path)
        self.assertIn("reconcile_publication_state", str(caught.exception))
        # The refusal happens before any mutation.
        self.assertEqual(self._published_rows(), rows_before)
        self.assertEqual(self._lineage_runs(), runs_before)
        self.assertTrue(self._pending_path().is_file())

        reconciled = self.builder.reconcile_publication_state(self.semantic_path)
        self.assertTrue(reconciled.clean, reconciled.blocking_reasons)
        self.assertFalse(self._pending_path().exists())
        # Reconciliation clears the residue, not the published model.
        self.assertTrue(self.semantic_path.is_file())
        [again] = self.builder.build_all(self._config(), self.semantic_path)
        self.assertEqual(again.status, "success")

    def test_orphan_checkpoint_backups_block_until_explicitly_discarded(self):
        publish_backup = self.state_root / ".publish-deadbeef.sqlite"
        metadata_backup = self.state_root / ".metadata-deadbeef.sqlite"
        publish_backup.write_bytes(b"")
        metadata_backup.write_bytes(b"")

        report = self.builder.check_publication_state(self.semantic_path)
        self.assertFalse(report.clean)
        # The builder resolves its state root, so compare resolved paths
        # (macOS temp dirs resolve /var -> /private/var).
        self.assertEqual(
            sorted(report.orphan_checkpoints),
            sorted([str(publish_backup.resolve()), str(metadata_backup.resolve())]),
        )
        self.assertTrue(
            any(
                "orphan_publication_checkpoint" in item
                for item in report.blocking_reasons
            )
        )
        with self.assertRaises(DataAssetError):
            self.builder.build_all(self._config(), self.semantic_path)

        # Default reconciliation keeps the backups: they may hold the last good
        # snapshot, so deleting them is an explicit operator decision.
        kept = self.builder.reconcile_publication_state(self.semantic_path)
        self.assertFalse(kept.clean)
        self.assertTrue(publish_backup.is_file())
        self.assertTrue(metadata_backup.is_file())

        cleared = self.builder.reconcile_publication_state(
            self.semantic_path, discard_orphan_checkpoints=True
        )
        self.assertTrue(cleared.clean, cleared.blocking_reasons)
        self.assertFalse(publish_backup.exists())
        self.assertFalse(metadata_backup.exists())
        [again] = self.builder.build_all(self._config(), self.semantic_path)
        self.assertEqual(again.status, "success")

    def test_registry_and_publish_database_mismatch_is_detected(self):
        # A crashed batch that committed published rows while the registry lost
        # its catalog entry (or never wrote it).
        with sqlite3.connect(self.builder.metadata_database) as connection:
            connection.execute("DELETE FROM semantic_catalog")

        report = self.builder.check_publication_state(self.semantic_path)
        self.assertFalse(report.clean)
        self.assertIn(
            "table_without_catalog_entry: fact_watch_events", report.catalog_mismatches
        )
        self.assertTrue(
            any("registry_publish_mismatch" in item for item in report.blocking_reasons)
        )

        # A catalog entry pointing at a table that does not exist is the mirror
        # image of the same mid-state.
        with sqlite3.connect(self.builder.metadata_database) as connection:
            connection.execute(
                "INSERT INTO semantic_catalog"
                "(asset_name, target_table, columns_json, semantic_json, updated_at)"
                " VALUES ('ghost', 'fact_ghost', '[]', '{}', '2024-01-01T00:00:00Z')"
            )
        report = self.builder.check_publication_state(self.semantic_path)
        self.assertIn(
            "catalog_entry_without_table: ghost -> fact_ghost",
            report.catalog_mismatches,
        )

    def test_publish_database_is_read_only_for_the_state_check(self):
        """The check never repairs anything: it only measures the state."""
        self._pending_path().write_text("{}", encoding="utf-8")
        before = self.publish_database.read_bytes()
        mtime = self.publish_database.stat().st_mtime_ns
        self.builder.check_publication_state(self.semantic_path)
        self.assertEqual(self.publish_database.read_bytes(), before)
        self.assertEqual(self.publish_database.stat().st_mtime_ns, mtime)

    def test_interrupted_publish_is_reported_after_a_crash_mid_batch(self):
        """A build that dies mid-batch leaves its checkpoint backups behind.

        The checkpoint is created for real (the same call the batch makes) and is
        then deliberately never restored or discarded — the residue a killed
        process leaves. The next build must refuse to continue on top of it and
        the operator must reconcile it explicitly.
        """
        self.builder._create_publication_checkpoint()
        report = self.builder.check_publication_state(self.semantic_path)
        self.assertFalse(report.clean)
        self.assertEqual(len(report.orphan_checkpoints), 2)
        self.assertTrue(
            any(
                "orphan_publication_checkpoint" in item
                for item in report.blocking_reasons
            )
        )
        with self.assertRaises(DataAssetError) as caught:
            self.builder.build_all(self._config(), self.semantic_path)
        self.assertIn("inconsistent", str(caught.exception))

        cleared = self.builder.reconcile_publication_state(
            self.semantic_path, discard_orphan_checkpoints=True
        )
        self.assertTrue(cleared.clean, cleared.blocking_reasons)
        [again] = self.builder.build_all(self._config(), self.semantic_path)
        self.assertEqual(again.status, "success")

    def test_state_check_ignores_engine_internal_tables(self):
        with sqlite3.connect(self.publish_database) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sequence_holder "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)"
            )
        report = self.builder.check_publication_state(self.semantic_path)
        self.assertFalse(
            any("sqlite_" in item for item in report.catalog_mismatches),
            report.catalog_mismatches,
        )
        # The extra table is a real unmatched table and is reported as such.
        self.assertIn(
            "table_without_catalog_entry: sequence_holder", report.catalog_mismatches
        )


class BuildScriptStateCheckTest(unittest.TestCase):
    """`scripts/build_data_assets.py --check-state/--reconcile` is the operator path."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.publish_database = self.root / "warehouse.sqlite"
        self.state_root = self.root / "asset_state"

    def tearDown(self):
        self.directory.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, dict]:
        import contextlib
        import importlib.util
        import io
        import sys

        script = Path(__file__).resolve().parents[1] / "scripts" / "build_data_assets.py"
        spec = importlib.util.spec_from_file_location("_qf_build_assets", script)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        stdout = io.StringIO()
        previous = sys.argv
        sys.argv = ["build_data_assets.py", *argv]
        try:
            with contextlib.redirect_stdout(stdout):
                code = module.main()
        finally:
            sys.argv = previous
        text = stdout.getvalue().strip()
        return code, json.loads(text) if text else {}

    def test_check_state_reports_clean_after_a_real_build_and_fails_when_dirty(self):
        from queryforge.data_assets.models import AssetBuildConfig

        csv_path = self.root / "watch_events.csv"
        csv_path.write_text(
            "Event ID,Anime Title,Watched At,Watch Seconds\n"
            "1,Azure Voyager,2024-01-01,10.5\n",
            encoding="utf-8",
        )
        config = AssetBuildConfig.model_validate(
            {
                "semantic_model": {
                    "name": "anime_uploads",
                    "description": "Reviewed anime upload semantics.",
                    "owner": "analytics",
                    "reviewed": True,
                },
                "assets": [
                    {
                        "name": "watch_events",
                        "target_table": "fact_watch_events",
                        "source": {"type": "csv", "path": str(csv_path)},
                        "column_aliases": {
                            "Event ID": "event_id",
                            "Anime Title": "anime_title",
                            "Watched At": "watched_at",
                        },
                        "quality": {
                            "required_columns": [
                                "event_id",
                                "anime_title",
                                "watched_at",
                            ],
                            "unique_key": ["event_id"],
                        },
                        "semantic": {
                            "entity_name": "watch_events",
                            "entity_type": "fact",
                            "description": "Reviewed anime playback events.",
                            "grain": ["event_id"],
                            "owner": "analytics",
                            "sla": "P1D",
                            "refresh_frequency": "daily",
                            "sensitivity": "internal",
                            "dimensions": ["event_id", "anime_title", "watched_at"],
                        },
                    }
                ],
            }
        )
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        [result] = builder.build_all(config, self.publish_database.with_suffix(".semantic.yml"))
        self.assertEqual(result.status, "success")

        base = [
            "--publish-database",
            str(self.publish_database),
            "--state-root",
            str(self.state_root),
        ]
        code, report = self._run([*base, "--check-state"])
        self.assertEqual(code, 0)
        self.assertTrue(report["clean"], report)

        # A killed publish leaves a pending semantic model behind.
        self.publish_database.with_suffix(".semantic.pending.yml").write_text(
            "{}", encoding="utf-8"
        )
        code, report = self._run([*base, "--check-state"])
        self.assertEqual(code, 1)
        self.assertFalse(report["clean"])
        self.assertEqual(
            [Path(item).name for item in report["pending_semantic_files"]],
            ["warehouse.semantic.pending.yml"],
        )

        code, report = self._run([*base, "--reconcile"])
        self.assertEqual(code, 0)
        self.assertTrue(report["clean"], report)


if __name__ == "__main__":
    unittest.main()
