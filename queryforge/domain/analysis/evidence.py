"""Evidence-backed answers: traceability from every number to its source.

Step 12 of the optimization plan.  Two ideas drive this module:

1. ``Evidence`` records *where a number came from* (source, data version, SQL or
   method, grain, unit, range, completeness, validation outcome, parent evidence).
2. ``AnswerComposer`` builds a ``FinalAnswer`` whose key numbers are **copied out
   of the referenced evidence payloads by code**.  A model may contribute prose
   (``Finding.statement``) and the analysis plan, but never re-types a key number.

``validate_answer`` re-checks the composed answer against the store and
``apply_validation`` records every problem on the answer (``review_required`` plus
a limitation line) instead of dropping it, so an unverifiable claim can never be
published as a verified one.

The module is deliberately dependency-free (stdlib + pydantic) so both the
workflow layer and the domain layer can use it.
"""

from __future__ import annotations

from numbers import Number
from typing import Any, Iterable, Iterator, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


EVIDENCE_ID_PREFIX = "ev_"

# Evidence kinds emitted by the pipeline and by analysis tools.
KIND_SQL_RESULT = "sql_result"
KIND_METRIC_RESOLUTION = "metric_resolution"
KIND_DATA_QUALITY = "data_quality"
KIND_SCHEMA_RETRIEVAL = "schema_retrieval"
KIND_SEMANTIC_VALIDATION = "semantic_validation"
KIND_PERIOD_COMPARISON = "period_comparison"
KIND_DRILL_DOWN = "drill_down"
KIND_CONTRIBUTION = "contribution"
KIND_ANOMALY = "anomaly"
KIND_CHART = "chart"
KIND_ASSUMPTION = "assumption"
KIND_LIMITATION = "limitation"

# Completeness vocabulary used by ``Evidence.completeness``.
COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_TRUNCATED = "truncated"
COMPLETENESS_UNKNOWN = "unknown"

TRUNCATED_COMPLETENESS = frozenset(
    {"truncated", "partial", "incomplete", "degraded", "limited"}
)

# Containers searched for a number key inside an evidence payload.  Keeping the
# list explicit keeps lookup deterministic instead of "search everywhere".
_NUMBER_CONTAINERS = (
    "numbers",
    "values",
    "metrics",
    "aggregates",
    "totals",
    "summary",
    "result",
    "measurements",
)

# ---------------------------------------------------------------------------
# Number helpers
# ---------------------------------------------------------------------------


def is_number(value: Any) -> bool:
    """Return ``True`` for real numbers; ``bool`` is not a number here."""
    return isinstance(value, Number) and not isinstance(value, bool)


def numbers_equal(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    """Compare two numbers with the declared absolute tolerance."""
    if not is_number(left) or not is_number(right):
        return False
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError, OverflowError):  # pragma: no cover - defensive
        return False


def format_number(value: Any) -> str:
    """Deterministic number formatting for template-generated conclusions."""
    if value is None:
        return "unknown"
    if isinstance(value, bool):  # pragma: no cover - rejected by validators
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


class _Missing:
    """Sentinel for "no value found"."""


_MISSING = _Missing()


def _collect_matches(payload: Any, key: str, depth: int = 3) -> list[Any]:
    """Collect values stored under ``key`` (bounded, deterministic search)."""
    matches: list[Any] = []
    stack: list[tuple[Any, int]] = [(payload, depth)]
    while stack and len(matches) < 2:
        node, remaining = stack.pop()
        if not isinstance(node, dict):
            continue
        for name, value in node.items():
            if name == key:
                matches.append(value)
                if len(matches) >= 2:
                    break
            elif remaining > 0 and isinstance(value, dict):
                stack.append((value, remaining - 1))
    return matches


def lookup_number(payload: Any, key: str) -> tuple[bool, Any]:
    """Look ``key`` up inside one evidence payload.

    Accepted shapes (in order): dotted path (``revenue.sum``), two-segment paths
    mixing a column and a container (``sum.revenue`` / ``revenue.sum``), a flat
    key inside a known container, and finally a unique recursive key match.
    An ambiguous match is reported as *not found* rather than guessed.
    """
    if not isinstance(payload, dict) or not key:
        return False, None
    parts = [part for part in key.split(".") if part]
    if parts:
        node: Any = payload
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = _MISSING
                break
        if node is not _MISSING:
            return True, node
    if len(parts) == 2:
        head, tail = parts
        for container in _NUMBER_CONTAINERS:
            group = payload.get(container)
            if not isinstance(group, dict):
                continue
            head_value = group.get(head)
            if isinstance(head_value, dict) and tail in head_value:
                return True, head_value[tail]
            tail_value = group.get(tail)
            if isinstance(tail_value, dict) and head in tail_value:
                return True, tail_value[head]
    for container in _NUMBER_CONTAINERS:
        group = payload.get(container)
        if isinstance(group, dict) and key in group:
            return True, group[key]
    matches = _collect_matches(payload, key)
    if len(matches) == 1:
        return True, matches[0]
    return False, None


def resolve_number(payloads: Iterable[Any], key: str) -> tuple[bool, Any]:
    """Return ``(found, value)`` scanning payloads in order."""
    for payload in payloads:
        found, value = lookup_number(payload, key)
        if found:
            return True, value
    return False, None


# ---------------------------------------------------------------------------
# Evidence contract
# ---------------------------------------------------------------------------


def new_evidence_id() -> str:
    return f"{EVIDENCE_ID_PREFIX}{uuid4().hex[:16]}"


class Evidence(BaseModel):
    """One auditable source behind a number or a claim.

    ``payload`` carries the structured values a finding may cite; numbers are
    resolved from it by :func:`lookup_number`.  Unknown extra keys are preserved
    so evidence produced by other steps round-trips unchanged.
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=new_evidence_id)
    kind: str
    source: str
    version: str | None = None
    method: str | None = None
    sql: str | None = None
    grain: str | None = None
    unit: str | None = None
    range: dict[str, Any] | None = None
    completeness: str | None = None
    validation: dict[str, Any] | None = None
    refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "kind", "source")
    @classmethod
    def _require_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("evidence id, kind and source must be non-empty")
        return text

    @field_validator("refs")
    @classmethod
    def _clean_refs(cls, value: list[str]) -> list[str]:
        refs: list[str] = []
        for ref in value:
            text = str(ref).strip()
            if not text:
                raise ValueError("evidence refs must be non-empty ids")
            if text not in refs:
                refs.append(text)
        return refs

    @field_validator("completeness")
    @classmethod
    def _clean_completeness(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None


class EvidenceStore:
    """Ordered, id-unique evidence store that refuses dangling references."""

    def __init__(self, items: Iterable[Evidence | Mapping[str, Any]] | None = None) -> None:
        self._items: dict[str, Evidence] = {}
        for item in items or ():
            self.add(item)

    # -- writes ------------------------------------------------------------
    def add(self, evidence: Evidence | Mapping[str, Any]) -> str:
        """Store ``evidence`` and return its id.

        Raises ``ValueError`` for a duplicate id, an unknown parent id in
        ``refs`` (no dangling references) or a parent recorded for a different
        data version (stale evidence may not silently back a newer claim).
        """
        item = evidence if isinstance(evidence, Evidence) else Evidence.model_validate(dict(evidence))
        if item.id in self._items:
            raise ValueError(f"duplicate evidence id: {item.id}")
        for ref in item.refs:
            parent = self._items.get(ref)
            if parent is None:
                raise ValueError(
                    f"unknown evidence ref '{ref}' referenced by '{item.id}'"
                )
            if item.version and parent.version and item.version != parent.version:
                raise ValueError(
                    f"stale evidence ref '{ref}' (version {parent.version}) cannot back "
                    f"'{item.id}' (version {item.version})"
                )
        self._items[item.id] = item
        return item.id

    # -- reads -------------------------------------------------------------
    def get(self, evidence_id: str) -> Evidence:
        try:
            return self._items[evidence_id]
        except KeyError as exc:
            raise KeyError(f"unknown evidence id: {evidence_id}") from exc

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self._items

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def by_kind(self, kind: str) -> list[Evidence]:
        wanted = str(kind).strip().lower()
        return [item for item in self._items.values() if item.kind.lower() == wanted]

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._items.values():
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return counts

    def ids(self) -> list[str]:
        return list(self._items)

    def to_list(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self._items.values()]

    def summary(self) -> dict[str, Any]:
        """Compact description for the final JSON payload."""
        return {"count": len(self._items), "kinds": self.kinds(), "ids": self.ids()}

    @classmethod
    def from_list(cls, items: Iterable[Evidence | Mapping[str, Any]] | None) -> "EvidenceStore":
        """Rebuild a store; parents must appear before the evidence citing them."""
        return cls(items)

    # -- dunder ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, evidence_id: object) -> bool:
        return isinstance(evidence_id, str) and evidence_id in self._items

    def __iter__(self) -> Iterator[Evidence]:
        return iter(self._items.values())


def load_evidence_store(
    items: Any,
) -> tuple[EvidenceStore, str | None]:
    """Load evidence from any payload shape used across the pipeline.

    Accepts an ``EvidenceStore``, a list of ``Evidence``/mappings, a payload
    wrapper (``{"evidence": [...]}`` / ``{"items": [...]}``) or ``None``.
    References may appear in any order; genuinely dangling references are
    reported as ``(store, error)`` instead of raising, so a caller such as the
    report generator can degrade with a visible reason rather than fail.
    """
    if items is None:
        return EvidenceStore(), None
    if isinstance(items, EvidenceStore):
        return items, None
    candidate: Any = items
    if isinstance(candidate, Mapping) and not candidate.get("kind"):
        candidate = candidate.get("evidence", candidate.get("items"))
        if candidate is None:
            return EvidenceStore(), None
    if isinstance(candidate, (Evidence, Mapping)):
        candidate = [candidate]
    if not isinstance(candidate, (list, tuple)):
        return EvidenceStore(), f"unsupported evidence payload type: {type(items).__name__}"

    store = EvidenceStore()
    pending = list(candidate)
    last_error: str | None = None
    while pending:
        remaining: list[Any] = []
        progressed = False
        for item in pending:
            try:
                store.add(item)
                progressed = True
            except ValueError as exc:
                last_error = str(exc)
                remaining.append(item)
            except Exception as exc:  # invalid shape: keep it visible, do not raise
                last_error = f"{type(exc).__name__}: {exc}"
                remaining.append(item)
        if not progressed:
            return store, last_error or "evidence payload could not be loaded"
        pending = remaining
    return store, None


# ---------------------------------------------------------------------------
# Findings and final answer
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """One structured result statement with the evidence that supports it."""

    model_config = ConfigDict(extra="allow")

    kind: str
    statement: str
    numbers: dict[str, float | int | None] = Field(default_factory=dict)
    dimensions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    degraded: bool = False
    review_required: bool = False

    @field_validator("numbers")
    @classmethod
    def _reject_bool(
        cls, value: dict[str, Any]
    ) -> dict[str, float | int | None]:
        for key, item in value.items():
            if isinstance(item, bool):
                raise ValueError(
                    f"finding number '{key}' must be numeric; booleans are not numbers"
                )
        return value


class FinalAnswer(BaseModel):
    """Evidence-backed answer separating conclusions, assumptions and gaps."""

    model_config = ConfigDict(extra="allow")

    question: str
    status: Literal[
        "success", "partial", "blocked", "failed", "needs_clarification"
    ]
    conclusions: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    charts: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    review_required: bool = False
    degraded: bool = False


def _append_unique(values: list[str], candidate: str) -> None:
    if candidate and candidate not in values:
        values.append(candidate)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


# ---------------------------------------------------------------------------
# Causal-language guard (correlation must not be stated as causation)
# ---------------------------------------------------------------------------

CAUSAL_MARKERS: tuple[str, ...] = (
    "导致",
    "造成",
    "归因于",
    "因为",
    "由于",
    "引起",
    "致使",
    "caused by",
    "cause",
    "led to",
    "leads to",
    "leading to",
    "due to",
    "because",
    "results in",
    "resulted in",
    "drives",
    "drove",
    "attributable to",
    "responsible for",
)

_NEGATION_MARKERS: tuple[str, ...] = (
    "不是",
    "并非",
    "无关",
    "不代表",
    "不能说明",
    "无法证明",
    "未",
    "not ",
    "no ",
    "never ",
    "without ",
    "cannot ",
    "doesn't ",
    "does not ",
)

_NEGATION_WINDOW = 16

# Evidence kinds that carry a designed causal source.
CAUSAL_SOURCE_KINDS = frozenset(
    {
        "experiment",
        "ab_test",
        "randomized_experiment",
        "causal_analysis",
        "causal_inference",
        "holdout_experiment",
        "instrumented_experiment",
    }
)

CAUSAL_METHOD_MARKERS: tuple[str, ...] = (
    "randomi",
    "experiment",
    "difference-in-differences",
    "difference in differences",
    "diff-in-diff",
    "causal",
    "counterfactual",
    "instrumental variable",
    "propensity score",
    "synthetic control",
    "regression discontinuity",
)

# Kinds that describe association/shape only; they can never support causation.
CORRELATIONAL_KINDS = frozenset(
    {
        KIND_CONTRIBUTION,
        KIND_ANOMALY,
        KIND_DRILL_DOWN,
        KIND_PERIOD_COMPARISON,
        KIND_SQL_RESULT,
        KIND_CHART,
        "trend",
    }
)


def causal_language_guard(text: str | None) -> str | None:
    """Return the causal phrase found in ``text`` (or ``None``).

    Deliberately a phrase check, not a semantic proof: it is the reusable first
    filter behind :func:`validate_answer`.  Simple negations ("not caused by")
    are ignored so honest disclaimers are not flagged.
    """
    if not text:
        return None
    lowered = str(text).lower()
    for marker in CAUSAL_MARKERS:
        start = lowered.find(marker)
        while start != -1:
            window = lowered[max(0, start - _NEGATION_WINDOW):start]
            if not any(negation in window for negation in _NEGATION_MARKERS):
                return marker
            start = lowered.find(marker, start + len(marker))
    return None


def declares_causal_source(evidence: Evidence) -> bool:
    """Return ``True`` when the evidence declares a designed causal source."""
    if evidence.kind.strip().lower() in CAUSAL_SOURCE_KINDS:
        return True
    payload = evidence.payload if isinstance(evidence.payload, dict) else {}
    if payload.get("causal") is True or payload.get("causal_design"):
        return True
    validation = evidence.validation if isinstance(evidence.validation, dict) else {}
    if validation.get("causal") is True:
        return True
    parts = [
        evidence.method,
        evidence.kind,
        payload.get("method"),
        payload.get("design"),
        payload.get("test"),
    ]
    text = " ".join(str(part) for part in parts if part).lower()
    return any(marker in text for marker in CAUSAL_METHOD_MARKERS)


# ---------------------------------------------------------------------------
# Answer composer
# ---------------------------------------------------------------------------

# Gap kind -> actionable next question that is tied to the missing evidence.
GAP_QUESTIONS: dict[str, str] = {
    KIND_SQL_RESULT: (
        "Which query should be run to collect the missing result evidence for this question?"
    ),
    KIND_METRIC_RESOLUTION: (
        "Which metric definition (version, unit, grain) must be confirmed before this number is reported?"
    ),
    KIND_DATA_QUALITY: (
        "Which data-quality check still has to pass on the complete input before this answer is treated as final?"
    ),
    KIND_SCHEMA_RETRIEVAL: (
        "Which tables or columns still need to be retrieved to cover the question completely?"
    ),
    KIND_SEMANTIC_VALIDATION: (
        "Which business rule (join, key, grain, filter) has not been validated yet for this result?"
    ),
    KIND_PERIOD_COMPARISON: (
        "Which baseline period should be queried to support the comparison that is still missing?"
    ),
    KIND_DRILL_DOWN: (
        "Which dimension should be drilled into next to locate the drivers of the change?"
    ),
    KIND_CONTRIBUTION: (
        "Which contribution decomposition is still missing for the target metric?"
    ),
    KIND_ANOMALY: (
        "Which anomaly test (baseline window and method) still needs to be run on this series?"
    ),
    KIND_CHART: (
        "Which chart evidence (metric kind and grain) is still missing for the requested display?"
    ),
}


def gap_question(kind: str) -> str:
    """Deterministic, gap-specific next question (never generic filler)."""
    key = str(kind or "").strip()
    if key in GAP_QUESTIONS:
        return GAP_QUESTIONS[key]
    lowered = key.lower()
    if lowered in GAP_QUESTIONS:
        return GAP_QUESTIONS[lowered]
    return (
        f"No {key or 'required'} evidence was collected for this answer; "
        f"which step would produce it before the result is treated as final?"
    )


class AnswerComposer:
    """Compose a :class:`FinalAnswer` whose numbers come from the store.

    The composer never invents a number: each key in ``Finding.numbers`` is
    resolved against the payloads of the evidence the finding cites.  A key that
    cannot be resolved becomes ``None`` (reported as unknown), a declared value
    that disagrees with the evidence is replaced *and* recorded as a limitation,
    and an evidence id that does not exist is dropped with the finding marked
    ``review_required``.
    """

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    # -- public API --------------------------------------------------------
    def compose(
        self,
        question: str,
        findings: Sequence[Finding | Mapping[str, Any]],
        *,
        status: Literal[
            "success", "partial", "blocked", "failed", "needs_clarification"
        ] = "success",
        charts: Sequence[Mapping[str, Any]] | None = None,
        assumptions: Sequence[str] | None = None,
        limitations: Sequence[str] | None = None,
        gaps: Sequence[str | Mapping[str, Any]] | None = None,
        degraded: bool = False,
    ) -> FinalAnswer:
        notes: list[str] = []
        resolved_findings: list[Finding] = []
        evidence_ids: list[str] = []

        for index, raw in enumerate(findings):
            finding = raw if isinstance(raw, Finding) else Finding.model_validate(dict(raw))
            known: list[str] = []
            rejected: list[str] = []
            for evidence_id in finding.evidence_ids:
                if self.store.has(evidence_id):
                    if evidence_id not in known:
                        known.append(evidence_id)
                elif evidence_id not in rejected:
                    rejected.append(evidence_id)
            if rejected:
                notes.append(
                    f"finding[{index}] cited unknown evidence id(s) "
                    f"{', '.join(sorted(rejected))}; the references were dropped and the "
                    "finding is marked review_required"
                )
            payloads = [self.store.get(item).payload for item in known]
            numbers: dict[str, float | int | None] = {}
            for key, declared in finding.numbers.items():
                found, actual = resolve_number(payloads, key)
                if not found or not is_number(actual):
                    numbers[key] = None
                    notes.append(
                        f"finding[{index}] number '{key}' has no numeric value in its "
                        "referenced evidence; reported as unknown"
                    )
                    continue
                if declared is not None and not numbers_equal(declared, actual):
                    notes.append(
                        f"finding[{index}] declared '{key}'={format_number(declared)} but the "
                        f"evidence says {format_number(actual)}; the evidence value is used"
                    )
                numbers[key] = actual
            update: dict[str, Any] = {"numbers": numbers, "evidence_ids": known}
            if rejected:
                update["review_required"] = True
            if any(value is None for value in numbers.values()):
                update["degraded"] = True
            resolved = finding.model_copy(update=update)
            resolved_findings.append(resolved)
            for evidence_id in known:
                _append_unique(evidence_ids, evidence_id)

        normalized_charts, chart_notes = self._normalize_charts(charts, evidence_ids)
        notes.extend(chart_notes)

        conclusions = [
            conclusion
            for conclusion in (self._conclusion(finding) for finding in resolved_findings)
            if conclusion
        ]
        answer_limitations = [_clean_text(item) for item in limitations or () if _clean_text(item)]
        for note in notes:
            _append_unique(answer_limitations, note)

        review_required = any(finding.review_required for finding in resolved_findings) or any(
            value is None
            for finding in resolved_findings
            for value in finding.numbers.values()
        )
        answer_degraded = bool(degraded) or any(
            finding.degraded for finding in resolved_findings
        )
        return FinalAnswer(
            question=question,
            status=status,
            conclusions=conclusions,
            findings=resolved_findings,
            evidence_ids=evidence_ids,
            charts=normalized_charts,
            assumptions=[_clean_text(item) for item in assumptions or () if _clean_text(item)],
            limitations=answer_limitations,
            open_questions=self._open_questions(gaps),
            review_required=review_required,
            degraded=answer_degraded,
        )

    # -- internals ---------------------------------------------------------
    def _normalize_charts(
        self,
        charts: Sequence[Mapping[str, Any]] | None,
        evidence_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        normalized: list[dict[str, Any]] = []
        notes: list[str] = []
        for index, chart in enumerate(charts or ()):
            record = dict(chart)
            cited = [str(item) for item in record.get("evidence_ids") or ()]
            kept = [item for item in cited if self.store.has(item)]
            rejected = sorted({item for item in cited if not self.store.has(item)})
            if rejected:
                notes.append(
                    f"chart[{index}] cited unknown evidence id(s) {', '.join(rejected)}; "
                    "the references were dropped"
                )
            # Chart semantics come from the metric kind and grain, so pull them
            # from the cited evidence when the caller did not state them.
            semantics = _chart_semantics([self.store.get(item) for item in kept])
            record["evidence_ids"] = kept
            for key, value in semantics.items():
                if value is not None and not record.get(key):
                    record[key] = value
            for evidence_id in kept:
                _append_unique(evidence_ids, evidence_id)
            normalized.append(record)
        return normalized, notes

    @staticmethod
    def _conclusion(finding: Finding) -> str:
        """Deterministic template; only the prose comes from the caller."""
        statement = _clean_text(finding.statement).rstrip(".。").strip()
        values = {
            key: value for key, value in finding.numbers.items() if is_number(value)
        }
        prefix = f"{statement}. " if statement else ""
        if values:
            pairs = ", ".join(f"{key}={format_number(value)}" for key, value in values.items())
            return f"{prefix}Evidence-backed numbers: {pairs}."
        if finding.numbers:
            return f"{prefix}Evidence-backed numbers: none traceable in the cited evidence."
        return statement if statement else ""

    @staticmethod
    def _open_questions(gaps: Sequence[str | Mapping[str, Any]] | None) -> list[str]:
        questions: list[str] = []
        for gap in gaps or ():
            if isinstance(gap, Mapping):
                kind = _clean_text(gap.get("kind") or gap.get("evidence_kind") or "")
                explicit = _clean_text(gap.get("question"))
                candidate = explicit or (gap_question(kind) if kind else "")
            else:
                candidate = gap_question(_clean_text(gap))
            _append_unique(questions, candidate)
        return questions


def _chart_semantics(items: Sequence[Evidence]) -> dict[str, Any]:
    """Derive chart semantics (metric kind, grain, unit) from cited evidence."""
    semantics: dict[str, Any] = {"metric_kind": None, "grain": None, "unit": None}
    for item in items:
        payload = item.payload if isinstance(item.payload, dict) else {}
        if semantics["grain"] is None:
            semantics["grain"] = item.grain or payload.get("grain")
        if semantics["unit"] is None:
            semantics["unit"] = item.unit or payload.get("unit")
        if semantics["metric_kind"] is None:
            semantics["metric_kind"] = (
                payload.get("metric_kind")
                or payload.get("measure")
                or (item.kind if item.kind == KIND_METRIC_RESOLUTION else None)
            )
        if semantics["grain"] and semantics["metric_kind"]:
            break
    return semantics


# ---------------------------------------------------------------------------
# Answer validation
# ---------------------------------------------------------------------------


def _answer_evidence_ids(answer: FinalAnswer) -> list[str]:
    ids: list[str] = []
    for evidence_id in answer.evidence_ids:
        _append_unique(ids, evidence_id)
    for finding in answer.findings:
        for evidence_id in finding.evidence_ids:
            _append_unique(ids, evidence_id)
    for chart in answer.charts:
        for evidence_id in chart.get("evidence_ids") or ():
            _append_unique(ids, str(evidence_id))
    return ids


def validate_answer(answer: FinalAnswer, store: EvidenceStore) -> list[str]:
    """Return the problems that block publishing ``answer`` as verified.

    Checks: unknown evidence ids, numbers that cannot be traced to (or disagree
    with) the cited evidence payloads, non-degraded findings without any
    evidence, and causal wording in a conclusion/statement whose supporting
    evidence declares no causal source.
    """
    problems: list[str] = []
    referenced = _answer_evidence_ids(answer)

    for evidence_id in referenced:
        if not store.has(evidence_id):
            problems.append(f"unknown evidence id: {evidence_id}")

    for index, finding in enumerate(answer.findings):
        label = f"findings[{index}]"
        if not finding.evidence_ids and not finding.degraded:
            problems.append(
                f"{label} is not marked degraded but cites no evidence"
            )
        payloads = [
            store.get(evidence_id).payload
            for evidence_id in finding.evidence_ids
            if store.has(evidence_id)
        ]
        for key, value in finding.numbers.items():
            if value is None:
                continue
            found, actual = resolve_number(payloads, key)
            if not found:
                problems.append(
                    f"{label} number '{key}'={format_number(value)} is not present in the "
                    "cited evidence payloads"
                )
            elif not is_number(actual):
                problems.append(
                    f"{label} number '{key}' is not numeric in the cited evidence "
                    f"(found {type(actual).__name__})"
                )
            elif not numbers_equal(value, actual):
                problems.append(
                    f"{label} number '{key}'={format_number(value)} disagrees with the cited "
                    f"evidence value {format_number(actual)}"
                )

    kinds = sorted({store.get(item).kind for item in referenced if store.has(item)})
    has_causal_source = any(
        declares_causal_source(store.get(item)) for item in referenced if store.has(item)
    )
    for index, conclusion in enumerate(answer.conclusions):
        marker = causal_language_guard(conclusion)
        if marker and not has_causal_source:
            problems.append(
                f"conclusions[{index}] asserts causation ('{marker}') but "
                f"{_support_description(kinds)}"
            )
    for index, finding in enumerate(answer.findings):
        marker = causal_language_guard(finding.statement)
        if marker and not has_causal_source:
            problems.append(
                f"findings[{index}].statement asserts causation ('{marker}') but "
                f"{_support_description(kinds)}"
            )
    return problems


def _support_description(kinds: Sequence[str]) -> str:
    if not kinds:
        return "no evidence is cited in the answer"
    if all(kind in CORRELATIONAL_KINDS for kind in kinds):
        return (
            f"the cited evidence only provides correlational kinds ({', '.join(kinds)}) "
            "without a declared causal source"
        )
    return (
        f"the cited evidence ({', '.join(kinds)}) declares no causal design "
        "(experiment, A/B test, difference-in-differences, ...)"
    )


def apply_validation(answer: FinalAnswer, problems: Sequence[str]) -> FinalAnswer:
    """Record validation problems on the answer instead of dropping them.

    Failing validation sets ``review_required`` and appends every problem to
    ``limitations`` so the answer is published as *needs review*, never as
    verified.
    """
    cleaned = [_clean_text(item) for item in problems or () if _clean_text(item)]
    if not cleaned:
        return answer
    updated = answer.model_copy(deep=True)
    updated.review_required = True
    for problem in cleaned:
        _append_unique(updated.limitations, problem)
    return updated


# ---------------------------------------------------------------------------
# Result evidence and honest findings for degenerate inputs
# ---------------------------------------------------------------------------

_NUMERIC_COMPLETENESS = frozenset({COMPLETENESS_COMPLETE})


def aggregate_payload(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    row_count: int | None = None,
    metric: str | None = None,
    dimension: str | None = None,
    series_key: str | None = None,
) -> dict[str, Any]:
    """Compute the structured payload stored on a ``sql_result`` evidence.

    Aggregates are computed from **every** returned row, so a report that
    truncates the displayed table still reports totals over the complete result
    set.  Flat convenience keys (``total_<col>``, ``min_<col>``, ...) are what
    findings cite.
    """
    column_list = [str(column) for column in columns]
    returned = len(rows)
    total = returned if row_count is None else int(row_count)
    payload: dict[str, Any] = {
        "row_count": total,
        "returned_rows": returned,
        "columns": column_list,
        "numeric_columns": [],
        "all_null_columns": [],
        "aggregates": {},
        "nulls": {},
    }
    numeric_columns: list[str] = []
    for index, column in enumerate(column_list):
        values = [
            row[index]
            for row in rows
            if index < len(row) and is_number(row[index])
        ]
        nulls = sum(
            1 for row in rows if index >= len(row) or row[index] is None
        )
        non_null = returned - nulls
        # Counts are recorded for every column (even an all-NULL or textual one)
        # so a degenerate metric is reported honestly instead of being ignored.
        payload["nulls"][column] = nulls
        payload[f"nulls_{column}"] = nulls
        payload[f"non_null_{column}"] = non_null
        if non_null == 0:
            payload["all_null_columns"].append(column)
        if not values:
            continue
        numeric_columns.append(column)
        stats = {
            "sum": _normalize(sum(values)),
            "min": _normalize(min(values)),
            "max": _normalize(max(values)),
            "count": len(values),
            "nulls": nulls,
        }
        payload["aggregates"][column] = stats
        payload[f"total_{column}"] = stats["sum"]
        payload[f"min_{column}"] = stats["min"]
        payload[f"max_{column}"] = stats["max"]
    payload["numeric_columns"] = numeric_columns

    chosen = metric if metric in numeric_columns else None
    if chosen is None and len(numeric_columns) == 1:
        chosen = numeric_columns[0]

    if dimension and chosen and dimension in column_list:
        dimension_index = column_list.index(dimension)
        metric_index = column_list.index(chosen)
        groups: dict[str, Any] = {}
        for row in rows:
            if metric_index >= len(row) or not is_number(row[metric_index]):
                continue
            label = str(row[dimension_index]) if dimension_index < len(row) else ""
            groups[label] = _normalize(groups.get(label, 0) + row[metric_index])
        payload["group_dimension"] = dimension
        payload["groups"] = groups
        if groups:
            top_label = max(groups, key=lambda label: (groups[label], label))
            payload["top_group"] = top_label
            payload[f"top_total_{chosen}"] = groups[top_label]

    if series_key and chosen and series_key in column_list:
        series_index = column_list.index(series_key)
        metric_index = column_list.index(chosen)
        points = [
            (str(row[series_index]) if series_index < len(row) else "", row[metric_index])
            for row in rows
            if metric_index < len(row) and is_number(row[metric_index])
        ]
        if points:
            first_label, first_value = points[0]
            last_label, last_value = points[-1]
            payload[f"series_{chosen}"] = {
                "points": len(points),
                "first_label": first_label,
                "last_label": last_label,
                "first": _normalize(first_value),
                "last": _normalize(last_value),
                "delta": _normalize(last_value - first_value),
            }
            payload[f"points_{chosen}"] = len(points)
            payload[f"delta_{chosen}"] = _normalize(last_value - first_value)
    return payload


def _normalize(value: Any) -> Any:
    """Keep JSON-friendly numbers (and avoid float noise for integral sums)."""
    if isinstance(value, bool):  # pragma: no cover - defensive
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 10)
    return value


def build_execution_evidence(
    *,
    sql: str,
    source: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    row_count: int | None = None,
    version: str | None = None,
    grain: str | None = None,
    unit: str | None = None,
    range_: Mapping[str, Any] | None = None,
    completeness: str | None = COMPLETENESS_COMPLETE,
    method: str | None = None,
    validation: Mapping[str, Any] | None = None,
    refs: Sequence[str] | None = None,
    kind: str = KIND_SQL_RESULT,
    evidence_id: str | None = None,
    metric: str | None = None,
    dimension: str | None = None,
    series_key: str | None = None,
) -> Evidence:
    """Build the ``sql_result`` evidence for an executed query."""
    returned = len(rows)
    resolved_row_count = returned if row_count is None else int(row_count)
    declared_completeness = completeness
    if declared_completeness is None:
        declared_completeness = (
            COMPLETENESS_COMPLETE
            if resolved_row_count == returned
            else COMPLETENESS_TRUNCATED
        )
    fields: dict[str, Any] = {
        "kind": kind,
        "source": source,
        "version": version,
        "method": method or "executed SQL over the run's data version",
        "sql": sql,
        "grain": grain,
        "unit": unit,
        "range": dict(range_) if range_ else None,
        "completeness": declared_completeness,
        "validation": dict(validation) if validation else None,
        "refs": [str(item) for item in refs or ()],
        "payload": aggregate_payload(
            columns,
            rows,
            row_count=resolved_row_count,
            metric=metric,
            dimension=dimension,
            series_key=series_key,
        ),
    }
    if evidence_id:
        fields["id"] = evidence_id
    return Evidence(**fields)


def result_findings(
    store: EvidenceStore,
    evidence_id: str,
    *,
    metric: str | None = None,
    dimension: str | None = None,
) -> tuple[list[Finding], list[str]]:
    """Honest findings for one result evidence.

    Returns ``(findings, limitations)``.  Degenerate inputs (empty result,
    all-NULL metric, a single point, all-negative values) never produce a
    fabricated trend, ranking or "top item": each case yields an explicit
    limitation and, where it matters, a finding marked ``degraded``.
    """
    evidence = store.get(evidence_id)
    payload = evidence.payload if isinstance(evidence.payload, dict) else {}
    row_count = int(payload.get("row_count") or 0)
    numbers = {"row_count": row_count, "returned_rows": int(payload.get("returned_rows") or 0)}
    findings: list[Finding] = []
    limitations: list[str] = []

    if row_count == 0:
        findings.append(
            Finding(
                kind=evidence.kind,
                statement=(
                    "The query returned no rows, so no total, ranking or trend can be "
                    "reported for this question."
                ),
                numbers=numbers,
                evidence_ids=[evidence_id],
                degraded=True,
            )
        )
        limitations.append(
            "The result set is empty: the answer reports the empty result instead of "
            "any aggregate or trend."
        )
        return findings, limitations

    numeric_columns = [str(item) for item in payload.get("numeric_columns") or ()]
    chosen = metric if metric in numeric_columns else None
    if chosen is None and len(numeric_columns) == 1:
        chosen = numeric_columns[0]
    if (
        chosen is None
        and metric
        and metric in (payload.get("columns") or ())
        and int(payload.get(f"non_null_{metric}") or 0) == 0
    ):
        # An explicitly requested metric that is entirely NULL is reported as
        # such instead of silently falling back to "no metric".
        chosen = metric
    if chosen is None:
        findings.append(
            Finding(
                kind=evidence.kind,
                statement=(
                    "The result has no unambiguous numeric metric column, so only its "
                    "shape is reported."
                ),
                numbers=numbers,
                evidence_ids=[evidence_id],
            )
        )
        limitations.append(
            "No single numeric metric column was identified; no aggregate or trend is claimed."
        )
        return findings, limitations

    non_null = int(payload.get(f"non_null_{chosen}") or 0)
    nulls = int(payload.get(f"nulls_{chosen}") or 0)
    if non_null == 0:
        findings.append(
            Finding(
                kind=evidence.kind,
                statement=(
                    f"Every value of '{chosen}' in the returned rows is NULL, so no "
                    "aggregate, ranking or trend is reported for it."
                ),
                numbers={**numbers, f"non_null_{chosen}": 0, f"nulls_{chosen}": nulls},
                dimensions=[dimension] if dimension else [],
                evidence_ids=[evidence_id],
                degraded=True,
            )
        )
        limitations.append(
            f"Metric '{chosen}' is entirely NULL in this result; aggregates and trends are withheld."
        )
        return findings, limitations

    summary = {
        **numbers,
        f"total_{chosen}": payload.get(f"total_{chosen}"),
        f"min_{chosen}": payload.get(f"min_{chosen}"),
        f"max_{chosen}": payload.get(f"max_{chosen}"),
        f"non_null_{chosen}": non_null,
        f"nulls_{chosen}": nulls,
    }
    findings.append(
        Finding(
            kind=evidence.kind,
            statement=(
                f"Across {row_count} returned row(s), '{chosen}' totals "
                f"{format_number(payload.get(f'total_{chosen}'))} over {non_null} "
                f"non-NULL value(s)."
            ),
            numbers=dict(summary),
            dimensions=[dimension] if dimension else [],
            evidence_ids=[evidence_id],
        )
    )
    if nulls:
        limitations.append(
            f"{nulls} row(s) have no value for '{chosen}'; they are excluded from the aggregate "
            "as reported by the evidence payload."
        )

    total = payload.get(f"total_{chosen}")
    if is_number(total) and total < 0:
        findings.append(
            Finding(
                kind=evidence.kind,
                statement=(
                    f"Every value of '{chosen}' is negative (the total is "
                    f"{format_number(total)}); the sign is reported as observed and no "
                    "positive growth is claimed."
                ),
                numbers={f"total_{chosen}": total, f"max_{chosen}": payload.get(f"max_{chosen}")},
                evidence_ids=[evidence_id],
            )
        )

    top_label = payload.get("top_group")
    if dimension and top_label is not None:
        findings.append(
            Finding(
                kind=KIND_DRILL_DOWN,
                statement=(
                    f"Top {dimension} by '{chosen}': {top_label} "
                    f"({format_number(payload.get(f'top_total_{chosen}'))})."
                ),
                numbers={
                    f"top_total_{chosen}": payload.get(f"top_total_{chosen}"),
                    f"total_{chosen}": payload.get(f"total_{chosen}"),
                },
                dimensions=[dimension],
                evidence_ids=[evidence_id],
            )
        )

    series = payload.get(f"series_{chosen}")
    if isinstance(series, dict):
        points = int(series.get("points") or 0)
        if points >= 3:
            delta = series.get("delta")
            if is_number(delta) and delta == 0:
                movement = "no net change"
            elif is_number(total) and total < 0:
                # In negative territory "increase" would read like growth; state
                # the signed change instead of inventing a direction label.
                movement = f"a net change of {format_number(delta)} (all values negative)"
            elif is_number(delta) and delta > 0:
                movement = f"a net increase of {format_number(delta)}"
            else:
                movement = f"a net decline of {format_number(abs(delta) if is_number(delta) else None)}"
            findings.append(
                Finding(
                    kind=KIND_PERIOD_COMPARISON,
                    statement=(
                        f"'{chosen}' moved from {format_number(series.get('first'))} "
                        f"({series.get('first_label')}) to {format_number(series.get('last'))} "
                        f"({series.get('last_label')}): {movement} across {points} point(s)."
                    ),
                    numbers={
                        f"delta_{chosen}": delta,
                        f"points_{chosen}": points,
                    },
                    dimensions=[dimension] if dimension else [],
                    evidence_ids=[evidence_id],
                )
            )
        else:
            limitations.append(
                f"Only {points} usable point(s) for '{chosen}'; no trend is reported "
                "(at least three points are required)."
            )
    return findings, limitations


# ---------------------------------------------------------------------------
# Display truncation vs analysis completeness
# ---------------------------------------------------------------------------


# Kinds that carry a data slice; only these can say anything about whether the
# *analysis* input was complete.  A metric definition or a retrieval record has
# no completeness of its own and must not drag the analysis to "unknown".
DATA_EVIDENCE_KINDS = frozenset(
    {
        KIND_SQL_RESULT,
        KIND_PERIOD_COMPARISON,
        KIND_DRILL_DOWN,
        KIND_CONTRIBUTION,
        KIND_ANOMALY,
        "aggregate",
        "metric_value",
        "query_metric",
        "result_set",
        "trend",
    }
)


def is_data_evidence(evidence: Evidence) -> bool:
    """Return ``True`` when the evidence carries an actual data slice."""
    if evidence.kind.strip().lower() in DATA_EVIDENCE_KINDS:
        return True
    payload = evidence.payload if isinstance(evidence.payload, dict) else {}
    return "row_count" in payload or "returned_rows" in payload


def summarize_completeness(
    *,
    total_row_count: int,
    displayed_row_count: int | None = None,
    evidence: Iterable[Evidence] | None = None,
    answer: FinalAnswer | None = None,
    extra_notes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Separate **display truncation** from **analysis completeness**.

    ``display_truncated`` describes the rendered table only.  ``analysis_complete``
    describes the input the numbers were computed over, and is taken from the
    cited evidence (``complete`` / ``truncated`` / ``unknown``) — a truncated
    display never makes the analysis incomplete, and a degraded analysis is never
    hidden behind a complete-looking table.
    """
    total = max(int(total_row_count or 0), 0)
    displayed = total if displayed_row_count is None else max(int(displayed_row_count), 0)
    displayed = min(displayed, total)
    display_truncated = displayed < total

    items = list(evidence or ())
    if answer is not None and answer.evidence_ids:
        wanted = set(answer.evidence_ids)
        scoped = [item for item in items if item.id in wanted]
        items = scoped or items
    items = [item for item in items if is_data_evidence(item)]

    scopes = [(item.completeness or COMPLETENESS_UNKNOWN).lower() for item in items]
    notes: list[str] = []
    if display_truncated:
        notes.append(
            f"The report displays {displayed} of {total} row(s) (display truncated)."
        )
    else:
        notes.append(f"The report displays all {total} row(s) (display truncated: no).")

    if not items:
        analysis_complete = True
        analysis_scope = "unreported"
        notes.append(
            "No evidence declared the completeness of the analysis input; display "
            "truncation is independent of analysis completeness."
        )
    elif any(scope in TRUNCATED_COMPLETENESS for scope in scopes):
        analysis_complete = False
        analysis_scope = COMPLETENESS_TRUNCATED
        notes.append(
            "The analysis input evidence reports completeness='truncated': the reported "
            "numbers cover the returned subset only."
        )
    elif all(scope in _NUMERIC_COMPLETENESS for scope in scopes):
        analysis_complete = True
        analysis_scope = "complete_result_set"
        notes.append(
            "The cited evidence states completeness='complete': the numbers are computed "
            "over the complete result set."
        )
    else:
        analysis_complete = False
        analysis_scope = COMPLETENESS_UNKNOWN
        notes.append(
            "The cited evidence does not state a usable completeness value, so the analysis "
            "cannot be called complete."
        )

    if answer is not None and answer.degraded:
        notes.append("The answer is marked degraded; parts of the analysis are incomplete.")

    for note in extra_notes or ():
        _append_unique(notes, _clean_text(note))

    return {
        "display_truncated": display_truncated,
        "displayed_row_count": displayed,
        "total_row_count": total,
        "analysis_complete": analysis_complete,
        "analysis_scope": analysis_scope,
        "notes": notes,
    }


def traceability_rows(evidence: Iterable[Evidence]) -> list[list[Any]]:
    """Rows for the report's evidence traceability table (id -> source)."""
    rows: list[list[Any]] = []
    for item in evidence:
        rows.append(
            [
                item.id,
                item.kind,
                item.source,
                item.method or item.sql or "",
                item.grain or "",
                item.unit or "",
                item.completeness or COMPLETENESS_UNKNOWN,
                item.version or "",
                ", ".join(item.refs),
            ]
        )
    return rows


TRACEABILITY_COLUMNS = [
    "Evidence ID",
    "Kind",
    "Source",
    "Method / SQL",
    "Grain",
    "Unit",
    "Completeness",
    "Version",
    "Refs",
]
