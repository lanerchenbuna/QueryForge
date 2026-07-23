"""Reusable, provider-neutral MCP prompt templates."""

from __future__ import annotations


def analyze_data(question: str, subject: str | None = None) -> str:
    scope = f" within subject {subject!r}" if subject else ""
    return (
        f"Analyze this data question{scope}: {question}\n"
        "State the intended metric, dimensions, filters, time range, and ambiguity "
        "before calling QueryForge."
    )


def sql_review(sql: str) -> str:
    return (
        "Review this SQLite SQL for correctness, governed joins, safety, and result "
        f"scope:\n\n{sql}"
    )


def troubleshoot(sql: str, error_message: str) -> str:
    return (
        "Troubleshoot this SQLite SQL without weakening read-only safety.\n"
        f"SQL:\n{sql}\n\nObserved error:\n{error_message}"
    )


def build_report(
    question: str,
    subject: str | None = None,
    metrics: str | None = None,
) -> str:
    details = []
    if subject:
        details.append(f"Subject: {subject}")
    if metrics:
        details.append(f"Requested metrics: {metrics}")
    suffix = "\n".join(details)
    return (
        f"Build a static analytical report for: {question}\n{suffix}\n"
        "Use QueryForge report generation after validating the SQL result."
    )
