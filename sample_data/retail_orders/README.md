# Retail Orders Analytics Sample

This directory contains a fully synthetic, deterministic retail order dataset for
a multi-store retailer. No row is copied from a real retailer, catalog, customer
base, or third-party benchmark.

## Scale

The generated SQLite database contains **2,264 rows across 6 tables**:

| Table | Grain | Rows |
| --- | --- | --- |
| `dim_date` | calendar day | 366 |
| `dim_store` | store | 24 |
| `dim_product` | product | 60 |
| `dim_customer` | customer | 120 |
| `fact_order` | order | 480 |
| `fact_order_item` | order line | 1,214 |

`fact_order` carries the order calendar (`order_date_key`), the fulfilling store,
the purchasing customer, the sales channel, and the order amounts; `fact_order_item`
holds one row per product within an order. `dim_customer` keeps contact data
(`customer_name`, `customer_email`) physically present but **outside the SQL
policy allowlist**, so an analytical request for it must be rejected rather than
answered.

Outlet-format stores only started trading on 2024-11-01, so the Outlet format has
no order rows at all in Q1/2024 — a real empty slice for the benchmark.

## Files

- `retail_orders.sqlite` — ready-to-query database;
- [semantic_model.yml](semantic_model.yml) — entities, dimensions, metrics, and safe Join Paths;
- [sql_policy.yml](sql_policy.yml) — table/column scope and query-shape budgets;
- [README.md](README.md) — this file.

Regenerate the database, semantic model, policy, and README from repository root
(with a fixed seed; regeneration is idempotent byte-for-byte):

```bash
python sample/generate_aux_datasets.py
```

The generator lives in [sample/generate_aux_datasets.py](../../sample/generate_aux_datasets.py).

Example:

```bash
python main.py \
  --database sample_data/retail_orders/retail_orders.sqlite \
  --semantic-model sample_data/retail_orders/semantic_model.yml \
  --sql-policy sample_data/retail_orders/sql_policy.yml \
  --question "What is net revenue by store region in 2024?"
```
