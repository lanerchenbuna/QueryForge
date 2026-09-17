"""Typed analysis intent: the structured contract behind a data question."""

from queryforge.domain.analysis.request import (
    DEFAULT_TIMEZONE,
    AnalysisRequest,
    apply_patch,
    detect_comparison_baseline,
    detect_time_grain,
    detect_time_range,
    high_impact_question,
    is_high_impact_ambiguity,
    time_range_text,
)

__all__ = [
    "DEFAULT_TIMEZONE",
    "AnalysisRequest",
    "apply_patch",
    "detect_comparison_baseline",
    "detect_time_grain",
    "detect_time_range",
    "high_impact_question",
    "is_high_impact_ambiguity",
    "time_range_text",
]
