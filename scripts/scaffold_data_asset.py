"""Create a reviewable upload contract with a mandatory semantic layer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.data_assets.scaffold import AssetScaffoldError, write_asset_scaffold


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a CSV or Parquet file and scaffold a data-asset contract "
            "whose semantic layer must be reviewed before upload."
        )
    )
    parser.add_argument("--source", required=True, help="Local CSV or Parquet file")
    parser.add_argument("--output", required=True, help="Draft asset YAML path")
    parser.add_argument("--asset-name", help="Logical asset name")
    parser.add_argument("--target-table", help="Published SQLite table")
    parser.add_argument(
        "--entity-type",
        choices=("auto", "fact", "dimension"),
        default="auto",
    )
    parser.add_argument(
        "--grain",
        action="append",
        default=[],
        help="Business grain column; repeat for a composite grain",
    )
    parser.add_argument("--owner", default="data-platform")
    parser.add_argument("--sample-rows", type=int, default=5_000)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        output = write_asset_scaffold(
            args.source,
            args.output,
            asset_name=args.asset_name,
            target_table=args.target_table,
            entity_type=args.entity_type,
            grain=args.grain,
            owner=args.owner,
            sample_rows=args.sample_rows,
            force=args.force,
        )
    except (AssetScaffoldError, OSError, ValueError) as exc:
        print(f"Semantic scaffold failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "draft",
                "config": str(output),
                "next": [
                    "Review grain, hidden columns, dimensions, relationships, and metrics.",
                    "Set semantic_model.reviewed to true.",
                    "Run scripts/build_data_assets.py with this config.",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
