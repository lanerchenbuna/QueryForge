"""Local, auditable ingestion -> staging -> quality -> semantic publication pipeline."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import yaml
from pydantic import ValidationError

from queryforge.data_assets.models import (
    AssetBuildConfig,
    AssetBuildResult,
    AssetSemanticModelSpec,
    DataAssetError,
    DataAssetSpec,
)
from queryforge.data_assets.sources import read_source
from queryforge.data_assets.transforms import (
    apply_quality_rules,
    is_after_watermark,
    max_watermark,
    normalize_contract_rules,
    normalize_fields,
    normalize_identifier,
    normalize_record,
)
from queryforge.domain.semantic import (
    SemanticContractValidator,
    SemanticModelLoader,
)
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool

_logger = logging.getLogger("queryforge.data_assets")

# Chunk size for streaming the published unique-key set out of SQLite so a huge
# table is never materialised in one unbounded fetchall.
_EXISTING_KEYS_CHUNK_SIZE = 10_000


@dataclass(frozen=True)
class _PublicationCheckpoint:
    publish_existed: bool
    publish_backup: Path
    metadata_backup: Path


@dataclass(frozen=True)
class PublicationStateReport:
    """What a crashed publication would have left behind.

    A build is only allowed to start from a clean state: publication is atomic
    across the publish database, the metadata registry, and the semantic model,
    so any residue of an interrupted batch means the three can disagree and must
    be reconciled by an operator before new data is written on top.
    """

    clean: bool
    pending_semantic_files: list[str]
    orphan_checkpoints: list[str]
    catalog_mismatches: list[str]
    staging_tables: list[str]
    blocking_reasons: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "pending_semantic_files": list(self.pending_semantic_files),
            "orphan_checkpoints": list(self.orphan_checkpoints),
            "catalog_mismatches": list(self.catalog_mismatches),
            "staging_tables": list(self.staging_tables),
            "blocking_reasons": list(self.blocking_reasons),
        }


class DataAssetBuilder:
    """Build governed SQLite data assets without weakening QueryForge's read-only path."""

    def __init__(self, publish_database: str | Path, state_root: str | Path) -> None:
        self.publish_database = Path(publish_database).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.staging_database = self.state_root / "staging.sqlite"
        self.metadata_database = self.state_root / "metadata.sqlite"
        #: Quarantine rows of the batch in flight, replayed if the batch rolls back.
        self._quarantine_buffer: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        self.default_semantic_model = self.publish_database.with_suffix(
            ".semantic.yml"
        )

    def build_from_file(self, config_path: str | Path) -> list[AssetBuildResult]:
        """Load one YAML contract and build every declared asset."""
        source = Path(config_path).expanduser().resolve()
        if not source.is_file():
            raise DataAssetError(f"Asset build configuration does not exist: {source}")
        try:
            payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
            config = AssetBuildConfig.model_validate(payload)
        except (OSError, UnicodeError, ValidationError, yaml.YAMLError) as exc:
            raise DataAssetError(f"Invalid asset build configuration {source}: {exc}") from exc
        for asset in config.assets:
            if asset.source.path:
                candidate = Path(asset.source.path).expanduser()
                if not candidate.is_absolute():
                    asset.source.path = str((source.parent / candidate).resolve())
        semantic_output = (
            Path(config.semantic_model_output).expanduser()
            if config.semantic_model_output
            else self.default_semantic_model
        )
        if not semantic_output.is_absolute():
            semantic_output = source.parent / semantic_output
        return self.build_all(config, semantic_output)

    def build_all(
        self,
        config: AssetBuildConfig,
        semantic_model_output: str | Path | None = None,
    ) -> list[AssetBuildResult]:
        """Publish one atomic data-and-semantics batch.

        An exclusive advisory lock (``fcntl.flock``) guards the whole batch so a
        second concurrent builder fails fast instead of racing on the publish and
        metadata databases.
        """
        if not config.semantic_model.reviewed:
            raise DataAssetError(
                "Upload blocked: semantic_model.reviewed must be true after a "
                "human has reviewed entity grain, hidden columns, relationships, "
                "Join Paths, and metric definitions."
            )
        lock_handle = self._acquire_builder_lock()
        try:
            return self._build_all_unlocked(config, semantic_model_output)
        finally:
            self._release_builder_lock(lock_handle)

    def _build_all_unlocked(
        self,
        config: AssetBuildConfig,
        semantic_model_output: str | Path | None = None,
    ) -> list[AssetBuildResult]:
        """The locked batch body; the builder lock is held by :meth:`build_all`."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.publish_database.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to build on top of an interrupted batch: the publish database,
        # the metadata registry, and the semantic model may disagree, and the
        # only safe starting point is a state an operator has reconciled.
        pre_state = self.check_publication_state(semantic_model_output)
        if pre_state.blocking_reasons:
            raise DataAssetError(
                "Publication blocked: the previous batch left an inconsistent "
                "state. "
                + " | ".join(pre_state.blocking_reasons)
                + " Run DataAssetBuilder.reconcile_publication_state() after "
                "inspecting the residue before publishing again."
            )
        self._initialize_metadata()
        checkpoint = self._create_publication_checkpoint()
        results = [self._build_asset(asset) for asset in config.assets]
        report = None
        semantic_path = Path(
            semantic_model_output or self.default_semantic_model
        ).expanduser().resolve()
        pending_path = semantic_path.with_suffix(
            f".pending{semantic_path.suffix}"
        )
        if all(result.status == "success" for result in results):
            if self._write_semantic_model(pending_path, config.semantic_model):
                try:
                    report = self._validate_semantic_publication(pending_path)
                    self._record_contract_report(report, pending_path)
                    for result in results:
                        result.details["semantic_contract"] = report.summary()
                    if report.passed:
                        pending_path.replace(semantic_path)
                        for result in results:
                            result.semantic_model_path = str(semantic_path.resolve())
                    else:
                        pending_path.unlink(missing_ok=True)
                        reason = self._contract_failure_reason(report)
                        for result in results:
                            result.status = "failed"
                            result.error = (
                                "Semantic publication blocked by operational "
                                f"contract validation: {reason}"
                            )
                except Exception as exc:
                    _logger.error(
                        "Semantic publication validation failed for %s: %s",
                        pending_path,
                        exc,
                        exc_info=True,
                    )
                    pending_path.unlink(missing_ok=True)
                    for result in results:
                        result.status = "failed"
                        result.error = f"Semantic publication validation failed: {exc}"
            else:
                for result in results:
                    result.status = "failed"
                    result.error = (
                        "Upload blocked: no semantic entities were available for "
                        "publication."
                    )
        if any(result.status == "failed" for result in results):
            pending_path.unlink(missing_ok=True)
            self._restore_publication_checkpoint(checkpoint)
            self._replay_quarantine()
            for result in results:
                if result.status == "success":
                    result.error = (
                        "Upload batch rolled back because another asset or the "
                        "semantic publication failed."
                    )
                result.status = "failed"
                result.published_rows = 0
                result.watermark_after = result.watermark_before
                result.semantic_model_path = None
                self._record_lineage(result, "failed")
            if report is not None:
                self._record_contract_report(report, pending_path)
        else:
            self._discard_publication_checkpoint(checkpoint)
        self._quarantine_buffer.clear()
        return results

    def _replay_quarantine(self) -> None:
        """Re-record the rejected rows of a rolled-back batch.

        The batch rollback restores the metadata database, so the quarantine rows
        written during the attempt disappear with it; an operator diagnosing a
        refusal would see the count but not the rows. Writing them back keeps the
        *failure evidence* without keeping any partially applied publish state.
        """
        if not self._quarantine_buffer:
            return
        rows = [
            (run_id, asset_name, reason, json.dumps(record, ensure_ascii=False, default=str))
            for run_id, entries in self._quarantine_buffer.items()
            for asset_name, reason, record in entries
        ]
        if not rows:
            return
        try:
            with sqlite3.connect(self.metadata_database) as connection:
                connection.executemany(
                    """
                    INSERT INTO asset_quarantine(
                        run_id, asset_name, reason, raw_record_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [(*row, _now()) for row in rows],
                )
        except sqlite3.Error as exc:  # pragma: no cover - diagnosis must not mask the failure
            _logger.error("Could not record quarantine rows after rollback: %s", exc)

    def build(self, asset: DataAssetSpec) -> AssetBuildResult:
        """Reject publication that could bypass the mandatory semantic gate."""
        raise DataAssetError(
            "Direct asset publication is disabled. Use build_all() or "
            "build_from_file() with a reviewed semantic_model contract."
        )

    def _build_asset(self, asset: DataAssetSpec) -> AssetBuildResult:
        """Stage one asset inside a batch pending semantic publication."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.publish_database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_metadata()
        run_id = f"asset_{uuid4().hex}"
        target_table = normalize_identifier(asset.target_table or asset.name)
        result = AssetBuildResult(
            asset_name=asset.name,
            run_id=run_id,
            source_type=asset.source.source_type,
            source_location=asset.source.path or asset.source.url or "",
            target_table=target_table,
            publish_database=str(self.publish_database),
            staging_database=str(self.staging_database),
            metadata_database=str(self.metadata_database),
        )
        raw_rows: list[dict[str, Any]] = []
        quarantined: list[tuple[dict[str, Any], str]] = []
        try:
            watermark_before = self._load_watermark(asset.name)
            result.watermark_before = watermark_before
            raw_rows = self._read_source(asset)
            result.input_rows = len(raw_rows)
            normalized_rows = [
                normalize_record(row, asset.column_aliases) for row in raw_rows
            ]
            staged_rows = [
                row
                for row in normalized_rows
                if is_after_watermark(row, asset.watermark_field, watermark_before)
            ]
            result.staged_rows = len(staged_rows)
            self._write_staging(asset.name, staged_rows)

            existing_keys = self._existing_keys(
                target_table, normalize_fields(asset.quality.unique_key)
            )
            publishable, quarantined = apply_quality_rules(
                staged_rows,
                asset,
                existing_keys,
            )
            self._write_quarantine(asset.name, run_id, quarantined)
            result.quarantined_rows = len(quarantined)
            invalid_ratio = len(quarantined) / len(staged_rows) if staged_rows else 0.0
            if invalid_ratio > asset.quality.max_invalid_ratio:
                raise DataAssetError(
                    f"Asset {asset.name!r} invalid ratio {invalid_ratio:.3f} exceeds "
                    f"quality.max_invalid_ratio={asset.quality.max_invalid_ratio:.3f}"
                )

            current_columns = self._published_columns(target_table)
            if publishable or current_columns:
                self._validate_semantic_columns(
                    asset,
                    set(publishable[0]) if publishable else set(current_columns),
                    target_table,
                )
            result.published_rows = self._publish(
                target_table,
                publishable,
                asset.publish_mode,
            )
            result.watermark_after = max_watermark(
                publishable, asset.watermark_field, watermark_before
            )
            if result.watermark_after is not None:
                self._save_watermark(asset.name, result.watermark_after, run_id)
            self._upsert_semantic_catalog(asset, target_table)
            self._record_lineage(result, "success")
            return result
        except Exception as exc:
            _logger.error(
                "Asset %r build failed (run_id=%s): %s",
                asset.name,
                run_id,
                exc,
                exc_info=True,
            )
            result.status = "failed"
            result.error = str(exc)
            result.quarantined_rows = len(quarantined)
            self._record_lineage(result, "failed")
            return result

    def _read_source(self, asset: DataAssetSpec) -> list[dict[str, Any]]:
        return read_source(asset.source)

    def _initialize_metadata(self) -> None:
        with sqlite3.connect(self.metadata_database) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS asset_watermarks (
                    asset_name TEXT PRIMARY KEY,
                    watermark_value TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_lineage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    asset_name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_location TEXT NOT NULL,
                    target_table TEXT NOT NULL,
                    input_rows INTEGER NOT NULL,
                    staged_rows INTEGER NOT NULL,
                    published_rows INTEGER NOT NULL,
                    quarantined_rows INTEGER NOT NULL,
                    watermark_before TEXT,
                    watermark_after TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    publish_database TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    asset_name TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    raw_record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS semantic_catalog (
                    asset_name TEXT PRIMARY KEY,
                    target_table TEXT NOT NULL,
                    columns_json TEXT NOT NULL,
                    semantic_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS semantic_contract_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_name TEXT NOT NULL,
                    model_path TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _load_watermark(self, asset_name: str) -> str | None:
        with sqlite3.connect(self.metadata_database) as connection:
            row = connection.execute(
                "SELECT watermark_value FROM asset_watermarks WHERE asset_name = ?",
                (asset_name,),
            ).fetchone()
        return str(row[0]) if row else None

    def _save_watermark(self, asset_name: str, value: str, run_id: str) -> None:
        with sqlite3.connect(self.metadata_database) as connection:
            connection.execute(
                """
                INSERT INTO asset_watermarks(asset_name, watermark_value, run_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asset_name) DO UPDATE SET
                    watermark_value = excluded.watermark_value,
                    run_id = excluded.run_id,
                    updated_at = excluded.updated_at
                """,
                (asset_name, value, run_id, _now()),
            )

    def _write_staging(self, asset_name: str, rows: list[dict[str, Any]]) -> None:
        table = normalize_identifier(f"staging_{asset_name}")
        if not rows:
            with sqlite3.connect(self.staging_database) as connection:
                connection.execute(f"DROP TABLE IF EXISTS {_quote(table)}")
            return
        _replace_table(self.staging_database, table, rows)

    def _write_quarantine(
        self,
        asset_name: str,
        run_id: str,
        quarantined: list[tuple[dict[str, Any], str]],
    ) -> None:
        if not quarantined:
            return
        # Keep the rows in memory as well: a batch that fails later rolls the
        # whole metadata database back to its checkpoint, which would erase the
        # diagnosis an operator needs most (WHICH rows were rejected). The failure
        # path replays them after the rollback, so a failed batch keeps exactly
        # one thing: the evidence of why it failed.
        self._quarantine_buffer.setdefault(run_id, []).extend(
            (asset_name, reason, record) for record, reason in quarantined
        )
        with sqlite3.connect(self.metadata_database) as connection:
            connection.executemany(
                """
                INSERT INTO asset_quarantine(
                    run_id, asset_name, reason, raw_record_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        asset_name,
                        reason,
                        json.dumps(record, ensure_ascii=False, default=str),
                        _now(),
                    )
                    for record, reason in quarantined
                ],
            )

    def _existing_keys(
        self, table: str, key_columns: list[str]
    ) -> set[tuple[Any, ...]]:
        if not key_columns or not self.publish_database.is_file():
            return set()
        with sqlite3.connect(self.publish_database) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not exists:
                return set()
            physical_columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
            }
            if not set(key_columns).issubset(physical_columns):
                return set()
            # The consumer (apply_quality_rules) needs the full key set for
            # membership checks, so the set itself must live in memory; what we
            # bound is the SQL read: keyset pagination by rowid streams bounded
            # chunks instead of one unbounded fetchall of the whole table.
            quoted_columns = ", ".join(_quote(column) for column in key_columns)
            keys: set[tuple[Any, ...]] = set()
            try:
                last_rowid = 0
                while True:
                    rows = connection.execute(
                        f"SELECT {quoted_columns}, rowid FROM {_quote(table)} "
                        "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                        (last_rowid, _EXISTING_KEYS_CHUNK_SIZE),
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        keys.add(tuple(row[:-1]))
                    last_rowid = rows[-1][-1]
            except sqlite3.OperationalError as exc:
                if "rowid" not in str(exc).lower():
                    raise
                # WITHOUT ROWID table: no stable rowid to page on; fall back to a
                # single read so behaviour matches the historical full-table load.
                rows = connection.execute(
                    f"SELECT {quoted_columns} FROM {_quote(table)}"
                ).fetchall()
                keys = {tuple(row) for row in rows}
        return keys

    def _publish(
        self,
        table: str,
        rows: list[dict[str, Any]],
        mode: str,
    ) -> int:
        if not rows:
            return 0
        columns = list(rows[0])
        for row in rows:
            if list(row) != columns:
                raise DataAssetError("Normalized rows do not share a stable schema")
        with sqlite3.connect(self.publish_database) as connection:
            # Python's sqlite3 runs DDL in autocommit; without an explicit
            # transaction a crash between DROP TABLE and the INSERT loop would
            # permanently lose the previous table. BEGIN IMMEDIATE takes the
            # write lock up front and COMMIT/ROLLBACK make the whole
            # drop-create-insert atomic. Append mode gets the same guarantee.
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            try:
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                if mode == "replace":
                    connection.execute(f"DROP TABLE IF EXISTS {_quote(table)}")
                    table_exists = None
                if not table_exists:
                    types = _infer_types(rows, columns)
                    definitions = ", ".join(
                        f"{_quote(column)} {types[column]}" for column in columns
                    )
                    connection.execute(f"CREATE TABLE {_quote(table)} ({definitions})")
                else:
                    existing = [
                        str(row[1])
                        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
                    ]
                    if existing != columns:
                        raise DataAssetError(
                            f"Published table {table!r} schema drift: expected {existing}, "
                            f"received {columns}"
                        )
                placeholders = ", ".join("?" for _ in columns)
                connection.executemany(
                    f"INSERT INTO {_quote(table)} "
                    f"({', '.join(_quote(column) for column in columns)}) "
                    f"VALUES ({placeholders})",
                    [[row[column] for column in columns] for row in rows],
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return len(rows)

    def _upsert_semantic_catalog(self, asset: DataAssetSpec, table: str) -> None:
        if not self.publish_database.is_file():
            return
        columns = self._published_columns(table)
        if not columns:
            return
        with sqlite3.connect(self.metadata_database) as connection:
            connection.execute(
                """
                INSERT INTO semantic_catalog(
                    asset_name, target_table, columns_json, semantic_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(asset_name) DO UPDATE SET
                    target_table = excluded.target_table,
                    columns_json = excluded.columns_json,
                    semantic_json = excluded.semantic_json,
                    updated_at = excluded.updated_at
                """,
                (
                    asset.name,
                    table,
                    json.dumps(columns),
                    asset.semantic.model_dump_json(),
                    _now(),
                ),
            )

    def _published_columns(self, table: str) -> list[str]:
        if not self.publish_database.is_file():
            return []
        with sqlite3.connect(self.publish_database) as connection:
            return [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
            ]

    @staticmethod
    def _validate_semantic_columns(
        asset: DataAssetSpec,
        columns: set[str],
        table: str,
    ) -> None:
        semantic_columns = (
            asset.semantic.dimensions
            or asset.semantic.primary_key
            or asset.semantic.grain
            or []
        )
        invalid_columns = {
            normalize_identifier(column) for column in semantic_columns
        } - set(columns)
        if invalid_columns:
            raise DataAssetError(
                f"Semantic contract references unknown columns in {table!r}: "
                f"{', '.join(sorted(invalid_columns))}"
            )

    def _write_semantic_model(
        self,
        path: Path,
        model_spec: AssetSemanticModelSpec,
    ) -> bool:
        with sqlite3.connect(self.metadata_database) as connection:
            rows = connection.execute(
                """
                SELECT asset_name, target_table, columns_json, semantic_json
                FROM semantic_catalog
                ORDER BY asset_name
                """
            ).fetchall()
        if not rows:
            return False
        entities = []
        for asset_name, table, columns_json, semantic_json in rows:
            columns = json.loads(columns_json)
            semantic = json.loads(semantic_json)
            hidden = [
                normalize_identifier(column) for column in semantic["hidden_columns"]
            ]
            dimensions = [
                normalize_identifier(column)
                for column in (semantic["dimensions"] or [])
            ] or [column for column in columns if column not in hidden]
            entities.append(
                {
                    "name": semantic["entity_name"] or asset_name,
                    "table": table,
                    "description": semantic["description"],
                    "entity_type": semantic["entity_type"],
                    "primary_key": [
                        normalize_identifier(column)
                        for column in semantic["primary_key"]
                    ],
                    "grain": [
                        normalize_identifier(column) for column in semantic["grain"]
                    ],
                    "hidden_columns": hidden,
                    "expected_columns": columns,
                    "allow_additive_columns": False,
                    "contract_version": semantic["contract_version"],
                    "owner": semantic["owner"],
                    "sla": semantic["sla"],
                    "refresh_frequency": semantic["refresh_frequency"],
                    "sensitivity": semantic["sensitivity"],
                    "quality_rules": normalize_contract_rules(
                        semantic["quality_rules"]
                    ),
                    "dimensions": [
                        {
                            "name": column,
                            "column": column,
                            "contract_version": semantic["contract_version"],
                            "owner": semantic["owner"],
                            "sla": semantic["sla"],
                            "refresh_frequency": semantic["refresh_frequency"],
                            "sensitivity": semantic["sensitivity"],
                        }
                        for column in dimensions
                    ],
                }
            )
        explicit_metrics = [
            metric.model_dump(by_alias=True, exclude_none=True)
            for metric in model_spec.metrics
        ]
        metric_names = {metric["name"] for metric in explicit_metrics}
        if model_spec.auto_count_metrics:
            for entity in entities:
                if entity["entity_type"] != "fact":
                    continue
                grain = entity["grain"] or entity["primary_key"]
                metric_name = f"{entity['name']}_count"
                if metric_name in metric_names:
                    continue
                expression = (
                    f"COUNT(DISTINCT {entity['table']}.{grain[0]})"
                    if len(grain) == 1
                    else "COUNT(*)"
                )
                explicit_metrics.append(
                    {
                        "name": metric_name,
                        "description": (
                            f"Count of {entity['name']} records at the declared grain."
                        ),
                        "entity": entity["name"],
                        "aggregation": "count",
                        "expression": expression,
                        "owner": entity["owner"] or model_spec.owner,
                        "sensitivity": entity["sensitivity"],
                    }
                )
        payload = {
            "version": 1,
            "name": model_spec.name,
            "description": model_spec.description,
            "entities": entities,
            "relationships": [
                relationship.model_dump(by_alias=True, exclude_none=True)
                for relationship in model_spec.relationships
            ],
            "join_paths": [
                join_path.model_dump(by_alias=True, exclude_none=True)
                for join_path in model_spec.join_paths
            ],
            "metrics": explicit_metrics,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=False, sort_keys=False),
            encoding="utf-8",
        )
        return True

    def _create_publication_checkpoint(self) -> "_PublicationCheckpoint":
        token = uuid4().hex
        publish_backup = self.state_root / f".publish-{token}.sqlite"
        metadata_backup = self.state_root / f".metadata-{token}.sqlite"
        publish_existed = self.publish_database.is_file()
        if not self.metadata_database.is_file():
            raise DataAssetError(
                "Cannot checkpoint publication: metadata database does not exist: "
                f"{self.metadata_database}"
            )
        try:
            if publish_existed:
                self._backup_database(self.publish_database, publish_backup)
            self._backup_database(self.metadata_database, metadata_backup)
        except Exception as exc:
            _logger.error(
                "Failed to create publication checkpoint for %s: %s",
                self.publish_database,
                exc,
                exc_info=True,
            )
            raise
        return _PublicationCheckpoint(
            publish_existed=publish_existed,
            publish_backup=publish_backup,
            metadata_backup=metadata_backup,
        )

    @staticmethod
    def _backup_database(source: Path, destination: Path) -> None:
        """Snapshot one SQLite database with the :meth:`sqlite3.Connection.backup`
        API instead of a raw file copy.

        ``backup()`` reads a transactionally consistent snapshot even while the
        source uses ``journal_mode=WAL``, which ``shutil.copy2`` cannot guarantee
        (it would miss committed data still sitting in the ``-wal`` file).
        ``PRAGMA wal_checkpoint(TRUNCATE)`` first folds any WAL content into the
        main file, so the checkpoint file is self-contained and unambiguous (a
        no-op for rollback-journal databases)."""
        destination.unlink(missing_ok=True)
        source_connection = sqlite3.connect(source)
        try:
            source_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            destination_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()

    def _restore_publication_checkpoint(
        self, checkpoint: "_PublicationCheckpoint"
    ) -> None:
        failures: list[tuple[str, BaseException]] = []
        try:
            if checkpoint.publish_existed:
                self._restore_from_backup(checkpoint.publish_backup, self.publish_database)
            else:
                self.publish_database.unlink(missing_ok=True)
                self._remove_sidecar_files(self.publish_database)
        except Exception as exc:
            failures.append(("publish", exc))
            _logger.error(
                "Failed to restore publish database %s from checkpoint %s: %s",
                self.publish_database,
                checkpoint.publish_backup,
                exc,
                exc_info=True,
            )
        try:
            self._restore_from_backup(checkpoint.metadata_backup, self.metadata_database)
        except Exception as exc:
            failures.append(("metadata", exc))
            _logger.error(
                "Failed to restore metadata database %s from checkpoint %s: %s",
                self.metadata_database,
                checkpoint.metadata_backup,
                exc,
                exc_info=True,
            )
        # Only discard a snapshot once its restore succeeded; keep failed ones for
        # manual recovery.
        if checkpoint.publish_existed and not any(
            name == "publish" for name, _ in failures
        ):
            checkpoint.publish_backup.unlink(missing_ok=True)
        if not any(name == "metadata" for name, _ in failures):
            checkpoint.metadata_backup.unlink(missing_ok=True)
        if failures:
            raise failures[0][1]

    @staticmethod
    def _restore_from_backup(backup: Path, target: Path) -> None:
        """Restore ``target`` from a snapshot produced by :meth:`_backup_database`.

        Stale ``-wal``/``-shm``/``-journal`` sidecars of the live target must be
        removed around the restore: SQLite would otherwise replay pre-rollback
        writes from them over the restored snapshot and corrupt or revert it. The
        restored file is a complete snapshot, so it must be read without any
        leftover sidecars. A final read-only open smoke-tests the result."""
        target.parent.mkdir(parents=True, exist_ok=True)
        DataAssetBuilder._remove_sidecar_files(target)
        target.unlink(missing_ok=True)
        source_connection = sqlite3.connect(backup)
        try:
            destination_connection = sqlite3.connect(target)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()
        DataAssetBuilder._remove_sidecar_files(target)
        # backup() copies the source's page header verbatim, so a snapshot of a
        # WAL database restores with journal_mode=WAL and would immediately
        # recreate -wal/-shm sidecars on the next open (even read-only). Normalise
        # the restored file to rollback-journal mode so it is fully self-contained
        # and safe to open read-only without any stale sidecars.
        with sqlite3.connect(target) as connection:
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        DataAssetBuilder._remove_sidecar_files(target)
        read_only = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
        try:
            read_only.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            read_only.close()

    @staticmethod
    def _remove_sidecar_files(database: Path) -> None:
        """Delete WAL/SHM/hot-journal files that may shadow a restored database."""
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(database) + suffix).unlink(missing_ok=True)

    @staticmethod
    def _discard_publication_checkpoint(
        checkpoint: "_PublicationCheckpoint",
    ) -> None:
        checkpoint.publish_backup.unlink(missing_ok=True)
        checkpoint.metadata_backup.unlink(missing_ok=True)

    # ------------------------------------------------- interrupted mid-state

    def check_publication_state(
        self, semantic_model_output: str | Path | None = None
    ) -> PublicationStateReport:
        """Detect residue of an interrupted publication.

        Publication is atomic across three artifacts — the publish database, the
        metadata registry (watermarks + ``semantic_catalog``), and the semantic
        model file — so a process that died mid-batch can leave them disagreeing.
        Nothing is mutated here: the state is measured so a caller can refuse to
        publish on top of it and an operator can reconcile it deliberately.
        """
        semantic_path = Path(
            semantic_model_output or self.default_semantic_model
        ).expanduser().resolve()
        pending_path = self._pending_semantic_path(semantic_path)

        pending: list[str] = []
        if pending_path.is_file():
            pending.append(str(pending_path))

        orphans: list[str] = []
        if self.state_root.is_dir():
            for pattern in (".publish-*.sqlite", ".metadata-*.sqlite"):
                for candidate in sorted(self.state_root.glob(pattern)):
                    if candidate.is_file():
                        orphans.append(str(candidate))

        mismatches: list[str] = []
        staging_tables: list[str] = []
        publish_tables = self._publish_table_names()
        if self.metadata_database.is_file():
            catalog = self._catalog_entries()
            for asset_name, target_table in sorted(catalog.items()):
                if publish_tables is not None and target_table not in publish_tables:
                    mismatches.append(
                        f"catalog_entry_without_table: {asset_name} -> {target_table}"
                    )
            if publish_tables is not None:
                for table in sorted(publish_tables):
                    if table.startswith("staging_"):
                        continue
                    if table not in set(catalog.values()):
                        mismatches.append(f"table_without_catalog_entry: {table}")
        staging_tables = self._staging_table_names()

        reasons: list[str] = []
        if pending:
            reasons.append(
                "pending_semantic_model: an interrupted batch left "
                f"{pending[0]}; the live semantic model was never replaced"
            )
        if orphans:
            reasons.append(
                "orphan_publication_checkpoint: an interrupted batch left "
                f"{len(orphans)} checkpoint file(s) under {self.state_root}; "
                "rollback or cleanup did not finish"
            )
        if mismatches:
            reasons.append(
                "registry_publish_mismatch: "
                + "; ".join(mismatches[:5])
                + ("" if len(mismatches) <= 5 else f" (+{len(mismatches) - 5} more)")
            )
        return PublicationStateReport(
            clean=not reasons,
            pending_semantic_files=pending,
            orphan_checkpoints=orphans,
            catalog_mismatches=mismatches,
            staging_tables=staging_tables,
            blocking_reasons=reasons,
        )

    def reconcile_publication_state(
        self,
        semantic_model_output: str | Path | None = None,
        *,
        discard_pending: bool = True,
        discard_orphan_checkpoints: bool = False,
    ) -> PublicationStateReport:
        """Clear the residue an interrupted publication may have left.

        Only residues that cannot be live state are removed by default: a pending
        semantic model is by definition not the published model. Checkpoint
        backups *can* hold the last good snapshot, so they are kept unless the
        caller explicitly opts in after inspecting them. The returned report is
        the state measured again after the cleanup.
        """
        semantic_path = Path(
            semantic_model_output or self.default_semantic_model
        ).expanduser().resolve()
        before = self.check_publication_state(semantic_path)
        if discard_pending:
            for name in before.pending_semantic_files:
                Path(name).unlink(missing_ok=True)
        if discard_orphan_checkpoints:
            for name in before.orphan_checkpoints:
                Path(name).unlink(missing_ok=True)
        after = self.check_publication_state(semantic_path)
        if before.blocking_reasons and not after.blocking_reasons:
            _logger.info(
                "Reconciled interrupted publication state for %s", self.publish_database
            )
        return after

    @staticmethod
    def _pending_semantic_path(semantic_path: Path) -> Path:
        return semantic_path.with_suffix(f".pending{semantic_path.suffix}")

    def _publish_table_names(self) -> set[str] | None:
        """Every table in the publish database, or ``None`` when it does not exist."""
        if not self.publish_database.is_file():
            return None
        try:
            with sqlite3.connect(self.publish_database) as connection:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover - unreadable database
            _logger.error("Cannot read publish database %s: %s", self.publish_database, exc)
            return None
        return {
            str(row[0])
            for row in rows
            # ``sqlite_%`` are engine internals (e.g. sqlite_sequence), never assets.
            if row and row[0] and not str(row[0]).startswith("sqlite_")
        }

    def _catalog_entries(self) -> dict[str, str]:
        try:
            with sqlite3.connect(self.metadata_database) as connection:
                rows = connection.execute(
                    "SELECT asset_name, target_table FROM semantic_catalog"
                ).fetchall()
        except sqlite3.Error:  # pragma: no cover - metadata not initialised yet
            return {}
        return {str(row[0]): str(row[1]) for row in rows}

    def _staging_table_names(self) -> list[str]:
        if not self.staging_database.is_file():
            return []
        try:
            with sqlite3.connect(self.staging_database) as connection:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE 'staging_%'"
                ).fetchall()
        except sqlite3.Error:  # pragma: no cover - unreadable staging database
            return []
        return sorted(str(row[0]) for row in rows if row and row[0])

    def _acquire_builder_lock(self) -> Any:
        """Take an exclusive advisory lock (``fcntl.flock``) for the whole batch.

        A second concurrent builder fails fast with :class:`DataAssetError`
        instead of racing on the publish/metadata databases. The lock file is left
        in place after release — flock is tied to the open file description, so
        unlinking it would only invite races on a fresh inode. Platforms without
        ``fcntl`` (e.g. some Windows builds) degrade to a logged no-op."""
        lock_path = self.publish_database.with_name(
            self.publish_database.name + ".lock"
        )
        try:
            import fcntl
        except ImportError:
            _logger.warning(
                "fcntl unavailable; skipping builder lock for %s "
                "(concurrent builds are not protected on this platform).",
                self.publish_database,
            )
            return None
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise DataAssetError(
                "Another data-asset build is already publishing to "
                f"{self.publish_database}; concurrent builders are not allowed "
                f"(advisory lock held: {lock_path})."
            ) from exc
        return handle

    @staticmethod
    def _release_builder_lock(handle: Any) -> None:
        if handle is None:
            return
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _validate_semantic_publication(self, path: Path):
        with SQLiteConnector(str(self.publish_database)) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(table) for table in tool.list_tables()]
        context = SemanticModelLoader.load_and_validate(path, schemas, "")
        return SemanticContractValidator.validate(context.model, self.publish_database)

    def _record_contract_report(self, report, model_path: Path) -> None:
        with sqlite3.connect(self.metadata_database) as connection:
            connection.execute(
                """
                INSERT INTO semantic_contract_reports(
                    model_name, model_path, passed, summary_json, report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report.model_name,
                    str(model_path.resolve()),
                    int(report.passed),
                    json.dumps(report.summary(), sort_keys=True),
                    report.model_dump_json(),
                    _now(),
                ),
            )

    @staticmethod
    def _contract_failure_reason(report) -> str:
        return "; ".join(
            f"{check.subject}:{check.rule}"
            for check in report.blocking_failures[:5]
        )

    def _record_lineage(self, result: AssetBuildResult, status: str) -> None:
        self._initialize_metadata()
        with sqlite3.connect(self.metadata_database) as connection:
            connection.execute(
                """
                INSERT INTO asset_lineage(
                    run_id, asset_name, source_type, source_location, target_table, input_rows, staged_rows,
                    published_rows, quarantined_rows, watermark_before, watermark_after,
                    status, error, publish_database, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.asset_name,
                    result.source_type,
                    result.source_location,
                    result.target_table,
                    result.input_rows,
                    result.staged_rows,
                    result.published_rows,
                    result.quarantined_rows,
                    result.watermark_before,
                    result.watermark_after,
                    status,
                    result.error,
                    result.publish_database,
                    _now(),
                ),
            )


def _replace_table(database: Path, table: str, rows: list[dict[str, Any]]) -> None:
    columns = list(rows[0])
    for row in rows:
        if list(row) != columns:
            raise DataAssetError("Normalized rows do not share a stable schema")
    with sqlite3.connect(database) as connection:
        # Same DDL-in-autocommit hazard as _publish: wrap drop/create/insert in an
        # explicit transaction so a mid-batch failure rolls the old table back.
        connection.isolation_level = None
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(f"DROP TABLE IF EXISTS {_quote(table)}")
            types = _infer_types(rows, columns)
            connection.execute(
                f"CREATE TABLE {_quote(table)} ("
                + ", ".join(f"{_quote(column)} {types[column]}" for column in columns)
                + ")"
            )
            connection.executemany(
                f"INSERT INTO {_quote(table)} "
                f"({', '.join(_quote(column) for column in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [[row[column] for column in columns] for row in rows],
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _infer_types(rows: list[dict[str, Any]], columns: Iterable[str]) -> dict[str, str]:
    inferred: dict[str, str] = {}
    for column in columns:
        values = [row[column] for row in rows if row[column] is not None]
        if values and all(isinstance(value, bool | int) for value in values):
            inferred[column] = "INTEGER"
        elif values and all(isinstance(value, bool | int | float) for value in values):
            inferred[column] = "REAL"
        else:
            inferred[column] = "TEXT"
    return inferred


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _now() -> str:
    return datetime.now(UTC).isoformat()
