"""Demo A — uploaded data decides the answer (step 17, Demo A).

Narrated, offline, reproducible:

1. a broken upload is refused *before* anything is published, with the offending
   rows quarantined rather than silently dropped;
2. the same contract with a corrected file publishes;
3. the published table is queried through the governed path, and the answer is
   derived from the uploaded rows (change the file, the number changes);
4. a failed upload never pollutes the previously published version.

Run: `.venv/bin/python docs/demo/run_demo_a.py` (exit 0 = every claim held).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_lib import (  # noqa: E402
    ASSET_CSV,
    PROJECT_ROOT,
    check,
    copy_asset_config,
    Demo,
    run_script,
    say,
)

GOOD_ROWS = [
    "evt-2001,Azure Voyager 001,2026-01-01,1800,Mobile",
    "evt-2002,Crimson Voyager 002,2026-01-01,1800,Web",
    "evt-2003,Neon Chronicle 003,2026-01-02,1800,TV",
]
#: The rows are dated after every good batch, so the watermark filter does not
#: hide them: this batch really is offered to the quality gate and refused.
BROKEN_ROWS = [
    "evt-3001,Azure Voyager 001,2026-02-01,1800,Mobile",
    "evt-3002,,2026-02-01,1800,Web",  # missing required anime_title
    "evt-3003,Neon Chronicle 003,2026-02-02,-5,TV",  # violates the range rule
]


def _build(workspace: Path, config: Path, database: Path, state: Path) -> tuple[int, dict]:
    code, stdout, stderr = run_script(
        [
            "scripts/build_data_assets.py",
            "--config",
            str(config),
            "--publish-database",
            str(database),
            "--state-root",
            str(state),
        ]
    )
    payload = {}
    start = stdout.find("{")
    if start >= 0:
        payload = json.loads(stdout[start:])
    return code, {"payload": payload, "stderr": stderr.strip()}


def _query(database: Path, model: Path) -> tuple[int, dict]:
    code, stdout, stderr = run_script(
        [
            "-c",
            "import json,sys;from queryforge.application.analysis_planner import "
            "AnalysisPlannerService;"
            "print(json.dumps(AnalysisPlannerService().analyze("
            "'What are the total uploaded watch hours?',"
            f"database={str(database)!r},semantic_model_path={str(model)!r}), "
            "ensure_ascii=False))",
        ]
    )
    payload = {}
    start = stdout.find("{")
    if start >= 0:
        payload = json.loads(stdout[start:])
    return code, {"payload": payload, "stderr": stderr.strip()}


def main() -> int:
    with Demo("Demo A (upload decides the answer)") as demo:
        root = demo.root
        database = root / "analytics.sqlite"
        state = root / "asset_state"

        say("A1: upload a file that breaks the contract", "missing title + out-of-range seconds")
        broken_config = copy_asset_config(root, csv_rows=BROKEN_ROWS)
        code, result = _build(root, broken_config, database, state)
        check(code != 0, "the broken upload is refused", f"exit={code}")
        failures = [
            item for item in result["payload"].get("results", []) if item.get("status") != "success"
        ]
        check(bool(failures), "the batch reports failure instead of a silent partial publish")
        failed = failures[0]
        check(
            int(failed.get("quarantined_rows") or 0) >= 1,
            "the refusal reports how many rows were rejected",
            f"input={failed.get('input_rows')} staged={failed.get('staged_rows')} "
            f"quarantined={failed.get('quarantined_rows')}",
        )
        check(
            "invalid ratio" in str(failed.get("error") or ""),
            "the reason names the rule that refused the batch",
            str(failed.get("error"))[:90],
        )
        with sqlite3.connect(state / "metadata.sqlite") as connection:
            quarantined = connection.execute(
                "SELECT asset_name, reason, raw_record_json FROM asset_quarantine"
            ).fetchall()
            watermarks = connection.execute(
                "SELECT COUNT(*) FROM asset_watermarks"
            ).fetchone()[0]
        # The batch rolls the metadata database back to its checkpoint, so the
        # quarantine rows are replayed afterwards: a failed batch keeps exactly the
        # evidence of why it failed and no partially applied state.
        check(
            len(quarantined) >= 1,
            "the rejected rows survive the rollback so an operator can fix the file",
            f"{len(quarantined)} row(s)",
        )
        say("   rejected row", (quarantined[0][1][:60] + " | " + quarantined[0][2][:70]) if quarantined else "-")
        check(watermarks == 0, "no watermark was advanced by the failed batch", f"watermarks={watermarks}")
        check(
            not database.is_file() or _table_rows(database, "fact_watch_events") == 0,
            "nothing was published by the failed batch",
            f"rows={_table_rows(database, 'fact_watch_events')}",
        )

        say("A2: fix the file, rebuild", "same contract, corrected rows")
        good_config = copy_asset_config(root, csv_rows=GOOD_ROWS)
        code, result = _build(root, good_config, database, state)
        check(code == 0, "the corrected upload publishes", f"exit={code}")
        statuses = [item.get("status") for item in result["payload"].get("results", [])]
        check(statuses == ["success"], "the batch is atomic and successful", str(statuses))
        published_rows = _table_rows(database, "fact_watch_events")
        check(published_rows == 3, "all three uploaded rows are published", f"rows={published_rows}")
        semantic_model = Path(result["payload"]["results"][0].get("semantic_model_path") or "")
        check(semantic_model.is_file(), "a reviewed semantic model was generated", str(semantic_model.name))

        say("A3: query the published data through the governed path")
        code, query = _query(database, semantic_model)
        value = (query["payload"].get("answer") or {}).get("value")
        check(code == 0 and query["payload"].get("status") == "succeeded", "the query succeeds", str(query["payload"].get("status")))
        check(abs(float(value) - 1.5) < 1e-9, "the answer equals the uploaded data (3 x 1800s = 1.5 h)", f"value={value}")

        say("A4: the uploaded file decides the number")
        changed_config = copy_asset_config(
            root, csv_rows=[*GOOD_ROWS, "evt-2004,Azure Voyager 001,2026-01-03,1800,Mobile"]
        )
        code, result = _build(root, changed_config, database, state)
        check(code == 0, "the second upload publishes", f"exit={code}")
        code, query = _query(database, semantic_model)
        new_value = (query["payload"].get("answer") or {}).get("value")
        check(
            abs(float(new_value) - 2.0) < 1e-9,
            "the answer follows the file (4 x 1800s = 2.0 h), so results are not canned",
            f"value={new_value}",
        )

        say("A5: a failing batch cannot damage the published version")
        rows_before = _table_rows(database, "fact_watch_events")
        broken_again = copy_asset_config(root, csv_rows=BROKEN_ROWS)
        code, result = _build(root, broken_again, database, state)
        check(code != 0, "the bad batch is refused again", f"exit={code}")
        check(
            _table_rows(database, "fact_watch_events") == rows_before,
            "the published rows are untouched after the refusal",
            f"rows={_table_rows(database, 'fact_watch_events')}",
        )
        code, query = _query(database, semantic_model)
        check(
            abs(float((query["payload"].get("answer") or {}).get("value")) - 2.0) < 1e-9,
            "the published version still answers correctly (17-E2E1)",
        )
    print("\n[demo] Demo A complete: every claim above was checked against the real pipeline.")
    return 0


def _table_rows(database: Path, table: str) -> int:
    if not database.is_file():
        return 0
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
