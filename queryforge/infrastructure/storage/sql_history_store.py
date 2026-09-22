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

from queryforge.core.paths import workspace_root
from queryforge.core.schemas.models import HistoryMatch
from queryforge.domain.knowledge import (
    VerificationLevel,
    is_trusted_for_examples,
    verification_level_of,
)
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


PROJECT_ROOT = workspace_root()
DEFAULT_HISTORY_DB_PATH = PROJECT_ROOT / ".queryforge/history.db"
#: Hard bound on the rows one search may scan. Similarity is scored in Python, so
#: the candidate window must be bounded; it is explicit (constructor override) and
#: always reported in the search evidence instead of being an invisible cut.
DEFAULT_SEARCH_WINDOW = 2000
#: Material curated outside a run: imported success stories and reference SQL.
#: Those rows are imported first and therefore carry the lowest ids, so a purely
#: recency-ordered window evicts them as soon as a workspace records enough runs.
CURATED_SOURCES = ("success_story", "reference_sql", "reference_template")
#: Textual spelling of a reviewed row as this store writes it (``json.dumps``
#: emits ``"key": value``, the compact form covers externally written files).
#: Ordering only: every governance decision is still decoded in Python, so a row
#: whose metadata misses these patterns is merely ranked lower, never trusted.
_REVIEWED_METADATA_PATTERNS = (
    '%"review_status": "reviewed"%',
    '%"review_status":"reviewed"%',
)


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
    # Step 13 governance: verification is never implied by a successful execution.
    verification_level: str = VerificationLevel.unverified.value
    review_status: str = "draft"
    domain_id: str | None = None
    data_version: str | None = None
    reviewed_by: str | None = None
    corrected_reason: str | None = None

    @property
    def trusted(self) -> bool:
        return is_trusted_for_examples(self.verification_level)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    inserted: int = 0
    duplicates: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HistorySearchResult:
    """Matches plus the evidence of how the window and the scope were applied.

    ``HistoryMatch`` carries no governance fields, so a caller that injects the
    matches into a prompt cannot tell from the matches alone whether a domain
    scope was applied, or how much history the ranking actually considered. The
    evidence records the applied scope, the candidate window, per-step candidate
    counts and the governance state of every returned row, so the control can be
    asserted instead of assumed.
    """

    matches: list[HistoryMatch]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": [match.model_dump() for match in self.matches],
            "evidence": self.evidence,
        }


class SQLHistoryStore:
    """Persist compact query metadata; result rows are deliberately never stored."""

    def __init__(
        self,
        database_path: str | Path = DEFAULT_HISTORY_DB_PATH,
        *,
        search_window: int = DEFAULT_SEARCH_WINDOW,
    ) -> None:
        path = Path(database_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.database_path = path.resolve()
        if int(search_window) < 1:
            raise SQLHistoryError("search_window must be a positive row count")
        #: Rows one search may scan; reported in every search evidence payload.
        self.search_window = int(search_window)
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
        verification_level: VerificationLevel | str | None = None,
        review_status: str = "draft",
        domain_id: str | None = None,
        data_version: str | None = None,
        owner: str | None = None,
        version: str | None = None,
    ) -> tuple[int, bool]:
        """Persist one history row with its step-13 governance fields.

        Governance lives in the existing ``metadata`` JSON rather than in new
        columns: ``CREATE TABLE IF NOT EXISTS`` cannot add columns to an existing
        history database, so a stored-metadata design keeps every previously
        written file readable without a migration (documented choice).
        """
        question = question.strip()
        sql = sql.strip()
        if not question or not sql:
            raise SQLHistoryError("History question and SQL must be non-empty")
        tables = list(dict.fromkeys(table.strip() for table in tables_used if table.strip()))
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        scope = dict(metadata or {})
        # A successful execution is at most execution_success: never trust.
        scope["verification_level"] = verification_level_of(
            verification_level
            if verification_level is not None
            else (
                VerificationLevel.execution_success.value
                if success
                else VerificationLevel.unverified.value
            )
        ).value
        scope["review_status"] = str(review_status or "draft")
        if domain_id is not None:
            scope["domain_id"] = domain_id
        if data_version is not None:
            scope["data_version"] = data_version
        if owner is not None:
            scope["owner"] = owner
        if version is not None:
            scope["version"] = version
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
                        json.dumps(scope, ensure_ascii=False),
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
        domain_id: str | None = None,
        data_version: str | None = None,
        trusted_only: bool = False,
    ) -> list[HistoryMatch]:
        """Return similar successful history rows.

        Passing ``domain_id`` switches the search to scoped mode: only rows whose
        metadata carries the same ``domain_id`` (and the same ``data_version``
        when one is given) qualify. Rows with missing or ``None`` metadata
        ``domain_id`` are ``legacy_unscoped`` and are deliberately excluded, so a
        scoped query never retrieves another domain's SQL. Note that filtering
        happens before the ``top_k`` cut, not after it.

        ``trusted_only`` restricts the result to ``human_reviewed`` rows: only a
        business review makes a row a trusted few-shot example, because a
        successful execution does not prove business correctness.

        The candidate window and the applied scope are part of the result of
        :meth:`search_with_evidence`; this convenience wrapper returns the matches.
        """
        return self.search_with_evidence(
            question,
            top_k=top_k,
            tables_used=tables_used,
            minimum_similarity=minimum_similarity,
            domain_id=domain_id,
            data_version=data_version,
            trusted_only=trusted_only,
        ).matches

    def search_with_evidence(
        self,
        question: str,
        *,
        top_k: int = 3,
        tables_used: Iterable[str] | None = None,
        minimum_similarity: float = 0.2,
        domain_id: str | None = None,
        data_version: str | None = None,
        trusted_only: bool = False,
        window: int | None = None,
    ) -> HistorySearchResult:
        """Search and report the window, the scope and the governance of the hits.

        The window is bounded by ``search_window`` (overridable per call) because
        similarity is scored in Python, and it is *prioritised*: reviewed rows
        first, then curated imports, then self-recorded run history by recency. A
        plain ``ORDER BY id DESC`` window silently evicts curated rows — they are
        imported first and therefore carry the lowest ids — and then returns
        unrelated recent rows that look like matches. The evidence records the
        limit, how many rows were scanned, every filter step's candidate count and
        the scope/verification state of each returned row.
        """
        limit = self.search_window if window is None else int(window)
        if limit < 1:
            raise SQLHistoryError("window must be a positive row count")
        scope_domain = self._normalize_scope(domain_id, "domain_id")
        scope_version = self._normalize_scope(data_version, "data_version")
        required_tables = {
            table.strip().lower() for table in (tables_used or ()) if table.strip()
        }
        evidence: dict[str, Any] = {
            "status": "active",
            "question": question.strip(),
            "scope": {"domain_id": scope_domain, "data_version": scope_version},
            "trusted_only": bool(trusted_only),
            "minimum_similarity": float(minimum_similarity),
            "tables_used": sorted(required_tables),
            "candidate_window": {
                "limit": limit,
                "scanned": 0,
                "order": ["reviewed", "curated_source", "id_desc"],
                "curated_sources": list(CURATED_SOURCES),
            },
            "counts": {
                "scanned": 0,
                "in_scope": 0,
                "trusted": 0,
                "matching_tables": 0,
                "similar": 0,
            },
            "returned": [],
        }
        if top_k <= 0 or not question.strip():
            evidence["status"] = "empty_request"
            evidence["reason"] = (
                "top_k_not_positive" if top_k <= 0 else "empty_question"
            )
            return HistorySearchResult(matches=[], evidence=evidence)
        try:
            rows = self._candidate_rows(limit)
        except sqlite3.Error as exc:
            raise SQLHistoryError(f"Could not search SQL history: {exc}") from exc
        evidence["candidate_window"]["scanned"] = len(rows)
        evidence["counts"]["scanned"] = len(rows)

        candidates: list[tuple[sqlite3.Row, dict[str, Any], list[str], float]] = []
        for row in rows:
            metadata = self._decode_json_metadata(row["metadata"])
            if scope_domain is not None and not self._in_domain_scope(
                row["metadata"], scope_domain, scope_version
            ):
                continue
            evidence["counts"]["in_scope"] += 1
            if trusted_only and not is_trusted_for_examples(
                metadata.get("verification_level")
            ):
                continue
            evidence["counts"]["trusted"] += 1
            tables = self._decode_json_list(row["tables_used"])
            if required_tables and not required_tables.issubset(
                {table.lower() for table in tables}
            ):
                continue
            evidence["counts"]["matching_tables"] += 1
            similarity = self.similarity(question, row["question"])
            if similarity < minimum_similarity:
                continue
            evidence["counts"]["similar"] += 1
            candidates.append((row, metadata, tables, similarity))
        candidates.sort(
            key=lambda item: (item[3], int(item[0]["id"])), reverse=True
        )
        selected = candidates[:top_k]
        evidence["returned"] = [
            {
                "id": int(row["id"]),
                "similarity": round(similarity, 4),
                "domain_id": metadata.get("domain_id"),
                "data_version": metadata.get("data_version"),
                "verification_level": verification_level_of(
                    metadata.get("verification_level")
                ).value,
                "review_status": str(metadata.get("review_status") or "draft"),
                "source": str(row["source"]),
            }
            for row, metadata, _tables, similarity in selected
        ]
        return HistorySearchResult(
            matches=[
                self._row_to_match(row, tables, similarity)
                for row, _metadata, tables, similarity in selected
            ],
            evidence=evidence,
        )

    def mark_reviewed(self, history_id: int, reviewer: str) -> HistoryEntry | None:
        """Promote one row to ``human_reviewed``: the trusted few-shot level.

        Returns the updated entry, or ``None`` when the id does not exist. A row
        that never executed successfully is left unverified: review cannot turn a
        broken query into a trusted positive example.
        """
        if not str(reviewer or "").strip():
            raise SQLHistoryError("mark_reviewed requires a non-empty reviewer")
        return self._update_governance(
            history_id,
            {
                "verification_level": VerificationLevel.human_reviewed.value,
                "review_status": "reviewed",
                "reviewed_by": str(reviewer).strip(),
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            },
            require_success=True,
        )

    def mark_corrected(self, history_id: int, reason: str) -> HistoryEntry | None:
        """Downgrade one row after a business correction.

        The row keeps its text (diagnostics may still use it) but loses all
        trust: ``verification_level=unverified`` and ``review_status=deprecated``,
        with ``invalidated_at`` recording that any cached reference to it is
        stale. Returns the updated entry, or ``None`` when the id does not exist.
        """
        if not str(reason or "").strip():
            raise SQLHistoryError("mark_corrected requires a non-empty reason")
        timestamp = datetime.now(timezone.utc).isoformat()
        return self._update_governance(
            history_id,
            {
                "verification_level": VerificationLevel.unverified.value,
                "review_status": "deprecated",
                "corrected_reason": str(reason).strip(),
                "corrected_at": timestamp,
                "invalidated_at": timestamp,
            },
        )

    def _update_governance(
        self,
        history_id: int,
        updates: dict[str, Any],
        *,
        require_success: bool = False,
    ) -> HistoryEntry | None:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM sql_history WHERE id = ?", (int(history_id),)
                ).fetchone()
                if row is None:
                    return None
                scope = self._decode_json_metadata(row["metadata"])
                scope.update(updates)
                if require_success and not bool(row["success"]):
                    scope["verification_level"] = VerificationLevel.unverified.value
                    scope["review_status"] = "draft"
                    scope["review_blocked_reason"] = "row_did_not_execute_successfully"
                connection.execute(
                    "UPDATE sql_history SET metadata = ? WHERE id = ?",
                    (json.dumps(scope, ensure_ascii=False), int(history_id)),
                )
                updated = connection.execute(
                    "SELECT * FROM sql_history WHERE id = ?", (int(history_id),)
                ).fetchone()
        except sqlite3.Error as exc:
            raise SQLHistoryError(f"Could not update history governance: {exc}") from exc
        return self._row_to_entry(updated) if updated is not None else None

    def list_domains(self) -> list[str]:
        """Return distinct non-empty ``domain_id`` values recorded in metadata."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT metadata FROM sql_history"
                ).fetchall()
        except sqlite3.Error as exc:
            raise SQLHistoryError(f"Could not list history domains: {exc}") from exc
        domains: set[str] = set()
        for row in rows:
            value = self._decode_json_metadata(row["metadata"]).get("domain_id")
            if isinstance(value, str) and value.strip():
                domains.add(value.strip())
        return sorted(domains)

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
                        sql = DatabaseTool.validate_readonly_shape(sql)
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
                    reviewer = str(
                        metadata.get("reviewer") or metadata.get("reviewed_by") or ""
                    ).strip()
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
                        # Imported material is trusted only when the source names
                        # the reviewer; otherwise it stays execution_success.
                        verification_level=(
                            VerificationLevel.human_reviewed
                            if reviewer
                            else VerificationLevel.execution_success
                        ),
                        review_status="reviewed" if reviewer else "draft",
                        owner=reviewer or None,
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
                    sql = DatabaseTool.validate_readonly_shape(sql)
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

    @staticmethod
    def _decode_json_metadata(value: str) -> dict[str, Any]:
        """Decode a metadata column; malformed or non-object values become ``{}``."""
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _normalize_scope(value: str | None, label: str) -> str | None:
        """Normalize an optional scope filter; a blank filter is never a wildcard."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise SQLHistoryError(
                f"{label} must be a non-empty string when provided for scoped search"
            )
        return value.strip()

    def _candidate_rows(self, limit: int) -> list[sqlite3.Row]:
        """Read the prioritised candidate window of successful rows."""
        reviewed_clauses = " OR ".join(
            "metadata LIKE ?" for _ in _REVIEWED_METADATA_PATTERNS
        )
        curated_clauses = ", ".join("?" for _ in CURATED_SOURCES)
        statement = (
            "SELECT *, CASE "
            f"WHEN {reviewed_clauses} THEN 0 "
            f"WHEN source IN ({curated_clauses}) THEN 1 "
            "ELSE 2 END AS curation_rank "
            "FROM sql_history WHERE success = 1 "
            "ORDER BY curation_rank, id DESC LIMIT ?"
        )
        parameters = (
            *_REVIEWED_METADATA_PATTERNS,
            *CURATED_SOURCES,
            int(limit),
        )
        with self._connection() as connection:
            return list(connection.execute(statement, parameters).fetchall())

    @staticmethod
    def _row_to_match(
        row: sqlite3.Row, tables: list[str], similarity: float
    ) -> HistoryMatch:
        return HistoryMatch(
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

    @classmethod
    def _in_domain_scope(
        cls, metadata: str, domain_id: str, data_version: str | None
    ) -> bool:
        """True when a row's metadata matches the requested domain scope."""
        recorded = cls._decode_json_metadata(metadata)
        if recorded.get("domain_id") != domain_id:
            return False
        if data_version is not None and recorded.get("data_version") != data_version:
            return False
        return True

    @classmethod
    def _row_to_entry(cls, row: sqlite3.Row) -> HistoryEntry:
        metadata = cls._decode_json_metadata(row["metadata"])
        recorded_level = metadata.get("verification_level")
        if recorded_level is None:
            # Legacy rows predate verification levels: a successful execution is
            # execution_success (never trusted), anything else is unverified.
            recorded_level = (
                VerificationLevel.execution_success.value
                if bool(row["success"])
                else VerificationLevel.unverified.value
            )
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
            verification_level=verification_level_of(recorded_level).value,
            review_status=str(metadata.get("review_status") or "draft"),
            domain_id=metadata.get("domain_id"),
            data_version=metadata.get("data_version"),
            reviewed_by=metadata.get("reviewed_by"),
            corrected_reason=metadata.get("corrected_reason"),
        )
