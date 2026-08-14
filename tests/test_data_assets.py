import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.data_assets import (
    AssetBuildConfig,
    DataAssetBuilder,
    DataAssetError,
    scaffold_asset_config,
)
from queryforge.domain.semantic import SemanticModelLoader
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None


class _FailingExecutemanyConnection(sqlite3.Connection):
    """A sqlite3 connection subclass that raises inside executemany when the SQL
    matches a target statement, simulating a crash inside the publish INSERT loop.
    Instances are still real sqlite3.Connection objects, so the backup() API
    accepts them as destinations."""

    def executemany(self, sql, parameters):
        failing_sql = getattr(self, "_failing_sql", None)
        if failing_sql and failing_sql in sql:
            raise RuntimeError("injected failure inside the publish INSERT loop")
        return super().executemany(sql, parameters)


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class DataAssetBuilderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.publish_database = self.root / "warehouse.sqlite"
        self.state_root = self.root / "asset_state"
        self.csv_path = self.root / "watch_events.csv"
        self.csv_path.write_text(
            "\n".join(
                [
                    "Event ID,Anime Title,Watched At,Watch Seconds",
                    "1, Azure Voyager ,2024-01-01,10.5",
                    "1,Duplicate,2024-01-02,11",
                    "2,,2024-01-03,12",
                    "3,Crimson Horizon,2024-01-04,13",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.directory.cleanup()

    def _watch_events_config(self, publish_mode="append"):
        """A reviewed one-asset batch for the shared watch_events CSV."""
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
                        "publish_mode": publish_mode,
                        "semantic": {
                            "entity_name": "watch_events",
                            "entity_type": "fact",
                            "description": "Reviewed anime playback events.",
                            "grain": ["event_id"],
                            "owner": "analytics",
                            "sla": "P1D",
                            "refresh_frequency": "daily",
                            "sensitivity": "internal",
                            "dimensions": [
                                "event_id",
                                "anime_title",
                                "watched_at",
                            ],
                        },
                    }
                ],
            }
        )

    def test_replace_mode_rollback_keeps_previous_rows_when_publish_insert_fails(self):
        config = self._watch_events_config(publish_mode="replace")
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        semantic_path = self.root / "semantic.yml"
        [first] = builder.build_all(config, semantic_path)
        self.assertEqual(first.status, "success")
        self.assertEqual(first.published_rows, 2)
        # Feed a brand-new key so replace mode actually drops and re-inserts.
        self.csv_path.write_text(
            "\n".join(
                [
                    "Event ID,Anime Title,Watched At,Watch Seconds",
                    "1,Azure Voyager,2024-01-01,10.5",
                    "3,Crimson Horizon,2024-01-04,13",
                    "5,Neon Chronicle,2024-01-05,14",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        real_connect = sqlite3.connect

        def failing_connect(database, *args, **kwargs):
            connection = real_connect(
                database, *args, factory=_FailingExecutemanyConnection, **kwargs
            )
            connection._failing_sql = 'INSERT INTO "fact_watch_events"'
            return connection

        with patch("sqlite3.connect", side_effect=failing_connect):
            [second] = builder.build_all(config, semantic_path)
        self.assertEqual(second.status, "failed")
        self.assertIn("injected failure", second.error or "")
        # The pre-build table and rows survived the in-transaction ROLLBACK and
        # the batch-level checkpoint restore.
        with sqlite3.connect(self.publish_database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT event_id FROM fact_watch_events ORDER BY event_id"
                ).fetchall(),
                [(1,), (3,)],
            )
        with sqlite3.connect(builder.metadata_database) as connection:
            failed_rows = connection.execute(
                "SELECT COUNT(*) FROM asset_lineage "
                "WHERE run_id = ? AND status = 'failed'",
                (second.run_id,),
            ).fetchone()[0]
            self.assertGreaterEqual(failed_rows, 1)

    def test_checkpoint_restore_with_wal_is_consistent(self):
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        with sqlite3.connect(self.publish_database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE fact_events (id INTEGER, name TEXT)")
            connection.executemany(
                "INSERT INTO fact_events VALUES (?, ?)", [(1, "a"), (2, "b")]
            )
        # A successful build on the WAL database exercises the checkpoint backup
        # against a WAL-mode source.
        [result] = builder.build_all(self._watch_events_config())
        self.assertEqual(result.status, "success")
        with sqlite3.connect(self.publish_database) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0],
                2,
            )
        checkpoint = builder._create_publication_checkpoint()
        # Post-checkpoint WAL writes plus a schema change must vanish on restore.
        with sqlite3.connect(self.publish_database) as connection:
            connection.execute("INSERT INTO fact_events VALUES (3, 'c')")
            connection.execute("ALTER TABLE fact_events ADD COLUMN extra TEXT")
        builder._restore_publication_checkpoint(checkpoint)
        self.assertFalse(Path(str(self.publish_database) + "-wal").exists())
        self.assertFalse(Path(str(self.publish_database) + "-shm").exists())
        with sqlite3.connect(self.publish_database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id, name FROM fact_events ORDER BY id"
                ).fetchall(),
                [(1, "a"), (2, "b")],
            )
            columns = [
                row[1]
                for row in connection.execute("PRAGMA table_info(fact_events)")
            ]
            self.assertEqual(columns, ["id", "name"])

    @unittest.skipIf(fcntl is None, "fcntl not available on this platform")
    def test_concurrent_builder_fails_fast_while_lock_is_held(self):
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        lock_path = self.publish_database.with_name(
            self.publish_database.name + ".lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DataAssetError, "concurrent builders"):
                builder.build_all(self._watch_events_config())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def test_replace_mode_drop_is_rolled_back_when_insert_phase_fails(self):
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        with sqlite3.connect(self.publish_database) as connection:
            connection.execute("CREATE TABLE fact_events (id INTEGER)")
            connection.executemany(
                "INSERT INTO fact_events(id) VALUES (?)", [(1,), (2,)]
            )
        with patch(
            "queryforge.data_assets.pipeline._infer_types",
            side_effect=DataAssetError("injected failure between DROP and INSERT"),
        ):
            with self.assertRaises(DataAssetError):
                builder._publish("fact_events", [{"id": 3}], "replace")
        with sqlite3.connect(self.publish_database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id FROM fact_events ORDER BY id"
                ).fetchall(),
                [(1,), (2,)],
            )

    def test_csv_pipeline_quarantines_invalid_rows_tracks_watermark_and_publishes_semantics(self):
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
                        "source": {"type": "csv", "path": str(self.csv_path)},
                        "column_aliases": {
                            "Event ID": "event_id",
                            "Anime Title": "anime_title",
                            "Watched At": "watched_at",
                        },
                        "watermark_field": "watched_at",
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
                            "dimensions": [
                                "event_id",
                                "anime_title",
                                "watched_at",
                            ],
                            "quality_rules": [
                                {
                                    "rule": "range",
                                    "column": "watch_seconds",
                                    "minimum": 0,
                                }
                            ],
                        },
                    }
                ]
            }
        )
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        semantic_path = self.root / "semantic.yml"
        [result] = builder.build_all(config, semantic_path)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.input_rows, 4)
        self.assertEqual(result.staged_rows, 4)
        self.assertEqual(result.published_rows, 2)
        self.assertEqual(result.quarantined_rows, 2)
        self.assertEqual(result.watermark_after, "2024-01-04")
        self.assertEqual(result.semantic_model_path, str(semantic_path.resolve()))

        with sqlite3.connect(self.publish_database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT event_id, anime_title, watch_seconds FROM fact_watch_events ORDER BY event_id"
                ).fetchall(),
                [(1, "Azure Voyager", 10.5), (3, "Crimson Horizon", 13)],
            )
        with sqlite3.connect(builder.metadata_database) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM asset_quarantine").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status, source_location FROM asset_lineage"
                ).fetchone(),
                ("success", str(self.csv_path)),
            )
        with SQLiteConnector(str(self.publish_database)) as connector:
            schemas = [connector.describe_table("fact_watch_events")]
        semantic = SemanticModelLoader.load_and_validate(semantic_path, schemas, "")
        self.assertEqual(semantic.model.entities[0].effective_grain, ["event_id"])
        self.assertEqual(semantic.model.entities[0].owner, "analytics")
        self.assertEqual(semantic.model.entities[0].dimensions[0].owner, "analytics")

        self.csv_path.write_text(
            "\n".join(
                [
                    "Event ID,Anime Title,Watched At,Watch Seconds",
                    "1,Azure Voyager,2024-01-01,10.5",
                    "3,Crimson Horizon,2024-01-04,13",
                    "4,Neon Chronicle,2024-01-05,14",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        [incremental] = builder.build_all(config, semantic_path)
        self.assertEqual(incremental.staged_rows, 1)
        self.assertEqual(incremental.published_rows, 1)
        self.assertEqual(incremental.watermark_before, "2024-01-04")
        self.assertEqual(incremental.watermark_after, "2024-01-05")
        with sqlite3.connect(self.publish_database) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM fact_watch_events").fetchone()[0],
                3,
            )

    def test_api_source_fetches_bounded_pages(self):
        config = AssetBuildConfig.model_validate(
            {
                "semantic_model": {
                    "name": "api_events",
                    "description": "Reviewed API event semantics.",
                    "owner": "analytics",
                    "reviewed": True,
                },
                "assets": [
                    {
                        "name": "api_events",
                        "source": {
                            "type": "api",
                            "url": "https://example.test/events",
                            "records_path": "records",
                            "page_param": "page",
                            "page_size": 2,
                            "max_pages": 3,
                        },
                        "quality": {"unique_key": ["id"]},
                        "semantic": {
                            "entity_name": "api_events",
                            "entity_type": "fact",
                            "description": "Events uploaded from a bounded API.",
                            "grain": ["id"],
                            "dimensions": ["id"],
                            "owner": "analytics",
                        },
                    }
                ]
            }
        )
        responses = [
            _JsonResponse({"records": [{"id": 1}, {"id": 2}]}),
            _JsonResponse({"records": [{"id": 3}]}),
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            [result] = DataAssetBuilder(
                self.publish_database, self.state_root
            ).build_all(config)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.published_rows, 3)
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("page=1", urlopen.call_args_list[0].args[0].full_url)
        self.assertIn("page=2", urlopen.call_args_list[1].args[0].full_url)

    def test_semantic_contract_failure_blocks_generated_model_publication(self):
        prices = self.root / "prices.csv"
        prices.write_text(
            "Item ID,Watch Seconds\n1,10\n2,-1\n",
            encoding="utf-8",
        )
        config = AssetBuildConfig.model_validate(
            {
                "semantic_model": {
                    "name": "prices",
                    "description": "Reviewed price upload semantics.",
                    "owner": "analytics",
                    "reviewed": True,
                },
                "assets": [
                    {
                        "name": "prices",
                        "source": {"type": "csv", "path": str(prices)},
                        "column_aliases": {"Item ID": "item_id"},
                        "semantic": {
                            "entity_name": "prices",
                            "entity_type": "fact",
                            "description": "Reviewed watch-duration prices.",
                            "grain": ["item_id"],
                            "dimensions": ["item_id", "watch_seconds"],
                            "owner": "analytics",
                            "quality_rules": [
                                {
                                    "rule": "range",
                                    "column": "watch_seconds",
                                    "minimum": 0,
                                }
                            ],
                        },
                    }
                ]
            }
        )
        semantic_path = self.root / "blocked_semantic.yml"
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        [result] = builder.build_all(config, semantic_path)

        self.assertEqual(result.status, "failed")
        self.assertIn("contract validation", result.error or "")
        self.assertFalse(semantic_path.exists())
        self.assertFalse(semantic_path.with_suffix(".pending.yml").exists())
        with sqlite3.connect(builder.metadata_database) as connection:
            passed, report_json = connection.execute(
                "SELECT passed, report_json FROM semantic_contract_reports"
            ).fetchone()
        self.assertEqual(passed, 0)
        self.assertIn('"rule":"range"', report_json)
        self.assertFalse(self.publish_database.exists())

    def test_upload_contract_requires_semantics_for_every_asset(self):
        with self.assertRaisesRegex(ValueError, "semantic"):
            AssetBuildConfig.model_validate(
                {
                    "semantic_model": {
                        "name": "missing_asset_semantics",
                        "description": "This batch intentionally omits asset semantics.",
                        "owner": "analytics",
                        "reviewed": True,
                    },
                    "assets": [
                        {
                            "name": "events",
                            "source": {"type": "csv", "path": str(self.csv_path)},
                        }
                    ],
                }
            )

    def test_unreviewed_semantic_model_blocks_upload_before_publication(self):
        config = AssetBuildConfig.model_validate(
            {
                "semantic_model": {
                    "name": "draft_upload",
                    "description": "A generated draft awaiting review.",
                    "owner": "analytics",
                    "reviewed": False,
                },
                "assets": [
                    {
                        "name": "watch_events",
                        "source": {"type": "csv", "path": str(self.csv_path)},
                        "semantic": {
                            "entity_name": "watch_events",
                            "entity_type": "fact",
                            "description": "Anime playback events.",
                            "grain": ["event_id"],
                            "dimensions": [
                                "event_id",
                                "anime_title",
                                "watched_at",
                            ],
                            "owner": "analytics",
                        },
                    }
                ],
            }
        )
        builder = DataAssetBuilder(self.publish_database, self.state_root)
        with self.assertRaisesRegex(ValueError, "reviewed must be true"):
            builder.build_all(config)
        self.assertFalse(self.publish_database.exists())

    def test_scaffold_infers_grain_pii_and_requires_review(self):
        source = self.root / "viewer_events.csv"
        source.write_text(
            "Event ID,Viewer Email,Watched At,Watch Seconds\n"
            "1,a@example.test,2026-01-01,120\n"
            "2,b@example.test,2026-01-02,240\n",
            encoding="utf-8",
        )
        payload = scaffold_asset_config(
            source,
            output_path=self.root / "assets.yml",
            owner="engagement",
        )
        self.assertFalse(payload["semantic_model"]["reviewed"])
        [asset] = payload["assets"]
        self.assertEqual(asset["semantic"]["entity_type"], "fact")
        self.assertEqual(asset["semantic"]["grain"], ["event_id"])
        self.assertEqual(asset["semantic"]["hidden_columns"], ["viewer_email"])
        self.assertNotIn("viewer_email", asset["semantic"]["dimensions"])

    def test_semantic_failure_rolls_back_an_existing_publication(self):
        with sqlite3.connect(self.publish_database) as connection:
            connection.execute("CREATE TABLE stable_data (id INTEGER)")
            connection.execute("INSERT INTO stable_data VALUES (1)")
        config = AssetBuildConfig.model_validate(
            {
                "semantic_model": {
                    "name": "broken_batch",
                    "description": "A reviewed model with a broken metric.",
                    "owner": "analytics",
                    "reviewed": True,
                    "metrics": [
                        {
                            "name": "broken_metric",
                            "description": "References an unknown physical column.",
                            "entity": "watch_events",
                            "aggregation": "sum",
                            "expression": "SUM(fact_watch_events.missing_amount)",
                        }
                    ],
                },
                "assets": [
                    {
                        "name": "watch_events",
                        "target_table": "fact_watch_events",
                        "source": {"type": "csv", "path": str(self.csv_path)},
                        "semantic": {
                            "entity_name": "watch_events",
                            "entity_type": "fact",
                            "description": "Anime playback events.",
                            "grain": ["event_id"],
                            "dimensions": [
                                "event_id",
                                "anime_title",
                                "watched_at",
                            ],
                            "owner": "analytics",
                        },
                    }
                ],
            }
        )
        [result] = DataAssetBuilder(
            self.publish_database, self.state_root
        ).build_all(config)
        self.assertEqual(result.status, "failed")
        with sqlite3.connect(self.publish_database) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM stable_data").fetchall(),
                [(1,)],
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("fact_watch_events", tables)


if __name__ == "__main__":
    unittest.main()
