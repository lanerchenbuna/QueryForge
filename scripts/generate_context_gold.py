"""Generate the context-dependent and compound NL2SQL gold set.

Why this file exists
--------------------
The original 120-case gold set has 12 cases carrying ``follow_up_context``, but **none of them
need it**: "Now count anime assigned to the Action genre" states its own metric and filter, so a
model that never reads the session history answers it correctly. Multi-turn context handling was
therefore effectively unmeasured (0 real cases), which makes any claim about it unfounded.

Two further problems were found while building this set, and both are why the references here are
deliberately written against the governed semantic model rather than against what the data
supports:

1. A reference SQL that joins a fact to a dimension through a path the semantic model does not
   declare (e.g. ``fact_watch_session`` -> ``bridge_anime_genre`` -> ``dim_genre``) is rejected by
   ``SemanticSQLValidator`` as a fan-out/join-key violation. A case whose own reference cannot pass
   the governed chain measures nothing.
2. A dimension outside the metric's ``allowed_dimensions`` (e.g. ``studio.tier`` for
   ``watch_hours``) is refused by ``metric_search`` with "Unsupported metric dimension
   combination". Same problem.

Both were caught by this generator's validation and by running the references through the real
governed tool, not by inspection.

Case shape
----------
* ``context_dependent`` — the follow-up is elliptical ("Break that down by region.") or
  referential ("Why is that so high?"). Without the prior turn the question has no metric or its
  referent is undefined. ``requires_context: true``.
* ``compound`` — two asks in one question. The reference encodes the measurement half; a complete
  answer must not silently drop the second ask. ``compound: true``.
* ``causal`` — a why/how-come question. No single SQL is correct, so the reference is the neutral
  current-state aggregate a defensible answer must be consistent with. ``causal: true``. These
  measure whether the system grounds an answer or invents a cause.

Regenerate with::

    python scripts/generate_context_gold.py

The generator refuses to write unless every reference (a) executes read-only through the governed
``DatabaseTool``, (b) returns a column set, (c) returns at least one row, and (d) passes
``SemanticSQLValidator`` when the case is governed. A reference that cannot satisfy the system's
own governance is not a usable gold answer.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT = PROJECT_ROOT / "evaluation" / "gold" / "context_and_compound.jsonl"

BASE = {
    "database": "sample_data/anime_streaming/anime_streaming.sqlite",
    "semantic_model": "sample_data/anime_streaming/semantic_model.yml",
    "sql_policy": "sample_data/anime_streaming/sql_policy.yml",
}

#: Joins the semantic model declares as safe, expressed as SQL. Every reference
#: below is built from these and nothing else.
JOIN_STUDIO = (
    "JOIN dim_episode e ON e.episode_id = w.episode_id "
    "JOIN dim_anime a ON a.anime_id = e.anime_id "
    "JOIN dim_studio s ON s.studio_id = a.studio_id"
)
JOIN_USER = (
    "JOIN dim_episode e ON e.episode_id = w.episode_id "
    "JOIN dim_anime a ON a.anime_id = e.anime_id "
    "JOIN dim_user u ON u.user_id = w.user_id"
)
#: Raw, unrounded expression. The evaluator declares a 10-decimal float policy, so a
#: reference that rounds first destroys the very precision the comparison relies on:
#: a model answering 3937.6125 would be scored wrong against a pre-rounded 3937.61.
#: References therefore state the expression faithfully and let the comparator apply
#: its own tolerance.
WATCH_HOURS = "SUM(w.watch_seconds) / 3600.0"


def _case(
    case_id: str,
    category: str,
    question: str,
    sql: str,
    *,
    context: list[str],
    requires_context: bool = False,
    compound: bool = False,
    causal: bool = False,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "id": case_id,
        **BASE,
        "domain": "anime_streaming",
        "category": category,
        "expected_outcome": "query",
        "question": question,
        "expected_sql": sql,
        "follow_up_context": context,
        # Candidate generation is off: these cases measure context and completeness,
        # not candidate selection, and the candidate mechanism was shown to add
        # latency without accuracy benefit (docs/evaluation_baselines.md).
        "candidate_selection": False,
        "requires_context": requires_context,
        "compound": compound,
        "causal": causal,
        "notes": notes,
    }


# --------------------------------------------------------------------------- cases

# Every dimension below is taken from the metric's allowed_dimensions, and every
# join from the declared join_paths. See the module docstring.
_CONTEXT_DEPENDENT: list[dict[str, Any]] = [
    _case(
        "ctx_01",
        "context_dependent",
        "Break that down by playback region.",
        f"SELECT w.playback_region, {WATCH_HOURS} AS watch_hours "
        "FROM fact_watch_session w GROUP BY w.playback_region "
        "ORDER BY watch_hours DESC",
        context=["Show total watch hours."],
        requires_context=True,
        notes="Metric only in the prior turn. playback_region is an allowed dimension.",
    ),
    _case(
        "ctx_02",
        "context_dependent",
        "And by anime release year?",
        f"SELECT a.release_year, {WATCH_HOURS} AS watch_hours "
        f"FROM fact_watch_session w JOIN dim_episode e ON e.episode_id = w.episode_id "
        f"JOIN dim_anime a ON a.anime_id = e.anime_id "
        "GROUP BY a.release_year ORDER BY a.release_year",
        context=["Show total watch hours."],
        requires_context=True,
        notes="Elliptical: 'And by ...' carries no metric. release_year is allowed.",
    ),
    _case(
        "ctx_03",
        "context_dependent",
        "Only APAC.",
        f"SELECT {WATCH_HOURS} AS watch_hours FROM fact_watch_session w "
        "WHERE w.playback_region = 'APAC'",
        context=["Show total watch hours by playback region."],
        requires_context=True,
        notes="Neither metric nor aggregation named; the referent is the prior request.",
    ),
    _case(
        "ctx_04",
        "context_dependent",
        "Only Major studios.",
        f"SELECT {WATCH_HOURS} AS watch_hours FROM fact_watch_session w "
        f"{JOIN_STUDIO} WHERE s.studio_tier = 'Major'",
        context=["Show total watch hours by studio."],
        requires_context=True,
        notes="Filter-only follow-up on studio.tier, which is allowed for watch_hours.",
    ),
    _case(
        "ctx_05",
        "context_dependent",
        "Top 5 only.",
        f"SELECT a.title, {WATCH_HOURS} AS watch_hours FROM fact_watch_session w "
        "JOIN dim_episode e ON e.episode_id = w.episode_id "
        "JOIN dim_anime a ON a.anime_id = e.anime_id "
        "GROUP BY a.title ORDER BY watch_hours DESC LIMIT 5",
        context=["Show watch hours by anime title."],
        requires_context=True,
        notes="A bare ranking follow-up: neither metric nor entity is restated.",
    ),
    _case(
        "ctx_06",
        "context_dependent",
        "Why is that so high?",
        f"SELECT {WATCH_HOURS} AS apac_watch_hours FROM fact_watch_session w "
        "WHERE w.playback_region = 'APAC'",
        context=[
            "Show total watch hours by playback region.",
            "APAC is the highest region.",
        ],
        requires_context=True,
        causal=True,
        notes="'that' resolves only against the previous turns; no metric is named.",
    ),
    _case(
        "ctx_07",
        "context_dependent",
        "Same thing but for Europe.",
        f"SELECT {WATCH_HOURS} AS watch_hours FROM fact_watch_session w "
        "WHERE w.playback_region = 'Europe'",
        context=["Show total watch hours by playback region. APAC is highest."],
        requires_context=True,
        notes="'Same thing' requires the prior metric AND aggregation.",
    ),
    _case(
        "ctx_08",
        "context_dependent",
        "Compare that with the other release cohort.",
        f"SELECT a.release_year, {WATCH_HOURS} AS watch_hours "
        "FROM fact_watch_session w "
        "JOIN dim_episode e ON e.episode_id = w.episode_id "
        "JOIN dim_anime a ON a.anime_id = e.anime_id "
        "WHERE a.release_year >= 2012 "
        "GROUP BY a.release_year ORDER BY a.release_year",
        context=["Show watch hours for anime released in 2019."],
        requires_context=True,
        notes="Comparison target and metric both come from context. Years present in the sample "
        "data are 1998/2005/2012/2019.",
    ),
    _case(
        "ctx_09",
        "context_dependent",
        "Break that down by product category.",
        "SELECT p.product_category, SUM(i.net_amount_usd) AS net_revenue "
        "FROM fact_merch_order_item i "
        "JOIN dim_merch_product p ON p.product_id = i.product_id "
        "GROUP BY p.product_category ORDER BY net_revenue DESC",
        context=["Show total merchandise net revenue."],
        requires_context=True,
        notes="Second domain, to avoid tuning to one shape. merch_product.category is allowed.",
    ),
    _case(
        "ctx_10",
        "context_dependent",
        "Only rewatched sessions.",
        f"SELECT {WATCH_HOURS} AS watch_hours FROM fact_watch_session w "
        "WHERE w.rewatch_flag = 1",
        context=["Show total watch hours."],
        requires_context=True,
    ),
    _case(
        "ctx_11",
        "context_dependent",
        "Now average rating instead.",
        "SELECT CAST(SUM(r.score) AS REAL) / NULLIF(COUNT(*), 0) AS average_rating "
        "FROM fact_rating r",
        context=["Show the total number of ratings."],
        requires_context=True,
        notes="Metric replacement on the same entity, which the question leaves implicit.",
    ),
    _case(
        "ctx_12",
        "context_dependent",
        "Just that one.",
        f"SELECT a.title, {WATCH_HOURS} AS watch_hours FROM fact_watch_session w "
        "JOIN dim_episode e ON e.episode_id = w.episode_id "
        "JOIN dim_anime a ON a.anime_id = e.anime_id "
        "GROUP BY a.title ORDER BY watch_hours DESC LIMIT 1",
        context=[
            "Show watch hours by anime title.",
            "The top title is the one we want to drill into.",
        ],
        requires_context=True,
        notes="Referential restriction with no metric, no dimension and no entity restated.",
    ),
]

_COMPOUND: list[dict[str, Any]] = [
    _case(
        "cmp_01",
        "compound",
        "How many anime are there in total, and how many ratings do they have?",
        "SELECT (SELECT COUNT(*) FROM dim_anime) AS anime_count, "
        "(SELECT COUNT(*) FROM fact_rating) AS rating_count",
        context=[],
        compound=True,
        notes="Two aggregates in one question; answering only the first is incomplete.",
    ),
    _case(
        "cmp_02",
        "compound",
        "Show total watch hours and the number of distinct viewers.",
        f"SELECT {WATCH_HOURS} AS watch_hours, "
        "COUNT(DISTINCT w.user_id) AS unique_viewers FROM fact_watch_session w",
        context=[],
        compound=True,
        notes="Volume plus reach.",
    ),
    _case(
        "cmp_03",
        "compound",
        "Compare average rating and average review length by anime format, and tell me "
        "which format does best.",
        "SELECT a.content_format, "
        "CAST(SUM(r.score) AS REAL) / NULLIF(COUNT(*), 0) AS average_rating, "
        "AVG(r.review_length) AS average_review_length "
        "FROM fact_rating r JOIN dim_anime a ON a.anime_id = r.anime_id "
        "GROUP BY a.content_format ORDER BY average_rating DESC",
        context=[],
        compound=True,
        notes="The measurement half is the oracle; 'which does best' is a judgement the answer "
        "must state rather than silently omit.",
    ),
    _case(
        "cmp_04",
        "compound",
        "What is merchandise net revenue by product category, and which category should we "
        "invest in next quarter?",
        "SELECT p.product_category, SUM(i.net_amount_usd) AS net_revenue "
        "FROM fact_merch_order_item i "
        "JOIN dim_merch_product p ON p.product_id = i.product_id "
        "GROUP BY p.product_category ORDER BY net_revenue DESC",
        context=[],
        compound=True,
        notes="Measurement plus a recommendation that the data does not settle.",
    ),
    _case(
        "cmp_05",
        "compound",
        "List the top 5 anime by watch hours, and give me the session counts too.",
        f"SELECT a.title, {WATCH_HOURS} AS watch_hours, COUNT(*) AS session_count "
        "FROM fact_watch_session w "
        "JOIN dim_episode e ON e.episode_id = w.episode_id "
        "JOIN dim_anime a ON a.anime_id = e.anime_id "
        "GROUP BY a.title ORDER BY watch_hours DESC LIMIT 5",
        context=[],
        compound=True,
        notes="Ranking plus a second measure; dropping the counts is a partial answer.",
    ),
    _case(
        "cmp_06",
        "compound",
        "How many users signed up in 2024, and how many of them activated a subscription?",
        "SELECT (SELECT COUNT(*) FROM dim_user u WHERE u.signup_date_key >= 20240101 "
        "AND u.signup_date_key <= 20241231) AS signups_2024, "
        "(SELECT COUNT(DISTINCT s.user_id) FROM fact_subscription s "
        "JOIN dim_user u ON u.user_id = s.user_id "
        "WHERE u.signup_date_key >= 20240101 AND u.signup_date_key <= 20241231) "
        "AS activated_subscribers",
        context=[],
        compound=True,
        notes="Funnel: two dependent counts in one ask.",
    ),
]

_CAUSAL: list[dict[str, Any]] = [
    _case(
        "cau_01",
        "causal",
        "Why did watch hours drop last month?",
        "SELECT d.year, d.month_number, "
        "SUM(w.watch_seconds) / 3600.0 AS watch_hours "
        "FROM fact_watch_session w "
        "JOIN dim_date d ON d.date_key = w.watch_date_key "
        "GROUP BY d.year, d.month_number ORDER BY d.year, d.month_number",
        context=[],
        causal=True,
        notes="No cause exists in the data. A defensible answer states the observed trend and "
        "either asks what changed or lists candidate explanations as hypotheses with evidence.",
    ),
    _case(
        "cau_02",
        "causal",
        "What is driving the growth in merchandise revenue?",
        "SELECT d.year, d.month_number, SUM(i.net_amount_usd) AS net_revenue "
        "FROM fact_merch_order_item i "
        "JOIN fact_merch_order o ON o.order_id = i.order_id "
        "JOIN dim_date d ON d.date_key = o.order_date_key "
        "GROUP BY d.year, d.month_number ORDER BY d.year, d.month_number",
        context=[],
        causal=True,
        notes="Same shape: measures the trend, must not invent a driver.",
    ),
    _case(
        "cau_03",
        "causal",
        "Users who watch more rate higher, right?",
        "SELECT CAST(SUM(r.score) AS REAL) / NULLIF(COUNT(*), 0) "
        "AS overall_average_rating FROM fact_rating r",
        context=[],
        causal=True,
        notes="Leading question with a false presupposition and no comparison in the data. A "
        "defensible answer declines to confirm it and states what would be needed to test it.",
    ),
]


def build() -> list[dict[str, Any]]:
    return [*_CONTEXT_DEPENDENT, *_COMPOUND, *_CAUSAL]


def validate(cases: list[dict[str, Any]]) -> list[str]:
    """Validate every reference through the real governed chain."""

    from queryforge.domain.semantic import SemanticModelLoader
    from queryforge.domain.semantic.sql_validator import SemanticSQLValidator
    from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
    from queryforge.infrastructure.tools.database_tool import DatabaseTool

    problems: list[str] = []
    database = PROJECT_ROOT / BASE["database"]
    model_path = PROJECT_ROOT / BASE["semantic_model"]
    with SQLiteConnector(str(database)) as connector:
        tool = DatabaseTool(connector)
        schemas = [tool.describe_table(name) for name in tool.list_tables()]
        for case in cases:
            sql = case["expected_sql"]
            try:
                result = tool.execute_sql(sql)
            except Exception as exc:  # noqa: BLE001 - any reference failure is a finding
                problems.append(f"{case['id']}: reference SQL failed: {exc}")
                continue
            if not result.columns:
                problems.append(f"{case['id']}: reference returned no columns")
            if not result.rows:
                problems.append(
                    f"{case['id']}: reference returned zero rows (unanswerable)"
                )
            if case.get("requires_context") and not case.get("follow_up_context"):
                problems.append(f"{case['id']}: requires_context with empty context")

            # Governance check: build a governed context so the semantic validator
            # can run exactly as ExecuteSqlNode would run it.
            try:
                from queryforge.core.schemas.models import Context, SqlTask

                semantic = SemanticModelLoader.load_and_validate(
                    model_path, schemas, case["question"]
                )
                matches = SemanticModelLoader.match_metrics(
                    semantic.model, case["question"]
                )
                governed = Context(
                    task=SqlTask(
                        question=case["question"], database_path=str(database)
                    ),
                    semantic_model=semantic,
                    metric_matches=[
                        m for m in matches if case.get("expected_sql")
                    ],
                )
                validator = SemanticSQLValidator.for_context(governed)
                if validator is not None:
                    verdict = validator.validate(sql)
                    if verdict.status == "violation":
                        problems.append(
                            f"{case['id']}: reference violates governance "
                            f"({','.join(verdict.rule_names)}): {verdict.unsupported_reason or verdict.error_message()}"
                        )
            except Exception as exc:  # noqa: BLE001 - surface, do not mask
                problems.append(f"{case['id']}: governance check errored: {exc}")
    return problems


def main() -> int:
    cases = build()
    problems = validate(cases)
    if problems:
        print("REFUSING TO WRITE — reference validation failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    OUTPUT.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )
    buckets = Counter(
        "requires_context"
        if c.get("requires_context")
        else "compound"
        if c.get("compound")
        else "causal"
        if c.get("causal")
        else "other"
        for c in cases
    )
    print(f"wrote {len(cases)} cases to {OUTPUT.relative_to(PROJECT_ROOT)}")
    for name, count in sorted(buckets.items()):
        print(f"  {name:18} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
