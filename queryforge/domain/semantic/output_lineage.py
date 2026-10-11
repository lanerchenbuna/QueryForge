"""Expressions that actually contribute to the final query projection."""

from sqlglot import exp
from sqlglot.optimizer.scope import Scope


def output_dependencies(root, scope_by_expression):
    nodes: set[int] = set()
    visited: set[tuple[int, int]] = set()

    def visit(node, scope, depth=0):
        if depth > 64:
            raise ValueError("Output lineage exceeds the deterministic depth bound")
        if not isinstance(node, exp.Expression) or scope is None:
            return
        key = (id(node), id(scope))
        if key in visited:
            return
        visited.add(key)
        nodes.add(id(node))
        if isinstance(node, exp.Select):
            scope = scope_by_expression.get(id(node), scope)
            for projection in node.selects:
                visit(projection, scope, depth + 1)
            return
        if isinstance(node, exp.Subquery):
            visit(node.this, scope_by_expression.get(id(node.this)), depth + 1)
            return
        if isinstance(node, (exp.Column, exp.Star)):
            for name, (_, source) in scope.selected_sources.items():
                if isinstance(node, exp.Column) and node.table and node.table.casefold() != name.casefold():
                    continue
                if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
                    continue
                for index, projection in enumerate(source.expression.selects):
                    label = (source.outer_columns[index] if index < len(source.outer_columns) else projection.alias_or_name)
                    if node.is_star or label.casefold() == node.name.casefold():
                        visit(projection, source, depth + 1)
            return
        for child in node.iter_expressions():
            visit(child, scope_by_expression.get(id(child), scope), depth + 1)

    visit(root, scope_by_expression.get(id(root)))
    return nodes
