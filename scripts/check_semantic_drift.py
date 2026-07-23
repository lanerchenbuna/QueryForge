"""Detect semantic, schema, relationship, metric, and data-quality drift."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.domain.semantic import (
    SemanticContractValidator,
    SemanticModelLoader,
)
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector


SECTION_KEYS = {
    "schema": "table_name",
    "entities": "name",
    "metrics": "name",
    "relationships": "name",
    "join_paths": "name",
}


def semantic_snapshot(
    database_path: str | Path,
    model_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return deterministic semantic state plus the current contract report."""

    database = Path(database_path).expanduser().resolve()
    model_source = Path(model_path).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"SQLite database does not exist: {database}")
    with SQLiteConnector(str(database)) as connector:
        schemas = [
            connector.describe_table(table)
            for table in connector.list_tables()
        ]
    context = SemanticModelLoader.load_and_validate(model_source, schemas, "")
    model = context.model
    sections = {
        "schema": [
            {
                "table_name": schema.table_name,
                "columns": [
                    column.model_dump()
                    for column in sorted(schema.columns, key=lambda item: item.name)
                ],
                "foreign_keys": [
                    foreign_key.model_dump()
                    for foreign_key in sorted(
                        schema.foreign_keys,
                        key=lambda item: (
                            item.column,
                            item.referenced_table,
                            item.referenced_column,
                        ),
                    )
                ],
            }
            for schema in sorted(schemas, key=lambda item: item.table_name)
        ],
        "entities": sorted(
            [
                entity.model_dump(exclude_none=True)
                for entity in model.entities
            ],
            key=lambda item: item["name"],
        ),
        "metrics": sorted(
            [
                metric.model_dump(exclude_none=True)
                for metric in model.metrics
            ],
            key=lambda item: item["name"],
        ),
        "relationships": sorted(
            [
                relationship.model_dump(
                    by_alias=True,
                    exclude_none=True,
                )
                for relationship in model.relationships
            ],
            key=lambda item: item["name"],
        ),
        "join_paths": sorted(
            [
                join_path.model_dump(exclude_none=True)
                for join_path in model.join_paths
            ],
            key=lambda item: item["name"],
        ),
    }
    canonical = json.dumps(
        sections,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot = {
        "version": 1,
        "database": _portable_path(database),
        "semantic_model": _portable_path(model_source),
        "model_name": model.name,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "sections": sections,
    }
    contract = SemanticContractValidator.validate(model, database)
    return snapshot, contract.model_dump()


def compare_snapshots(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return reader-friendly section changes between two snapshots."""

    changes: list[dict[str, Any]] = []
    baseline_sections = baseline.get("sections", {})
    current_sections = current.get("sections", {})
    for section, identity in SECTION_KEYS.items():
        before = {
            item[identity]: item
            for item in baseline_sections.get(section, [])
        }
        after = {
            item[identity]: item
            for item in current_sections.get(section, [])
        }
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(
            name
            for name in set(before) & set(after)
            if before[name] != after[name]
        )
        if added or removed or changed:
            changes.append(
                {
                    "section": section,
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                }
            )
    return changes


def run_check(
    database_path: str | Path,
    model_path: str | Path,
    baseline_path: str | Path,
    report_path: str | Path,
    *,
    update_baseline: bool = False,
) -> dict[str, Any]:
    """Run one audit and persist a bounded JSON report."""

    baseline_file = Path(baseline_path).expanduser().resolve()
    report_file = Path(report_path).expanduser().resolve()
    current, contract = semantic_snapshot(database_path, model_path)
    blocking_failures = sum(
        check["status"] == "failed" and check["severity"] == "error"
        for check in contract["checks"]
    )
    contract_summary = {
        "passed": blocking_failures == 0,
        "checks": len(contract["checks"]),
        "passed_checks": sum(
            check["status"] == "passed" for check in contract["checks"]
        ),
        "failed_checks": sum(
            check["status"] == "failed" for check in contract["checks"]
        ),
        "skipped_checks": sum(
            check["status"] == "skipped" for check in contract["checks"]
        ),
        "blocking_failures": blocking_failures,
    }
    if update_baseline:
        if not contract_summary["passed"]:
            raise ValueError(
                "Cannot update semantic baseline while data-quality contracts fail"
            )
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        changes: list[dict[str, Any]] = []
        status = "baseline_updated"
    else:
        if not baseline_file.is_file():
            raise ValueError(
                f"Semantic baseline does not exist: {baseline_file}; create it "
                "with --update-baseline after reviewing the current model"
            )
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        changes = compare_snapshots(baseline, current)
        if not contract_summary["passed"]:
            status = "contract_failed"
        elif changes:
            status = "drift_detected"
        else:
            status = "no_change"

    report = {
        "version": 1,
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
        "database": current["database"],
        "semantic_model": current["semantic_model"],
        "baseline": _portable_path(baseline_file),
        "current_fingerprint": current["fingerprint"],
        "changes": changes,
        "contract": contract_summary,
        "failed_contract_checks": [
            check
            for check in contract["checks"]
            if check["status"] == "failed"
        ],
    }
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _portable_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a database and semantic model with a reviewed baseline, "
            "then run operational data-quality contracts."
        )
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument(
        "--report",
        default=".queryforge/semantic-drift-report.json",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Replace the reviewed baseline after contract validation passes",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = run_check(
            args.database,
            args.model,
            args.baseline,
            args.report,
            update_baseline=args.update_baseline,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Semantic drift check failed: {exc}", file=sys.stderr)
        return 2
    summary = {
        "status": report["status"],
        "changes": report["changes"],
        "contract": report["contract"],
        "report": str(Path(args.report).expanduser().resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"no_change", "baseline_updated"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
