# API Reference

Start the optional REST server:

```bash
queryforge --serve-api --api-host 127.0.0.1 --api-port 8000
```

The NL2SQL request bodies use the `AskRequest` contract. The common fields are `question`,
`database`, `semantic_model_path`, `sql_policy_path`, `skills`, session options, Tool Loop,
parallel candidates, Subject Tree and report options.

A semantic model is required by default and is discovered beside the selected
database. `allow_schema_only: true` is an explicit diagnostics-only escape hatch;
normal analytical clients should never set it.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/health` | GET | Service health and capability summary |
| `/models` | GET | Configured model providers |
| `/skills` | GET | Local prompt-only Skills |
| `/ask` | POST | Execute the full governed NL2SQL workflow |
| `/ask/stream` | POST | SSE progress events; no SQL, prompts, or result rows in events |
| `/plan` | POST | Generate and preflight a plan without executing SQL |
| `/analyze` | POST | Execute a deterministic governed analysis plan (`AnalyzeRequest`) |
| `/analyze/runs/{run_id}` | GET | Inspect a durable analysis run |
| `/analyze/runs/{run_id}/cancel` | POST | Cancel a durable analysis run |
| `/report/{run_id}` | GET | Download static HTML from the configured report directory |
| `/reports/expire` | POST | Preview expired reports; `dry_run: false` quarantines HTML/manifest pairs for recovery |
| `/gateway/webhook` | POST | Gateway adapter with a stable user/channel session |

Example:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question":"Top anime by watch hours","database":"sample_data/anime_streaming/anime_streaming.sqlite"}'
```

For SSE, send the same body to `/ask/stream`. Each `data:` line is a `WorkflowEvent` containing
only progress metadata. Use `report: true` to create a static report after a successful execution,
then retrieve it from `/report/<run_id>`.

Errors caused by invalid inputs return `400`; workflow, policy, or execution failures return
`422`. SQL policy denials include a named `SQL_SECURITY_ERROR` and occur before SQLite execution.

Reports expire by HTML file modification time (30 days by default). Expired or
unsafe report paths return `404`; symlinked HTML/manifests cannot be served or
overwritten. `POST /reports/expire` defaults to `{"dry_run":true}`. An explicit
`{"dry_run":false}` moves expired pairs into the configured report directory's
`.expired/<run_id>_<uuid>/` directory; it does not delete evidence. To recover,
copy the pair back, which starts a new retention period. This operation uses the
configured root only and shares the existing API authorization boundary.

Loopback API requests with a foreign browser `Origin` or `Sec-Fetch-Site:
cross-site` are rejected before protected endpoints run. Local CLI requests
without an Origin remain supported; Studio uses its guarded same-origin proxy.

Studio history stores an immutable result snapshot with the run id, domain and
data version. `GET /api/studio/runs?domain_id=...&run_id=...` replays saved results
without calling a model or querying the current data version. Cross-domain ids
return `404`, ids already assigned to another domain return `409`, and expired
or unretained snapshots return `410`. Snapshots are limited to 2 MiB and expire
after 30 days. They are submitted by the trusted local Studio client and are
not signed evidence for a multi-user deployment.

`/analyze` (or CLI `--analyze`) aggregates each metric population independently,
then joins the aggregate results at a governed common grain. This supports different
facts and different default filters without joining raw fact rows. Scalar responses retain `answer.metric` / `answer.value`
and add `answer.metrics`, `answer.values` and `answer.columns`; grouped results carry all
requested metric columns in `answer.rows`.

Explicit literal filters such as `where item_id = 1`, `for category = 'a'`,
`IN`, `BETWEEN`, `IS NULL`, and boolean combinations are bound to declared base-entity
columns and checked in the compiled SQL. Cross-fact filters must name a qualified
conformed dimension allowed by every metric, for example `where user.region = 'APAC'`.
Functions, subqueries, unknown columns and
unparseable filters return `needs_clarification`, before query execution.

Daily, weekly (Monday start), monthly, quarterly and yearly comparisons use dated calendar buckets and
the requested baseline. `mom` / `qoq` identify monthly / quarterly grain; an ambiguous
baseline requires clarification. Missing baselines and a series reaching its query
limit cannot be substituted with another period and reported as success. A bounded
replan to a different dimension retains evidence but returns `blocked` for the
original request. The query automatically includes required baseline periods.
Multi-metric time analysis requires a shared time dimension, timezone and calendar;
each metric uses its own event-time field. `answer.comparisons` contains a comparison
for every metric in a trend response.
A dated comparison with governed category dimensions uses the diagnostic template:
independent period totals → per-category results → period comparison → reconciled
contribution → anomaly scan → evidence-backed answer. `answer.diagnosis` exposes
`comparison`, `contributions` and `anomalies` by metric, quality checks,
period completeness and evidence ids. Bucket keys are JSON arrays preserving NULL
and multiple dimension values. Time bucket columns have stable semantic reference
names such as `calendar.month`.

Both current and baseline category sums must match independently queried totals;
a zero change residual alone cannot prove that joins preserved the population.
Non-additive metrics/dimensions and capped queries are refused. A missing category
period becomes zero only with enforced complete coverage. Opposing drivers remain
visible when total change is zero, with undefined percentage shares. Decomposition
is arithmetic attribution and carries no causal claim. A two-period query has
insufficient history for anomaly detection; it returns that state and an explicit
limitation instead of claiming no anomaly.

`answer.metric_contracts` exposes units, additivity, event roles and population filters.
Declared ratios return numerator/denominator audit columns and `answer.ratio_components`;
ratios and distinct counts are recomputed at each grain and are never summed into
bucket totals. Missing fact groups and zero denominators produce an explicit incomplete
result instead of an invented zero. Actual join-key uniqueness and contracted event-date
validity are checked through DatabaseTool before the metric query.

`answer.period_completeness` is `complete` only when all requested periods meet enforced
authored ingestion coverage bounds; otherwise it is `unverified`, with a limitation
for time analysis. Enforced contracts block clipped or missing periods and windows outside
coverage. UTC/fixed offsets and fiscal start months are supported; named DST zones require
a backend with those semantics and are rejected by the current SQLite contract.

Clients must inspect the payload `status`: `/analyze` returns HTTP `400` for
`needs_clarification` / `blocked`, and can return HTTP `200` with `partial` for usable
intermediate evidence and an unmet analysis requirement.
