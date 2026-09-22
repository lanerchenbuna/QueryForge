"""Demo C — a multi-step analysis with evidence (step 17, Demo C).

Narrated, offline, reproducible: an ambiguous question is clarified instead of
guessed, the quality gate runs as a plan step, a period comparison and a
drill-down are computed and checked against independent SQLite queries, a
contribution request converges honestly to a period comparison, and every claim
in the answer is anchored to evidence.

Run: `.venv/bin/python docs/demo/run_demo_c.py`
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_lib import (  # noqa: E402
    ANIME_DATABASE,
    ANIME_MODEL,
    ANIME_POLICY,
    check,
    Demo,
    run_script,
    say,
)

TREND_QUESTION = "How did watch hours change month over month in 2024?"
MULTI_DIMENSION_QUESTION = "Watch hours by device vs the previous month in 2024"
DRILL_QUESTION = "What are the total watch hours by device?"
AMBIGUOUS_QUESTION = "What is revenue?"
EMPTY_WINDOW_QUESTION = "How many watch hours in Q1 1990?"


def _analyze(question: str) -> dict:
    body = (
        "import json;"
        "from queryforge.application.analysis_planner import AnalysisPlannerService;"
        "print(json.dumps(AnalysisPlannerService().analyze("
        f"{question!r}, database={str(ANIME_DATABASE)!r}, "
        f"semantic_model_path={str(ANIME_MODEL)!r}, sql_policy_path={str(ANIME_POLICY)!r}), "
        "ensure_ascii=False, default=str))"
    )
    code, stdout, stderr = run_script(["-c", body])
    start = stdout.find("{")
    if start < 0:
        raise AssertionError(f"analysis failed (exit={code}): {stderr[:300]}")
    return json.loads(stdout[start:])


def _evidence(payload: dict, kind: str) -> dict:
    for item in payload.get("evidence") or []:
        if item.get("kind") == kind:
            return dict(item.get("payload") or {})
    return {}


def _sql(sql: str) -> list[list]:
    with sqlite3.connect(f"{ANIME_DATABASE.as_uri()}?mode=ro", uri=True) as connection:
        return [list(row) for row in connection.execute(sql).fetchall()]


def main() -> int:
    with Demo("Demo C (governed multi-step analysis)") as _:
        say("C1: an ambiguous question is clarified, not guessed")
        ambiguous = _analyze(AMBIGUOUS_QUESTION)
        check(
            ambiguous["status"] == "needs_clarification",
            "the run stops and asks instead of inventing a metric",
            str(ambiguous["status"]),
        )
        check(
            bool(ambiguous.get("unresolved_questions")),
            "it says which question must be answered first",
            str(ambiguous["unresolved_questions"][0])[:90],
        )
        check(
            (ambiguous.get("plan") or {}).get("steps") == [],
            "and it runs no SQL at all",
        )

        say("C2: the quality gate runs as a plan step")
        drill = _analyze(DRILL_QUESTION)
        steps = [step["step_id"] for step in drill["steps"]]
        check("check_data_quality" in steps, "the plan contains the quality step", ", ".join(steps))
        quality = _evidence(drill, "data_quality")
        check(bool(quality), "and it produced data-quality evidence", str(quality.get("status")))
        say("   quality status", f"{quality.get('status')} (warnings are recorded, not hidden)")

        say("C3: a period comparison, checked against independent SQL")
        trend = _analyze(TREND_QUESTION)
        comparison = _evidence(trend, "period_comparison")
        metric_payload = _evidence(trend, "metric_value")
        check(trend["status"] == "succeeded", "the trend question succeeds", str(trend["status"]))
        check(bool(comparison), "a period comparison was computed", str(comparison.get("label")))
        current, baseline = comparison.get("current"), comparison.get("baseline")
        delta = float(current) - float(baseline)
        check(
            abs(float(comparison.get("delta", delta)) - delta) < 1e-6,
            "its delta equals current - baseline",
            f"delta={comparison.get('delta')}",
        )
        check(
            "ORDER BY dim_date.month_number" in str(metric_payload.get("sql") or ""),
            "the time series is ordered chronologically, not alphabetically",
            str(metric_payload.get("sql"))[-70:],
        )
        check(
            "watch_date_key = dim_date.date_key" in str(metric_payload.get("sql") or ""),
            "the calendar is joined on the metric's OWN time column (the watch date, not the episode release date)",
        )
        observed = {row[0]: round(float(row[1]), 6) for row in metric_payload.get("rows") or []}
        oracle = {
            name: round(float(value), 6)
            for name, value in _sql(
                "SELECT d.month_name, SUM(w.watch_seconds)/3600.0 FROM fact_watch_session w "
                "JOIN dim_date d ON d.date_key = w.watch_date_key "
                "WHERE w.watch_date_key BETWEEN 20240101 AND 20241231 GROUP BY d.month_name"
            )
        }
        check(
            observed == oracle,
            "every monthly value matches an independent SQLite computation",
            f"{len(observed)} months compared",
        )
        label_parts = [part.strip() for part in str(comparison.get("label") or "").split("->")]
        check(
            len(label_parts) == 2 and label_parts[0] != label_parts[1],
            "the comparison names two DIFFERENT periods",
            str(comparison.get("label")),
        )
        check(
            abs(float(current) - oracle.get(label_parts[1], 0)) < 1e-3
            and abs(float(baseline) - oracle.get(label_parts[0], 0)) < 1e-3,
            "and both sides equal the independent values of the named months",
            f"{label_parts[0]}={baseline} {label_parts[1]}={current}",
        )

        say("C4: a drill-down with bounded buckets")
        rows = _evidence(drill, "metric_value").get("rows") or []
        drill_payload = _evidence(drill, "drill_down")
        check(bool(rows), "the grouped metric returned buckets", f"{len(rows)} device buckets")
        check(
            bool(drill_payload.get("buckets")),
            "a drill-down breakdown was produced",
            f"buckets={len(drill_payload.get('buckets') or [])} coverage={drill_payload.get('coverage')}",
        )
        total = sum(float(row[1]) for row in rows)
        drill_total = sum(
            float(bucket.get("value") or 0) for bucket in drill_payload.get("buckets") or []
        )
        check(
            abs(drill_total + float(drill_payload.get("others") or 0) - total) < 1e-3,
            "buckets + others reconcile with the metric total (no silent loss)",
            f"{drill_total:.3f} + {drill_payload.get('others') or 0} == {total:.3f}",
        )

        say("C5: a breakdown AND a period comparison is refused, not faked")
        mixed = _analyze(MULTI_DIMENSION_QUESTION)
        mixed_comparison = _evidence(mixed, "period_comparison")
        mixed_metric = _evidence(mixed, "metric_value")
        label = str(mixed_comparison.get("label") or "")
        parts = [part.strip() for part in label.split("->")]
        dimensions = list(mixed_metric.get("dimensions") or [])
        say("   metric grain", f"dimensions={dimensions} rows={len(mixed_metric.get('rows') or [])}")
        if mixed_comparison:
            say("   comparison", f"{label} current={mixed_comparison.get('current')} baseline={mixed_comparison.get('baseline')}")
        check(
            not mixed_comparison or (len(parts) == 2 and parts[0] != parts[1]),
            "a two-dimension result never yields a same-period 'comparison' (that would compare two devices, not two months)",
            label or "no comparison produced",
        )
        check(
            not mixed_comparison or len(dimensions) <= 1,
            "if a comparison is produced at all, the metric grain is a single time dimension",
            f"comparison={'yes' if mixed_comparison else 'no'} dimensions={dimensions}",
        )
        say(
            "   limitation",
            "per-category two-period contribution decomposition is not expressible with the governed compiler; "
            "the honest outcome is a refusal or a single-dimension comparison",
        )

        say("C6: an empty window is reported as a gap, never as a zero answer")
        empty = _analyze(EMPTY_WINDOW_QUESTION)
        check(
            empty["status"] == "partial",
            "the run is partial, not successful",
            str(empty["status"]),
        )
        check(empty.get("answer") is None, "and no answer is composed for an empty slice")
        check(
            bool(empty.get("replan_reasons")),
            "the attempts are recorded",
            f"{len(empty['replan_reasons'])} bounded replan(s)",
        )

        say("C7: every claim in the drill-down answer is anchored to evidence")
        known = {item["evidence_id"] for item in drill["evidence"] if item.get("evidence_id")}
        findings = (drill.get("final_answer") or {}).get("findings") or []
        anchored = [
            finding
            for finding in findings
            if finding.get("evidence_ids")
            and all(str(item) in known for item in finding["evidence_ids"])
        ]
        check(bool(findings), "the answer carries findings", f"{len(findings)} findings")
        check(
            len(anchored) == len(findings),
            "each finding references evidence produced by this run",
            f"{len(anchored)}/{len(findings)}",
        )
    print("\n[demo] Demo C complete: clarification, quality, trend, drill-down and gaps all verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
