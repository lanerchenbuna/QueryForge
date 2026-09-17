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
    parser.add_argument("--config", help="Asset build YAML path")
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
    parser.add_argument(
        "--check-state",
        action="store_true",
        help=(
            "Only report the state left by a previous, possibly interrupted "
            "publication (pending semantic model, orphan checkpoints, registry "
            "mismatch). Exits 1 when the state needs reconciliation."
        ),
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help=(
            "Clear residue an interrupted publication left behind (a pending "
            "semantic model is always discarded; checkpoint backups only with "
            "--discard-checkpoints) and report the resulting state."
        ),
    )
    parser.add_argument(
        "--discard-checkpoints",
        action="store_true",
        help=(
            "With --reconcile, also delete orphan publication checkpoint "
            "backups. They may hold the last good snapshot, so inspect them first."
        ),
    )
    parser.add_argument(
        "--semantic-model",
        help="Semantic model path the state is checked or reconciled against",
    )
    args = parser.parse_args()
    if not (args.check_state or args.reconcile) and not args.config:
        parser.error("--config is required unless --check-state or --reconcile is used")
    return args


def main() -> int:
    args = parse_args()
    semantic_model = args.semantic_model or str(
        Path(args.publish_database).with_suffix(".semantic.yml")
    )
    try:
        builder = DataAssetBuilder(args.publish_database, args.state_root)
        if args.check_state or args.reconcile:
            report = (
                builder.reconcile_publication_state(
                    semantic_model, discard_orphan_checkpoints=args.discard_checkpoints
                )
                if args.reconcile
                else builder.check_publication_state(semantic_model)
            )
            print(json.dumps(report.summary(), ensure_ascii=False, indent=2))
            return 0 if report.clean else 1
        results = builder.build_from_file(args.config)
    except Exception as exc:
        print(f"QueryForge asset build failed: {exc}", file=sys.stderr)
        return 1
    payload = {"results": [result.model_dump() for result in results]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(result.status == "success" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
