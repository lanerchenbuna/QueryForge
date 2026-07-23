"""Build governed SQLite data assets from a declarative YAML contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.data_assets import DataAssetBuilder


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run CSV/Parquet/API ingestion through staging, quality quarantine, "
            "SQLite publication, lineage, and semantic publication."
        )
    )
    parser.add_argument("--config", required=True, help="Asset build YAML path")
    parser.add_argument(
        "--publish-database",
        required=True,
        help="Writable SQLite database containing published analytics tables",
    )
    parser.add_argument(
        "--state-root",
        default=".queryforge/data_assets",
        help="Staging, quality, watermark, lineage, and generated semantic output directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        builder = DataAssetBuilder(args.publish_database, args.state_root)
        results = builder.build_from_file(args.config)
    except Exception as exc:
        print(f"QueryForge asset build failed: {exc}", file=sys.stderr)
        return 1
    payload = {"results": [result.model_dump() for result in results]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(result.status == "success" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
