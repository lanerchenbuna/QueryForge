"""Build compact vector documents from QueryForge's supported knowledge sources."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from queryforge.core.schemas.models import SQLContext, TableSchema
from queryforge.domain.knowledge import (
    GovernedDocument,
    HoldoutRegistry,
    StructuredKnowledgeBase,
    VerificationLevel,
    verification_level_of,
)
from queryforge.infrastructure.storage.sql_history_store import SQLHistoryStore
from queryforge.infrastructure.storage.vector_store import (
    VectorDocument,
    VectorStore,
    VectorStoreError,
    document_content_hash,
)


SQL_SOURCE_TYPES = ("sql_history", "reference_sql", "reference_template", "success_story")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
#: Gold/evaluation tasks whose questions and reference SQL must never enter the
#: knowledge base that answers them: indexing the holdout set would invalidate the
#: benchmark it belongs to, because a few-shot example could then be the answer
#: being measured. The default is the repository's frozen holdout split, so the
#: control is wired into the ordinary rebuild path instead of depending on a
#: caller to remember it (step 13, 13-EV1). Pass ``holdout_tasks_path=None`` to
#: ingest material that is deliberately outside the benchmark.
DEFAULT_HOLDOUT_TASKS_PATH = PROJECT_ROOT / "evaluation/tasks/holdout.jsonl"
#: Labels that must stay attached to the block of text that follows them.
_SECTION_LABELS = (
    "Metric ID:",
    "Metric:",
    "Expression:",
    "Aggregation:",
    "Entity:",
    "Definition:",
    "Glossary term:",
    "Synonyms:",
    "Question:",
    "SQL:",
    "Explanation:",
    "Tables:",
)
#: Text carrying an authoritative definition is never split into chunks.
_DEFINITION_MARKERS = ("metric id:", "expression:", "definition:", "glossary term:")
DEFAULT_CHUNK_CHARS = 1200


def load_holdout_registry(path: str | Path | None) -> HoldoutRegistry | None:
    """Build a holdout fingerprint registry from a gold-task JSONL file.

    Reads ``evaluation/tasks/<split>.jsonl`` directly (``question`` plus
    ``reference_sql``) so the storage layer keeps no dependency on the evaluation
    package: the file is a contract, the loader is one line of JSON. A missing
    path means "no holdout material configured" and yields ``None``; a file that
    exists but cannot be parsed raises, because silently ingesting a corpus that
    was supposed to be filtered is the failure mode this control exists to stop.
    """
    if path is None:
        return None
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    if not resolved.is_file():
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VectorStoreError(f"Could not read holdout tasks {resolved}: {exc}") from exc
    registry = HoldoutRegistry()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VectorStoreError(
                f"Holdout task {resolved}:{number} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise VectorStoreError(f"Holdout task {resolved}:{number} is not an object")
        question = str(payload.get("question") or "").strip()
        if not question:
            raise VectorStoreError(f"Holdout task {resolved}:{number} has no question")
        reference_sql = str(payload.get("reference_sql") or "").strip()
        registry.register_holdout(question, reference_sql or None)
    return registry if registry.evaluations else None


class KnowledgeBaseBuilder:
    """Build governed vector documents and keep the index in sync with sources.

    ``rebuild`` is incremental: chunks whose content hash is unchanged are not
    re-embedded, and documents whose source disappeared are deleted. The manifest
    of managed document ids lives in memory by default; pass ``manifest_path`` to
    make stale-source cleanup survive a process restart.

    Holdout isolation is applied at ingest: ``holdout`` takes precedence, then the
    registry loaded from ``holdout_tasks_path`` (the frozen evaluation holdout
    split by default), and ``holdout_tasks_path=None`` disables it.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        manifest_path: str | Path | None = None,
        chunk_max_chars: int = DEFAULT_CHUNK_CHARS,
        holdout: HoldoutRegistry | None = None,
        holdout_tasks_path: str | Path | None = DEFAULT_HOLDOUT_TASKS_PATH,
    ) -> None:
        self.vector_store = vector_store
        self.manifest_path = (
            Path(manifest_path).expanduser() if manifest_path is not None else None
        )
        self.chunk_max_chars = chunk_max_chars
        self.holdout_tasks_path = (
            None if holdout_tasks_path is None else Path(holdout_tasks_path).expanduser()
        )
        self.holdout = (
            holdout
            if holdout is not None
            else load_holdout_registry(self.holdout_tasks_path)
        )
        self.manifest = self._load_manifest()

    def rebuild(
        self,
        *,
        history_store: SQLHistoryStore | None = None,
        schemas: Iterable[TableSchema] = (),
        sources: Iterable[str | Path] = (),
        knowledge: StructuredKnowledgeBase | None = None,
        domain_id: str | None = None,
        data_version: str | None = None,
        holdout: HoldoutRegistry | None = None,
    ) -> dict:
        # Ingest-time isolation: an explicit per-call registry wins, otherwise the
        # builder's registry (the evaluation holdout split by default) applies.
        registry = holdout if holdout is not None else self.holdout
        documents: list[VectorDocument] = []
        if knowledge is not None:
            documents.extend(
                self.build_governed_documents(knowledge, holdout=registry)
            )
        if history_store is not None:
            documents.extend(self.history_documents(history_store))
        documents.extend(self.schema_documents(schemas))
        for source in sources:
            documents.extend(self.source_documents(source))
        refused: list[str] = []
        if registry is not None:
            kept: list[VectorDocument] = []
            for document in documents:
                reason = registry.tainted(document)
                if reason is None:
                    kept.append(document)
                else:
                    refused.append(f"{document.id}: {reason}")
            documents = kept
        chunked: list[VectorDocument] = []
        for document in self._deduplicate(documents):
            chunked.extend(self.chunk_document(document, max_chars=self.chunk_max_chars))
        current = {document.id: document for document in chunked}
        previous = dict(self.manifest)
        stale = [document_id for document_id in previous if document_id not in current]
        write_result = self._write(list(current.values()))
        deleted = 0
        delete_error: str | None = None
        if stale:
            try:
                deleted = int(self.vector_store.delete_documents(ids=stale))
            except (NotImplementedError, VectorStoreError) as exc:
                delete_error = str(exc)
        self.manifest = {
            document_id: self._manifest_entry(document)
            for document_id, document in current.items()
        }
        self._save_manifest()
        try:
            stats = dict(self.vector_store.stats())
        except Exception as exc:  # pragma: no cover - defensive diagnostics only
            stats = {"stats_error": str(exc)}
        stats["source_documents"] = len(documents)
        stats["chunks"] = len(chunked)
        stats["stale_deleted"] = deleted
        stats["write"] = write_result
        stats["holdout_skipped"] = len(refused)
        stats["holdout_refused_ids"] = sorted(refused)
        stats["holdout_source"] = (
            None if self.holdout_tasks_path is None else str(self.holdout_tasks_path)
        )
        stats["holdout_fingerprints"] = (
            0 if registry is None else len(registry.evaluations)
        )
        if delete_error is not None:
            stats["stale_delete_error"] = delete_error
        return stats

    def _write(self, documents: Sequence[VectorDocument]) -> dict[str, int]:
        documents = list(documents)
        try:
            result = self.vector_store.upsert_documents(documents)
        except NotImplementedError:
            result = {"inserted": 0, "updated": 0, "unchanged": 0, "embedded": 0}
        except AttributeError:  # pragma: no cover - pre-step-13 store objects
            result = {"inserted": 0, "updated": 0, "unchanged": 0, "embedded": 0}
        else:
            return {str(key): int(value) for key, value in dict(result).items()}
        written = int(self.vector_store.add_documents(documents))
        return {"inserted": written, "updated": 0, "unchanged": 0, "embedded": written}

    # ------------------------------------------------------------ manifests
    def _manifest_entry(self, document: VectorDocument) -> dict[str, str]:
        return {
            "source_key": str(document.metadata.get("source_key") or document.source_type),
            "content_hash": document_content_hash(document),
        }

    def _load_manifest(self) -> dict[str, dict[str, str]]:
        if self.manifest_path is None or not self.manifest_path.is_file():
            return {}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        documents = payload.get("documents") if isinstance(payload, dict) else None
        if not isinstance(documents, dict):
            return {}
        return {
            str(key): value
            for key, value in documents.items()
            if isinstance(value, dict)
        }

    def _save_manifest(self) -> None:
        if self.manifest_path is None:
            return
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps({"documents": self.manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def history_documents(store: SQLHistoryStore) -> list[VectorDocument]:
        documents = []
        for entry in store.list_entries(limit=100_000):
            if not entry.success:
                continue
            text = KnowledgeBaseBuilder.sql_text(
                entry.question, entry.sql, entry.explanation, entry.tables_used
            )
            scope = entry.metadata if isinstance(entry.metadata, dict) else {}
            documents.append(
                VectorDocument.create(
                    id=f"history:{entry.id}",
                    text=text,
                    source_type="sql_history",
                    created_at=entry.created_at,
                    metadata={
                        "history_id": entry.id,
                        "question": entry.question,
                        "sql": entry.sql,
                        "explanation": entry.explanation,
                        "tables_used": entry.tables_used,
                        "source": entry.source,
                        "source_key": f"history:{entry.id}",
                        # An ungoverned legacy row is at most execution_success:
                        # it is never promoted to a trusted positive example.
                        "verification_level": verification_level_of(
                            scope.get("verification_level")
                            or VerificationLevel.execution_success.value
                        ).value,
                        "review_status": str(scope.get("review_status") or "draft"),
                        "version": scope.get("version"),
                        "owner": scope.get("owner"),
                        # Rebuilding the KB must not silently drop domain scope.
                        **KnowledgeBaseBuilder._domain_metadata(
                            scope.get("domain_id"), scope.get("data_version")
                        ),
                    },
                )
            )
        return KnowledgeBaseBuilder._governed(documents)

    @staticmethod
    def schema_documents(
        schemas: Iterable[TableSchema],
        *,
        domain_id: str | None = None,
        data_version: str | None = None,
        version: str | None = None,
        owner: str | None = None,
        review_status: str = "reviewed",
    ) -> list[VectorDocument]:
        documents = []
        for schema in schemas:
            columns = [
                f"{column.name} ({column.data_type or 'unknown'})"
                for column in schema.columns
            ]
            text = f"Table: {schema.table_name}\nColumns: " + ", ".join(columns)
            documents.append(
                VectorDocument.create(
                    id=f"schema:{schema.table_name}",
                    text=text,
                    source_type="schema_doc",
                    metadata={
                        "table_name": schema.table_name,
                        "columns": [column.model_dump() for column in schema.columns],
                        "source_key": f"schema_doc:{schema.table_name}",
                        "version": version,
                        "owner": owner,
                        "review_status": review_status,
                        "verification_level": VerificationLevel.human_reviewed.value,
                        **KnowledgeBaseBuilder._domain_metadata(
                            domain_id, data_version
                        ),
                    },
                )
            )
        return KnowledgeBaseBuilder._governed(documents)

    @staticmethod
    def successful_query_document(
        *,
        question: str,
        sql_context: SQLContext,
        history_id: int | None,
        domain_id: str | None = None,
        data_version: str | None = None,
        verification_level: VerificationLevel | str | None = None,
        review_status: str = "draft",
        version: str | None = None,
        owner: str | None = None,
    ) -> VectorDocument:
        identifier = f"history:{history_id}" if history_id else KnowledgeBaseBuilder._id(
            "query", question, sql_context.sql
        )
        return KnowledgeBaseBuilder._governed_one(
            VectorDocument.create(
                id=identifier,
                text=KnowledgeBaseBuilder.sql_text(
                    question,
                    sql_context.sql,
                    sql_context.explanation,
                    sql_context.tables_used,
                ),
                source_type="sql_history",
                metadata={
                    "history_id": history_id,
                    "question": question,
                    "sql": sql_context.sql,
                    "explanation": sql_context.explanation,
                    "tables_used": sql_context.tables_used,
                    "source_key": identifier,
                    # A freshly executed query is execution_success, never a
                    # trusted business example.
                    "verification_level": verification_level_of(
                        verification_level
                        or VerificationLevel.execution_success.value
                    ).value,
                    "review_status": review_status,
                    "version": version,
                    "owner": owner,
                    **KnowledgeBaseBuilder._domain_metadata(domain_id, data_version),
                },
            )
        )

    @staticmethod
    def build_governed_documents(
        knowledge: StructuredKnowledgeBase,
        *,
        domain_id: str | None = None,
        permissions: Iterable[str] = (),
        now: Any = None,
        holdout: HoldoutRegistry | None = None,
        skip_tainted: bool = True,
    ) -> list[VectorDocument]:
        """Project a structured knowledge base into governed vector documents.

        Metrics and glossary entries keep their whole definition in one chunk, and
        conflicting documents carry ``conflict_detected`` so a prompt builder can
        surface the disagreement instead of letting similarity rewrite the
        reviewed definition.
        """
        governed: list[GovernedDocument] = knowledge.to_documents(
            domain_id=domain_id,
            permissions=permissions,
            now=now,
            skip_tainted=skip_tainted,
        )
        documents: list[VectorDocument] = []
        for entry in governed:
            reason = holdout.tainted(entry) if holdout is not None else None
            if reason is not None:
                if skip_tainted:
                    continue
                raise VectorStoreError(
                    f"Holdout/evaluation material refused by the knowledge store: "
                    f"{entry.id}: {reason}"
                )
            documents.append(
                KnowledgeBaseBuilder._governed_one(
                    VectorDocument.create(
                        id=entry.id,
                        text=entry.text,
                        source_type=entry.source_type,
                        metadata={
                            **entry.metadata,
                            "content_hash": entry.content_hash
                            or document_content_hash(entry),
                            "source_key": entry.source_type,
                            "chunk_id": f"{entry.id}#1",
                            "atomic_definition": KnowledgeBaseBuilder.is_atomic_definition(
                                entry.text
                            ),
                        },
                    )
                )
            )
        return documents

    @staticmethod
    def _domain_metadata(
        domain_id: str | None, data_version: str | None
    ) -> dict[str, str]:
        """Domain scope entries; omitted entirely for unscoped (legacy) documents."""
        metadata: dict[str, str] = {}
        if isinstance(domain_id, str) and domain_id.strip():
            metadata["domain_id"] = domain_id.strip()
        if isinstance(data_version, str) and data_version.strip():
            metadata["data_version"] = data_version.strip()
        return metadata

    # ------------------------------------------------------- governance glue
    @staticmethod
    def is_atomic_definition(text: str) -> bool:
        """True when a text carries a definition that must never be split."""
        lowered = (text or "").lower()
        return any(marker in lowered for marker in _DEFINITION_MARKERS)

    @staticmethod
    def _governed_one(document: VectorDocument) -> VectorDocument:
        metadata = dict(document.metadata)
        metadata.setdefault("content_hash", document_content_hash(document))
        metadata.setdefault(
            "verification_level",
            verification_level_of(metadata.get("verification_level")).value,
        )
        metadata.setdefault("review_status", "draft")
        metadata.setdefault("chunk_id", f"{document.id}#1")
        return VectorDocument.create(
            id=document.id,
            text=document.text,
            source_type=document.source_type,
            created_at=document.created_at,
            metadata=metadata,
        )

    @staticmethod
    def _governed(documents: Iterable[VectorDocument]) -> list[VectorDocument]:
        return [KnowledgeBaseBuilder._governed_one(document) for document in documents]

    # --------------------------------------------------------------- chunking
    @staticmethod
    def chunk_document(
        document: VectorDocument, *, max_chars: int = DEFAULT_CHUNK_CHARS
    ) -> list[VectorDocument]:
        """Split one document without breaking a definition away from its formula.

        A document whose metadata marks it as an atomic definition (or whose text
        contains a definition marker such as ``Expression:``) is returned as a
        single chunk. Everything else is packed on section boundaries: the label
        lines in ``_SECTION_LABELS`` stay attached to the block they introduce,
        and an oversized block is kept whole rather than truncated.
        """
        atomic = bool(document.metadata.get("atomic_definition")) or (
            KnowledgeBaseBuilder.is_atomic_definition(document.text)
        )
        pieces = [document.text] if atomic else KnowledgeBaseBuilder._pack(document.text, max_chars)
        if len(pieces) == 1:
            metadata = {**document.metadata, "chunk_index": 1, "chunk_count": 1}
            metadata["chunk_id"] = f"{document.id}#1"
            metadata["atomic_definition"] = atomic
            return [
                KnowledgeBaseBuilder._governed_one(
                    VectorDocument.create(
                        id=document.id,
                        text=document.text,
                        source_type=document.source_type,
                        created_at=document.created_at,
                        metadata=metadata,
                    )
                )
            ]
        chunks: list[VectorDocument] = []
        for index, piece in enumerate(pieces, 1):
            metadata = {
                **document.metadata,
                "parent_id": document.id,
                "chunk_index": index,
                "chunk_count": len(pieces),
                "chunk_id": f"{document.id}#{index}",
                "atomic_definition": False,
            }
            chunks.append(
                KnowledgeBaseBuilder._governed_one(
                    VectorDocument.create(
                        id=f"{document.id}#{index}",
                        text=piece,
                        source_type=document.source_type,
                        created_at=document.created_at,
                        metadata=metadata,
                    )
                )
            )
        return chunks

    @staticmethod
    def chunk_documents(
        documents: Iterable[VectorDocument],
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
    ) -> list[VectorDocument]:
        chunks: list[VectorDocument] = []
        for document in documents:
            chunks.extend(
                KnowledgeBaseBuilder.chunk_document(document, max_chars=max_chars)
            )
        return chunks

    @staticmethod
    def _pack(text: str, max_chars: int) -> list[str]:
        blocks = KnowledgeBaseBuilder._blocks(text)
        packed: list[str] = []
        current = ""
        for block in blocks:
            candidate = f"{current}\n{block}".strip() if current else block
            if current and len(candidate) > max_chars:
                packed.append(current)
                current = block
                continue
            current = candidate
        if current:
            packed.append(current)
        return packed or [text]

    @staticmethod
    def _blocks(text: str) -> list[str]:
        """Group label lines with the lines they introduce."""
        blocks: list[str] = []
        current: list[str] = []
        for line in (text or "").splitlines():
            starts_label = any(
                line.strip().startswith(label) for label in _SECTION_LABELS
            )
            if starts_label and current:
                blocks.append("\n".join(current))
                current = [line]
                continue
            if not line.strip() and current:
                blocks.append("\n".join(current))
                current = []
                continue
            current.append(line)
        if current:
            blocks.append("\n".join(current))
        return [block for block in blocks if block.strip()]

    @staticmethod
    def source_documents(source: str | Path) -> list[VectorDocument]:
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"KB source does not exist: {path}")
        if path.is_dir():
            files = sorted(
                file for file in path.iterdir() if file.suffix.lower() in {".sql", ".j2", ".csv"}
            )
        else:
            files = [path]
        documents: list[VectorDocument] = []
        for file in files:
            suffix = file.suffix.lower()
            if suffix == ".sql":
                documents.extend(KnowledgeBaseBuilder._sql_file_documents(file))
            elif suffix == ".j2":
                text = file.read_text(encoding="utf-8").strip()
                if text:
                    documents.append(
                        KnowledgeBaseBuilder._governed_one(
                            VectorDocument.create(
                                id=KnowledgeBaseBuilder._id("template", str(file), text),
                                text=f"Reference SQL template: {file.name}\n{text}",
                                source_type="reference_template",
                                metadata={
                                    "source_file": str(file),
                                    "source_key": f"reference_template:{file}",
                                    "source_path": str(file),
                                    "template": text,
                                    "verification_level": (
                                        VerificationLevel.execution_success.value
                                    ),
                                    "review_status": "reviewed",
                                    "owner": "reference_sql",
                                },
                            )
                        )
                    )
            elif suffix == ".csv":
                documents.extend(KnowledgeBaseBuilder._csv_documents(file))
        return documents

    @staticmethod
    def sql_text(question: str, sql: str, explanation: str, tables: Iterable[str]) -> str:
        return (
            f"Question: {question}\nSQL: {sql}\nExplanation: {explanation}\n"
            f"Tables: {', '.join(tables)}"
        )

    @staticmethod
    def _sql_file_documents(path: Path) -> list[VectorDocument]:
        text = path.read_text(encoding="utf-8")
        chunks = [chunk.strip() for chunk in re.split(r";\s*(?:\n|$)", text) if chunk.strip()]
        documents = []
        for index, chunk in enumerate(chunks, 1):
            comments = re.findall(r"^\s*--\s*(.+)$", chunk, re.MULTILINE)
            sql = re.sub(r"^\s*--.*$", "", chunk, flags=re.MULTILINE).strip()
            if not sql:
                continue
            question = comments[0] if comments else f"{path.stem} query {index}"
            explanation = "\n".join(comments[1:])
            tables = SQLHistoryStore.extract_tables(sql)
            documents.append(
                VectorDocument.create(
                    id=KnowledgeBaseBuilder._id("reference_sql", str(path), str(index), sql),
                    text=KnowledgeBaseBuilder.sql_text(question, sql, explanation, tables),
                    source_type="reference_sql",
                    metadata={
                        "source_file": str(path),
                        "source_key": f"reference_sql:{path}",
                        "source_path": str(path),
                        "question": question,
                        "sql": sql,
                        "explanation": explanation,
                        "tables_used": tables,
                        # Reference SQL is curated material, but only a human
                        # decision can mark it as a trusted positive example.
                        "verification_level": VerificationLevel.execution_success.value,
                        "review_status": "reviewed",
                        "owner": "reference_sql",
                    },
                )
            )
        return KnowledgeBaseBuilder._governed(documents)

    @staticmethod
    def _csv_documents(path: Path) -> list[VectorDocument]:
        documents = []
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for index, row in enumerate(csv.DictReader(handle), 1):
                question = (row.get("question") or "").strip()
                sql = (row.get("sql") or "").strip()
                if not question or not sql:
                    continue
                explanation = (row.get("evidence") or "").strip()
                tables = SQLHistoryStore.extract_tables(sql)
                documents.append(
                    VectorDocument.create(
                        id=KnowledgeBaseBuilder._id("success_story", str(path), str(index), question, sql),
                        text=KnowledgeBaseBuilder.sql_text(question, sql, explanation, tables),
                        source_type="success_story",
                        metadata={
                            "source_file": str(path),
                            "source_key": f"success_story:{path}",
                            "source_path": str(path),
                            "question": question,
                            "sql": sql,
                            "explanation": explanation,
                            "tables_used": tables,
                            "row": row,
                            "verification_level": VerificationLevel.human_reviewed.value,
                            "review_status": "reviewed",
                            "owner": str(row.get("owner") or "business"),
                            "version": row.get("version"),
                        },
                    )
                )
        return KnowledgeBaseBuilder._governed(documents)

    @staticmethod
    def _deduplicate(documents: Iterable[VectorDocument]) -> list[VectorDocument]:
        """Drop duplicate ids (last wins) and byte-identical duplicate chunks."""
        by_id: dict[str, VectorDocument] = {}
        seen_content: set[tuple[str, str]] = set()
        for document in documents:
            if document.id in by_id:
                by_id[document.id] = document
                continue
            fingerprint = (document.source_type, document_content_hash(document))
            if fingerprint in seen_content:
                continue
            seen_content.add(fingerprint)
            by_id[document.id] = document
        return list(by_id.values())

    @staticmethod
    def _id(*parts: str) -> str:
        digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
        return digest[:32]
