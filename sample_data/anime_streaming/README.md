# Anime Streaming Analytics Sample

This directory contains a fully synthetic, deterministic analytics dataset for an
anime streaming platform with community and commerce features. No row is copied
from a real website, title catalog, user base, or third-party benchmark.

## Scale

The generated SQLite database contains **370,762 rows across 15 tables**:

| Area | Tables | Representative grain |
| --- | --- | --- |
| Content | `dim_anime`, `dim_episode`, `dim_studio`, `dim_genre`, `bridge_anime_genre` | title, episode, studio, and anime–genre |
| Audience | `dim_user`, `fact_watch_session`, `fact_rating` | user, playback session, and user–anime score |
| Monetization | `fact_subscription`, `fact_ad_impression` | subscription period and ad impression |
| Community | `fact_user_follow` | directed follower–followed edge |
| Commerce | `dim_merch_product`, `fact_merch_order`, `fact_merch_order_item` | product, order, and order line |
| Shared | `dim_date` | calendar day |

The model demonstrates many-to-one relationships, a many-to-many bridge,
self-referencing sequel and referral relationships, optional foreign keys, two
role-playing user relationships, and governed multi-hop Join Paths.

## Files

- `anime_streaming.sqlite` — ready-to-query database;
- `tables/*.csv` — optional deterministic exports generated on demand and excluded
  from Git to avoid duplicating the SQLite database;
- `semantic_model.yml` — entities, dimensions, metrics, cardinality contracts,
  operational contracts, and safe Join Paths;
- `semantic_baseline.json` — reviewed weekly-drift baseline for schema, metrics,
  relationships, Join Paths, and quality contracts;
- `subjects.yml` — six bounded analytical subject areas;
- `sql_policy.yml` — table/column scope and query-shape budgets;
- `reference_sql/` — reviewed query patterns for retrieval.

Regenerate the SQLite database and deterministic CSV exports from repository root
(the reviewed semantic YAML is intentionally maintained separately):

```bash
python sample/generate_anime_streaming.py
```

Example:

```bash
python main.py \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --semantic-model sample_data/anime_streaming/semantic_model.yml \
  --sql-policy sample_data/anime_streaming/sql_policy.yml \
  --question "What are watch hours and completion rate by anime format?"
```
