"""Bound literal request predicates to reviewed semantic columns.

These helpers parse expressions only. Execution and physical column authorization
remain owned by DatabaseTool; unsupported expressions never become raw SQL tails.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from queryforge.domain.semantic.schemas import SemanticModel, SemanticMetric


class UnsupportedConstraint(ValueError):
    pass


def _mask_literals(text: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", lambda m: " " * len(m.group()), text)


def literal_predicate(text: str) -> exp.Expression:
    """Accept only column/literal predicates, never functions or query clauses."""
    if re.search(r";|--|/\*|\*/", _mask_literals(text)):
        raise UnsupportedConstraint("comments and multiple statements are unsupported")
    try:
        trees = sqlglot.parse(f"SELECT 1 WHERE {text}", read="sqlite")
    except Exception as exc:
        raise UnsupportedConstraint("filter cannot be parsed") from exc
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise UnsupportedConstraint("filter must be one predicate")
    tree = trees[0]
    if any(value for key, value in tree.args.items() if key not in {"expressions", "where"}):
        raise UnsupportedConstraint("query clauses are not filters")
    where = tree.args.get("where")
    if where is None:
        raise UnsupportedConstraint("empty filter")

    def scalar(node):
        return isinstance(node, (exp.Literal, exp.Null, exp.Boolean)) or (
            isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal)
            and not node.this.is_string
        )

    def check(node):
        if isinstance(node, (exp.Paren, exp.Not)):
            return check(node.this)
        if isinstance(node, (exp.And, exp.Or)):
            return check(node.this) and check(node.expression)
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is)):
            return isinstance(node.this, exp.Column) and scalar(node.expression)
        if isinstance(node, exp.In):
            return (isinstance(node.this, exp.Column) and not node.args.get("query")
                    and bool(node.expressions) and all(scalar(v) for v in node.expressions))
        if isinstance(node, exp.Between):
            return (isinstance(node.this, exp.Column) and scalar(node.args.get("low"))
                    and scalar(node.args.get("high")))
        return False

    predicate = where.this
    if not check(predicate) or any(node.comments for node in tree.walk()):
        raise UnsupportedConstraint("only column comparisons with literal values are supported")
    return predicate


def extract_filters(question: str) -> tuple[str, list[str]]:
    """Separate explicit filter text from metric/grouping intent.

    Quoted values cannot introduce a marker or a trailing grouping instruction.
    Non-temporal 'for/with' clauses must use literal comparison syntax; an
    unparsed slice cannot silently turn into an unrestricted total.
    """
    masked = _mask_literals(question)
    marker = re.search(
        r"\b(?:where|filtered\s+by|only\s+include)\b|只看|仅看|筛选|条件为|其中", masked, re.I
    )
    if marker is None:
        marker = re.search(
            r"\b(?:for|with)\s+(?=[\w.\"\s]+(?:=|!=|<>|<=|>=|<|>|\bIN\b|\bIS\b|\bBETWEEN\b))",
            masked, re.I,
        )
    if marker is None:
        informal = re.search(r"\b(?:for|with)\s+", masked, re.I)
        if informal:
            tail = question[informal.end():].strip()
            temporal = re.match(
                r"(?:\d{4}\b|Q[1-4]\b|(?:the\s+)?(?:last|previous|past|this|next)\s+"
                r"(?:\d+\s+)?(?:days?|weeks?|months?|quarters?|years?)\b"
                r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
                r"|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b)",
                tail, re.I,
            )
            if not temporal:
                raise UnsupportedConstraint("non-temporal for/with clauses require a literal comparison")
        return question, []
    tail = question[marker.end():].strip()
    boundary = re.search(r"\s+(?:by|grouped\s+by|按)\s*", _mask_literals(tail), re.I)
    rest = tail[boundary.start():] if boundary else ""
    fragment = tail[:boundary.start()] if boundary else tail
    fragment = fragment.strip().rstrip("?？。")
    predicate = literal_predicate(fragment)
    return question[:marker.start()] + rest, [predicate.sql(dialect="sqlite")]


def bind_filters(model: SemanticModel, metrics: list[SemanticMetric], filters: list[str]) -> list[str]:
    """Resolve an unqualified column to the metric base entity, fail closed."""
    entity = next(e for e in model.entities if e.name == metrics[0].entity)
    columns = set(entity.expected_columns + entity.primary_key + entity.grain)
    columns.update(d.column for d in entity.dimensions)
    for metric in metrics:
        parsed = sqlglot.parse_one(f"SELECT {metric.expression}", read="sqlite")
        columns.update(c.name for c in parsed.find_all(exp.Column) if c.table == entity.table)
    columns.difference_update(entity.hidden_columns)
    bound = []
    for text in filters:
        predicate = literal_predicate(text)
        for column in predicate.find_all(exp.Column):
            if column.db or column.catalog:
                raise UnsupportedConstraint("database-qualified filters are unsupported")
            # Cross-fact filters must name a conformed, governed dimension. An
            # unqualified fact-local predicate has no common population meaning.
            targets = [e for e in model.entities if column.table in {e.name, e.table}
                       and any(d.column == column.name for d in e.dimensions)
                       and column.name not in e.hidden_columns]
            if targets and (targets[0].name != entity.name or len({m.entity for m in metrics}) > 1):
                target = targets[0]
                references = [f"{target.name}.{d.name}" for d in target.dimensions if d.column == column.name]
                if any(not any(ref in m.allowed_dimensions for ref in references) for m in metrics):
                    raise UnsupportedConstraint("filter dimension must be allowed by every requested metric")
                column.set("table", exp.to_identifier(target.table))
                continue
            if len({m.entity for m in metrics}) > 1:
                raise UnsupportedConstraint("cross-fact filters require a qualified conformed dimension")
            if (column.name not in columns or column.db or column.catalog
                    or column.table not in {"", entity.table, entity.name}):
                raise UnsupportedConstraint(f"unknown or unavailable filter column {column.sql()!r}")
            column.set("table", exp.to_identifier(entity.table))
        bound.append(predicate.sql(dialect="sqlite"))
    return bound


def require_filter_predicates(sql: str, filters: list[str], *, metric_ctes: list[str] | None = None) -> None:
    """Prove each requested predicate is a mandatory WHERE conjunct.

    Looking for a column or literal anywhere in SQL is insufficient: an OR
    branch could make the requested restriction optional. Compiled queries use
    exact predicates, so AST equality is a conservative delivery check.
    """
    if not filters:
        return
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception as exc:
        raise UnsupportedConstraint("compiled filter SQL cannot be verified") from exc
    scopes = [tree]
    if metric_ctes:
        ctes = {cte.alias: cte.this for cte in tree.find_all(exp.CTE)}
        if any(name not in ctes for name in metric_ctes):
            raise UnsupportedConstraint("request_incomplete: missing metric aggregation scope")
        scopes = [ctes[name] for name in metric_ctes]

    def conjuncts(node):
        if isinstance(node, exp.Paren):
            return conjuncts(node.this)
        if isinstance(node, exp.And):
            return conjuncts(node.this) + conjuncts(node.expression)
        return [node]

    for scope in scopes:
        where = scope.args.get("where") if isinstance(scope, exp.Select) else None
        observed = conjuncts(where.this) if where else []
        for text in filters:
            if any(predicate not in observed for predicate in conjuncts(literal_predicate(text))):
                raise UnsupportedConstraint("request_incomplete: SQL is missing a mandatory filter predicate")
