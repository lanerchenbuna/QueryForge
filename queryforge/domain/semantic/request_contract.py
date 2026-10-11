"""Explicit output labels and measure intent, independent of model verdicts."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp


_LABEL = r"[A-Za-z_][A-Za-z0-9_]*"
_RETURN = re.compile(
    rf"\breturn\s+(?:(?:a single|one|a) column named\s+|only\s+)?"
    rf"(?P<labels>{_LABEL}(?:\s*(?:,|and)\s*{_LABEL})*)"
    r"(?=\s*(?:[.]|$|,\s*(?:sorted by|in that order)|\s+in one row))",
    re.IGNORECASE,
)
_CHINESE_RETURN = re.compile(rf"(?:仅|只)返回\s*(?P<labels>{_LABEL})\s*列")


def output_contract(question: str) -> tuple[str, ...]:
    """Recognize formal identifier lists only; ordinary prose remains intent."""
    match = _RETURN.search(question) or _CHINESE_RETURN.search(question)
    if match is None:
        return ()
    return tuple(re.split(r"\s*(?:,|\band\b)\s*", match["labels"], flags=re.I))


def measure_intent(question: str) -> str:
    match = _RETURN.search(question) or _CHINESE_RETURN.search(question)
    return question[:match.start()] if match else question


def validate_output_contract(question: str, sql: str) -> str | None:
    required = output_contract(question)
    if not required:
        return None
    try:
        statements = [item for item in sqlglot.parse(sql, read="sqlite") if item]
        root = statements[0] if len(statements) == 1 else None
        if not isinstance(root, exp.Select) or any(item.is_star for item in root.selects):
            raise ValueError("final projection is not an explicit SELECT")
        actual = tuple(item.alias_or_name for item in root.selects)
        if actual == required:
            if re.search(r"\b(?:list|show)\b.*\b(?:identifiers|ids)\b", measure_intent(question), re.I):
                from sqlglot.optimizer.scope import traverse_scope
                from queryforge.domain.semantic.output_lineage import output_dependencies
                scopes = {id(scope.expression): scope for scope in traverse_scope(root)}
                dependencies = output_dependencies(root, scopes)
                if any(id(node) in dependencies for node in root.find_all(exp.AggFunc)):
                    return "Output contract requests identifiers; an aggregate cannot satisfy the list."
                if any(id(node) in dependencies and not isinstance(node, (exp.Select, exp.Alias, exp.Column, exp.Identifier)) for node in root.walk()):
                    return "Output contract requests original identifiers; transformed or constant values cannot satisfy the list."
                if any(not isinstance(item.this if isinstance(item, exp.Alias) else item, exp.Column) for item in root.selects):
                    return "Output contract requests identifiers as explicit columns."
            return None
    except (sqlglot.errors.ParseError, ValueError):
        actual = ()
    return f"Output contract requires columns {list(required)} in order; observed {list(actual)}."


def null_filter_terms(question: str) -> tuple[str, ...]:
    text = re.sub(r"'([^']|'')*'", " ", measure_intent(question))
    matches = re.findall(r"\b(?:have|has|with)\s+(?:a\s+)?NULL\s+(\w+)\b|\b(\w+)\s+IS\s+NULL\b", text, re.I)
    return tuple(left or right for left, right in matches)
