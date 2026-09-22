"""Server-side data-domain publication: uploaded files -> governed SQLite asset.

Step 03 of the optimization plan. A reviewed semantic contract plus uploaded
source files are built through the existing ``DataAssetBuilder`` pipeline
(staging -> quality -> atomic publish -> semantic validation) and then
published as a typed, versioned ``DomainContext`` in the domain registry so
queries resolve to the published version.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from queryforge.core.config import Config, load_config
from queryforge.data_assets import DataAssetBuilder
from queryforge.domain.domains import (
    DomainContext,
    DomainError,
    DomainResolver,
)

LOGGER_NAME = "queryforge.publish"
DOMAIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
ALLOWED_EXTENSIONS = {".csv", ".parquet"}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 10
SENSITIVITY_VALUES = {"public", "internal", "confidential", "restricted"}
AGGREGATION_VALUES = {"count", "sum", "ratio"}


class PublishError(ValueError):
    """Raised when an upload batch cannot be published."""


@dataclass(frozen=True)
class PublishResult:
    """Auditable summary of one published domain version."""

    domain_id: str
    data_version: str
    schema_fingerprint: str
    database_path: str
    semantic_model_path: str | None
    status: str = "published"
    assets: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "domain_id": self.domain_id,
            "data_version": self.data_version,
            "schema_fingerprint": self.schema_fingerprint,
            "database_path": self.database_path,
            "semantic_model_path": self.semantic_model_path,
            "status": self.status,
            "assets": self.assets,
        }


class PublishService:
    """Build and publish one governed data-domain version from uploads."""

    def __init__(self, config_loader=load_config) -> None:
        self.config_loader = config_loader
        self._config: Config | None = None

    def config(self) -> Config:
        if self._config is None:
            self._config = self.config_loader()
        return self._config

    def publish(
        self,
        *,
        domain_id: str,
        files: list[tuple[str, bytes]],
        contract: dict,
    ) -> PublishResult:
        domain_id = self._validate_domain_id(domain_id)
        contract = self._validate_contract(contract)
        files = self._validate_files(files)
        fingerprint, content_hash = self._fingerprint(files)
        from uuid import uuid4
        data_version = (
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{content_hash[:8]}"
            f"-{uuid4().hex[:8]}"
        )

        config = self.config()
        root = (
            Path(config.orchestration_state_root).expanduser().resolve().parent
            / "domains"
            / domain_id
            / data_version
        )
        root.mkdir(parents=True, exist_ok=True)
        publish_database = root / f"{domain_id}.sqlite"
        for name, content in files:
            (root / self._safe_file_name(name)).write_bytes(content)

        config_path = root / "assets.yml"
        config_path.write_text(
            self._build_asset_config(contract, files, domain_id),
            encoding="utf-8",
        )
        try:
            builder = DataAssetBuilder(publish_database, root / "state")
            results = builder.build_from_file(config_path)
        except Exception as exc:
            raise PublishError(
                f"asset build failed for domain {domain_id!r}: {exc}"
            ) from exc
        failures = [result.error for result in results if result.status != "success"]
        if failures:
            raise PublishError(
                f"asset build failed for domain {domain_id!r}: {failures}"
            )

        semantic_path = publish_database.with_suffix(".semantic.yml")
        context = DomainContext(
            domain_id=domain_id,
            source_id=data_version,
            data_version=data_version,
            schema_fingerprint=fingerprint,
            semantic_version="1",
            policy_version=None,
            database_path=str(publish_database),
            semantic_model_path=(
                str(semantic_path) if semantic_path.is_file() else None
            ),
            status="published",
        )
        try:
            DomainResolver.from_config(config).publish(context)
        except DomainError as exc:
            raise PublishError(str(exc)) from exc
        return PublishResult(
            domain_id=domain_id,
            data_version=data_version,
            schema_fingerprint=fingerprint,
            database_path=str(publish_database),
            semantic_model_path=context.semantic_model_path,
            assets=[result.model_dump() for result in results],
        )

    # -- validation ----------------------------------------------------------

    @staticmethod
    def _validate_domain_id(domain_id: str | None) -> str:
        if not domain_id or not DOMAIN_ID_PATTERN.fullmatch(domain_id.strip()):
            raise PublishError(
                "domain_id must match [a-z0-9][a-z0-9_-]{1,63}"
            )
        return domain_id.strip()

    @staticmethod
    def _validate_contract(raw: dict | None) -> dict:
        if not isinstance(raw, dict):
            raise PublishError("a semantic contract object is required")
        contract = dict(raw)
        for key in ("entity", "description", "owner", "reviewed_by"):
            value = str(contract.get(key) or "").strip()
            if not value:
                raise PublishError(f"contract.{key} must be non-blank")
            contract[key] = value
        sensitivity = str(contract.get("sensitivity") or "internal")
        if sensitivity not in SENSITIVITY_VALUES:
            raise PublishError(
                f"contract.sensitivity must be one of {sorted(SENSITIVITY_VALUES)}"
            )
        contract["sensitivity"] = sensitivity
        grain = contract.get("grain") or contract.get("primaryKey") or []
        if not isinstance(grain, list) or not all(
            isinstance(item, str) and item.strip() for item in grain
        ):
            raise PublishError(
                "contract.grain or contract.primaryKey must be a non-empty string list"
            )
        contract["grain"] = [item.strip() for item in grain]
        contract["primaryKey"] = [
            str(item).strip()
            for item in (contract.get("primaryKey") or [])
            if str(item).strip()
        ]
        dimensions = contract.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions or not all(
            isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and str(item.get("column") or "").strip()
            for item in dimensions
        ):
            raise PublishError(
                "contract.dimensions must be a non-empty list of "
                "{name, column} objects"
            )
        contract["dimensions"] = [
            {"name": str(item["name"]).strip(), "column": str(item["column"]).strip()}
            for item in dimensions
        ]
        metrics = contract.get("metrics")
        if not isinstance(metrics, list) or not metrics or not all(
            isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and str(item.get("description") or "").strip()
            and item.get("aggregation") in AGGREGATION_VALUES
            and str(item.get("expression") or "").strip()
            for item in metrics
        ):
            raise PublishError(
                "contract.metrics must be a non-empty list of {name, "
                "description, aggregation (count|sum|ratio), expression} objects"
            )
        contract["metrics"] = [
            {
                "name": str(item["name"]).strip(),
                "description": str(item["description"]).strip(),
                "aggregation": item["aggregation"],
                "expression": str(item["expression"]).strip(),
            }
            for item in metrics
        ]
        return contract

    @staticmethod
    def _validate_files(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
        if not files:
            raise PublishError("at least one data file is required")
        if len(files) > MAX_FILES:
            raise PublishError(f"at most {MAX_FILES} files per publish batch")
        validated = []
        for name, content in files:
            if not name or Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
                raise PublishError(
                    f"unsupported file type {name!r}; allowed: csv, parquet"
                )
            if len(content) > MAX_FILE_BYTES:
                raise PublishError(
                    f"{name!r} exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit"
                )
            validated.append((name, content))
        return validated

    @staticmethod
    def _safe_file_name(name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(name).name)
        cleaned = cleaned.strip(".-")
        return cleaned or "upload"

    @staticmethod
    def _fingerprint(files: list[tuple[str, bytes]]) -> tuple[str, str]:
        payload = sorted(
            (Path(name).name, hashlib.sha256(content).hexdigest())
            for name, content in files
        )
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return digest, digest

    def _build_asset_config(
        self,
        contract: dict,
        files: list[tuple[str, bytes]],
        domain_id: str,
    ) -> str:
        entity = str(contract["entity"])
        table_name = self._table_name(entity)
        dimensions = contract["dimensions"]
        required_columns = list(
            dict.fromkeys(
                [
                    *contract["grain"],
                    *contract["primaryKey"],
                    *(item["column"] for item in dimensions),
                ]
            )
        )
        assets = []
        for index, (name, _content) in enumerate(files, start=1):
            suffix = Path(name).suffix.lower()
            assets.append(
                {
                    "name": f"asset_{index}_{self._safe_file_name(Path(name).stem)}",
                    "target_table": table_name if len(files) == 1 else f"{table_name}_{index}",
                    "source": {
                        "type": "csv" if suffix == ".csv" else "parquet",
                        "path": self._safe_file_name(name),
                    },
                    "quality": {
                        "required_columns": required_columns,
                        "unique_key": contract["primaryKey"],
                        "max_invalid_ratio": 0.05,
                    },
                    "semantic": {
                        "entity_name": entity if len(files) == 1 else f"{entity}_{index}",
                        "entity_type": "fact" if contract["grain"] else "dimension",
                        "description": contract["description"],
                        "primary_key": contract["primaryKey"],
                        "grain": contract["grain"],
                        "dimensions": [item["name"] for item in dimensions],
                        "contract_version": "1.0",
                        "owner": contract["owner"],
                        "sensitivity": contract["sensitivity"],
                    },
                }
            )
        metrics = [
            {
                **metric,
                "entity": entity,
                "owner": contract["owner"],
                "sensitivity": contract["sensitivity"],
            }
            for metric in contract["metrics"]
        ]
        payload = {
            "version": 1,
            "semantic_model": {
                "name": f"{domain_id}_semantics",
                "description": contract["description"],
                "owner": contract["owner"],
                "reviewed": True,
                "auto_count_metrics": False,
                "metrics": metrics,
            },
            "assets": assets,
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    @staticmethod
    def _table_name(entity: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", entity).strip("_")
        return cleaned or "entity"
