"""Optional vector storage abstraction and LanceDB implementation."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VECTOR_KB_PATH = PROJECT_ROOT / ".queryforge/lancedb"
SQL_HISTORY_VECTORS = "sql_history_vectors"
SCHEMA_DOC_VECTORS = "schema_doc_vectors"


class VectorStoreError(RuntimeError):
    """Raised when the optional vector knowledge base is unavailable."""


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class VectorDocument:
    id: str
    text: str
    metadata: dict[str, Any]
    source_type: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        id: str,
        text: str,
        source_type: str,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> "VectorDocument":
        return cls(
            id=id,
            text=text.strip(),
            metadata=metadata or {},
            source_type=source_type,
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
        )


@dataclass(frozen=True, slots=True)
class VectorSearchResult:
    id: str
    text: str
    metadata: dict[str, Any]
    source_type: str
    created_at: str
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VectorStore(ABC):
    """Small backend-neutral interface used by QueryForge nodes."""

    @abstractmethod
    def add_documents(self, documents: Iterable[VectorDocument]) -> int:
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        source_types: Iterable[str] | None = None,
    ) -> list[VectorSearchResult]:
        raise NotImplementedError

    @abstractmethod
    def rebuild(self, documents: Iterable[VectorDocument]) -> dict[str, int]:
        raise NotImplementedError

    @abstractmethod
    def stats(self) -> dict[str, Any]:
        raise NotImplementedError


class OpenAIEmbeddingProvider:
    """Lightweight hosted embeddings; no local model download is required."""

    def __init__(
        self,
        api_key: str | None,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
    ) -> None:
        if not api_key:
            raise VectorStoreError(
                "Vector KB disabled: no embedding API key. Set EMBEDDING_API_KEY "
                "or OPENAI_API_KEY."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - required base dependency
            raise VectorStoreError("OpenAI SDK is required for embeddings") from exc
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        clean = [text.strip() for text in texts]
        if not clean or any(not text for text in clean):
            raise VectorStoreError("Embedding input must contain non-empty text")
        try:
            response = self.client.embeddings.create(model=self.model, input=clean)
            ordered = sorted(response.data, key=lambda item: item.index)
            return [list(item.embedding) for item in ordered]
        except Exception as exc:
            raise VectorStoreError(f"Embedding request failed: {exc}") from exc


class LanceDBVectorStore(VectorStore):
    """LanceDB backend loaded lazily so the base installation stays small."""

    def __init__(
        self,
        path: str | Path = DEFAULT_VECTOR_KB_PATH,
        *,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        try:
            import lancedb
        except ImportError as exc:
            raise VectorStoreError(
                "Vector KB disabled: optional dependency 'lancedb' is not installed. "
                "Install it with: pip install -e '.[vector]'"
            ) from exc
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = PROJECT_ROOT / resolved
        try:
            resolved.mkdir(parents=True, exist_ok=True)
            self.database = lancedb.connect(str(resolved.resolve()))
        except Exception as exc:
            raise VectorStoreError(f"Could not initialize LanceDB at {resolved}: {exc}") from exc
        self.path = resolved.resolve()
        self.embedding_provider = embedding_provider

    def add_documents(self, documents: Iterable[VectorDocument]) -> int:
        docs = [document for document in documents if document.text.strip()]
        if not docs:
            return 0
        vectors = self.embedding_provider.embed([document.text for document in docs])
        if len(vectors) != len(docs):
            raise VectorStoreError("Embedding provider returned an unexpected vector count")
        added = 0
        grouped: dict[str, list[dict[str, Any]]] = {}
        for document, vector in zip(docs, vectors, strict=True):
            table_name = self._table_for_source(document.source_type)
            grouped.setdefault(table_name, []).append(
                {
                    "id": document.id,
                    "text": document.text,
                    "metadata": json.dumps(document.metadata, ensure_ascii=False),
                    "source_type": document.source_type,
                    "created_at": document.created_at,
                    "vector": vector,
                }
            )
        try:
            existing = self._table_names()
            for table_name, rows in grouped.items():
                if table_name in existing:
                    table = self.database.open_table(table_name)
                    ids = ",".join(self._quoted(row["id"]) for row in rows)
                    if ids:
                        table.delete(f"id IN ({ids})")
                    table.add(rows)
                else:
                    self.database.create_table(table_name, data=rows)
                    existing.add(table_name)
                added += len(rows)
        except Exception as exc:
            raise VectorStoreError(f"Could not add documents to LanceDB: {exc}") from exc
        return added

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        source_types: Iterable[str] | None = None,
    ) -> list[VectorSearchResult]:
        if top_k <= 0 or not query.strip():
            return []
        requested = set(source_types or ())
        table_names = (
            {self._table_for_source(source) for source in requested}
            if requested
            else {SQL_HISTORY_VECTORS, SCHEMA_DOC_VECTORS}
        )
        vector = self.embedding_provider.embed([query])[0]
        matches: list[VectorSearchResult] = []
        try:
            existing = self._table_names()
            for table_name in table_names & existing:
                rows = (
                    self.database.open_table(table_name)
                    .search(vector)
                    .limit(max(top_k * 5, top_k))
                    .to_list()
                )
                for row in rows:
                    source_type = str(row.get("source_type") or "")
                    if requested and source_type not in requested:
                        continue
                    distance = row.get("_distance")
                    score = None if distance is None else 1.0 / (1.0 + float(distance))
                    matches.append(
                        VectorSearchResult(
                            id=str(row["id"]),
                            text=str(row["text"]),
                            metadata=self._metadata(row.get("metadata")),
                            source_type=source_type,
                            created_at=str(row.get("created_at") or ""),
                            score=round(score, 6) if score is not None else None,
                        )
                    )
        except Exception as exc:
            raise VectorStoreError(f"Could not search LanceDB: {exc}") from exc
        matches.sort(key=lambda item: item.score or 0.0, reverse=True)
        return matches[:top_k]

    def rebuild(self, documents: Iterable[VectorDocument]) -> dict[str, int]:
        docs = [document for document in documents if document.text.strip()]
        try:
            existing = self._table_names()
            for table_name in (SQL_HISTORY_VECTORS, SCHEMA_DOC_VECTORS):
                if table_name in existing:
                    self.database.drop_table(table_name)
        except Exception as exc:
            raise VectorStoreError(f"Could not reset LanceDB tables: {exc}") from exc
        self.add_documents(docs)
        return self.stats()

    def stats(self) -> dict[str, Any]:
        result: dict[str, Any] = {"path": str(self.path), "tables": {}, "total": 0}
        try:
            existing = self._table_names()
            for table_name in (SQL_HISTORY_VECTORS, SCHEMA_DOC_VECTORS):
                count = (
                    int(self.database.open_table(table_name).count_rows())
                    if table_name in existing
                    else 0
                )
                result["tables"][table_name] = count
                result["total"] += count
        except Exception as exc:
            raise VectorStoreError(f"Could not read LanceDB stats: {exc}") from exc
        return result

    @staticmethod
    def _table_for_source(source_type: str) -> str:
        return SCHEMA_DOC_VECTORS if source_type == "schema_doc" else SQL_HISTORY_VECTORS

    def _table_names(self) -> set[str]:
        if hasattr(self.database, "list_tables"):
            response = self.database.list_tables()
            return {str(name) for name in response.tables}
        return set(self.database.table_names())  # pragma: no cover - older LanceDB

    @staticmethod
    def _quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _metadata(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            decoded = json.loads(value or "{}")
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
