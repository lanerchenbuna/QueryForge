"""Validate QueryForge's bundled synthetic anime streaming sample."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sample.generate_anime_streaming import EXPECTED_COUNTS, TABLES


SAMPLE_ROOT = PROJECT_ROOT / "sample_data" / "anime_streaming"
DATABASE = SAMPLE_ROOT / "anime_streaming.sqlite"
SEMANTIC_MODEL = SAMPLE_ROOT / "semantic_model.yml"
SUBJECT_TREE = SAMPLE_ROOT / "subjects.yml"
SQL_POLICY = SAMPLE_ROOT / "sql_policy.yml"
SUCCESS_STORY = SAMPLE_ROOT / "success_story.csv"
REFERENCE_SQL = SAMPLE_ROOT / "reference_sql"


def main() -> int:
    problems: list[str] = []
    counts: dict[str, int] = {}
    if not DATABASE.is_file():
        problems.append(f"missing database: {DATABASE}")
    else:
        try:
            with closing(
                sqlite3.connect(f"{DATABASE.as_uri()}?mode=ro", uri=True)
            ) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                missing = set(TABLES) - tables
                if missing:
                    problems.append("database is missing tables: " + ", ".join(sorted(missing)))
                for table in sorted(set(TABLES) & tables):
                    count = int(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0]
                    )
                    counts[table] = count
                    if count != EXPECTED_COUNTS[table]:
                        problems.append(
                            f"{table} has {count:,} rows; expected "
                            f"{EXPECTED_COUNTS[table]:,}"
                        )
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    problems.append(
                        f"database has foreign-key violations: {violations[:3]}"
                    )
        except sqlite3.Error as exc:
            problems.append(f"database could not be inspected: {exc}")

    for path, label in (
        (SEMANTIC_MODEL, "semantic model"),
        (SUBJECT_TREE, "subject tree"),
        (SQL_POLICY, "SQL policy"),
        (SUCCESS_STORY, "success story"),
    ):
        if not path.is_file():
            problems.append(f"missing {label}: {path}")

    reference_queries = list(REFERENCE_SQL.glob("*.sql")) if REFERENCE_SQL.is_dir() else []
    if not reference_queries:
        problems.append(f"missing reference SQL files in: {REFERENCE_SQL}")
    if problems:
        print("Bundled anime streaming sample is incomplete:")
        for problem in problems:
            print(f"- {problem}")
        print("Regenerate it with: python sample/generate_anime_streaming.py")
        return 1

    print(f"Anime streaming database is ready: {DATABASE}")
    print(
        f"Tables: {len(TABLES)} · Rows: {sum(counts.values()):,} · "
        f"Reference SQL: {len(reference_queries)}"
    )
    print(
        "Largest facts: "
        + ", ".join(
            f"{table} ({counts[table]:,})"
            for table in (
                "fact_watch_session",
                "fact_ad_impression",
                "fact_rating",
                "fact_merch_order_item",
                "fact_user_follow",
            )
        )
    )
    print(f"Semantic model: {SEMANTIC_MODEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
