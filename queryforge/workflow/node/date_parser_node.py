"""Resolve common English and Chinese date expressions without heavy dependencies."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable

from queryforge.workflow.node.base import Node
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.core.schemas.models import Context, DateContext, DateRange, NodeResult


class DateParserNode(Node):
    name = "date_parser"
    description = "Resolve natural-language dates into inclusive calendar ranges"

    #: "from X to Y" (or the Chinese/tilde equivalents) joins two explicit dates
    #: into one inclusive window instead of two single-day points.
    _RANGE_CONNECTOR = re.compile(r"^\s*(?:to|until|through|till|-|~|—|–|至|到)\s*$", re.IGNORECASE)
    _BETWEEN_AND = re.compile(r"^\s*(?:,?\s*and|和|、|至|到)\s*$", re.IGNORECASE)
    _ROLLING_KEYWORDS = re.compile(r"\brolling\b|\btrailing\b|滚动", re.IGNORECASE)
    _RELATIVE_MONTH = re.compile(r"\blast\s+(\d+)\s+months?\b|最近\s*(\d+)\s*个月", re.IGNORECASE)
    _RELATIVE_DAY = re.compile(r"\blast\s+(\d+)\s+days?\b|最近\s*(\d+)\s*天", re.IGNORECASE)

    def __init__(
        self,
        llm: BaseModelProvider | None = None,
        enable_llm_fallback: bool = False,
        today_provider: Callable[[], date] = date.today,
    ) -> None:
        self.llm = llm
        self.enable_llm_fallback = enable_llm_fallback
        self.today_provider = today_provider

    def execute(self, context: Context) -> NodeResult:
        today = self.today_provider()
        try:
            ranges, explicit_merge = self.resolve_rules(context.task.question, today)
        except ValueError as exc:
            return self.failure(str(exc))

        window = self.window_semantics(
            context.task.question,
            explicit_merge=explicit_merge,
            resolved=bool(ranges),
        )
        context.task_context["date_window"] = window

        if ranges:
            context.date_context = DateContext(
                reference_date=today.isoformat(), source="rule", ranges=ranges
            )
            context.date_context.note = f"{context.date_context.note} {window['note']}"
            return self.success(f"Resolved {len(ranges)} date expression(s) by rule")

        if self.enable_llm_fallback and self.llm is not None:
            try:
                ranges = self._parse_llm_fallback(context.task.question, today)
            except Exception as exc:
                context.date_context = DateContext(
                    reference_date=today.isoformat(),
                    source="llm_fallback_failed",
                    note=(
                        "No rule-based date was found and LLM date fallback failed: "
                        f"{exc}"
                    ),
                )
                return self.success("No date resolved; optional LLM fallback failed")
            context.date_context = DateContext(
                reference_date=today.isoformat(), source="llm", ranges=ranges
            )
            context.date_context.note = f"{context.date_context.note} {window['note']}"
            return self.success(
                f"Resolved {len(ranges)} date expression(s) with LLM fallback"
            )

        context.date_context = DateContext(reference_date=today.isoformat())
        return self.success("No supported date expression found")

    @classmethod
    def window_semantics(
        cls,
        question: str,
        *,
        explicit_merge: bool = False,
        resolved: bool = True,
    ) -> dict[str, Any]:
        """Label the resolved window: calendar-aligned or rolling/trailing.

        Month counts stay complete calendar months; day counts and explicit
        "rolling"/"trailing"/"滚动" wording are labelled rolling.  The label
        never changes the resolved dates, it only makes the semantics readable
        by later stages.
        """

        if cls._ROLLING_KEYWORDS.search(question):
            mode = "rolling"
            note = (
                "The question asks for a rolling/trailing window measured back "
                "from the reference date."
            )
        elif cls._RELATIVE_DAY.search(question):
            mode = "rolling"
            note = (
                "A trailing day count is resolved against the reference date as "
                "an inclusive rolling window."
            )
        elif cls._RELATIVE_MONTH.search(question):
            mode = "calendar"
            note = (
                "A month count is resolved as complete calendar months ending on "
                "the reference date."
            )
        else:
            mode = "calendar"
            note = (
                "Windows are aligned to natural day/month/quarter/year boundaries."
            )
        if not resolved:
            note = "No date window was resolved from the question."
        return {
            "mode": mode,
            "explicit_merge": bool(explicit_merge),
            "resolved": bool(resolved),
            "note": note,
        }

    #: English month names (and the usual abbreviations) to their numbers.
    _MONTH_NUMBERS = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    @classmethod
    def _month_number(cls, name: str) -> int:
        number = cls._MONTH_NUMBERS.get(name.strip().strip(".").casefold())
        if number is None:  # pragma: no cover - the pattern only matches known names
            raise ValueError(f"Unknown month name {name!r}")
        return number

    @staticmethod
    def _month_range(year: int, month: int) -> tuple[date, date]:
        """Inclusive first and last day of one calendar month."""
        start = date(year, month, 1)
        end = (
            date(year + 1, 1, 1)
            if month == 12
            else date(year, month + 1, 1)
        ) - timedelta(days=1)
        return start, end

    @classmethod
    def parse_rules(cls, question: str, today: date) -> list[DateRange]:
        ranges, _explicit_merge = cls.resolve_rules(question, today)
        return ranges

    @classmethod
    def resolve_rules(cls, question: str, today: date) -> tuple[list[DateRange], bool]:
        """Resolve rule-based ranges and report whether explicit dates merged."""

        matches: list[tuple[int, int, DateRange, bool]] = []

        def add(
            match: re.Match[str],
            start: date,
            end: date,
            *,
            explicit: bool = False,
        ) -> None:
            if any(
                match.start() < existing_end and match.end() > existing_start
                for existing_start, existing_end, _, _ in matches
            ):
                return
            matches.append(
                (
                    match.start(),
                    match.end(),
                    DateRange(
                        expression=match.group(0),
                        start_date=start.isoformat(),
                        end_date=end.isoformat(),
                    ),
                    explicit,
                )
            )

        explicit_date = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
        for match in explicit_date.finditer(question):
            try:
                resolved = date(*(int(part) for part in match.groups()))
            except ValueError as exc:
                raise ValueError(
                    f"Invalid explicit date {match.group(0)!r}: {exc}"
                ) from exc
            add(match, resolved, resolved, explicit=True)

        relative_days = (
            re.compile(r"\blast\s+(\d+)\s+days?\b", re.IGNORECASE),
            re.compile(r"最近\s*(\d+)\s*天"),
        )
        for pattern in relative_days:
            for match in pattern.finditer(question):
                days = int(match.group(1))
                if days < 1 or days > 36_500:
                    raise ValueError(
                        f"Relative day count must be between 1 and 36500: {days}"
                    )
                add(match, today - timedelta(days=days - 1), today)

        relative_months = (
            re.compile(r"\blast\s+(\d+)\s+months?\b", re.IGNORECASE),
            re.compile(r"最近\s*(\d+)\s*个月"),
        )
        for pattern in relative_months:
            for match in pattern.finditer(question):
                months = int(match.group(1))
                if months < 1 or months > 1_200:
                    raise ValueError(
                        f"Relative month count must be between 1 and 1200: {months}"
                    )
                add(match, *cls._month_window(today, months))

        fixed_rules = (
            (r"\btoday\b", cls._single_day(today)),
            (r"今天", cls._single_day(today)),
            (r"\byesterday\b", cls._single_day(today - timedelta(days=1))),
            (r"昨天", cls._single_day(today - timedelta(days=1))),
            (r"\bthis\s+month\b", (today.replace(day=1), today)),
            (r"本月", (today.replace(day=1), today)),
            (r"\blast\s+month\b", cls._previous_month(today)),
            (r"上月", cls._previous_month(today)),
            (r"\bthis\s+quarter\b", (cls._quarter_start(today), today)),
            (r"本季度", (cls._quarter_start(today), today)),
            (r"\blast\s+quarter\b", cls._previous_quarter(today)),
            (r"上季度", cls._previous_quarter(today)),
            (r"\bthis\s+year\b", (date(today.year, 1, 1), today)),
            (r"今年", (date(today.year, 1, 1), today)),
            (r"\blast\s+year\b", cls._year_range(today.year - 1)),
            (r"去年", cls._year_range(today.year - 1)),
        )
        for pattern, (start, end) in fixed_rules:
            for match in re.finditer(pattern, question, re.IGNORECASE):
                add(match, start, end)

        # A named month (or an explicit year-month) must win over the bare-year
        # rule below. Without this, "December 2024 compared to November 2024"
        # resolved to the SAME full year twice (the bare-year rule matched "2024"
        # after each month name), so a two-month comparison compiled a duplicated
        # whole-year filter and answered with whole-year totals.
        month_year_patterns = (
            (
                re.compile(
                    r"\b(January|February|March|April|May|June|July|August|"
                    r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|"
                    r"Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+((?:19|20)\d{2})\b",
                    re.IGNORECASE,
                ),
                "named",
            ),
            (re.compile(r"(?<!\d)((?:19|20)\d{2})-(\d{1,2})(?!\d)"), "iso"),
            (re.compile(r"((?:19|20)\d{2})年\s*(\d{1,2})月"), "chinese"),
        )
        for pattern, shape in month_year_patterns:
            for match in pattern.finditer(question):
                if shape == "named":
                    month = cls._month_number(match.group(1))
                    year = int(match.group(2))
                elif shape == "iso":
                    month = int(match.group(2))
                    year = int(match.group(1))
                else:
                    month = int(match.group(2))
                    year = int(match.group(1))
                if not 1 <= month <= 12:
                    raise ValueError(f"Invalid month in {match.group(0)!r}")
                add(match, *cls._month_range(year, month))

        year_patterns = (
            re.compile(r"(?<![\d-])((?:19|20)\d{2})年"),
            re.compile(r"\b(?:in\s+)?((?:19|20)\d{2})\b", re.IGNORECASE),
        )
        for pattern in year_patterns:
            for match in pattern.finditer(question):
                year = int(match.group(1))
                start, end = cls._year_range(year)
                add(match, start, end)

        matches.sort(key=lambda item: item[0])
        return cls._merge_matches(question, matches)

    @classmethod
    def _merge_matches(
        cls,
        question: str,
        matches: list[tuple[int, int, DateRange, bool]],
    ) -> tuple[list[DateRange], bool]:
        """Merge overlapping, touching, and explicitly joined ranges.

        Two explicit dates joined by a range connector ("from 2026-01-01 to
        2026-01-05", "2026-01-01 至 2026-01-05") become one inclusive range
        instead of two separate single days.
        """

        merged: list[tuple[int, int, DateRange]] = []
        explicit_merge = False
        for start_span, end_span, current, explicit in matches:
            if not merged:
                merged.append((start_span, end_span, current))
                continue
            previous_start, previous_end, previous = merged[-1]
            gap = question[previous_end:start_span]
            both_single_days = (
                bool(explicit)
                and _is_single_day(previous)
                and _is_single_day(current)
            )
            joined = False
            if both_single_days:
                if cls._RANGE_CONNECTOR.match(gap):
                    joined = True
                elif cls._BETWEEN_AND.match(gap) and question[:previous_start].rstrip().lower().endswith(
                    ("between", "从", "自")
                ):
                    joined = True
            touching = _touching(previous, current) and _is_joinable_gap(gap)
            if not joined and not touching:
                merged.append((start_span, end_span, current))
                continue
            expression = (
                question[previous_start:end_span].strip() if joined else previous.expression
            )
            if joined and current.end_date < previous.start_date:
                raise ValueError(
                    f"Explicit date range is reversed: {expression!r} ends before "
                    "it starts"
                )
            merged[-1] = (
                previous_start,
                end_span,
                DateRange(
                    expression=expression,
                    start_date=min(previous.start_date, current.start_date),
                    end_date=max(previous.end_date, current.end_date),
                ),
            )
            explicit_merge = explicit_merge or joined
        return [item[2] for item in merged], explicit_merge

    def _parse_llm_fallback(self, question: str, today: date) -> list[DateRange]:
        assert self.llm is not None
        payload = self.llm.generate_json(
            f"""Resolve date expressions in the user question relative to {today.isoformat()}.
Return inclusive calendar dates. If there is no date expression, return an empty list.
Return only this JSON shape:
{{"ranges": [{{"expression": "...", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}]}}

User question:
{question}
"""
        )
        raw_ranges = payload.get("ranges")
        if not isinstance(raw_ranges, list) or len(raw_ranges) > 5:
            raise ValueError("LLM date response must contain at most five ranges")
        ranges: list[DateRange] = []
        for raw in raw_ranges:
            if not isinstance(raw, dict):
                raise ValueError("Each LLM date range must be an object")
            resolved = DateRange.model_validate(raw)
            start = date.fromisoformat(resolved.start_date)
            end = date.fromisoformat(resolved.end_date)
            if start > end:
                raise ValueError("LLM date range start_date is after end_date")
            ranges.append(resolved)
        return ranges

    @staticmethod
    def _single_day(value: date) -> tuple[date, date]:
        return value, value

    @staticmethod
    def _year_range(year: int) -> tuple[date, date]:
        return date(year, 1, 1), date(year, 12, 31)

    @staticmethod
    def _previous_month(today: date) -> tuple[date, date]:
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end

    @staticmethod
    def _month_window(today: date, months: int) -> tuple[date, date]:
        """Return the complete calendar months window ending on ``today``."""

        index = today.year * 12 + (today.month - 1) - (months - 1)
        return date(index // 12, index % 12 + 1, 1), today

    @staticmethod
    def _quarter_start(value: date) -> date:
        month = ((value.month - 1) // 3) * 3 + 1
        return date(value.year, month, 1)

    @classmethod
    def _previous_quarter(cls, today: date) -> tuple[date, date]:
        end = cls._quarter_start(today) - timedelta(days=1)
        return cls._quarter_start(end), end


_JOINABLE_GAP = re.compile(
    r"^[\s,;]*(?:(?:and|or|to|until|through|till|和|与|及|、|至|到)[\s,;]*)?$",
    re.IGNORECASE,
)


def _is_joinable_gap(gap: str) -> bool:
    """True when only a conjunction or separator sits between two ranges."""

    return bool(_JOINABLE_GAP.match(gap))


def _is_single_day(value: DateRange) -> bool:
    return value.start_date == value.end_date


def _touching(previous: DateRange, current: DateRange) -> bool:
    """True when two ranges overlap or share a boundary day."""

    if current.start_date <= previous.end_date:
        return True
    try:
        following = date.fromisoformat(previous.end_date) + timedelta(days=1)
    except ValueError:
        return False
    return following.isoformat() == current.start_date
