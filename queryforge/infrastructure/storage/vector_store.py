"""Optional vector storage abstraction and LanceDB implementation."""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VECTOR_KB_PATH = PROJECT_ROOT / ".queryforge/lancedb"
SQL_HISTORY_VECTORS = "sql_history_vectors"
SCHEMA_DOC_VECTORS = "schema_doc_vectors"
#: Filter keys with list semantics (any-of); every other key is an exact match.
ANY_OF_FILTER_KEYS = frozenset({"permissions", "source_type", "source_types"})


class VectorStoreError(RuntimeError):
    """Raised when the optional vector knowledge base is unavailable."""


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def document_content_hash(document: "VectorDocument | Any") -> str:
    """Content hash deciding whether a chunk needs re-embedding.

    The hash covers the normalized text, the source type, and every governance
    metadata field except the store-managed ``content_hash`` key itself. So an
    unchanged chunk is never re-embedded, while edited text *or* a changed
    governance field (review status, version, permissions) refreshes the stored
    document — and reading a document back always reproduces the same hash.
    """
    text = getattr(document, "text", None)
    if text is None and isinstance(document, Mapping):
        text = document.get("text")
    source_type = getattr(document, "source_type", None)
    if source_type is None and isinstance(document, Mapping):
        source_type = document.get("source_type")
    normalized = "\n".join(
        line.strip() for line in str(text or "").strip().splitlines() if line.strip()
    )
    metadata = getattr(document, "metadata", None)
    if metadata is None and isinstance(document, Mapping):
        metadata = document.get("metadata")
    governance: dict[str, Any] = {}
    if isinstance(metadata, Mapping):
        governance = {
            str(key): value
            for key, value in metadata.items()
            if str(key) != "content_hash"
        }
    payload = json.dumps(
        [str(source_type or ""), normalized, governance],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for item in value]
    return [value]


def document_matches_filters(
    document: "VectorDocument | VectorSearchResult | Mapping[str, Any]",
    filters: Mapping[str, Any] | None,
) -> bool:
    """True when a document satisfies every requested governance filter.

    Semantics (documented once, reused by every backend):

    * ``source_type``/``source_types``/``permissions`` accept a scalar or a list;
      a document matches when **any** requested value matches (for
      ``permissions`` that means a permission intersection).
    * every other key is an exact match against the document metadata.
    * a filter key that is absent from a document's metadata **excludes** that
      document (fail closed), which is what keeps legacy unscoped documents out
      of a scoped domain query.
    * list-valued metadata matches an exact request when the request value is in
      the list.
    """
    if not filters:
        return True
    metadata = (
        document.get("metadata")
        if isinstance(document, Mapping)
        else getattr(document, "metadata", None)
    )
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    source_type = (
        document.get("source_type")
        if isinstance(document, Mapping)
        else getattr(document, "source_type", None)
    )
    for key, raw_request in filters.items():
        if raw_request is None:
            continue
        requested = _values(raw_request)
        if not requested:
            continue
        if key in {"source_type", "source_types"}:
            if str(source_type or "") not in {str(item) for item in requested}:
                return False
            continue
        recorded = metadata.get(key)
        if recorded is None:
            return False
        if key in ANY_OF_FILTER_KEYS:
            recorded_values = {str(item) for item in _values(recorded)}
            if not recorded_values & {str(item) for item in requested}:
                return False
            continue
        recorded_values = {str(item) for item in _values(recorded)}
        if not recorded_values & {str(item) for item in requested}:
            return False
    return True


def effective_filters(
    filters: Mapping[str, Any] | None,
) -> dict[str, list[Any]]:
    """Return only the filter entries that actually constrain a selection.

    :func:`document_matches_filters` treats a ``None`` or empty request as "not
    requested" and simply skips it — the right reading for *retrieval*, where an
    unfilled scope must not hide every document. A *destructive* call cannot
    inherit that leniency: a caller passing ``filters={"domain_id": None}`` or
    ``{"permissions": []}`` would otherwise have every document matched and two
    whole tables wiped. So deletion resolves its filters through this function
    and refuses to run when nothing effective is left.
    """
    effective: dict[str, list[Any]] = {}
    if not isinstance(filters, Mapping):
        return effective
    for key, raw_request in filters.items():
        if raw_request is None:
            continue
        values = [item for item in _values(raw_request) if str(item).strip()]
        if not values:
            continue
        effective[str(key)] = values
    return effective


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
    """Small backend-neutral interface used by QueryForge nodes.

    Step 13 adds governed retrieval (``filters``), incremental writes
    (``upsert_documents``), and deletion (``delete_documents``). The new methods
    are concrete defaults so stores written against the earlier interface keep
    working unchanged.
    """

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
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        """Return the best matches, applying ``filters`` BEFORE ranking and top-k.

        Order of operations is part of the contract: candidate selection, then
        governance filters (``source_types`` is a candidate constraint exactly
        like ``filters``, never a post-filter on an ANN window), then similarity
        ranking, then the ``top_k`` cut. A document that fails a filter can
        therefore never displace a legal, lower similarity candidate.
        """
        raise NotImplementedError

    def upsert_documents(self, documents: Iterable[VectorDocument]) -> dict[str, int]:
        """Idempotent write by document id.

        The base implementation only guarantees idempotency by id (it overwrites
        and re-embeds); stores with content-hash bookkeeping override this to
        re-embed only changed chunks.
        """
        docs = list(documents)
        written = self.add_documents(docs)
        return {
            "inserted": written,
            "updated": 0,
            "unchanged": 0,
            "embedded": written,
        }

    def delete_documents(
        self,
        ids: Iterable[str] | None = None,
        *,
        filters: dict[str, Any] | None = None,
    ) -> int:
        """Delete documents by id and/or governance filter; returns the count.

        Deletion is destructive, so it requires a *selective* request: either
        explicit ids, or a filter that names at least one real value
        (:func:`effective_filters`). ``filters={}`` and valueless filters such as
        ``{"domain_id": None}`` or ``{"permissions": []}`` are refused instead of
        being read as "match everything".
        """
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
        grouped = self._group_rows(docs, vectors)
        added = 0
        try:
            existing = self._table_names()
            for table_name, rows in grouped.items():
                self._write_rows(table_name, rows, existing)
                added += len(rows)
        except Exception as exc:
            raise VectorStoreError(f"Could not add documents to LanceDB: {exc}") from exc
        return added

    def upsert_documents(self, documents: Iterable[VectorDocument]) -> dict[str, int]:
        """Idempotent write with content-hash based incremental embedding.

        Documents whose content hash is unchanged are skipped entirely (no
        embedding call). Changed documents are deleted and re-added, so a store
        never keeps a stale vector for edited text. The content hash lives inside
        the existing metadata JSON: adding a column to LanceDB tables written by
        an older QueryForge version risks a schema conflict, and metadata needs no
        migration.
        """
        docs = [document for document in documents if document.text.strip()]
        result = {"inserted": 0, "updated": 0, "unchanged": 0, "embedded": 0}
        if not docs:
            return result
        known = self._known_hashes()
        pending: list[VectorDocument] = []
        for document in docs:
            digest = document_content_hash(document)
            recorded = known.get(document.id)
            if recorded == digest:
                result["unchanged"] += 1
                continue
            if recorded is None:
                result["inserted"] += 1
            else:
                result["updated"] += 1
            pending.append(
                VectorDocument.create(
                    id=document.id,
                    text=document.text,
                    source_type=document.source_type,
                    created_at=document.created_at,
                    metadata={**document.metadata, "content_hash": digest},
                )
            )
        if not pending:
            return result
        vectors = self.embedding_provider.embed(
            [document.text for document in pending]
        )
        if len(vectors) != len(pending):
            raise VectorStoreError("Embedding provider returned an unexpected vector count")
        result["embedded"] = len(pending)
        grouped = self._group_rows(pending, vectors)
        try:
            existing = self._table_names()
            for table_name, rows in grouped.items():
                self._write_rows(table_name, rows, existing)
        except Exception as exc:
            raise VectorStoreError(f"Could not upsert documents into LanceDB: {exc}") from exc
        return result

    def delete_documents(
        self,
        ids: Iterable[str] | None = None,
        *,
        filters: dict[str, Any] | None = None,
    ) -> int:
        """Delete by id and/or governance filter (e.g. one revoked source).

        A destructive call must be selective: a filter whose values are all
        ``None``/empty is *not* "everything", it is an unset scope, and is
        refused together with the empty filter. Only values that actually
        constrain the selection are matched, so a valueless filter can never
        widen an explicit id list into a table wipe.
        """
        requested_ids = [str(item) for item in (ids or ()) if str(item)]
        resolved = effective_filters(filters)
        if not requested_ids and not resolved:
            raise VectorStoreError(
                "delete_documents requires ids or filters naming at least one "
                "value; refusing to delete everything"
            )
        try:
            existing = self._table_names()
            deleted = 0
            for table_name in (SQL_HISTORY_VECTORS, SCHEMA_DOC_VECTORS):
                if table_name not in existing:
                    continue
                table = self.database.open_table(table_name)
                # Plan the deletion from the documents the table actually holds:
                # the return value becomes ``stale_deleted``, so an id that is
                # absent from this table (or lives in the other one) must not be
                # reported as deleted. Deletes are rare next to reads, so the exact
                # plan is worth one scan per table.
                hits = sorted(
                    document.id
                    for document in (
                        self._row_document(row)
                        for row in table.to_arrow().to_pylist()
                    )
                    if document.id
                    and (
                        document.id in requested_ids
                        or (
                            bool(resolved)
                            and document_matches_filters(document, resolved)
                        )
                    )
                )
                if not hits:
                    continue
                table.delete(
                    "id IN (" + ",".join(self._quoted(item) for item in hits) + ")"
                )
                deleted += len(hits)
            return deleted
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(f"Could not delete LanceDB documents: {exc}") from exc

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        source_types: Iterable[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        """Vector search with governance filters applied before ranking.

        Order of operations: table/candidate selection, then ``source_types`` and
        ``filters`` on **every** candidate, then similarity ranking, then the
        ``top_k`` cut. ``source_types`` is a *candidate constraint* with exactly
        the same semantics as ``filters`` — never a post-filter on a truncated
        ANN window, which silently returned an empty governed channel whenever the
        window happened to be filled by excluded rows (e.g. a store dominated by
        ``sql_history`` documents). A constrained query is evaluated over an
        exhaustive scan so a legal lower-similarity document is still recalled;
        only an unconstrained query keeps the pure LanceDB ANN path.
        """
        if top_k <= 0 or not query.strip():
            return []
        # Kept exactly as requested (a blank entry stays a blank entry): a blank
        # constraint must select nothing rather than silently degrade into "no
        # constraint at all".
        requested = {str(item) for item in (source_types or ())}
        if filters:
            requested.update(
                str(item)
                for item in _values(
                    filters.get("source_type") or filters.get("source_types")
                )
            )
        table_names = (
            {self._table_for_source(source) for source in requested}
            if requested
            else {SQL_HISTORY_VECTORS, SCHEMA_DOC_VECTORS}
        )
        vector = self.embedding_provider.embed([query])[0]
        matches: list[VectorSearchResult] = []
        try:
            existing = self._table_names()
            for table_name in sorted(table_names & existing):
                table = self.database.open_table(table_name)
                if requested or filters:
                    rows = table.to_arrow().to_pylist()
                else:
                    rows = (
                        table.search(vector)
                        .limit(max(top_k * 5, top_k))
                        .to_list()
                    )
                for row in rows:
                    source_type = str(row.get("source_type") or "")
                    if requested and source_type not in requested:
                        continue
                    document = self._row_document(row)
                    if filters and not document_matches_filters(document, filters):
                        continue
                    distance = row.get("_distance")
                    if distance is None:
                        distance = self._distance(vector, row.get("vector"))
                    score = None if distance is None else 1.0 / (1.0 + float(distance))
                    matches.append(
                        VectorSearchResult(
                            id=document.id,
                            text=document.text,
                            metadata=document.metadata,
                            source_type=document.source_type,
                            created_at=document.created_at,
                            score=round(score, 6) if score is not None else None,
                        )
                    )
        except Exception as exc:
            raise VectorStoreError(f"Could not search LanceDB: {exc}") from exc
        matches.sort(key=lambda item: (item.score or 0.0, item.id), reverse=True)
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
        result: dict[str, Any] = {
            "path": str(self.path),
            "tables": {},
            "total": 0,
            "by_source_type": {},
            "by_review_status": {},
            "chunks": 0,
        }
        try:
            existing = self._table_names()
            chunks: set[str] = set()
            for table_name in (SQL_HISTORY_VECTORS, SCHEMA_DOC_VECTORS):
                if table_name not in existing:
                    result["tables"][table_name] = 0
                    continue
                table = self.database.open_table(table_name)
                result["tables"][table_name] = int(table.count_rows())
                result["total"] += int(table.count_rows())
                for row in table.to_arrow().to_pylist():
                    document = self._row_document(row)
                    key = document.source_type or "unknown"
                    result["by_source_type"][key] = (
                        result["by_source_type"].get(key, 0) + 1
                    )
                    review = str(document.metadata.get("review_status") or "unknown")
                    result["by_review_status"][review] = (
                        result["by_review_status"].get(review, 0) + 1
                    )
                    chunk_id = document.metadata.get("chunk_id")
                    if isinstance(chunk_id, str) and chunk_id:
                        chunks.add(chunk_id)
            result["chunks"] = len(chunks)
        except Exception as exc:
            raise VectorStoreError(f"Could not read LanceDB stats: {exc}") from exc
        return result

    # ------------------------------------------------------------- internals
    def _group_rows(
        self, docs: Sequence[VectorDocument], vectors: Sequence[Sequence[float]]
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for document, vector in zip(docs, vectors, strict=True):
            metadata = {**document.metadata, "content_hash": document_content_hash(document)}
            grouped.setdefault(self._table_for_source(document.source_type), []).append(
                {
                    "id": document.id,
                    "text": document.text,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                    "source_type": document.source_type,
                    "created_at": document.created_at,
                    "vector": list(vector),
                }
            )
        return grouped

    def _write_rows(
        self,
        table_name: str,
        rows: Sequence[dict[str, Any]],
        existing: set[str],
    ) -> None:
        if table_name in existing:
            table = self.database.open_table(table_name)
            ids = ",".join(self._quoted(row["id"]) for row in rows)
            if ids:
                table.delete(f"id IN ({ids})")
            table.add(list(rows))
            return
        self.database.create_table(table_name, data=list(rows))
        existing.add(table_name)

    def _known_hashes(self) -> dict[str, str]:
        """Read id → content hash for the incremental write path."""
        known: dict[str, str] = {}
        try:
            existing = self._table_names()
            for table_name in (SQL_HISTORY_VECTORS, SCHEMA_DOC_VECTORS):
                if table_name not in existing:
                    continue
                for row in self.database.open_table(table_name).to_arrow().to_pylist():
                    document = self._row_document(row)
                    known[document.id] = document_content_hash(document)
        except Exception as exc:
            raise VectorStoreError(f"Could not read LanceDB document hashes: {exc}") from exc
        return known

    def _row_document(self, row: Mapping[str, Any]) -> VectorDocument:
        return VectorDocument(
            id=str(row.get("id") or ""),
            text=str(row.get("text") or ""),
            metadata=self._metadata(row.get("metadata")),
            source_type=str(row.get("source_type") or ""),
            created_at=str(row.get("created_at") or ""),
        )

    @staticmethod
    def _distance(
        vector: Sequence[float], candidate: Any
    ) -> float | None:
        """L2 distance, matching LanceDB's default metric for ``.search(vector)``."""
        if candidate is None:
            return None
        try:
            values = [float(item) for item in candidate]
        except (TypeError, ValueError):
            return None
        if len(values) != len(vector):
            return None
        return math.sqrt(
            sum((float(left) - right) ** 2 for left, right in zip(vector, values))
        )

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
