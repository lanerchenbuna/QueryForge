"""Build or incrementally refresh a governed QueryForge semantic model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.domain.semantic.builder import SemanticBuildError, SemanticModelBuilder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Infer a semantic model from SQLite, merge curated definitions, "
            "validate contracts, and publish atomically."
        )
    )
    parser.add_argument("--database", required=True, help="SQLite database path")
    parser.add_argument(
        "--output",
        help="Published YAML path (default: semantic_model.yml beside the database)",
    )
    parser.add_argument(
        "--existing",
        help=(
            "Curated model to merge. When omitted and --output already exists, "
            "the output file is used automatically."
        ),
    )
    parser.add_argument("--name", help="Semantic model name")
    parser.add_argument("--description", help="Semantic model description")
    parser.add_argument("--owner", default="data-platform", help="Default inferred owner")
    parser.add_argument(
        "--report",
        help="Inference and validation JSON report path",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Ignore an existing output model and generate a fresh scaffold",
    )
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="Write and validate a .draft.yml file without publishing it",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="Skip bounded row/null/uniqueness profiling",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database = Path(args.database).expanduser().resolve()
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else database.parent / "semantic_model.yml"
    )
    existing = args.existing
    if not args.replace and not existing and output.is_file():
        existing = str(output)
    try:
        result = SemanticModelBuilder(
            database,
            owner=args.owner,
            profile=not args.no_profile,
        ).build(
            output,
            existing_model_path=existing,
            report_path=args.report,
            name=args.name,
            description=args.description,
            publish=not args.draft_only,
        )
    except (SemanticBuildError, ValueError, OSError) as exc:
        print(f"Semantic build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    if not result.contract_passed:
        print(
            "Semantic publication was blocked by contract failures; inspect "
            f"{result.report_path}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
