"""Typed, validatable analysis intent with rule-based follow-up patches.

An :class:`AnalysisRequest` is the contract the analysis stage hands to SQL
generation.  It is deliberately rule-first: every field is either parsed
deterministically from the question or left unresolved so the caller asks for
clarification instead of inventing a business definition or a metric that the
governed semantic layer never declared.

The module knows nothing about orchestration or transports, so both the product
analyst agent and the session layer can depend on it.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

#: MVP constant.  A governed per-request timezone is not wired through
#: ``AgentOptions`` yet (that file is outside this step's edit scope), so every
#: request is labelled UTC and a timezone mentioned in the question is reported
#: as an assumption instead of being silently applied.
DEFAULT_TIMEZONE = "UTC"

#: Grain tokens the analyst turns into ``time_grain``.  Only explicit
#: grain-request phrasing qualifies: a bare "month" inside "last month" is a
#: window, not a grain.
GRAIN_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("daily", re.compile(r"\bdaily\b|\bper\s+day\b|\bby\s+day\b|按天|按日|逐日|每天", re.IGNORECASE)),
    ("weekly", re.compile(r"\bweekly\b|\bper\s+week\b|\bby\s+week\b|按周|每周|逐周", re.IGNORECASE)),
    (
        "monthly",
        re.compile(
            r"\bmonthly\b|\bper\s+month\b|\bby\s+month\b|\bmonth\s+over\s+month\b|按月|每月|逐月|月度",
            re.IGNORECASE,
        ),
    ),
    (
        "quarterly",
        re.compile(
            r"\bquarterly\b|\bper\s+quarter\b|\bby\s+quarter\b|按季|每季|季度",
            re.IGNORECASE,
        ),
    ),
    (
        "yearly",
        re.compile(
            r"\byearly\b|\bannually\b|\bper\s+year\b|\bby\s+year\b|\byear\s+over\s+year\b|按年|每年|年度",
            re.IGNORECASE,
        ),
    ),
)

#: Structured comparison baselines.  Order matters: the first match wins.
BASELINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "same_period_last_year",
        re.compile(r"\byoy\b|\byear[\s-]?over[\s-]?year\b|同比", re.IGNORECASE),
    ),
    (
        "previous_period",
        re.compile(r"\bmom\b|\bqoq\b|\bmonth[\s-]?over[\s-]?month\b|环比", re.IGNORECASE),
    ),
    (
        "previous_month",
        re.compile(
            r"\b(?:vs\.?|versus|compared?\s+(?:to|with))\s+(?:the\s+)?last\s+month\b"
            r"|与上月相比|上月对比",
            re.IGNORECASE,
        ),
    ),
    (
        "previous_quarter",
        re.compile(
            r"\b(?:vs\.?|versus|compared?\s+(?:to|with))\s+(?:the\s+)?last\s+quarter\b"
            r"|与上季度相比",
            re.IGNORECASE,
        ),
    ),
    (
        "previous_year",
        re.compile(
            r"\b(?:vs\.?|versus|compared?\s+(?:to|with))\s+(?:the\s+)?last\s+year\b"
            r"|与去年相比|去年同期",
            re.IGNORECASE,
        ),
    ),
    (
        "previous_period",
        re.compile(
            r"\b(?:vs\.?|versus|compared?\s+(?:to|with))\s+(?:the\s+)?"
            r"(?:previous|prior|last)\s+(?:period|week|day|run)\b",
            re.IGNORECASE,
        ),
    ),
)

#: A generic "compare to <explicit target>" tail, kept verbatim when no
#: canonical baseline above matched.
GENERIC_BASELINE = re.compile(
    r"\b(?:vs\.?|versus|compared?\s+(?:to|with))\s+(.{1,40}?)\s*[?.。]?$",
    re.IGNORECASE,
)

#: High-impact ambiguity classes: business definitions the analyst must never
#: assume on the user's behalf.  "sales" is deliberately absent because in the
#: governed deployments it commonly names a table/entity rather than a metric.
UNGOVERNED_METRIC_SIGNALS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "ambiguous_metric_definition",
        re.compile(r"\brevenue\b|\bprofit\b|\bmargin\b|收入|营收|销售额", re.IGNORECASE),
        "Multiple or no governed metric definition matches this term; confirm the "
        "intended business definition before running business SQL.",
    ),
    (
        "ambiguous_active_user_definition",
        re.compile(
            r"\bactive\s+(?:users?|customers?)\b|\bdau\b|\bmau\b|活跃用户|日活|月活",
            re.IGNORECASE,
        ),
        "The active-user definition is not governed; confirm whether this means "
        "distinct users, active accounts, or event rows.",
    ),
)

#: Growth phrasing that needs a baseline before any answer can be trustworthy.
GROWTH_SIGNAL = re.compile(
    r"\b(?:revenue|sales|profit|margin|user|customer)\s+growth\b"
    r"|\bgrowth\s+(?:rate|percentage)\b|\bgrowth\s*%"
    r"|收入增长|营收增长|用户增长|增长率|同比增长|环比增长",
    re.IGNORECASE,
)

#: Baseline phrasing that is explicit enough to stop a growth question from
#: being treated as high-impact ambiguous.
BASELINE_MENTION = re.compile(
    r"\bthan\b|\bvs\.?\b|\bversus\b|\bcompared?\s+(?:to|with)\b|\byoy\b|\bmom\b|\bqoq\b"
    r"|\blast\s+(?:month|quarter|year|week)\b|\bprevious\s+(?:month|quarter|year|week|period)\b"
    r"|同比|环比|对比|相比",
    re.IGNORECASE,
)

_TOP_FOLLOWUP = re.compile(r"^(?:top\s+(\d+)|前\s*(\d+)\s*(?:名|个)?)[?.。]?$", re.IGNORECASE)
_LIMIT_INLINE = re.compile(r"\b(?:top|limit)\s+(\d+)\b", re.IGNORECASE)
_REPLACE_DIMENSION = re.compile(
    r"^(?:by|per|按|按照)\s+(.+?)\s*(?:instead\s+of\s+the\s+current\s+one|instead|换成|替换为|改为)[?.。]?$",
    re.IGNORECASE,
)
_BREAKDOWN_FOLLOWUP = re.compile(
    r"^(?:by|per|break(?:\s+it)?\s+down\s+by|grouped\s+by|按|按照)\s+(.+?)"
    r"(?:\s*(?:again|再|一下|呢))?[?.。]?$",
    re.IGNORECASE,
)
_TIME_FOLLOWUP = re.compile(
    r"^(?:break(?:\s+it)?\s+down\s+over\s+time|by\s+time|over\s+time|按时间展开|按时间拆分|按时间)$",
    re.IGNORECASE,
)
_FILTER_FOLLOWUP = re.compile(
    r"^(?:only(?:\s+(?:show|include|look\s+at))?|filter(?:\s+to)?|只看|仅看|只保留)\s+(.+?)[?.。]?$",
    re.IGNORECASE,
)
_ADD_FOLLOWUP = re.compile(
    r"^(?:also\s+(?:include|add)|add|再加上|加上)\s+(.+?)[?.。]?$",
    re.IGNORECASE,
)
_REMOVE_FOLLOWUP = re.compile(
    r"^(?:remove|drop|去掉|移除|删除)\s+(.+?)[?.。]?$",
    re.IGNORECASE,
)
_REFERENCE_FOLLOWUP = re.compile(
    r"\b(?:that result|previous result|same result|the same one|that one|刚才那个|那个结果|上一个结果|还是那个)\b",
    re.IGNORECASE,
)

_EXPLICIT_RANGE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*(?:to|until|through|till|-|~|—|–|至|到)\s*(\d{4}-\d{2}-\d{2})"
)

#: Rule-based window expressions understood by the patch helper.  The analyst's
#: date node resolves the same wording into calendar dates.
_TIME_RANGE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\blast\s+month\b|上个月|上月", re.IGNORECASE), "last month"),
    (re.compile(r"\bthis\s+month\b|本月|这个月", re.IGNORECASE), "this month"),
    (re.compile(r"\blast\s+quarter\b|上季度", re.IGNORECASE), "last quarter"),
    (re.compile(r"\bthis\s+quarter\b|本季度", re.IGNORECASE), "this quarter"),
    (re.compile(r"\blast\s+year\b|去年", re.IGNORECASE), "last year"),
    (re.compile(r"\bthis\s+year\b|今年", re.IGNORECASE), "this year"),
    (re.compile(r"\blast\s+week\b|上周", re.IGNORECASE), "last week"),
    (re.compile(r"\blast\s+(\d+)\s+months?\b|最近\s*(\d+)\s*个月", re.IGNORECASE), "last {n} months"),
    (re.compile(r"\blast\s+(\d+)\s+days?\b|最近\s*(\d+)\s*天", re.IGNORECASE), "last {n} days"),
    (re.compile(r"\btoday\b|今天", re.IGNORECASE), "today"),
    (re.compile(r"\byesterday\b|昨天", re.IGNORECASE), "yesterday"),
)

_TIME_WORD = re.compile(r"^(?:day|week|month|quarter|year|日|天|周|月|季|年)$", re.IGNORECASE)

_VALID_STATUSES = {"valid", "warning", "blocked"}


class AnalysisRequest(BaseModel):
    """A typed, validatable analysis intent.

    ``model_validate_artifact`` coerces today's free-form artifact payload into
    this model, so nothing downstream must depend on the legacy key set.
    """

    intent: str = "ask_sql"
    metric_ids: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, str]] = Field(default_factory=list)
    time_range: str | None = None
    timezone: str = DEFAULT_TIMEZONE
    time_grain: str | None = None
    comparison_baseline: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    output: str | None = None
    top_n: int | None = None
    clarifications: list[dict[str, str]] = Field(default_factory=list)
    status: Literal["valid", "warning", "blocked"] = "valid"

    @classmethod
    def model_validate_artifact(cls, payload: dict[str, Any] | None) -> "AnalysisRequest":
        """Coerce a legacy analysis artifact payload into the typed contract.

        The current artifact uses ``metrics`` / ``filters`` / ``time_range`` /
        ``clarification_reasons`` / ``ambiguities`` and a dict-shaped date
        context; all of those are accepted and never raise, so this can be used
        on any historical payload.
        """

        data = payload if isinstance(payload, dict) else {}
        try:
            metrics = _string_list(
                data.get("metric_ids") or data.get("metrics") or data.get("metric_names")
            )
            dimensions = _string_list(data.get("dimensions") or data.get("dimension_ids"))
            unresolved = _string_list(data.get("unresolved_questions"))
            if not unresolved:
                unresolved = _string_list(data.get("ambiguities"))
            if not unresolved:
                unresolved = _string_list(data.get("clarification_reasons"))
            return cls(
                intent=_text(data.get("intent")) or "ask_sql",
                metric_ids=metrics,
                dimensions=dimensions,
                filters=_filter_entries(data.get("filters")),
                time_range=time_range_text(data.get("time_range")),
                timezone=_text(data.get("timezone")) or DEFAULT_TIMEZONE,
                time_grain=_text(data.get("time_grain")) or _grain_from_grain_text(
                    data.get("grain")
                ),
                comparison_baseline=_text(data.get("comparison_baseline")),
                assumptions=_string_list(data.get("assumptions")),
                unresolved_questions=unresolved,
                output=_text(data.get("output")),
                top_n=_positive_int(data.get("top_n", data.get("limit"))),
                clarifications=_clarification_entries(data.get("clarifications")),
                status=_coerced_status(data.get("status")),
            )
        except Exception:  # defensive: coercion must never break a run
            return cls(
                assumptions=["Analysis artifact could not be coerced into AnalysisRequest."],
                unresolved_questions=["analysis_artifact_coercion_failed"],
                status="warning",
            )

    def clarification_aspects(self) -> list[str]:
        return [item.get("aspect", "") for item in self.clarifications if item.get("aspect")]

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked" or any(
            item.get("severity") == "high" for item in self.clarifications
        )


def detect_time_grain(question: str) -> str | None:
    """Return the requested time grain, or ``None`` when the question has none."""
    for grain, pattern in GRAIN_TOKENS:
        if pattern.search(question):
            return grain
    return None


def detect_comparison_baseline(question: str) -> str | None:
    """Return a structured comparison baseline for the question."""
    for label, pattern in BASELINE_RULES:
        if pattern.search(question):
            return label
    generic = GENERIC_BASELINE.search(question)
    if generic:
        target = generic.group(1).strip()
        if target and not _REFERENCE_FOLLOWUP.search(target):
            return f"vs {target}"
    return None


def detect_time_range(question: str) -> str | None:
    """Return a normalized window expression for the question.

    A span already consumed as a comparison baseline is masked out, because
    "vs last month" names the baseline rather than the analysis window.
    """

    text = question
    for label, pattern in BASELINE_RULES:
        match = pattern.search(text)
        if match:
            text = f"{text[: match.start()]} {text[match.end() :]}"
    explicit = _EXPLICIT_RANGE.search(text)
    if explicit:
        return f"{explicit.group(1)}..{explicit.group(2)}"
    for pattern, template in _TIME_RANGE_RULES:
        match = pattern.search(text)
        if not match:
            continue
        count = next(
            (group for group in match.groups() if group and group.isdigit()),
            None,
        )
        return template.format(n=int(count)) if count else template
    return None


def apply_patch(
    question: str,
    previous: AnalysisRequest,
) -> tuple[AnalysisRequest, str | None]:
    """Apply a rule-based follow-up patch to a previous analysis request.

    The follow-up is treated as a *patch* instead of ever-growing question text:
    dimensions, filters, metrics, time range, grain, top-n, and comparison
    baseline are updated in place, and unrelated fields are preserved.  The
    returned reason uses the same vocabulary as
    ``ProductAnalystAgent.rewrite_followup`` so both views of a follow-up can be
    correlated.  ``None`` means no rule matched and nothing changed.
    """

    text = question.strip()
    updated = previous.model_copy(deep=True)
    if not text:
        return updated, None

    reasons: list[str] = []

    # Comparison baseline and window are detected first; the window rule masks
    # the baseline span so "vs last month" does not also become the window.
    baseline = detect_comparison_baseline(text)
    if baseline:
        updated.comparison_baseline = baseline
        reasons.append("set_comparison_baseline")
    window = detect_time_range(text)
    if window:
        updated.time_range = window
        reasons.append("set_time_range")

    top = _TOP_FOLLOWUP.match(text) or _LIMIT_INLINE.search(text)
    if top:
        limit = _positive_int(top.group(1) if top.re is _TOP_FOLLOWUP else top.group(1))
        if limit is not None:
            updated.top_n = limit
            reasons.append("set_ranking")

    filtered = _FILTER_FOLLOWUP.match(text)
    if filtered:
        _append_filter(updated, filtered.group(1).strip())
        reasons.append("add_filter")

    added = _ADD_FOLLOWUP.match(text)
    if added:
        # Never invent a governed metric id: the term is recorded for
        # resolution against the semantic layer instead.
        term = added.group(1).strip()
        _append_unique(
            updated.unresolved_questions,
            f"Resolve requested metric or field: {term}",
        )
        reasons.append("add_metric")

    removed = _REMOVE_FOLLOWUP.match(text)
    if removed:
        if _remove_target(updated, removed.group(1).strip()):
            reasons.append("remove_dimension_or_metric")

    if _TIME_FOLLOWUP.match(text):
        updated.time_grain = updated.time_grain or "monthly"
        reasons.append("add_time_dimension")
    else:
        replaced = _REPLACE_DIMENSION.match(text)
        if replaced:
            name = replaced.group(1).strip()
            grain = _grain_of_token(name)
            if grain:
                updated.time_grain = grain
                reasons.append("add_time_dimension")
            else:
                updated.dimensions = [name]
                reasons.append("replace_dimension")
        else:
            breakdown = _BREAKDOWN_FOLLOWUP.match(text)
            if breakdown:
                name = breakdown.group(1).strip()
                grain = _grain_of_token(name)
                if grain:
                    updated.time_grain = grain
                    reasons.append("add_time_dimension")
                else:
                    _append_unique(updated.dimensions, name)
                    reasons.append("add_dimension")

    if not reasons:
        # Grain-only phrasing such as "monthly" or "按月".
        grain = detect_time_grain(text)
        if grain:
            updated.time_grain = grain
            reasons.append("add_time_dimension")

    if _REFERENCE_FOLLOWUP.search(text) and _is_empty(previous):
        # "the same one" without any prior intent is a question, not a guess.
        _append_unique(
            updated.unresolved_questions,
            "The follow-up references a prior request that is not available in "
            "this session; confirm the intended analysis.",
        )
        reasons.append("resolve_reference")

    if not reasons:
        return updated, None
    return updated, "+".join(reasons)


def is_high_impact_ambiguity(question: str, request: AnalysisRequest) -> list[str]:
    """Return ambiguity aspects that must be clarified before business SQL runs.

    Revenue/active-user wording without a matched governed metric, and growth
    wording without an explicit baseline, are never silently assumed.
    """

    aspects: list[str] = []
    if not request.metric_ids:
        for aspect, pattern, _message in UNGOVERNED_METRIC_SIGNALS:
            if pattern.search(question):
                _append_unique(aspects, aspect)
    if (
        GROWTH_SIGNAL.search(question)
        and not request.comparison_baseline
        and not BASELINE_MENTION.search(question)
    ):
        _append_unique(aspects, "missing_comparison_baseline")
    return aspects


def high_impact_question(aspect: str) -> str:
    """Return the clarification question text for one high-impact aspect."""
    for candidate, _pattern, message in UNGOVERNED_METRIC_SIGNALS:
        if candidate == aspect:
            return message
    if aspect == "missing_comparison_baseline":
        return (
            "Growth was requested without an explicit baseline; confirm the "
            "comparison baseline (previous period, same period last year, or a "
            "named target)."
        )
    return "Confirm the intended business definition before running this analysis."


def _grain_of_token(name: str) -> str | None:
    token = name.strip().lower()
    if not _TIME_WORD.match(token):
        return None
    for grain, pattern in GRAIN_TOKENS:
        if pattern.search(f"by {token}"):
            return grain
    return None


def _is_empty(request: AnalysisRequest) -> bool:
    """True when a request carries no intent at all."""

    return not (
        request.metric_ids
        or request.dimensions
        or request.filters
        or request.time_range
        or request.time_grain
        or request.comparison_baseline
        or request.top_n
    )


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _append_filter(request: AnalysisRequest, expression: str) -> None:
    if not expression:
        return
    entry = {"expression": expression}
    if entry not in request.filters:
        request.filters.append(entry)


def _remove_target(request: AnalysisRequest, name: str) -> bool:
    lowered = name.lower()
    for collection in (request.dimensions, request.metric_ids):
        for value in list(collection):
            if lowered in value.lower() or value.lower() in lowered:
                collection.remove(value)
                return True
    for entry in list(request.filters):
        if lowered in str(entry.get("expression", "")).lower():
            request.filters.remove(entry)
            return True
    return False


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string_list(value: Any) -> list[str]:
    items: list[str] = []
    if isinstance(value, dict):
        value = list(value.values())
    for item in _as_list(value):
        if isinstance(item, dict):
            text = _text(item.get("metric") or item.get("name") or item.get("reference"))
        else:
            text = _text(item)
        if text:
            _append_unique(items, text)
    return items


def _filter_entries(value: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in _as_list(value):
        if isinstance(item, str):
            entry = {"expression": item.strip()}
        elif isinstance(item, dict):
            entry = {
                str(key): str(raw)
                for key, raw in item.items()
                if raw is not None and isinstance(raw, (str, int, float, bool))
            }
            if "expression" not in entry and entry:
                entry["expression"] = "; ".join(f"{k}={v}" for k, v in entry.items())
        else:
            entry = {"expression": str(item)}
        if entry.get("expression"):
            entries.append(entry)
    return entries


def _clarification_entries(value: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        entry = {
            str(key): str(raw)
            for key, raw in item.items()
            if raw is not None and isinstance(raw, (str, int, float, bool))
        }
        if entry:
            entries.append(entry)
    return entries


def time_range_text(value: Any) -> str | None:
    """Render a resolved date context (or range list) as window text.

    Accepts the artifact-shaped dict produced by ``DateContext.model_dump`` and
    returns ``"start..end; start..end"``; ``None`` when nothing was resolved.
    """
    text = _text(value)
    if text:
        return text
    if not isinstance(value, dict):
        return None
    from_ranges: list[str] = []
    for item in _as_list(value.get("ranges")):
        if not isinstance(item, dict):
            continue
        start = _text(item.get("start_date"))
        end = _text(item.get("end_date"))
        if start and end:
            from_ranges.append(f"{start}..{end}")
    if from_ranges:
        return "; ".join(from_ranges)
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed >= 1 else None
    return None


def _grain_from_grain_text(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return detect_time_grain(text)


def _coerced_status(value: Any) -> Literal["valid", "warning", "blocked"]:
    text = _text(value)
    if text in _VALID_STATUSES:
        return text  # type: ignore[return-value]
    if text in {"degraded", "llm_fallback_failed"}:
        return "warning"
    if text == "failed":
        return "blocked"
    return "valid"
