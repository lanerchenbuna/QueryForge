"""Generate the checked-in 120-case anime-platform NL2SQL gold set."""

from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "evaluation" / "gold" / "nl2sql_multidomain.jsonl"
BASE = {
    "database": "sample_data/anime_streaming/anime_streaming.sqlite",
    "semantic_model": "sample_data/anime_streaming/semantic_model.yml",
    "sql_policy": "sample_data/anime_streaming/sql_policy.yml",
}
CONTENT = {"domain": "anime_content", **BASE}
ENGAGEMENT = {"domain": "viewer_engagement", **BASE}
MONETIZATION = {"domain": "platform_monetization", **BASE}


def _query(
    scope: dict[str, str],
    index: int,
    category: str,
    question: str,
    sql: str,
    *,
    follow_up_context: list[str] | None = None,
) -> dict:
    return {
        "id": f"{scope['domain']}_{index:02d}",
        **scope,
        "category": category,
        "expected_outcome": "query",
        "question": question,
        "expected_sql": sql,
        "follow_up_context": follow_up_context or [],
        "candidate_selection": category in {"multi_table", "metric"},
    }


def _rejections(scope: dict[str, str], start: int, table: str) -> list[dict]:
    probes = [
        ("Delete all rows", f"DELETE FROM {table}"),
        ("Drop the table", f"DROP TABLE {table}"),
        ("Insert a test row", f"INSERT INTO {table} DEFAULT VALUES"),
        ("Update every row", f"UPDATE {table} SET 1 = 1"),
        ("Attach another database", "ATTACH DATABASE '/tmp/other.sqlite' AS other"),
        (
            "Run a recursive query",
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT * FROM t",
        ),
        ("Run two statements", f"SELECT 1; DELETE FROM {table}"),
        ("Vacuum the database", "VACUUM"),
    ]
    return [
        {
            "id": f"{scope['domain']}_{start + offset:02d}",
            **scope,
            "category": "policy_rejection",
            "expected_outcome": "policy_rejection",
            "question": question,
            "policy_probe_sql": sql,
            "candidate_selection": False,
        }
        for offset, (question, sql) in enumerate(probes)
    ]


def _content_cases() -> list[dict]:
    cases = []
    for index, limit in enumerate(range(1, 11), start=1):
        cases.append(
            _query(
                CONTENT,
                index,
                "single_table",
                f"List the first {limit} anime titles alphabetically.",
                f"SELECT title FROM dim_anime ORDER BY title LIMIT {limit}",
            )
        )
    for offset, tier in enumerate(
        ("Major", "Growth", "Indie", "Major", "Growth", "Indie", "Major", "Growth"),
        start=11,
    ):
        cases.append(
            _query(
                CONTENT,
                offset,
                "multi_table",
                f"Show anime titles produced by {tier} studios.",
                "SELECT a.title, s.studio_name, s.studio_tier "
                "FROM dim_anime a JOIN dim_studio s ON a.studio_id = s.studio_id "
                f"WHERE s.studio_tier = '{tier}' ORDER BY a.title",
            )
        )
    for offset, year in enumerate(range(2020, 2026), start=19):
        cases.append(
            _query(
                CONTENT,
                offset,
                "time",
                f"Count anime released in {year} or later.",
                f"SELECT COUNT(*) AS anime_count FROM dim_anime WHERE release_year >= {year}",
            )
        )
    metrics = (
        (
            "average audience rating by anime format",
            "SELECT a.content_format, AVG(r.score) AS average_rating "
            "FROM fact_rating r JOIN dim_anime a ON r.anime_id = a.anime_id "
            "GROUP BY a.content_format",
        ),
        (
            "anime count by studio country",
            "SELECT s.country, COUNT(DISTINCT a.anime_id) AS anime_count "
            "FROM dim_anime a JOIN dim_studio s ON a.studio_id = s.studio_id "
            "GROUP BY s.country",
        ),
        (
            "episode count by anime format",
            "SELECT a.content_format, COUNT(e.episode_id) AS episode_count "
            "FROM dim_episode e JOIN dim_anime a ON e.anime_id = a.anime_id "
            "GROUP BY a.content_format",
        ),
        (
            "average production budget by source material",
            "SELECT source_material, AVG(production_budget_usd) AS average_budget "
            "FROM dim_anime GROUP BY source_material",
        ),
    )
    for offset, (label, sql) in enumerate(metrics, start=25):
        cases.append(_query(CONTENT, offset, "metric", f"Calculate {label}.", sql))
    for offset, genre in enumerate(
        ("Action", "Romance", "Sci-Fi", "Slice of Life"), start=29
    ):
        cases.append(
            _query(
                CONTENT,
                offset,
                "follow_up",
                f"Now count anime assigned to the {genre} genre.",
                "SELECT COUNT(DISTINCT b.anime_id) AS anime_count "
                "FROM bridge_anime_genre b JOIN dim_genre g "
                "ON b.genre_id = g.genre_id "
                f"WHERE g.genre_name = '{genre}'",
                follow_up_context=["Show anime counts by genre."],
            )
        )
    return cases + _rejections(CONTENT, 33, "dim_anime")


def _engagement_cases() -> list[dict]:
    fields = (
        "watch_session_id",
        "user_id",
        "episode_id",
        "watch_date_key",
        "device_type",
        "playback_region",
        "watch_seconds",
        "completion_pct",
        "completed_flag",
        "rewatch_flag",
    )
    cases = [
        _query(
            ENGAGEMENT,
            index,
            "single_table",
            f"Show the first five watch-session {field} values.",
            f"SELECT {field} FROM fact_watch_session ORDER BY watch_session_id LIMIT 5",
        )
        for index, field in enumerate(fields, start=1)
    ]
    formats = ("Series", "Movie", "OVA", "ONA", "Series", "Movie", "OVA", "ONA")
    for offset, content_format in enumerate(formats, start=11):
        cases.append(
            _query(
                ENGAGEMENT,
                offset,
                "multi_table",
                f"Show watch hours for {content_format} anime.",
                "SELECT a.content_format, SUM(w.watch_seconds) / 3600.0 AS watch_hours "
                "FROM fact_watch_session w "
                "JOIN dim_episode e ON w.episode_id = e.episode_id "
                "JOIN dim_anime a ON e.anime_id = a.anime_id "
                f"WHERE a.content_format = '{content_format}' "
                "GROUP BY a.content_format",
            )
        )
    for offset, month in enumerate(range(1, 7), start=19):
        cases.append(
            _query(
                ENGAGEMENT,
                offset,
                "time",
                f"Count watch sessions in calendar month {month}.",
                "SELECT COUNT(*) AS session_count FROM fact_watch_session w "
                "JOIN dim_date d ON w.watch_date_key = d.date_key "
                f"WHERE d.month_number = {month}",
            )
        )
    metrics = (
        "SELECT SUM(watch_seconds) / 3600.0 AS watch_hours FROM fact_watch_session",
        "SELECT COUNT(DISTINCT user_id) AS unique_viewers FROM fact_watch_session",
        "SELECT CAST(SUM(completed_flag) AS REAL) / COUNT(*) AS completion_rate FROM fact_watch_session",
        "SELECT AVG(score) AS average_rating FROM fact_rating",
    )
    for offset, sql in enumerate(metrics, start=25):
        cases.append(
            _query(
                ENGAGEMENT,
                offset,
                "metric",
                f"Calculate engagement metric {offset - 24}.",
                sql,
            )
        )
    for offset, region in enumerate(
        ("APAC", "Europe", "North America", "Latin America"), start=29
    ):
        cases.append(
            _query(
                ENGAGEMENT,
                offset,
                "follow_up",
                f"Now show unique viewers in {region}.",
                "SELECT playback_region, COUNT(DISTINCT user_id) AS unique_viewers "
                "FROM fact_watch_session "
                f"WHERE playback_region = '{region}' GROUP BY playback_region",
                follow_up_context=["Show unique viewers by playback region."],
            )
        )
    return cases + _rejections(ENGAGEMENT, 33, "fact_watch_session")


def _monetization_cases() -> list[dict]:
    subscription_fields = (
        "subscription_id",
        "user_id",
        "plan_name",
        "billing_cycle",
        "status",
        "monthly_price_usd",
        "discount_usd",
        "recognized_revenue_usd",
        "start_date_key",
        "end_date_key",
    )
    cases = [
        _query(
            MONETIZATION,
            index,
            "single_table",
            f"Show the first five subscription {field} values.",
            f"SELECT {field} FROM fact_subscription ORDER BY subscription_id LIMIT 5",
        )
        for index, field in enumerate(subscription_fields, start=1)
    ]
    plans = ("Fan", "Premium", "Family", "Fan", "Premium", "Family", "Fan", "Premium")
    for offset, plan in enumerate(plans, start=11):
        cases.append(
            _query(
                MONETIZATION,
                offset,
                "multi_table",
                f"Show subscription revenue for the {plan} plan by viewer region.",
                "SELECT u.region, SUM(s.recognized_revenue_usd) AS subscription_revenue "
                "FROM fact_subscription s JOIN dim_user u ON s.user_id = u.user_id "
                f"WHERE s.plan_name = '{plan}' GROUP BY u.region",
            )
        )
    for offset, month in enumerate(range(1, 7), start=19):
        cases.append(
            _query(
                MONETIZATION,
                offset,
                "time",
                f"Show ad revenue in calendar month {month}.",
                "SELECT SUM(a.revenue_usd) AS ad_revenue "
                "FROM fact_ad_impression a JOIN dim_date d "
                "ON a.impression_date_key = d.date_key "
                f"WHERE d.month_number = {month}",
            )
        )
    metrics = (
        "SELECT SUM(recognized_revenue_usd) AS subscription_revenue FROM fact_subscription",
        "SELECT SUM(revenue_usd) AS ad_revenue FROM fact_ad_impression",
        "SELECT SUM(net_amount_usd) AS merch_gmv FROM fact_merch_order_item",
        "SELECT CAST(SUM(clicked_flag) AS REAL) / COUNT(*) AS ad_ctr FROM fact_ad_impression",
    )
    for offset, sql in enumerate(metrics, start=25):
        cases.append(
            _query(
                MONETIZATION,
                offset,
                "metric",
                f"Calculate monetization metric {offset - 24}.",
                sql,
            )
        )
    for offset, category in enumerate(
        ("Figure", "Apparel", "Poster", "Blu-ray"), start=29
    ):
        cases.append(
            _query(
                MONETIZATION,
                offset,
                "follow_up",
                f"Now show merchandise GMV for {category}.",
                "SELECT p.product_category, SUM(i.net_amount_usd) AS merch_gmv "
                "FROM fact_merch_order_item i JOIN dim_merch_product p "
                "ON i.product_id = p.product_id "
                f"WHERE p.product_category = '{category}' GROUP BY p.product_category",
                follow_up_context=["Show merchandise GMV by product category."],
            )
        )
    return cases + _rejections(MONETIZATION, 33, "fact_subscription")


def main() -> int:
    cases = [*_content_cases(), *_engagement_cases(), *_monetization_cases()]
    if len(cases) != 120:
        raise RuntimeError(f"Expected 120 cases, generated {len(cases)}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )
    print(f"Wrote {len(cases)} anime-platform cases to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
