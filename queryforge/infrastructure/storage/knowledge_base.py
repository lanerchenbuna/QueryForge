"""Build compact vector documents from QueryForge's supported knowledge sources."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from queryforge.core.schemas.models import SQLContext, TableSchema
from queryforge.infrastructure.storage.sql_history_store import SQLHistoryStore
from queryforge.infrastructure.storage.vector_store import VectorDocument, VectorStore


SQL_SOURCE_TYPES = ("sql_history", "reference_sql", "reference_template", "success_story")


class KnowledgeBaseBuilder:
    def __init__(self, vector_store: VectorStore) -> None:
        self.vector_store = vector_store

    def rebuild(
        self,
        *,
        history_store: SQLHistoryStore | None = None,
        schemas: Iterable[TableSchema] = (),
        sources: Iterable[str | Path] = (),
    ) -> dict:
        documents: list[VectorDocument] = []
        if history_store is not None:
            documents.extend(self.history_documents(history_store))
        documents.extend(self.schema_documents(schemas))
        for source in sources:
            documents.extend(self.source_documents(source))
        stats = self.vector_store.rebuild(self._deduplicate(documents))
        stats["source_documents"] = len(documents)
        return stats

    @staticmethod
    def history_documents(store: SQLHistoryStore) -> list[VectorDocument]:
        documents = []
        for entry in store.list_entries(limit=100_000):
            if not entry.success:
                continue
            text = KnowledgeBaseBuilder.sql_text(
                entry.question, entry.sql, entry.explanation, entry.tables_used
            )
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
                    },
                )
            )
        return documents

    @staticmethod
    def schema_documents(schemas: Iterable[TableSchema]) -> list[VectorDocument]:
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
                    },
                )
            )
        return documents

    @staticmethod
    def successful_query_document(
        *, question: str, sql_context: SQLContext, history_id: int | None
    ) -> VectorDocument:
        identifier = f"history:{history_id}" if history_id else KnowledgeBaseBuilder._id(
            "query", question, sql_context.sql
        )
        return VectorDocument.create(
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
            },
        )

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
                        VectorDocument.create(
                            id=KnowledgeBaseBuilder._id("template", str(file), text),
                            text=f"Reference SQL template: {file.name}\n{text}",
                            source_type="reference_template",
                            metadata={"source_file": str(file), "template": text},
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
                        "question": question,
                        "sql": sql,
                        "explanation": explanation,
                        "tables_used": tables,
                    },
                )
            )
        return documents

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
                            "question": question,
                            "sql": sql,
                            "explanation": explanation,
                            "tables_used": tables,
                            "row": row,
                        },
                    )
                )
        return documents

    @staticmethod
    def _deduplicate(documents: Iterable[VectorDocument]) -> list[VectorDocument]:
        return list({document.id: document for document in documents}.values())

    @staticmethod
    def _id(*parts: str) -> str:
        digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
        return digest[:32]
