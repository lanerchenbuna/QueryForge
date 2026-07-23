"""Resolve common English and Chinese date expressions without heavy dependencies."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Callable

from queryforge.workflow.node.base import Node
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.core.schemas.models import Context, DateContext, DateRange, NodeResult


class DateParserNode(Node):
    name = "date_parser"
    description = "Resolve natural-language dates into inclusive calendar ranges"

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
            ranges = self.parse_rules(context.task.question, today)
        except ValueError as exc:
            return self.failure(str(exc))

        if ranges:
            context.date_context = DateContext(
                reference_date=today.isoformat(), source="rule", ranges=ranges
            )
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
            return self.success(
                f"Resolved {len(ranges)} date expression(s) with LLM fallback"
            )

        context.date_context = DateContext(reference_date=today.isoformat())
        return self.success("No supported date expression found")

    @classmethod
    def parse_rules(cls, question: str, today: date) -> list[DateRange]:
        matches: list[tuple[int, int, DateRange]] = []

        def add(match: re.Match[str], start: date, end: date) -> None:
            if any(
                match.start() < existing_end and match.end() > existing_start
                for existing_start, existing_end, _ in matches
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
            add(match, resolved, resolved)

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
        return [item[2] for item in matches]

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
    def _quarter_start(value: date) -> date:
        month = ((value.month - 1) // 3) * 3 + 1
        return date(value.year, month, 1)

    @classmethod
    def _previous_quarter(cls, today: date) -> tuple[date, date]:
        end = cls._quarter_start(today) - timedelta(days=1)
        return cls._quarter_start(end), end
