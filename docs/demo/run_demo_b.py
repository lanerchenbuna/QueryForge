"""Demo B — a legal query that answers the wrong question is caught (step 17, Demo B).

Narrated, offline, reproducible:

1. a hand-written SQL statement runs successfully but answers a different business
   question (seconds instead of governed hours);
2. the semantic validator locates the violation with a rule name and a readable
   reason;
3. the governed compiler renders the correct statement, which is executed and
   checked against an independent SQLite computation;
4. the evidence-anchored answer references its evidence, so a number cannot be
   invented (and a beauty-only answer would fail the same check);
5. an honest limitation discovered by this demo: a hand-rolled fan-out join is
   *not* caught by the validator — fan-out is prevented by refusing to compile an
   unsafe join path, not by validating arbitrary SQL.

Run: `.venv/bin/python docs/demo/run_demo_b.py`
"""

from __future__ import annotations

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

QUESTION = "What are the total watch hours by device?"
#: Executable, plausible, and wrong: it sums *seconds* while the governed metric
#: `watch_hours` is defined as SUM(watch_seconds) / 3600.0.
WRONG_SQL = (
    "SELECT device_type, SUM(watch_seconds) AS watch_hours "
    "FROM fact_watch_session GROUP BY device_type LIMIT 100"
)
#: A hand-rolled join that multiplies the fact grain (one row per session turned
#: into one row per session x anime). It is used only to document the limitation.
FANOUT_SQL = (
    "SELECT d.title, SUM(w.watch_seconds) / 3600.0 AS watch_hours "
    "FROM fact_watch_session w JOIN dim_anime d ON 1=1 GROUP BY d.title LIMIT 100"
)


def main() -> int:
    with Demo("Demo B (semantic validation catches a legal but wrong query)") as demo:
        payload = _analyze(QUESTION)
        check(
            payload.get("status") == "succeeded",
            "the governed path answers the question",
            str(payload.get("status")),
        )
        governed_sql = _metric_sql(payload)
        governed_rows = _metric_rows(payload)
        say("B0: the governed answer", f"{len(governed_rows)} buckets")

        say("B1: run the hand-written SQL that answers a different question")
        wrong_rows = _execute(WRONG_SQL)
        check(bool(wrong_rows), "the wrong SQL is perfectly executable", f"{len(wrong_rows)} buckets")
        check(
            _scaled_by_3600(wrong_rows, governed_rows),
            "its totals are 3600x the governed ones (seconds vs hours), i.e. plausible but wrong",
        )
        say("   wrong vs governed", f"{wrong_rows[0]} vs {governed_rows[0]}")

        say("B2: the semantic validator locates the business-semantic violation")
        validation = _validate(WRONG_SQL)
        check(validation["status"] == "violation", "the wrong SQL is rejected", validation["status"])
        check(
            "metric_expression" in validation["rules"],
            "the rule that fired is named",
            ", ".join(validation["rules"]),
        )
        say("   reason", validation["message"][:150])

        say("B3: the governed compiler renders the correct statement")
        check(
            "fact_watch_session.device_type" in governed_sql and "/ 3600.0" in governed_sql,
            "the compiled SQL groups by the entity dimension and applies the governed formula",
            governed_sql[:110],
        )
        oracle = _independent_hours_by_device()
        observed = {row[0]: round(float(row[1]), 6) for row in governed_rows}
        check(
            observed == oracle,
            "its result matches an independent SQLite computation exactly",
            f"{len(observed)} buckets compared",
        )

        say("B4: numbers in the answer are anchored to evidence")
        anchored, total_numbers = _anchoring_report(payload)
        check(total_numbers > 0, "the answer carries claims", f"{total_numbers} findings/conclusions")
        check(
            anchored == total_numbers,
            "every claim in the final answer references evidence that exists in this run (a pretty answer without evidence fails this check)",
            f"{anchored}/{total_numbers}",
        )

        say("B5: where fan-out prevention actually lives")
        check(
            _fanout_inflates(FANOUT_SQL),
            "a hand-rolled 1=1 join really does inflate the metric",
            "the inflated total is far above the governed total",
        )
        fanout_validated = _validate(FANOUT_SQL, requested_dimension="anime.title")
        say(
            "   same statement, validated against the joined dimension",
            f"validator status={fanout_validated['status']} (rules={fanout_validated['rules']})",
        )
        check(
            fanout_validated["status"] == "passed",
            "the validator does NOT detect the multiplication on its own (honest gap, stated here rather than hidden)",
            "it catches a grain mismatch (see below), not a bad join",
        )
        mismatched = _validate(FANOUT_SQL)
        check(
            "grain" in mismatched["rules"],
            "it does catch a statement whose grouped grain contradicts the requested dimensions",
            ", ".join(mismatched["rules"]),
        )
        unsafe = _resolve_join("merch_order", "merch_order_item")
        check(
            unsafe["resolved"] and not unsafe["safe"],
            "and the real prevention is compile time: an order-grain metric may not be joined to its line items",
            "; ".join(unsafe["fanout_steps"])[:120],
        )
        say("   refusal reason", "; ".join(unsafe["fanout_steps"])[:150])
    print("\n[demo] Demo B complete: the violation was located, the governed SQL verified against an oracle.")
    return 0


def _analyze(question: str) -> dict:
    code, result = _service_call(
        "from queryforge.application.analysis_planner import AnalysisPlannerService;"
        "print(json.dumps(AnalysisPlannerService().analyze("
        f"{question!r}, database={str(ANIME_DATABASE)!r}, "
        f"semantic_model_path={str(ANIME_MODEL)!r}, sql_policy_path={str(ANIME_POLICY)!r}), "
        "ensure_ascii=False, default=str))"
    )
    if code != 0:
        raise AssertionError(result)
    return result


def _service_call(body: str) -> tuple[int, dict]:
    import json

    prelude = "import json;"
    code, stdout, stderr = run_script(["-c", prelude + body])
    start = stdout.find("{")
    if start < 0:
        raise AssertionError(f"no JSON payload (exit={code}): {stderr[:300]}")
    return code, json.loads(stdout[start:])


def _metric_sql(payload: dict) -> str:
    for evidence in payload.get("evidence") or []:
        if evidence.get("kind") == "metric_value":
            return str((evidence.get("payload") or {}).get("sql") or "")
    return ""


def _metric_rows(payload: dict) -> list[list]:
    for evidence in payload.get("evidence") or []:
        if evidence.get("kind") == "metric_value":
            return list((evidence.get("payload") or {}).get("rows") or [])
    return []


def _execute(sql: str) -> list[list]:
    with sqlite3.connect(f"{ANIME_DATABASE.as_uri()}?mode=ro", uri=True) as connection:
        return [list(row) for row in connection.execute(sql).fetchall()]


def _scaled_by_3600(wrong: list[list], governed: list[list]) -> bool:
    if len(wrong) != len(governed) or not wrong:
        return False
    by_device = {row[0]: float(row[1]) for row in governed}
    for row in wrong:
        reference = by_device.get(row[0])
        if reference is None or abs(float(row[1]) / reference - 3600.0) > 1.0:
            return False
    return True


def _resolve_join(base: str, target: str) -> dict:
    """Whether the semantic model lets a metric at ``base`` grain reach ``target``."""
    body = f"""
from pathlib import Path
from queryforge.domain.semantic.model import SemanticModelLoader
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool
root = Path({str(ANIME_DATABASE.parent)!r})
with SQLiteConnector(str(root / "anime_streaming.sqlite")) as connector:
    tool = DatabaseTool(connector)
    schemas = [tool.describe_table(name) for name in tool.list_tables()]
context = SemanticModelLoader.load_and_validate(str(root / "semantic_model.yml"), schemas, "fanout probe")
resolved = SemanticModelLoader.resolve_join_path(context.model, {base!r}, {target!r})
print(json.dumps({{
    "resolved": resolved is not None,
    "safe": bool(resolved.safe) if resolved else None,
    "fanout_steps": list(resolved.fanout_steps) if resolved else [],
}}))
"""
    code, payload = _service_call(body)
    if code != 0:
        raise AssertionError(payload)
    return payload


def _validate(sql: str, *, requested_dimension: str = "watch_session.device") -> dict:
    body = f"""
from pathlib import Path
from queryforge.domain.semantic.model import SemanticModelLoader
from queryforge.domain.semantic.schemas import MetricMatch
from queryforge.domain.semantic.sql_validator import SemanticSQLValidator
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool
root = Path({str(ANIME_DATABASE.parent)!r})
with SQLiteConnector(str(root / "anime_streaming.sqlite")) as connector:
    tool = DatabaseTool(connector)
    schemas = [tool.describe_table(name) for name in tool.list_tables()]
context = SemanticModelLoader.load_and_validate(str(root / "semantic_model.yml"), schemas, {QUESTION!r})
metric = next(m for m in context.model.metrics if m.name == "watch_hours")
result = SemanticSQLValidator(
    context, [MetricMatch(matched_term="watch hours", metric=metric)],
    requested_dimensions=[{requested_dimension!r}],
).validate({sql!r})
print(json.dumps({{"status": result.status, "rules": result.rule_names, "message": result.summary()}}))
"""
    code, payload = _service_call(body)
    if code != 0:
        raise AssertionError(payload)
    return payload


def _independent_hours_by_device() -> dict[str, float]:
    rows = _execute(
        "SELECT device_type, ROUND(SUM(watch_seconds)/3600.0, 6) "
        "FROM fact_watch_session GROUP BY device_type"
    )
    return {row[0]: round(float(row[1]), 6) for row in rows}


def _anchoring_report(payload: dict) -> tuple[int, int]:
    """(claims anchored to real evidence ids, total claims) in the answer layer.

    A grouped metric answer carries one evidence reference per finding rather than
    one per number (the numbers live in the evidence payload), so the claim being
    checked is: every finding references evidence that really exists in this run.
    """
    from queryforge.evaluation.evaluator import final_answer_of

    answer = final_answer_of(payload)
    known = {
        str(item.get("evidence_id"))
        for item in payload.get("evidence") or []
        if item.get("evidence_id")
    }
    anchored = 0
    total = 0
    for finding in answer.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        total += 1
        ids = [str(item) for item in (finding.get("evidence_ids") or []) if item]
        for number in finding.get("numbers") or []:
            if isinstance(number, dict) and number.get("evidence_id"):
                ids.append(str(number["evidence_id"]))
        if ids and all(item in known for item in ids):
            anchored += 1
    if not total:
        for conclusion in answer.get("conclusions") or []:
            total += 1
            ids = [str(item) for item in (conclusion.get("evidence_ids") or [])] if isinstance(conclusion, dict) else []
            if ids and all(item in known for item in ids):
                anchored += 1
    return anchored, total


def _fanout_inflates(sql: str) -> bool:
    rows = _execute(sql)
    total = sum(float(row[1]) for row in rows)
    governed_total = sum(_independent_hours_by_device().values())
    return total > governed_total * 1.5


if __name__ == "__main__":
    raise SystemExit(main())
