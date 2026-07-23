"""SQLite-backed successful SQL history and lightweight lexical retrieval."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from queryforge.core.schemas.models import HistoryMatch
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HISTORY_DB_PATH = PROJECT_ROOT / ".queryforge/history.db"


class SQLHistoryError(RuntimeError):
    """Raised for history storage, parsing, or schema failures."""


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: int
    question: str
    sql: str
    explanation: str
    tables_used: list[str]
    success: bool
    error: str | None
    row_count: int | None
    created_at: str
    provider: str | None
    model: str | None
    metadata: dict[str, Any]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    inserted: int = 0
    duplicates: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class SQLHistoryStore:
    """Persist compact query metadata; result rows are deliberately never stored."""

    def __init__(self, database_path: str | Path = DEFAULT_HISTORY_DB_PATH) -> None:
        path = Path(database_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.database_path = path.resolve()
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error) as exc:
            raise SQLHistoryError(
                f"Could not initialize SQL history at {self.database_path}: {exc}"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sql_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '',
                    tables_used TEXT NOT NULL DEFAULT '[]',
                    success INTEGER NOT NULL CHECK (success IN (0, 1)),
                    error TEXT,
                    row_count INTEGER,
                    created_at TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'query',
                    UNIQUE(question, sql)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sql_history_success_created "
                "ON sql_history(success, created_at DESC)"
            )

    def add(
        self,
        *,
        question: str,
        sql: str,
        explanation: str = "",
        tables_used: Iterable[str] = (),
        success: bool,
        error: str | None = None,
        row_count: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "query",
        created_at: str | None = None,
    ) -> tuple[int, bool]:
        question = question.strip()
        sql = sql.strip()
        if not question or not sql:
            raise SQLHistoryError("History question and SQL must be non-empty")
        tables = list(dict.fromkeys(table.strip() for table in tables_used if table.strip()))
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO sql_history (
                        question, sql, explanation, tables_used, success, error,
                        row_count, created_at, provider, model, metadata, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(question, sql) DO NOTHING
                    """,
                    (
                        question,
                        sql,
                        explanation.strip(),
                        json.dumps(tables, ensure_ascii=False),
                        int(success),
                        error,
                        row_count,
                        timestamp,
                        provider,
                        model,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        source,
                    ),
                )
                inserted = cursor.rowcount == 1
                if inserted:
                    return int(cursor.lastrowid), True
                row = connection.execute(
                    "SELECT id FROM sql_history WHERE question = ? AND sql = ?",
                    (question, sql),
                ).fetchone()
                if row is None:
                    raise SQLHistoryError("History deduplication did not return an id")
                return int(row["id"]), False
        except (sqlite3.Error, TypeError, ValueError) as exc:
            if isinstance(exc, SQLHistoryError):
                raise
            raise SQLHistoryError(f"Could not write SQL history: {exc}") from exc

    def search(
        self,
        question: str,
        top_k: int = 3,
        tables_used: Iterable[str] | None = None,
        minimum_similarity: float = 0.2,
    ) -> list[HistoryMatch]:
        if top_k <= 0 or not question.strip():
            return []
        required_tables = {
            table.strip().lower() for table in (tables_used or ()) if table.strip()
        }
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM sql_history WHERE success = 1 "
                    "ORDER BY id DESC LIMIT 2000"
                ).fetchall()
        except sqlite3.Error as exc:
            raise SQLHistoryError(f"Could not search SQL history: {exc}") from exc

        matches: list[HistoryMatch] = []
        for row in rows:
            tables = self._decode_json_list(row["tables_used"])
            if required_tables and not required_tables.issubset(
                {table.lower() for table in tables}
            ):
                continue
            similarity = self.similarity(question, row["question"])
            if similarity < minimum_similarity:
                continue
            matches.append(
                HistoryMatch(
                    id=row["id"],
                    question=row["question"],
                    sql=row["sql"],
                    explanation=row["explanation"],
                    tables_used=tables,
                    similarity=round(similarity, 4),
                    row_count=row["row_count"],
                    provider=row["provider"],
                    model=row["model"],
                    created_at=row["created_at"],
                    source=row["source"],
                )
            )
        matches.sort(key=lambda item: (item.similarity, item.id), reverse=True)
        return matches[:top_k]

    def list_entries(self, limit: int = 50) -> list[HistoryEntry]:
        if limit <= 0:
            return []
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM sql_history ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        except sqlite3.Error as exc:
            raise SQLHistoryError(f"Could not list SQL history: {exc}") from exc
        return [self._row_to_entry(row) for row in rows]

    def clear(self) -> int:
        try:
            with self._connection() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM sql_history"
                ).fetchone()[0]
                connection.execute("DELETE FROM sql_history")
                return int(count)
        except sqlite3.Error as exc:
            raise SQLHistoryError(f"Could not clear SQL history: {exc}") from exc

    def import_success_stories(self, csv_path: str | Path) -> ImportSummary:
        path = Path(csv_path).expanduser().resolve()
        if not path.is_file():
            raise SQLHistoryError(f"Success story CSV does not exist: {path}")
        inserted = duplicates = skipped = 0
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    question = (row.get("question") or "").strip()
                    sql = (row.get("sql") or "").strip()
                    if not question or not sql:
                        skipped += 1
                        continue
                    try:
                        sql = DatabaseTool.validate_readonly_sql(sql)
                    except UnsafeSQLError:
                        skipped += 1
                        continue
                    metadata = {
                        key: value
                        for key, value in row.items()
                        if key not in {"question", "sql"} and value not in {None, ""}
                    }
                    evidence = str(metadata.get("evidence") or "")
                    expected = str(metadata.get("expected_table") or "")
                    tables = self._split_table_names(expected) or self.extract_tables(sql)
                    _, created = self.add(
                        question=question,
                        sql=sql,
                        explanation=evidence,
                        tables_used=tables,
                        success=True,
                        provider="import",
                        model=path.name,
                        metadata=metadata,
                        source="success_story",
                    )
                    if created:
                        inserted += 1
                    else:
                        duplicates += 1
        except (OSError, csv.Error) as exc:
            raise SQLHistoryError(f"Could not import success stories: {exc}") from exc
        return ImportSummary(inserted, duplicates, skipped)

    def import_reference_sql(self, source_path: str | Path) -> ImportSummary:
        path = Path(source_path).expanduser().resolve()
        files = sorted(path.glob("*.sql")) if path.is_dir() else [path]
        if not files or any(not file.is_file() for file in files):
            raise SQLHistoryError(f"Reference SQL path has no readable SQL files: {path}")
        inserted = duplicates = skipped = 0
        for file in files:
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SQLHistoryError(f"Could not read reference SQL {file}: {exc}") from exc
            for index, (comments, sql) in enumerate(self._parse_reference_text(text), 1):
                try:
                    sql = DatabaseTool.validate_readonly_sql(sql)
                except UnsafeSQLError:
                    skipped += 1
                    continue
                question = comments[0] if comments else f"{file.stem} query {index}"
                evidence = "\n".join(comments[1:])
                _, created = self.add(
                    question=question,
                    sql=sql,
                    explanation=evidence,
                    tables_used=self.extract_tables(sql),
                    success=True,
                    provider="import",
                    model=file.name,
                    metadata={"source_file": str(file), "evidence": evidence},
                    source="reference_sql",
                )
                if created:
                    inserted += 1
                else:
                    duplicates += 1
        return ImportSummary(inserted, duplicates, skipped)

    @staticmethod
    def similarity(left: str, right: str) -> float:
        normalized_left = SQLHistoryStore._normalize(left)
        normalized_right = SQLHistoryStore._normalize(right)
        if normalized_left == normalized_right:
            return 1.0
        left_tokens = set(normalized_left.split())
        right_tokens = set(normalized_right.split())
        union = left_tokens | right_tokens
        token_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
        sequence_score = SequenceMatcher(
            None, normalized_left, normalized_right
        ).ratio()
        left_bigrams = SQLHistoryStore._bigrams(normalized_left.replace(" ", ""))
        right_bigrams = SQLHistoryStore._bigrams(normalized_right.replace(" ", ""))
        bigram_union = left_bigrams | right_bigrams
        bigram_score = (
            len(left_bigrams & right_bigrams) / len(bigram_union)
            if bigram_union
            else 0.0
        )
        return max(token_score, sequence_score, bigram_score)

    @staticmethod
    def extract_tables(sql: str) -> list[str]:
        tables = re.findall(
            r"\b(?:from|join)\s+[`\"\[]?([A-Za-z_][A-Za-z0-9_]*)",
            sql,
            re.IGNORECASE,
        )
        return list(dict.fromkeys(tables))

    @staticmethod
    def _parse_reference_text(text: str) -> list[tuple[list[str], str]]:
        results: list[tuple[list[str], str]] = []
        comments: list[str] = []
        sql_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("--") and not sql_lines:
                comments.append(stripped[2:].strip())
                continue
            if not stripped and not sql_lines:
                continue
            sql_lines.append(line)
            combined = "\n".join(sql_lines).strip()
            while ";" in combined:
                statement, combined = combined.split(";", 1)
                if statement.strip():
                    results.append((comments, statement.strip()))
                comments = []
                combined = combined.strip()
            sql_lines = [combined] if combined else []
        trailing = "\n".join(sql_lines).strip()
        if trailing:
            results.append((comments, trailing))
        return results

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(re.findall(r"[^\W_]+", text.lower(), re.UNICODE))

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        if len(text) < 2:
            return {text} if text else set()
        return {text[index : index + 2] for index in range(len(text) - 1)}

    @staticmethod
    def _split_table_names(value: str) -> list[str]:
        return [
            item.strip().strip("`\"[]")
            for item in re.split(r"[,;\s]+", value)
            if item.strip()
        ]

    @staticmethod
    def _decode_json_list(value: str) -> list[str]:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    @classmethod
    def _row_to_entry(cls, row: sqlite3.Row) -> HistoryEntry:
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return HistoryEntry(
            id=row["id"],
            question=row["question"],
            sql=row["sql"],
            explanation=row["explanation"],
            tables_used=cls._decode_json_list(row["tables_used"]),
            success=bool(row["success"]),
            error=row["error"],
            row_count=row["row_count"],
            created_at=row["created_at"],
            provider=row["provider"],
            model=row["model"],
            metadata=metadata,
            source=row["source"],
        )
