"""Typed workflow error taxonomy shared by execute, fix, and reflect nodes.

Step 07 requires the repair loop to reason about *why* an attempt failed
instead of pattern-matching on free-form error strings: a permission denial or
an exhausted budget must never trigger a more permissive strategy, and a data
quality gap must not be "fixed" by silently changing the metric definition.

``TypedWorkflowError`` subclasses :class:`queryforge.workflow.workflow.WorkflowError`
so existing ``except WorkflowError`` handling keeps working. Because of that
subclass link this module imports ``workflow`` at module scope; ``workflow``
therefore imports this module lazily inside its functions (never at module
scope) so that neither import order can deadlock.
"""

from __future__ import annotations

from enum import Enum
import re
from typing import Any

from queryforge.workflow.workflow import WorkflowError


class WorkflowErrorCategory(str, Enum):
    """Stable error categories used by the repair loop and by reports."""

    syntax = "syntax"
    identifier = "identifier"
    execution = "execution"
    semantic = "semantic"
    permission = "permission"
    budget = "budget"
    data_quality = "data_quality"
    unsupported = "unsupported"
    unknown = "unknown"


# Named rules of queryforge.domain.security.sql_policy -> category.
POLICY_RULE_CATEGORIES: dict[str, WorkflowErrorCategory] = {
    "ast_parse": WorkflowErrorCategory.syntax,
    "syntax_error": WorkflowErrorCategory.syntax,
    "table_scope": WorkflowErrorCategory.identifier,
    "column_scope": WorkflowErrorCategory.identifier,
    "column_scope_star": WorkflowErrorCategory.identifier,
    "ambiguous_column_scope": WorkflowErrorCategory.identifier,
    "dangerous_function": WorkflowErrorCategory.permission,
    "read_only_ast": WorkflowErrorCategory.permission,
    "recursive_cte": WorkflowErrorCategory.permission,
    "cross_join": WorkflowErrorCategory.permission,
    "limit_literal": WorkflowErrorCategory.budget,
    "max_limit": WorkflowErrorCategory.budget,
}

# Business-semantic validator rule names -> semantic category.
SEMANTIC_RULE_NAMES = (
    "metric_expression",
    "default_filter",
    "time_filter",
    "join_key",
    "fanout",
    "grain",
    "unknown_table_or_column",
)

_SQLITE_SUBSTRING_CATEGORIES: tuple[tuple[str, WorkflowErrorCategory], ...] = (
    ("no such table", WorkflowErrorCategory.identifier),
    ("no such column", WorkflowErrorCategory.identifier),
    ("has no column named", WorkflowErrorCategory.identifier),
    ("ambiguous column name", WorkflowErrorCategory.identifier),
    ("no such function", WorkflowErrorCategory.identifier),
    ("syntax error", WorkflowErrorCategory.syntax),
    ("incomplete input", WorkflowErrorCategory.syntax),
    ("unrecognized token", WorkflowErrorCategory.syntax),
    ("misuse of aggregate", WorkflowErrorCategory.semantic),
    ("database is locked", WorkflowErrorCategory.execution),
    ("disk i/o error", WorkflowErrorCategory.execution),
    ("unable to open database", WorkflowErrorCategory.execution),
    ("datatype mismatch", WorkflowErrorCategory.data_quality),
)

_KEYWORD_CATEGORIES: tuple[tuple[str, WorkflowErrorCategory], ...] = (
    ("semantic sql validation failed", WorkflowErrorCategory.semantic),
    ("semantic contract violation", WorkflowErrorCategory.semantic),
    ("repeated sql cycle", WorkflowErrorCategory.budget),
    ("maximum sql retries", WorkflowErrorCategory.budget),
    ("retry limit", WorkflowErrorCategory.budget),
    ("retry budget", WorkflowErrorCategory.budget),
    ("preview budget", WorkflowErrorCategory.budget),
    ("budget exhausted", WorkflowErrorCategory.budget),
    ("unsupported", WorkflowErrorCategory.unsupported),
    ("outside supported coverage", WorkflowErrorCategory.unsupported),
    ("quality rule", WorkflowErrorCategory.data_quality),
    ("null rate", WorkflowErrorCategory.data_quality),
    ("read-only", WorkflowErrorCategory.permission),
    ("permission", WorkflowErrorCategory.permission),
    ("not authorised", WorkflowErrorCategory.permission),
    ("not authorized", WorkflowErrorCategory.permission),
)

CATEGORY_GUIDANCE: dict[WorkflowErrorCategory, str] = {
    WorkflowErrorCategory.syntax: (
        "Repair only the syntax/parsing defect; keep the governed metric, filters, "
        "join keys, and grain unchanged."
    ),
    WorkflowErrorCategory.identifier: (
        "Only use identifiers that exist in the supplied schema and semantic model. "
        "Do not invent, rename, or drop columns/tables."
    ),
    WorkflowErrorCategory.execution: (
        "The statement failed at execution time. Make the smallest correction that "
        "keeps the business semantics intact."
    ),
    WorkflowErrorCategory.semantic: (
        "The structured metric contract was violated. Restore the metric expression, "
        "every default filter (including its exact compared value), the governed join "
        "keys, and the requested GROUP BY grain."
    ),
    WorkflowErrorCategory.permission: (
        "The SQL was refused by the security policy or a permission boundary. Do not "
        "ask for wider access, do not target hidden/unauthorised identifiers, and do "
        "not switch to a looser strategy; stay inside the visible schema."
    ),
    WorkflowErrorCategory.budget: (
        "A retry/preview budget bound or a repeated-SQL guard stopped the loop. Do not "
        "expand scope, remove LIMITs, or repeat an SQL attempt; produce a materially "
        "different, smaller correction."
    ),
    WorkflowErrorCategory.data_quality: (
        "The data failed a declared quality rule or the requested slice is genuinely "
        "empty. Do not redefine the metric or drop filters to force a non-empty result."
    ),
    WorkflowErrorCategory.unsupported: (
        "The shape is outside the supported coverage. Simplify toward a single "
        "aggregate over the base entity with declared joins instead of adding more "
        "nesting."
    ),
    WorkflowErrorCategory.unknown: (
        "Diagnose from the supplied evidence; do not change metric semantics to make "
        "the error disappear."
    ),
}


class TypedWorkflowError(WorkflowError):
    """WorkflowError carrying a typed category for the repair loop."""

    def __init__(
        self,
        node_name: str,
        error: str,
        context: Any,
        category: WorkflowErrorCategory,
    ) -> None:
        super().__init__(node_name, error, context)
        self.category = category


def categorize_error(error: str | Exception | None) -> WorkflowErrorCategory:
    """Map an error object or message onto one stable :class:`WorkflowErrorCategory`."""
    if error is None:
        return WorkflowErrorCategory.unknown
    decision = getattr(error, "decision", None)
    rule = getattr(decision, "rule", None)
    if isinstance(rule, str) and rule in POLICY_RULE_CATEGORIES:
        return POLICY_RULE_CATEGORIES[rule]

    message = str(error) if not isinstance(error, str) else error
    lowered = message.casefold()
    if not lowered.strip():
        return WorkflowErrorCategory.unknown

    if any(name in lowered for name in SEMANTIC_RULE_NAMES) and (
        "semantic" in lowered or "contract" in lowered
    ):
        return WorkflowErrorCategory.semantic

    matched_rule = re.search(r"\brule=([a-z_]+)", lowered)
    if matched_rule and matched_rule.group(1) in POLICY_RULE_CATEGORIES:
        return POLICY_RULE_CATEGORIES[matched_rule.group(1)]

    if "sql_security_error" in lowered or "unsafesqlerror" in lowered:
        return WorkflowErrorCategory.permission

    for needle, category in _SQLITE_SUBSTRING_CATEGORIES:
        if needle in lowered:
            return category

    for needle, category in _KEYWORD_CATEGORIES:
        if needle in lowered:
            return category

    return WorkflowErrorCategory.unknown


def guidance_for(category: WorkflowErrorCategory) -> str:
    """Human-readable repair guidance for a typed category."""
    return CATEGORY_GUIDANCE.get(category, CATEGORY_GUIDANCE[WorkflowErrorCategory.unknown])


def record_error_category(context: Any, error: str | Exception | None) -> WorkflowErrorCategory:
    """Record ``str(category)`` on ``context.task_context["error_categories"]``."""
    category = categorize_error(error)
    task_context = getattr(context, "task_context", None)
    if isinstance(task_context, dict):
        categories = task_context.setdefault("error_categories", [])
        if isinstance(categories, list):
            categories.append(str(category.value))
    return category


def workflow_error(
    node_name: str,
    error: str,
    context: Any,
    *,
    category: WorkflowErrorCategory | None = None,
) -> TypedWorkflowError:
    """Build a typed workflow error, categorizing the message when needed."""
    resolved = category if category is not None else categorize_error(error)
    return TypedWorkflowError(node_name, error, context, resolved)
