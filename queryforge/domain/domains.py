"""Typed data-domain identity and server-side domain resolution.

A *data domain* is one published, versioned data asset: the SQLite database plus
the semantic model and SQL policy that were reviewed together. Callers name a
domain by id (``domain_id``) instead of shipping raw file paths, so the server
decides which controlled data locations and which policy a request runs against.
The registry is a small JSON file; it is the only source clients may resolve from.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from queryforge.core.config import DEFAULT_DOMAIN_REGISTRY_PATH, PROJECT_ROOT


class DomainError(ValueError):
    """Raised when a data domain cannot be resolved, validated, or published."""


def _resolve_registry_path(registry_path: str | Path) -> Path:
    """Expand ``~`` and resolve relative registry paths against the project root."""
    path = Path(registry_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _exists(path_value: str) -> bool:
    try:
        return Path(path_value).expanduser().is_file()
    except (OSError, ValueError):
        return False


class DomainContext(BaseModel):
    """The published identity, versions, and controlled locations of one domain."""

    domain_id: str = Field(min_length=1)
    source_id: str | None = None
    data_version: str
    schema_fingerprint: str
    semantic_version: str | None = None
    policy_version: str | None = None
    database_path: str
    semantic_model_path: str | None = None
    sql_policy_path: str | None = None
    status: Literal["published", "revoked"] = "published"

    @field_validator("domain_id", mode="before")
    @classmethod
    def _clean_domain_id(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    def validate_paths(self) -> None:
        """Reject a context whose controlled data/semantic/policy files are missing."""
        if not _exists(self.database_path):
            raise DomainError(
                f"data domain {self.domain_id!r} database does not exist: "
                f"{self.database_path}"
            )
        for label, value in (
            ("semantic model", self.semantic_model_path),
            ("SQL policy", self.sql_policy_path),
        ):
            if value is None:
                continue
            if not _exists(value):
                raise DomainError(
                    f"data domain {self.domain_id!r} {label} does not exist: {value}"
                )

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view of this context; paths are reported as given."""
        return {
            "domain_id": self.domain_id,
            "source_id": self.source_id,
            "data_version": self.data_version,
            "schema_fingerprint": self.schema_fingerprint,
            "semantic_version": self.semantic_version,
            "policy_version": self.policy_version,
            "database_path": self.database_path,
            "semantic_model_path": self.semantic_model_path,
            "sql_policy_path": self.sql_policy_path,
            "status": self.status,
        }


class DomainRegistry(BaseModel):
    """Serialized set of published domains keyed by ``domain_id``."""

    version: str = "1.0"
    domains: dict[str, DomainContext] = Field(default_factory=dict)


class DomainResolver:
    """Resolve ``domain_id`` values against a server-side registry file.

    A missing registry file is an empty registry (no domains are published yet),
    not an error; a present-but-unreadable registry is an error, because silently
    ignoring a corrupt registry would widen access to uncontrolled paths.
    """

    def __init__(self, registry_path: str | Path) -> None:
        self.registry_path = _resolve_registry_path(registry_path)
        self.registry = self._load()

    @classmethod
    def from_config(cls, config: Any) -> "DomainResolver":
        """Build a resolver from ``config.domain_registry_path``."""
        registry_path = getattr(
            config, "domain_registry_path", None
        ) or DEFAULT_DOMAIN_REGISTRY_PATH
        return cls(registry_path)

    def _load(self) -> DomainRegistry:
        if not self.registry_path.is_file():
            return DomainRegistry()
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise DomainError(
                f"Could not read data domain registry {self.registry_path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise DomainError(
                f"Invalid data domain registry {self.registry_path}: {exc}"
            ) from exc
        try:
            return DomainRegistry.model_validate(payload)
        except ValidationError as exc:
            raise DomainError(
                f"Invalid data domain registry {self.registry_path}: {exc}"
            ) from exc

    def resolve(self, domain_id: str) -> DomainContext:
        """Return the published context for ``domain_id`` or raise ``DomainError``."""
        key = domain_id.strip() if isinstance(domain_id, str) else ""
        context = self.registry.domains.get(key)
        if context is None:
            known = ", ".join(self.list_domains()) or "<none>"
            raise DomainError(
                f"unknown data domain {key!r}; published domains: {known}"
            )
        if context.status == "revoked":
            raise DomainError(
                f"data domain {key!r} is revoked and can no longer be queried"
            )
        context.validate_paths()
        return context

    def resolve_optional(self, domain_id: str | None) -> DomainContext | None:
        """Resolve when an id is supplied, otherwise return ``None``."""
        if domain_id is None:
            return None
        if not domain_id.strip():
            return None
        return self.resolve(domain_id)

    def list_domains(self) -> list[str]:
        """Return known domain ids in deterministic (sorted) order."""
        return sorted(self.registry.domains)

    def publish(self, context: DomainContext) -> None:
        """Validate, upsert, and atomically persist one published domain."""
        context.validate_paths()
        self.registry.domains[context.domain_id] = context
        self._write()

    def revoke(self, domain_id: str) -> None:
        """Mark a known domain as revoked and persist the change atomically."""
        key = domain_id.strip() if isinstance(domain_id, str) else ""
        context = self.registry.domains.get(key)
        if context is None:
            raise DomainError(f"unknown data domain {key!r} cannot be revoked")
        self.registry.domains[key] = context.model_copy(update={"status": "revoked"})
        self._write()

    def _write(self) -> None:
        payload = json.dumps(
            self.registry.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary = self.registry_path.with_name(self.registry_path.name + ".tmp")
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload + "\n", encoding="utf-8")
            os.replace(temporary, self.registry_path)
        except OSError as exc:
            raise DomainError(
                f"Could not write data domain registry {self.registry_path}: {exc}"
            ) from exc
