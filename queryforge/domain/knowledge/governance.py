"""Governed knowledge contracts: identity, version, validity, permission, review.

This module is the authoritative, deterministic half of the step-13 retrieval
chain. It deliberately depends on nothing but the standard library and
``pydantic``: it must not import workflow, infrastructure, or application code,
because the same governance rules guard interactive retrieval, offline indexing,
and evaluation data.

Design rules encoded here:

* A structured metric definition is authoritative. Vector similarity may point at
  a document, but it never overrides the reviewed definition of a metric.
* ``execution_success`` proves the SQL ran; it does **not** prove the business
  answer is right. Only ``human_reviewed`` material is a trusted positive
  example.
* Expired, deprecated, or permission-denied candidates are rejected *before*
  ranking, so an out-of-scope document can never win a similarity contest.
* Evaluation/holdout material is fingerprinted and refused entry to a knowledge
  store, so gold answers cannot leak into the corpus that is being evaluated.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, Field


__all__ = [
    "CONTENT_VERSION_LENGTH",
    "DEFAULT_HOLDOUT_OVERLAP_RATIO",
    "GlossaryEntry",
    "GovernedDocument",
    "HoldoutContaminationError",
    "HoldoutEntry",
    "HoldoutRegistry",
    "KnowledgeGovernanceError",
    "KnowledgeResolution",
    "KnowledgeSource",
    "MetricKnowledgeEntry",
    "SqlExampleDecision",
    "SqlExampleGovernance",
    "StructuredKnowledgeBase",
    "VerificationLevel",
    "classify_sql_example",
    "content_hash",
    "content_version",
    "is_trusted_for_examples",
    "metric_key",
    "normalize_term",
    "verification_level_of",
]


DEFAULT_HOLDOUT_OVERLAP_RATIO = 0.65
#: Length of a governed content version. Short digests keep a version handle
#: usable in an operator command, and match the semantic-model fingerprint
#: convention (``sha256`` hex prefix) so every kind of definition a session turn
#: records carries a version string of the same shape.
CONTENT_VERSION_LENGTH = 12
_REVIEW_STATUS = Literal["draft", "reviewed", "deprecated"]
_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_AGGREGATE_PATTERN = re.compile(
    r"\b(?:sum|avg|average|count|min|max|median|percentile|round)\s*\(",
    re.IGNORECASE,
)
_FORMULA_PATTERN_TEMPLATE = r"%s\s*(?:=|:|is|为|是|定义)\s*([^\n;。]{2,160})"


class KnowledgeGovernanceError(RuntimeError):
    """Raised when governed knowledge is malformed or out of policy."""


class HoldoutContaminationError(KnowledgeGovernanceError):
    """Raised when evaluation/holdout material tries to enter a knowledge store."""


def normalize_term(value: str) -> str:
    """Normalize a business term for alias matching (case/space/punctuation free)."""
    return " ".join(_TOKEN_PATTERN.findall((value or "").lower()))


def _normalize_expression(value: str) -> str:
    """Normalize a metric expression for comparison, keeping operators intact.

    Unlike :func:`normalize_term` this keeps ``SUM(a * b)``-style operators, so
    two different formulas cannot collapse into the same string.
    """
    return re.sub(r"\s+", " ", (value or "").strip().lower()).strip()


def _alias_pattern(alias: str) -> str:
    return r"\s+".join(re.escape(part) for part in alias.lower().split())


def content_hash(text: str) -> str:
    """Stable content hash used to decide whether a chunk needs re-embedding."""
    normalized = "\n".join(
        line.strip() for line in (text or "").strip().splitlines() if line.strip()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_version(*texts: str) -> str:
    """Short content version of governed material (a glossary or document digest).

    A governed definition needs a *version* an operator can name and a later
    process can compare, not just an equality check: ``SessionStore``'s
    ``invalidate_version`` marks the turns that used the version that was
    superseded, so "which revision did this answer rely on?" has to be
    answerable after the definition changed. The version is therefore the digest
    of the entry content — any edit to a definition, a synonym, an owner or a
    review status changes it — and it is deliberately short: the full 64-char
    hash is unusable as a handle in a command line.

    Each text is hashed on its own before the parts are combined, so moving text
    between two entries can never produce the same version for two different
    partitions of the same content. An empty call returns ``""``: no content
    means no version, and callers must then record no reference instead of
    inventing one.
    """
    digests = [
        content_hash(str(text))
        for text in texts
        if str(text or "").strip()
    ]
    if not digests:
        return ""
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()[
        :CONTENT_VERSION_LENGTH
    ]


def _text_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_term(text).encode("utf-8")).hexdigest()


def metric_key(entry: "MetricKnowledgeEntry") -> str:
    """Storage key for a metric: id plus version, so versions coexist."""
    return f"{entry.metric_id}::{entry.version or 'unversioned'}"


def glossary_key(entry: "GlossaryEntry") -> str:
    """Storage key for a glossary term: term plus domain plus version.

    Two domains may legitimately define the same term differently ("revenue" in
    commerce vs payments), so a term alone is not an identity: keying by term
    would let the last writer silently delete the other domain's definition and
    its provenance.
    """
    return (
        f"{normalize_term(entry.term)}::{entry.domain_id or 'global'}"
        f"::{entry.version or 'unversioned'}"
    )


def _token_hashes(text: str) -> list[str]:
    """Hashed normalized tokens: overlap can be measured without keeping plaintext."""
    tokens = _TOKEN_PATTERN.findall(normalize_term(text))
    shingles = {" ".join(tokens[index : index + 2]) for index in range(len(tokens))}
    shingles.update(tokens)
    return sorted(
        {
            hashlib.sha256(shingle.encode("utf-8")).hexdigest()
            for shingle in shingles
            if shingle
        }
    )


def _utc_now(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnowledgeGovernanceError(f"Invalid timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        return _utc_now(str(value))
    except KnowledgeGovernanceError:
        return None


class VerificationLevel(str, Enum):
    """How far an SQL example has actually been verified.

    ``execution_success`` is intentionally *not* trust: a query can run and still
    answer the wrong business question, so it is never promoted to a trusted
    positive example on its own.
    """

    unverified = "unverified"
    execution_success = "execution_success"
    human_reviewed = "human_reviewed"


def verification_level_of(value: Any) -> VerificationLevel:
    """Coerce unknown/missing values to ``unverified`` (fail closed)."""
    if isinstance(value, VerificationLevel):
        return value
    if isinstance(value, str):
        try:
            return VerificationLevel(value.strip().lower())
        except ValueError:
            return VerificationLevel.unverified
    return VerificationLevel.unverified


def is_trusted_for_examples(level: VerificationLevel | str | None) -> bool:
    """Only human-reviewed examples count as trusted positive examples.

    ``execution_success`` MUST NOT be treated as business correctness.
    """
    return verification_level_of(level) is VerificationLevel.human_reviewed


class SqlExampleDecision(BaseModel):
    """Verification decision for one SQL example, with the reason recorded."""

    level: VerificationLevel
    trusted: bool
    reason: str
    downgraded: bool = False


class SqlExampleGovernance:
    """Deterministic verification-level rules for SQL examples."""

    @staticmethod
    def evaluate(
        *,
        execution_success: bool,
        human_reviewed: bool,
        corrected_by_human: bool,
    ) -> SqlExampleDecision:
        """Classify one example and record why the level was chosen.

        A human correction always downgrades to ``unverified``: the business
        owner rejected the previous answer, so neither successful execution nor
        an earlier review may keep it in the trusted example set.
        """
        if corrected_by_human:
            return SqlExampleDecision(
                level=VerificationLevel.unverified,
                trusted=False,
                reason="human_correction_downgrades_verification",
                downgraded=True,
            )
        if human_reviewed:
            return SqlExampleDecision(
                level=VerificationLevel.human_reviewed,
                trusted=True,
                reason="business_reviewer_confirmed",
            )
        if execution_success:
            return SqlExampleDecision(
                level=VerificationLevel.execution_success,
                trusted=False,
                reason="execution_succeeded_business_correctness_unverified",
            )
        return SqlExampleDecision(
            level=VerificationLevel.unverified,
            trusted=False,
            reason="no_successful_execution",
        )


def classify_sql_example(
    execution_success: bool,
    human_reviewed: bool,
    corrected_by_human: bool,
) -> VerificationLevel:
    """Return the verification level for one SQL example (see decision rules)."""
    return SqlExampleGovernance.evaluate(
        execution_success=execution_success,
        human_reviewed=human_reviewed,
        corrected_by_human=corrected_by_human,
    ).level


class KnowledgeSource(BaseModel):
    """Provenance and governance state of one knowledge source."""

    id: str
    kind: Literal["metric", "glossary", "document", "sql_example", "schema_doc"]
    name: str
    version: str | None = None
    owner: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    permissions: list[str] = Field(default_factory=list)
    review_status: _REVIEW_STATUS = "draft"
    content_hash: str
    chunk_id: str | None = None
    domain_id: str | None = None
    source_path: str | None = None

    def is_valid_at(self, now: datetime | str | None = None) -> bool:
        moment = _utc_now(now)
        start = _parse_timestamp(self.valid_from)
        end = _parse_timestamp(self.valid_until)
        if start is not None and moment < start:
            return False
        if end is not None and moment > end:
            return False
        return True

    def validity_reason(self, now: datetime | str | None = None) -> str | None:
        """Return ``None`` when valid, else the rejection reason."""
        moment = _utc_now(now)
        start = _parse_timestamp(self.valid_from)
        end = _parse_timestamp(self.valid_until)
        if start is not None and moment < start:
            return "not_yet_valid"
        if end is not None and moment > end:
            return "expired"
        return None


class MetricKnowledgeEntry(BaseModel):
    """Structured, authoritative metric definition."""

    metric_id: str
    name: str
    synonyms: list[str] = Field(default_factory=list)
    expression: str
    aggregation: str | None = None
    entity: str | None = None
    version: str | None = None
    owner: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    sensitivity: str = "internal"
    glossary_terms: list[str] = Field(default_factory=list)
    domain_id: str | None = None
    permissions: list[str] = Field(default_factory=list)
    review_status: _REVIEW_STATUS = "draft"
    source_id: str | None = None

    def terms(self) -> list[str]:
        return [self.name, *self.synonyms, *self.glossary_terms]

    def matches(self, normalized: str) -> bool:
        return any(normalize_term(term) == normalized for term in self.terms())

    def to_text(self) -> str:
        """One self-contained definition; never split from its expression."""
        lines = [
            f"Metric: {self.name}",
            f"Metric ID: {self.metric_id}",
            f"Expression: {self.expression}",
        ]
        if self.aggregation:
            lines.append(f"Aggregation: {self.aggregation}")
        if self.entity:
            lines.append(f"Entity: {self.entity}")
        if self.synonyms:
            lines.append(f"Synonyms: {', '.join(self.synonyms)}")
        if self.glossary_terms:
            lines.append(f"Glossary terms: {', '.join(self.glossary_terms)}")
        if self.owner:
            lines.append(f"Owner: {self.owner}")
        if self.version:
            lines.append(f"Version: {self.version}")
        if self.valid_from or self.valid_until:
            lines.append(
                f"Valid: {self.valid_from or 'open'} .. {self.valid_until or 'open'}"
            )
        if self.sensitivity:
            lines.append(f"Sensitivity: {self.sensitivity}")
        return "\n".join(lines)


class GlossaryEntry(BaseModel):
    """Structured glossary term bound to an owner and a version."""

    term: str
    definition: str
    synonyms: list[str] = Field(default_factory=list)
    owner: str | None = None
    version: str | None = None
    domain_id: str | None = None
    permissions: list[str] = Field(default_factory=list)
    review_status: _REVIEW_STATUS = "draft"
    valid_from: str | None = None
    valid_until: str | None = None
    source_id: str | None = None

    def terms(self) -> list[str]:
        return [self.term, *self.synonyms]

    def matches(self, normalized: str) -> bool:
        return any(normalize_term(term) == normalized for term in self.terms())

    def to_text(self) -> str:
        lines = [f"Glossary term: {self.term}", f"Definition: {self.definition}"]
        if self.synonyms:
            lines.append(f"Synonyms: {', '.join(self.synonyms)}")
        if self.owner:
            lines.append(f"Owner: {self.owner}")
        if self.version:
            lines.append(f"Version: {self.version}")
        return "\n".join(lines)


class GovernedDocument(BaseModel):
    """A retrieval document plus the governance metadata it must carry."""

    id: str
    text: str
    source_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""

    def with_content_hash(self) -> "GovernedDocument":
        return self.model_copy(update={"content_hash": content_hash(self.text)})


class KnowledgeResolution(BaseModel):
    """Result of resolving one business term against the governed knowledge base."""

    term: str
    normalized_term: str
    kind: Literal["metric", "glossary", "none"] = "none"
    metric: MetricKnowledgeEntry | None = None
    glossary: GlossaryEntry | None = None
    source: KnowledgeSource | None = None
    authoritative: bool = False
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def entry(self) -> MetricKnowledgeEntry | GlossaryEntry | None:
        return self.metric or self.glossary

    def __bool__(self) -> bool:  # pragma: no cover - trivial truthiness
        return self.authoritative


class HoldoutEntry(BaseModel):
    """Fingerprint of one evaluation/holdout case (no plaintext retained)."""

    fingerprint: str
    sql_fingerprint: str | None = None
    domain_id: str | None = None
    token_hashes: list[str] = Field(default_factory=list)
    registered_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class HoldoutRegistry(BaseModel):
    """Fingerprint registry that keeps evaluation material out of a KB.

    Detection uses three independent signals: an exact normalized question
    fingerprint, an exact SQL fingerprint, and normalized-text overlap (hashed
    token/bigram containment) above ``overlap_ratio``. Overlap catches a
    paraphrased holdout question that was "reworded" into a few-shot example.
    """

    overlap_ratio: float = DEFAULT_HOLDOUT_OVERLAP_RATIO
    #: Minimum candidate token count before overlap matching is trusted; two or
    #: three shared words ("merch GMV") are not evidence of contamination, while
    #: a reworded question still shares most of its shingles.
    min_overlap_tokens: int = 5
    evaluations: list[HoldoutEntry] = Field(default_factory=list)

    def register_holdout(
        self,
        question: str,
        sql: str | None = None,
        domain_id: str | None = None,
    ) -> HoldoutEntry:
        if not (question or "").strip():
            raise KnowledgeGovernanceError("A holdout question must be non-empty")
        entry = HoldoutEntry(
            fingerprint=_text_fingerprint(question),
            sql_fingerprint=_text_fingerprint(sql) if sql and sql.strip() else None,
            domain_id=domain_id,
            token_hashes=_token_hashes(question),
        )
        self.evaluations.append(entry)
        return entry

    @property
    def question_fingerprints(self) -> set[str]:
        return {entry.fingerprint for entry in self.evaluations}

    @property
    def sql_fingerprints(self) -> set[str]:
        return {
            entry.sql_fingerprint
            for entry in self.evaluations
            if entry.sql_fingerprint
        }

    def overlap(self, text: str, entry: HoldoutEntry) -> float:
        candidate = set(_token_hashes(text))
        if not candidate or not entry.token_hashes:
            return 0.0
        reference = set(entry.token_hashes)
        return len(candidate & reference) / min(len(candidate), len(reference))

    def tainted(self, document: Any) -> str | None:
        """Return a contamination reason for one document, else ``None``."""
        metadata = _document_metadata(document)
        if metadata.get("evaluation") is True or str(
            metadata.get("split") or ""
        ).lower() in {"holdout", "test", "eval", "evaluation"}:
            return "evaluation_split_material"
        text = _document_text(document)
        if not text.strip():
            return None
        if any(
            text_value
            and (
                _text_fingerprint(text_value) in self.question_fingerprints
                or (
                    _text_fingerprint(text_value) in self.sql_fingerprints
                )
            )
            for text_value in _candidate_texts(text, metadata)
        ):
            return "holdout_fingerprint_match"
        for entry in self.evaluations:
            if len(entry.token_hashes) < self.min_overlap_tokens:
                continue
            ratio = self.overlap(text, entry)
            if ratio >= self.overlap_ratio:
                return f"holdout_text_overlap={ratio:.2f}"
        return None

    def is_holdout(self, text: str) -> bool:
        return self.tainted({"text": text}) is not None

    def assert_not_tainted(self, documents: Iterable[Any]) -> None:
        """Raise when any document carries evaluation/holdout material."""
        contaminated: list[str] = []
        for document in documents:
            reason = self.tainted(document)
            if reason is not None:
                identifier = _document_id(document)
                contaminated.append(f"{identifier}: {reason}")
        if contaminated:
            raise HoldoutContaminationError(
                "Holdout/evaluation material refused by the knowledge store: "
                + "; ".join(contaminated)
            )


def _candidate_texts(text: str, metadata: Mapping[str, Any]) -> list[str]:
    candidates = [text]
    for key in ("question", "sql", "definition", "expression"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    match = re.search(r"^Question:\s*(.+)$", text, re.MULTILINE)
    if match:
        candidates.append(match.group(1))
    match = re.search(r"^SQL:\s*(.+)$", text, re.MULTILINE | re.DOTALL)
    if match:
        candidates.append(match.group(1))
    return candidates


def _document_text(document: Any) -> str:
    if isinstance(document, str):
        return document
    if isinstance(document, Mapping):
        return str(document.get("text") or "")
    return str(getattr(document, "text", "") or "")


def _document_metadata(document: Any) -> dict[str, Any]:
    if isinstance(document, Mapping):
        metadata = document.get("metadata")
    else:
        metadata = getattr(document, "metadata", None)
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _document_id(document: Any) -> str:
    if isinstance(document, Mapping):
        return str(document.get("id") or "document")
    return str(getattr(document, "id", "document"))


class StructuredKnowledgeBase(BaseModel):
    """Versioned metrics, glossary terms, documents, and their sources.

    ``resolve_term`` is deterministic: it normalizes aliases, rejects candidates
    that are expired, deprecated, or outside the caller's domain/permissions, and
    then picks the highest allowed version. Nothing in this class looks at vector
    similarity, so a document can never outvote a reviewed definition.
    """

    metrics: dict[str, MetricKnowledgeEntry] = Field(default_factory=dict)
    glossary: dict[str, GlossaryEntry] = Field(default_factory=dict)
    sources: dict[str, KnowledgeSource] = Field(default_factory=dict)
    documents: dict[str, str] = Field(default_factory=dict)
    holdout: HoldoutRegistry | None = None

    # ---------------------------------------------------------------- indexing
    def add_metric(
        self,
        entry: MetricKnowledgeEntry,
        *,
        source: KnowledgeSource | None = None,
    ) -> str:
        self._guard(entry.to_text())
        record = source or self._derived_source(
            entry.source_id or entry.metric_id, entry.name, "metric", entry
        )
        self.sources[record.id] = record
        stored = entry.model_copy(update={"source_id": record.id})
        # Metrics are keyed by id *and* version so v1 and v2 of the same metric
        # coexist; version resolution (not dictionary order) picks the winner.
        self.metrics[metric_key(stored)] = stored
        return stored.metric_id

    def add_glossary(
        self,
        entry: GlossaryEntry,
        *,
        source: KnowledgeSource | None = None,
    ) -> str:
        self._guard(entry.to_text())
        record = source or self._derived_source(
            f"{normalize_term(entry.term)}:{entry.domain_id or 'global'}",
            entry.term,
            "glossary",
            entry,
        )
        self.sources[record.id] = record
        stored = entry.model_copy(update={"source_id": record.id})
        self.glossary[glossary_key(stored)] = stored
        return stored.term

    def remove_glossary(
        self,
        term: str,
        *,
        domain_id: str | None = None,
        version: str | None = None,
    ) -> list[str]:
        """Remove glossary entries matching a term (optionally by domain/version).

        Returns the storage keys that were removed, so a caller can report what
        actually changed. ``domain_id=None`` means "every domain".
        """
        normalized = normalize_term(term)
        removed: list[str] = []
        for key, entry in list(self.glossary.items()):
            if normalize_term(entry.term) != normalized:
                continue
            if domain_id is not None and entry.domain_id != domain_id:
                continue
            if version is not None and entry.version != version:
                continue
            removed.append(key)
        for key in removed:
            entry = self.glossary.pop(key, None)
            if entry is not None and entry.source_id:
                self.sources.pop(entry.source_id, None)
        return removed

    def add_source(self, source: KnowledgeSource, *, text: str | None = None) -> str:
        """Register provenance, optionally with the document body it describes."""
        payload = text if text is not None else source.name
        self._guard(payload)
        self.sources[source.id] = source
        if text is not None:
            self.documents[source.id] = text
        return source.id

    # -------------------------------------------------------------- resolution
    def resolve_term(
        self,
        term: str,
        *,
        domain_id: str | None = None,
        version: str | None = None,
        permissions: Iterable[str] = (),
        now: datetime | str | None = None,
    ) -> KnowledgeResolution:
        """Resolve a business alias to its authoritative governed entry."""
        normalized = normalize_term(term)
        resolution = KnowledgeResolution(term=term, normalized_term=normalized)
        if not normalized:
            return resolution
        granted = {item.strip().lower() for item in permissions if str(item).strip()}
        candidates: list[tuple[int, str, str]] = []
        for key in sorted(self.metrics):
            entry = self.metrics[key]
            if not entry.matches(normalized):
                continue
            reason = self._reject(
                entry, key, "metric", granted, domain_id, version, now
            )
            if reason is not None:
                resolution.rejected.append(reason)
                continue
            candidates.append((0, key, entry.version or ""))
        for term_key in sorted(self.glossary):
            entry = self.glossary[term_key]
            if not entry.matches(normalized):
                continue
            reason = self._reject(
                entry, term_key, "glossary", granted, domain_id, version, now
            )
            if reason is not None:
                resolution.rejected.append(reason)
                continue
            candidates.append((1, term_key, entry.version or ""))
        if not candidates:
            return resolution
        kind_rank, key, _ = self._best(candidates)
        if kind_rank == 0:
            entry = self.metrics[key]
            resolution.kind = "metric"
            resolution.metric = entry
            resolution.conflicts = self.document_conflicts(entry)
        else:
            entry = self.glossary[key]
            resolution.kind = "glossary"
            resolution.glossary = entry
        if entry.source_id:
            resolution.source = self.sources.get(entry.source_id)
        resolution.authoritative = True
        return resolution

    def document_conflicts(self, metric: MetricKnowledgeEntry) -> list[dict[str, Any]]:
        """Find documents whose stated formula disagrees with the metric.

        Conflicts are *surfaced*, never resolved by similarity: the authoritative
        entry stays authoritative and the conflicting document is marked so a
        prompt builder can show the disagreement instead of silently rewriting
        the definition.
        """
        conflicts: list[dict[str, Any]] = []
        authoritative = _normalize_expression(metric.expression)
        aliases = [term.strip() for term in metric.terms() if term.strip()]
        for document_id in sorted(self.documents):
            text = self.documents[document_id]
            lowered = text.lower()
            for alias in aliases:
                for statement in _formula_statements(lowered, alias):
                    normalized = _normalize_expression(statement).rstrip(" .,")
                    if not normalized or normalized == authoritative:
                        continue
                    if not _AGGREGATE_PATTERN.search(statement):
                        continue
                    if normalized in authoritative or authoritative in normalized:
                        continue
                    conflicts.append(
                        {
                            "document_id": document_id,
                            "metric_id": metric.metric_id,
                            "metric_version": metric.version,
                            "authoritative_expression": metric.expression,
                            "document_expression": statement.strip(),
                            "reason": "document_expression_conflicts_with_reviewed_metric",
                        }
                    )
                    break
                else:
                    continue
                break
        return conflicts

    def conflicting_document_ids(self) -> set[str]:
        ids: set[str] = set()
        for key in sorted(self.metrics):
            for conflict in self.document_conflicts(self.metrics[key]):
                ids.add(str(conflict["document_id"]))
        return ids

    # -------------------------------------------------------------- documents
    def to_documents(
        self,
        *,
        domain_id: str | None = None,
        permissions: Iterable[str] = (),
        now: datetime | str | None = None,
        skip_tainted: bool = False,
    ) -> list[GovernedDocument]:
        """Project the knowledge base into governed retrieval documents."""
        granted = {item.strip().lower() for item in permissions if str(item).strip()}
        documents: list[GovernedDocument] = []
        for key in sorted(self.metrics):
            entry = self.metrics[key]
            if self._reject(entry, key, "metric", granted, domain_id, None, now):
                continue
            documents.append(self._metric_document(entry))
        for term in sorted(self.glossary):
            entry = self.glossary[term]
            if self._reject(entry, term, "glossary", granted, domain_id, None, now):
                continue
            documents.append(self._glossary_document(entry))
        conflicts_by_document: dict[str, list[str]] = {}
        for key in sorted(self.metrics):
            for conflict in self.document_conflicts(self.metrics[key]):
                conflicts_by_document.setdefault(str(conflict["document_id"]), []).append(
                    str(self.metrics[key].metric_id)
                )
        for document_id in sorted(self.documents):
            source = self.sources.get(document_id)
            # A plain document is governed exactly like a metric or a glossary
            # entry: its source record carries the permissions, review status and
            # validity window. Emitting it unchecked let a permission-denied,
            # deprecated or expired document reach the retrieval corpus (and the
            # prompt) for a caller with no permissions at all.
            if source is not None and self._reject(
                source, document_id, "document", granted, domain_id, None, now
            ):
                continue
            document_conflicts = sorted(conflicts_by_document.get(document_id, []))
            metadata = {
                "knowledge_id": document_id,
                "kind": source.kind if source else "document",
                "domain_id": source.domain_id if source else None,
                "version": source.version if source else None,
                "owner": source.owner if source else None,
                "review_status": source.review_status if source else "draft",
                "permissions": list(source.permissions) if source else [],
                "valid_from": source.valid_from if source else None,
                "valid_until": source.valid_until if source else None,
                "source_path": source.source_path if source else None,
                "content_role": "data",
                "authoritative": False,
                "conflict_with": document_conflicts,
            }
            if document_conflicts:
                metadata["conflict_detected"] = True
            documents.append(
                GovernedDocument(
                    id=f"knowledge:{document_id}",
                    text=self.documents[document_id],
                    source_type="knowledge_document",
                    metadata=metadata,
                ).with_content_hash()
            )
        if skip_tainted:
            return [document for document in documents if not self.tainted_reason(document)]
        self.assert_not_tainted(documents)
        return documents

    def tainted_reason(self, document: Any) -> str | None:
        if self.holdout is None:
            return None
        return self.holdout.tainted(document)

    def assert_not_tainted(self, documents: Iterable[Any]) -> None:
        if self.holdout is not None:
            self.holdout.assert_not_tainted(documents)

    # ------------------------------------------------------------ persistence
    def export(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def load(cls, payload: Mapping[str, Any] | str) -> "StructuredKnowledgeBase":
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls.model_validate(payload)

    def merge(self, other: "StructuredKnowledgeBase") -> "StructuredKnowledgeBase":
        return StructuredKnowledgeBase(
            metrics={**self.metrics, **other.metrics},
            glossary={**self.glossary, **other.glossary},
            sources={**self.sources, **other.sources},
            documents={**self.documents, **other.documents},
            holdout=self.holdout or other.holdout,
        )

    # --------------------------------------------------------------- internal
    def _guard(self, text: str) -> None:
        if self.holdout is not None:
            reason = self.holdout.tainted({"text": text})
            if reason is not None:
                raise HoldoutContaminationError(
                    f"Holdout/evaluation material refused by the knowledge store: {reason}"
                )

    def _derived_source(
        self,
        identifier: str,
        name: str,
        kind: str,
        entry: MetricKnowledgeEntry | GlossaryEntry,
    ) -> KnowledgeSource:
        # Source identity is version-aware: v1 and v2 of one metric must not
        # share a provenance record, or the older validity window would leak
        # into the newer definition (and every version would look expired).
        version_key = entry.version or "unversioned"
        return KnowledgeSource(
            id=f"source:{kind}:{identifier}:{version_key}",
            kind="metric" if kind == "metric" else "glossary",
            name=name,
            version=entry.version,
            owner=entry.owner,
            valid_from=entry.valid_from,
            valid_until=entry.valid_until,
            permissions=list(entry.permissions),
            review_status=entry.review_status,
            content_hash=content_hash(entry.to_text()),
            domain_id=entry.domain_id,
        )

    def _metric_document(self, entry: MetricKnowledgeEntry) -> GovernedDocument:
        conflicts = self.document_conflicts(entry)
        return GovernedDocument(
            id=f"metric:{entry.metric_id}:{entry.version or 'unversioned'}",
            text=entry.to_text(),
            source_type="metric_knowledge",
            metadata={
                "knowledge_id": entry.metric_id,
                "knowledge_kind": "metric",
                "metric_id": entry.metric_id,
                "name": entry.name,
                "synonyms": list(entry.synonyms),
                "expression": entry.expression,
                "aggregation": entry.aggregation,
                "entity": entry.entity,
                "version": entry.version,
                "owner": entry.owner,
                "valid_from": entry.valid_from,
                "valid_until": entry.valid_until,
                "sensitivity": entry.sensitivity,
                "glossary_terms": list(entry.glossary_terms),
                "domain_id": entry.domain_id,
                "permissions": list(entry.permissions),
                "review_status": entry.review_status,
                "source_id": entry.source_id,
                "authoritative": entry.review_status == "reviewed",
                "content_role": "data",
                "verification_level": (
                    VerificationLevel.human_reviewed.value
                    if entry.review_status == "reviewed"
                    else VerificationLevel.unverified.value
                ),
                "conflicts_with_documents": [
                    str(item["document_id"]) for item in conflicts
                ],
            },
        ).with_content_hash()

    def _glossary_document(self, entry: GlossaryEntry) -> GovernedDocument:
        return GovernedDocument(
            id=(
                f"glossary:{normalize_term(entry.term)}:"
                f"{entry.domain_id or 'global'}:{entry.version or 'unversioned'}"
            ),
            text=entry.to_text(),
            source_type="glossary",
            metadata={
                "knowledge_id": entry.term,
                "knowledge_kind": "glossary",
                "term": entry.term,
                "synonyms": list(entry.synonyms),
                "version": entry.version,
                "owner": entry.owner,
                "domain_id": entry.domain_id,
                "permissions": list(entry.permissions),
                "review_status": entry.review_status,
                "source_id": entry.source_id,
                "authoritative": entry.review_status == "reviewed",
                "content_role": "data",
                "verification_level": (
                    VerificationLevel.human_reviewed.value
                    if entry.review_status == "reviewed"
                    else VerificationLevel.unverified.value
                ),
            },
        ).with_content_hash()

    @staticmethod
    def _best(candidates: Sequence[tuple[int, str, str]]) -> tuple[int, str, str]:
        """Deterministic pick: reviewed metric first, then highest version, then id."""

        def sort_key(candidate: tuple[int, str, str]) -> tuple:
            kind_rank, key, version = candidate
            numeric = _version_sort_key(version)
            if numeric[0] == 0:
                # Negated numeric components: ascending sort yields the newest.
                version_key: tuple = (0, tuple(-part for part in numeric[1]))
            else:
                version_key = (1, ())
            return (kind_rank, version_key, key)

        return sorted(candidates, key=sort_key)[0]

    def _reject(
        self,
        entry: MetricKnowledgeEntry | GlossaryEntry | KnowledgeSource,
        key: str,
        kind: str,
        granted: set[str],
        domain_id: str | None,
        version: str | None,
        now: datetime | str | None,
    ) -> dict[str, Any] | None:
        """Return the rejection reason for one governed entry, else ``None``.

        ``entry`` may also be a :class:`KnowledgeSource`: a plain document has no
        inline governance fields, so its source record *is* its governance state
        (permissions, review status, validity, domain). Applying the same rule to
        documents is what keeps a permission-denied, deprecated or expired
        document out of the retrieval corpus instead of merely out of
        ``resolve_term``.
        """
        if isinstance(entry, KnowledgeSource):
            source: KnowledgeSource | None = entry
        else:
            source = self.sources.get(entry.source_id) if entry.source_id else None
        review_status = entry.review_status
        permissions = {
            item.strip().lower() for item in entry.permissions if str(item).strip()
        }
        if source is not None:
            permissions.update(
                item.strip().lower() for item in source.permissions if str(item).strip()
            )
        valid_from = entry.valid_from or (source.valid_from if source else None)
        valid_until = entry.valid_until or (source.valid_until if source else None)
        entry_domain = entry.domain_id or (source.domain_id if source else None)
        if version is not None and (entry.version or "") != version:
            return self._reason(kind, key, "version_not_requested", entry, version=version)
        if valid_until is not None:
            end = _parse_timestamp(valid_until)
            if end is not None and _utc_now(now) > end:
                return self._reason(kind, key, "expired", entry, valid_until=valid_until)
        if valid_from is not None:
            start = _parse_timestamp(valid_from)
            if start is not None and _utc_now(now) < start:
                return self._reason(kind, key, "not_yet_valid", entry, valid_from=valid_from)
        if review_status == "deprecated":
            return self._reason(kind, key, "deprecated", entry)
        if domain_id is not None and entry_domain != domain_id:
            return self._reason(kind, key, "out_of_domain", entry, domain_id=entry_domain)
        if permissions and not (permissions & granted):
            return self._reason(
                kind, key, "permission_denied", entry, permissions=sorted(permissions)
            )
        return None

    @staticmethod
    def _reason(
        kind: str,
        key: str,
        reason: str,
        entry: MetricKnowledgeEntry | GlossaryEntry | KnowledgeSource,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "identifier": key,
            "version": entry.version,
            "reason": reason,
            **extra,
        }


def _version_sort_key(version: str) -> tuple:
    """Sort versions so v10 > v9 > v2 > v1; fall back to the raw string."""
    parts = re.findall(r"\d+", version or "")
    if not parts:
        return (1, (), version or "")
    return (0, tuple(int(part) for part in parts), version or "")


def _formula_statements(lowered_text: str, alias: str) -> list[str]:
    """Extract ``<alias> = <formula>`` statements from a lower-cased document."""
    if not alias:
        return []
    pattern = re.compile(
        _FORMULA_PATTERN_TEMPLATE % _alias_pattern(alias),
        re.IGNORECASE,
    )
    return [match.group(1) for match in pattern.finditer(lowered_text)]
