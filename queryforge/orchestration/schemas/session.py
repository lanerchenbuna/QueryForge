"""Persistent, privacy-bounded conversation memory contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from queryforge.orchestration.schemas import utc_now


#: Keys that would persist result rows. Session memory must never carry them by
#: default: only the shape of a result (column names) is useful for follow-ups.
FORBIDDEN_RESULT_KEYS = frozenset(
    {"rows", "result_rows", "result_data", "preview_rows", "sample_rows"}
)
DEFAULT_MEMORY_RETENTION_DAYS = 30


class KnowledgeVersionRef(BaseModel):
    """A definition version a turn relied on (metric formula or semantic model)."""

    kind: Literal["metric", "model", "glossary", "knowledge"]
    id: str
    version: str
    #: Where the version came from, for auditability (e.g. "semantic_model").
    origin: str | None = None

    def reference(self) -> str:
        return f"{self.kind}:{self.id}@{self.version}"

    def matches(self, version_ref: str) -> bool:
        """True when ``version_ref`` names this metric/model version.

        Accepts the full ``kind:id@version`` form, the ``kind:id`` handle an
        operator copies out of a session status, ``id@version``, the bare id, and
        the bare version, so an operator can invalidate by whichever identifier
        they have at hand — including the ``glossary:<term>`` form for a governed
        glossary definition, which has no single-id spelling otherwise.
        """
        candidate = str(version_ref or "").strip()
        if not candidate:
            return False
        return candidate in {
            self.reference(),
            f"{self.kind}:{self.id}",
            f"{self.id}@{self.version}",
            self.id,
            self.version,
        }


class UserPreference(BaseModel):
    """One user-declared preference, always scoped to a user and session.

    Preferences are never global: ``user_id`` is mandatory, and an operator may
    also bind the preference to a domain so that one domain's preferences cannot
    leak into another domain's answers.
    """

    user_id: str
    name: str
    value: Any = None
    domain_id: str | None = None
    session_id: str | None = None
    updated_at: str = Field(default_factory=utc_now)

    def matches_scope(self, *, user_id: str, domain_id: str | None = None) -> bool:
        if self.user_id != user_id:
            return False
        if domain_id is not None and self.domain_id != domain_id:
            return False
        return True


class SessionTurn(BaseModel):
    turn_number: int = Field(ge=1)
    question: str
    rewritten_question: str | None = None
    sql: str | None = None
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    result_schema: list[str] = Field(default_factory=list)
    status: Literal["success", "planned", "blocked", "failed"]
    #: JSON dump of the typed analysis request this turn ran with, so a
    #: follow-up can be applied as a patch instead of appended text.
    analysis_request: dict[str, Any] | None = None
    #: Clarifications the analysis stage raised for this turn.
    needs_clarification: list[dict[str, Any]] = Field(default_factory=list)
    #: Definition versions this turn used; a superseded version invalidates the
    #: turn's context so a later follow-up does not reuse a stale formula.
    knowledge_versions: list[KnowledgeVersionRef] = Field(default_factory=list)
    invalidated: bool = False
    invalidated_reason: str | None = None
    created_at: str = Field(default_factory=utc_now)

    def uses_version(self, version_ref: str) -> bool:
        return any(ref.matches(version_ref) for ref in self.knowledge_versions)


class SessionMemory(BaseModel):
    session_id: str
    #: Declared scope of this session; a preference without a matching user is
    #: never applied, and deletion never crosses this boundary.
    user_id: str | None = None
    domain_id: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    #: Retention window used by :meth:`SessionStore.expire` when no explicit
    #: ``before`` timestamp is given.
    retention_days: int | None = DEFAULT_MEMORY_RETENTION_DAYS
    expires_at: str | None = None
    turn_count: int = 0
    last_question: str | None = None
    last_sql: str | None = None
    last_result_schema: list[str] = Field(default_factory=list)
    last_metrics: list[str] = Field(default_factory=list)
    last_dimensions: list[str] = Field(default_factory=list)
    last_filters: list[dict[str, Any]] = Field(default_factory=list)
    last_time_range: dict[str, Any] | None = None
    #: Unanswered clarifications; a later turn can resume the same intent.
    pending_clarifications: list[dict[str, Any]] = Field(default_factory=list)
    #: User-scoped preferences; never a global default (see ``UserPreference``).
    preferences: list[UserPreference] = Field(default_factory=list)
    history: list[SessionTurn] = Field(default_factory=list)


def strip_result_rows(payload: Any) -> tuple[Any, int]:
    """Remove result-row keys from a session payload, returning (clean, count).

    Result rows are never persisted by default: session memory keeps the shape of
    a result (``result_schema``) and the intent behind it, not the data itself.
    """
    if isinstance(payload, dict):
        stripped = 0
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if key in FORBIDDEN_RESULT_KEYS:
                stripped += 1
                continue
            new_value, nested = strip_result_rows(value)
            stripped += nested
            cleaned[key] = new_value
        return cleaned, stripped
    if isinstance(payload, list):
        stripped = 0
        items = []
        for item in payload:
            new_item, nested = strip_result_rows(item)
            stripped += nested
            items.append(new_item)
        return items, stripped
    return payload, 0
