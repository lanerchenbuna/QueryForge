"""Validate a semantic model against SQLite and generate Markdown documentation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.domain.semantic import SemanticContractValidator, SemanticModelLoader
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate QueryForge semantic YAML and generate Markdown docs."
    )
    parser.add_argument("--database", required=True, help="SQLite database path")
    parser.add_argument("--model", required=True, help="Semantic model YAML path")
    parser.add_argument(
        "--output",
        help="Optional Markdown output path; validation-only when omitted",
    )
    parser.add_argument(
        "--contract-report",
        help="Optional JSON output for operational contract validation results",
    )
    return parser.parse_args()


def render_markdown(context) -> str:
    model = context.model
    lines = [
        f"# Semantic Model: {model.name}",
        "",
        model.description or "No description provided.",
        "",
        "## Entities",
        "",
        "| Entity | Table | Type | Grain | Dimensions |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entity in model.entities:
        dimensions = ", ".join(dimension.name for dimension in entity.dimensions) or "-"
        grain = ", ".join(entity.effective_grain) or "-"
        lines.append(
            f"| {entity.name} | {entity.table} | {entity.entity_type} | "
            f"{grain} | {dimensions} |"
        )
    lines.extend(["", "## Metrics", "", "| Metric | Entity | Aggregation | Expression |"])
    lines.append("| --- | --- | --- | --- |")
    for metric in model.metrics:
        lines.append(
            f"| {metric.name} | {metric.entity} | {metric.aggregation} | "
            f"`{metric.expression}` |"
        )
    lines.extend(["", "## Relationships", ""])
    for relationship in model.relationships:
        lines.append(
            f"- `{relationship.name}`: `{relationship.from_ref}` -> "
            f"`{relationship.to_ref}` ({relationship.relationship_type})"
        )
    lines.extend(["", "## Join Paths", ""])
    for path in model.join_paths:
        lines.append(
            f"- `{path.name}`: {path.from_entity} -> {path.to_entity} via "
            + ", ".join(path.relationships)
        )
    lines.extend(["", "## Operational Contracts", ""])
    lines.append("| Subject | Owner | SLA | Refresh | Sensitivity | Version | Rules |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for entity in model.entities:
        lines.append(_contract_row(entity.name, entity))
        for dimension in entity.dimensions:
            lines.append(_contract_row(f"{entity.name}.{dimension.name}", dimension))
    for metric in model.metrics:
        lines.append(_contract_row(metric.name, metric))
    for path in model.join_paths:
        lines.append(_contract_row(path.name, path))
    return "\n".join(lines) + "\n"


def _contract_row(subject, contract) -> str:
    return (
        f"| {subject} | {contract.owner or '-'} | {contract.sla or '-'} | "
        f"{contract.refresh_frequency or '-'} | {contract.sensitivity} | "
        f"{contract.contract_version} | {len(contract.quality_rules)} |"
    )


def main() -> int:
    args = parse_args()
    with SQLiteConnector(args.database) as connector:
        tool = DatabaseTool(connector)
        schemas = [tool.describe_table(table) for table in tool.list_tables()]
    context = SemanticModelLoader.load_and_validate(args.model, schemas, "")
    report = SemanticContractValidator.validate(context.model, args.database)
    print(
        f"Validated semantic model {context.model.name!r}: "
        f"{len(context.model.entities)} entities, {len(context.model.metrics)} metrics."
    )
    print(
        "Operational contract validation: "
        + json.dumps(report.summary(), ensure_ascii=False, sort_keys=True)
    )
    if args.output:
        output = Path(args.output).expanduser()
        output.write_text(render_markdown(context), encoding="utf-8")
        print(f"Wrote Markdown documentation: {output}")
    if args.contract_report:
        report_path = Path(args.contract_report).expanduser()
        report_path.write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Wrote contract validation report: {report_path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
