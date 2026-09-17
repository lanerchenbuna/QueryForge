# Database adapter contract (Step 18)

This document freezes the **read-only analytics database adapter contract** and
records the evidence behind the second backend (DuckDB) and the server-type backend
(PostgreSQL). The deployment/security view of the same feature is in
`database-adapters.md`; the plan item is
the QueryForge database-adapter programme (step 18).

Files:

| File | Role |
| --- | --- |
| `queryforge/infrastructure/db/adapter.py` | the frozen contract, capability registry, error taxonomy, value/type normalization |
| `queryforge/infrastructure/db/sqlite_connector.py` | unchanged SQLite default path (pre-contract connector) |
| `queryforge/infrastructure/db/duckdb_connector.py` | DuckDB backend implementing the contract (optional driver) |
| `queryforge/infrastructure/db/postgres_connector.py` | PostgreSQL server backend implementing the contract (optional `psycopg` driver) |
| `queryforge/infrastructure/db/adapters.py` | factory: SQLite/DuckDB file paths, plus PostgreSQL DSNs and `*.pg` marker files; never imports a driver eagerly |
| `queryforge/infrastructure/db/__init__.py` | lazy exports: importing the package never imports duckdb |
| `tests/test_db_adapter_contract.py` | parameterised conformance suite + 18-N1 cross-backend equivalence (SQLite/DuckDB) |
| `tests/test_postgres_adapter_contract.py` | always-run wiring/dialect checks + server-gated PostgreSQL conformance suite |

## 1. The contract surface

`DatabaseAdapter` declares exactly these operations. Primitives are abstract;
everything else has one shared implementation so backends cannot drift apart.

| Operation | Kind | Semantics |
| --- | --- | --- |
| `connect()` | derived | returns the connected read-only handle (idempotent); connections are opened read-only in the constructor today |
| `close()` / `with` | primitive | releases the connection |
| `list_tables()` | primitive | readable base tables of the current catalog/schema |
| `describe_table(name)` | primitive | `TableSchema`: column name, raw engine `data_type`, `nullable`, keys |
| `describe_logical_table(name)` | derived | `(column, frozen logical type, nullable)` — dialect-free schema view |
| `execute_sql(sql)` | primitive | engine execution of already-checked SQL; **no** policy check, **no** row bound (trusted, used by `DatabaseTool`) |
| `execute_readonly(sql, limit=None, timeout=None)` | derived | the entry point: capability guard → object check → AST policy → engine read-only role → engine row bound + streaming fetch bound → normalization |
| `preview(sql, limit=20)` | derived | bounded, policy-checked preview; cap 100 rows, aligned with `DatabaseTool.execute_sql_preview` |
| `cancel()` | primitive | interrupts in-flight engine work (SQLite `interrupt()`, DuckDB `interrupt()`, PostgreSQL cancel request via `cancel_safe()`/`cancel()`) |
| `explain(sql)` | derived | capability/policy-checked plan request using `capabilities.explain_prefix` |
| `normalize_value(v)` / `normalize_type(t)` | derived | the single value/type rendering rules (below) |
| `dialect`, `capabilities` | declaration | dialect name + truthful feature declaration |

### Error taxonomy (one class for every backend)

| Error | Raised when |
| --- | --- |
| `AdapterUnavailableError` | driver missing / connection impossible (`DuckDBUnavailableError`, `PostgresUnavailableError` are both this and their backend's connector error) |
| `AdapterPolicyError` | the shared AST policy refused the SQL **before** execution; carries the `SqlPolicyDecision` |
| `AdapterUnsupportedError` | the SQL needs a feature this backend declares unsupported |
| `AdapterTimeoutError` | the caller's `timeout` expired and the engine was interrupted |
| `AdapterCancelledError` | an external `cancel()` interrupted in-flight work |
| `AdapterQueryError` | the engine failed the statement (unknown column/table, syntax, engine refusal) |
| `AdapterTypeError` | a driver value cannot be normalized honestly |

Backend-specific errors (`DuckDBConnectorError`, `PostgresConnectorError`,
`SQLiteConnectorError`) stay on the `__cause__` chain and are never leaked by
`execute_readonly`. The uniform taxonomy covers the derived operations
(`execute_readonly`, `preview`, `explain`); the schema primitives may still raise the
backend's own class, because `describe_table`/`list_tables` are exactly what the
pre-contract connectors already did and changing their error classes would change
existing behaviour.

The PostgreSQL backend detects a cancelled statement by **SQLSTATE** (`57014`,
`query_canceled`) rather than by message text, because the driver reports it as
`errors.QueryCanceled` — a name and message the contract's `_looks_interrupted`
heuristic does not recognize, and the message itself is localized by the server.

## 2. Capability matrix

Declared in `CAPABILITY_REGISTRY` and asserted in both directions by the suite:
a declared-supported feature must actually run, a declared-unsupported one must be
refused (`AdapterUnsupportedError`) instead of being silently mistranslated.

| Capability | SQLite | DuckDB | PostgreSQL |
| --- | --- | --- | --- |
| `dialect` | `sqlite` | `duckdb` | `postgres` |
| `window_functions` | ✅ | ✅ | ✅ |
| `cte` (non-recursive) | ✅ | ✅ | ✅ |
| `ilike` | ❌ refused | ✅ | ✅ |
| `qualify` | ❌ refused | ✅ | ❌ refused (PostgreSQL has no `QUALIFY`; sqlglot *could* rewrite it into a derived table, but an adapter must refuse rather than silently rewrite) |
| `limit_style` | `limit` | `limit` | `limit` |
| date functions declared | `date`, `datetime`, `julianday`, `strftime`, `time`, `unixepoch` | `date`, `date_add`, `date_diff`, `date_part`, `date_sub`, `date_trunc`, `epoch_ms`, `extract`, `strftime`, `to_timestamp` | `date_bin`, `date_part`, `date_trunc`, `extract`, `to_timestamp` (PostgreSQL 14+; each one is probed on the server by the suite) |
| `integer_division` | truncates — cast the numerator for ratios | fractional | truncates |
| `readonly_enforced_by_engine` | ✅ (`mode=ro` + `PRAGMA query_only`) | ✅ (`read_only=True`, external access off, config locked) | ✅ (`default_transaction_read_only` pinned in the DSN options and re-verified with `SHOW`, plus a non-superuser role expectation) |
| `cancellation` | ✅ | ✅ | ✅ (PostgreSQL cancel request) |
| `explain` / `explain_prefix` | ✅ `EXPLAIN QUERY PLAN` | ✅ `EXPLAIN` | ✅ `EXPLAIN` |
| `cost_estimates` | ❌ (plan only) | ❌ (plan only) | ❌ (plan only; plain `EXPLAIN` prints planner `cost=` units, which are not a calibrated cost model) |

Two dialect subtleties worth knowing before reading the table as "what SQL is
policed":

* when sqlglot reads the `postgres` dialect it normalizes some date-function names
  (`date_trunc` → `timestamp_trunc`, `to_timestamp` → `unix_to_time`), so those names
  are *not* in the frozen vocabulary and `check_capabilities` leaves them to the
  engine; the declaration is therefore documentation plus the suite's per-function
  probes, and `date_bin` is the one declared date function the guard actually polices;
* SQLite's date-function set is enforced against the frozen vocabulary, so the same
  `date_trunc(...)` query is refused on SQLite and runs on PostgreSQL — the declared
  difference, asserted in both directions.

Only the frozen `DATE_FUNCTION_VOCABULARY` is policed; unknown functions are left
to the engine, because the contract does not claim to validate every dialect.

## 3. Value and type normalization

`normalize_value` (identical rules for every backend, so result rendering and
comparison stay backend independent):

| Driver value | Normalized | Why |
| --- | --- | --- |
| `None` | `None` | SQL NULL stays JSON null |
| `bool` | `bool` | checked before `int` |
| `int` | `int` | |
| finite `float` | `float` | |
| `Decimal` | exact text (`"12.50"`) | JSON has no exact decimal; `float` would silently lose precision |
| `date` | `"YYYY-MM-DD"` | one textual date shape for both drivers |
| aware `datetime` | UTC ISO-8601 | DuckDB returns `TIMESTAMPTZ` in the session zone |
| naive `datetime` / `time` | ISO-8601, unchanged wall clock | no invented offset |
| `bytes` | lowercase hex | driver objects never reach JSON |
| non-finite `float`, containers, unknown objects | `AdapterTypeError` | refusing is honest; `CAST` in SQL instead |

`normalize_type` maps declared/engine type names (`TEXT`/`VARCHAR`,
`DECIMAL(12,2)`, `INTEGER`/`HUGEINT`, `REAL`/`DOUBLE PRECISION`,
`BLOB`/`BYTEA`, `DATE`, `TIMESTAMPTZ`, `JSON`, …) onto the frozen logical
vocabulary `integer, float, decimal, boolean, text, binary, date, time,
timestamp, json, unknown`. Arrays/structs are `unknown`, never guessed.

**Decimal caveat (documented, not hidden):** SQLite has no DECIMAL storage class,
so its driver returns a `REAL` for a `DECIMAL(12,2)` column while DuckDB and
PostgreSQL return exact decimal text. The contract promises *logical-type and value*
equality (`Decimal(str(value))`), not identical Python types. A backend whose driver
returns floats cannot recover the declared scale.

Raw type spellings are also engine-specific and are kept raw on purpose:
PostgreSQL's `information_schema` reports the canonical lowercase `integer` and drops
the numeric modifier, so the PostgreSQL backend re-attaches it (`numeric(12,2)`) to
keep the original DDL auditable, while `normalize_type` ignores the modifier and maps
both spellings to `decimal`. Cross-backend comparisons must use
`describe_logical_table`, never the raw `data_type` string.

## 4. What the contract does not promise

* **Write prevention is layered, not absolute.** `execute_readonly` refuses
  write/admin SQL in the AST policy layer (`INSERT`, `UPDATE`, `DELETE`,
  `DROP`, `CREATE`, `ATTACH`, `PRAGMA`, …) *and* the engine role refuses writes
  that reach it. But the engine role is not a complete boundary on SQLite:
  `mode=ro` protects the main database only, so the engine still accepts
  `ATTACH` of another file (writes *into* an attached database are refused by
  `PRAGMA query_only`). That is exactly why the AST layer is load-bearing, and it
  is asserted by `test_18_s1_sqlite_role_boundary_is_documented`.
* **`execute_sql` is a trusted primitive.** A caller that invokes it directly has
  already waived the policy layer; it exists because `DatabaseTool` owns the
  policy check for the existing callers.
* **No credentials, no vault, no DSN passthrough.** SQLite and DuckDB adapters open
  local files with the OS user's permissions. There is no credential vault and no
  per-domain authorisation in this layer.
* **The server backend takes a DSN and stores nothing.** `open_postgres(dsn)` /
  `open_database("postgresql://…")` / `open_database("target.pg")` (a marker file
  whose first non-comment line is the DSN) hand the connection string straight to the
  driver: the adapter keeps no secret, never echoes the DSN (connection failures are
  reported by driver error *category* only) and performs no per-user authentication.
  Treat a marker file as a credential file: keep it out of the repository and readable
  only by the process user.
* **The server backend expects a read-only role, and verifies what it can.**
  `PostgresConnector` pins `default_transaction_read_only = on` in the DSN options,
  re-reads it with `SHOW`, and refuses a **superuser** session by default
  (`require_readonly_role=True`), because that GUC is user-settable and a superuser is
  not subject to ordinary privilege checks. It does **not** enumerate the role's
  privileges: a non-superuser role that happens to hold `INSERT` is accepted, and the
  session flag is then the engine-side boundary. `readonly_role_verified` reports
  which of the two situations the session is in. Recommended setup:
  `CREATE ROLE qf_reader LOGIN PASSWORD …; GRANT pg_read_all_data TO qf_reader;`
  (PostgreSQL 14+).
* **Known server-side boundary gaps, asserted rather than hidden.** PostgreSQL's
  read-only transactions still allow writes to *temporary* tables (the AST policy
  refuses `CREATE` outright, so only the engine layer is relaxed there), and
  `SELECT ... INTO hacked FROM fact_orders` parses as a plain `SELECT` for the AST
  policy, so on this backend the **engine** is what refuses it
  (`test_18_s1_ast_policy_misses_select_into_and_the_server_catches_it`). The
  reverse also holds: `ATTACH` is SQLite syntax, so on the PostgreSQL dialect the
  refusal is a typed parse error before the server is contacted.
* **No pooling.** One adapter owns one connection and is never shared between
  domains or runs; callers must `close()` it. There is no session reuse, therefore
  no session-state crossover (18-C1 is out of scope for this step).
* **No cost model.** `explain` returns a plan (access paths), not a calibrated
  cost or wall-time estimate. PostgreSQL's plain `EXPLAIN` does print planner
  `cost=` units, which is why the declaration documents them as plan output rather
  than setting `cost_estimates`.
* **Cancellation is best effort.** It interrupts engine work; it cannot cancel an
  in-flight model call and cannot undo a partial side effect.
* **The tool layer's SQLite deadline cannot interrupt a server statement.**
  `install_sql_deadline_handler` needs `set_progress_handler`/`interrupt`, which a
  psycopg connection does not have, so it installs a no-op guard for this backend.
  A real deadline on PostgreSQL comes from
  `DatabaseAdapter.execute_readonly(sql, timeout=…)` (whose watchdog sends a cancel
  request) or from a `statement_timeout` set for the role/server.
* **Schema reads are not governance.** `list_tables`/`describe_table` return the
  physical catalog; `DatabaseTool` applies the policy-filtered view. On PostgreSQL the
  catalog is additionally filtered by the connected role's privileges (a role without
  `USAGE`/`SELECT` simply sees no tables), and keys are read from `pg_catalog` rather
  than `information_schema`, whose constraint views hide rows from a non-owner — a
  read-only role would otherwise appear to have no primary keys at all.

## 5. Adding a third backend

1. Decide whether the engine really fits "read-only analytics database"; if it
   needs credentials, pooling or per-user sessions, extend this contract first.
2. Implement the primitives (`list_tables`, `describe_table`, `execute_sql`,
   `cancel`, `close`) in `queryforge/infrastructure/db/<engine>_connector.py` and
   subclass `DatabaseAdapter`. Import the driver **inside** `__init__` and raise
   an `AdapterUnavailableError` subclass when it is missing, so the module and the
   package stay importable on a core-only install.
3. Publish a declaration in `CAPABILITY_REGISTRY` and keep it truthful — the
   conformance suite runs every declared capability.
4. Add the driver to `pyproject.toml` as an optional extra; the `dependencies`
   list must stay SQLite-only.
5. Reuse the inherited `execute_readonly`/`preview`/`explain`/normalization. Do
   not re-implement the policy check, the row bound or the rendering rules. A
   backend with a streaming cursor may override `_fetch_bounded` to stop pulling
   rows at the limit (DuckDB does; the pre-contract SQLite connector keeps its
   full fetch because the engine LIMIT already bounds the result).
6. Run the parameterised suite for the new backend: subclass the conformance mixin
   in `tests/test_db_adapter_contract.py`, add its fixture builder (same DDL and
   the same deterministic data) and extend the 18-N1 equivalence test.
7. A pre-contract connector (like `SQLiteConnector`) can be published unchanged
   through `adapt_connector(connector)`, which adds the contract seam without
   touching the connector's own behaviour.

### Adding a *server* backend (what PostgreSQL needed on top)

8. Route the target in `adapters.py` **without importing the driver**: a DSN scheme
   and/or a marker-file suffix (see `is_postgres_target`/`resolve_postgres_dsn`), so
   file-backed paths stay driver-free and the router is testable offline.
9. Pin and verify the read-only session, then decide what role you require. Two
   layers are not two layers if the caller can turn one off: `SET
   default_transaction_read_only = on` is a *user-settable* GUC, so a superuser
   session has no engine boundary at all. Refuse it by default, record what was
   verified, and document the escape hatch.
10. A streaming bound needs the server's own cursor (`DECLARE`/`FETCH FORWARD n`
    inside an explicit transaction): a client cursor materializes the whole result
    before `fetchmany` can stop, so the bound would only save Python objects. Close
    the portal and commit/roll back the transaction in the same scope, so a cancelled
    read cannot poison the session.
11. Translate engine errors by **SQLSTATE**, not by message text: server messages are
   localized, and a driver's "query canceled" error class is not named what the
   contract's interrupt heuristic looks for. Map the cancelled statement to a cancel,
   a server-side statement timeout to a timeout, and everything else to a query error
   that contains no SQL text.
12. Split the tests in two: an always-run half (import without the driver, capability
   registration, routing, dialect-level policy/bound behaviour) and a server-gated
   half behind an env var such as `QUERYFORGE_TEST_POSTGRES_DSN`, with an opt-in
   "require" switch so a job that is supposed to run it cannot pass by skipping.

## 6. Verification

```bash
# the contract suite (all backends; the DuckDB half needs the extra)
LOG_LEVEL=CRITICAL .venv/bin/python -m unittest tests.test_db_adapter_contract -v

# the DuckDB extra must be installed for the tier-2 job:
pip install -e '.[duckdb]'
```

### Running the server-backed suite

```bash
# 1. the extra (psycopg 3, wheels only):
pip install -e '.[postgres]'

# 2. a read-only role for the adapter (PostgreSQL 14+); run as an admin:
#    CREATE ROLE qf_reader LOGIN PASSWORD '...';
#    GRANT pg_read_all_data TO qf_reader;

# 3. the suite -- point the DSN at a disposable database:
QUERYFORGE_TEST_POSTGRES_DSN=postgresql://qf_reader:secret@127.0.0.1:5432/qf_test \
    python -m unittest tests.test_postgres_adapter_contract -v
```

Optional variables: `QUERYFORGE_TEST_POSTGRES_SETUP_DSN` (a write-capable DSN used
only to create/drop the fixture schema, for when the main DSN is a genuinely
read-only role), `QUERYFORGE_TEST_POSTGRES_SCHEMA` (fixture schema prefix, default
`queryforge_step18`, created and dropped by the suite), and
`QUERYFORGE_TEST_POSTGRES_ALLOW_SUPERUSER=1` (throwaway containers where only a
superuser exists — the role layer is then explicitly not claimed). Setting
`QUERYFORGE_REQUIRE_POSTGRES=1` turns a missing DSN into a **failure** instead of a
skip, so a tier-2 job cannot go green without a server.

**Unverified without a server.** The `psycopg`-side engine half of the PostgreSQL
conformance suite (18-N1 against SQLite, 18-S1's engine layer, cancellation,
`explain`, the streaming bound) executes **only** when
`QUERYFORGE_TEST_POSTGRES_DSN` is set. Without it those cases are skipped with that
reason in the skip text, which is why this document does not call them verified by
the default `python -m unittest discover -s tests -q` run. Everything that *can* be
checked without a server is checked unconditionally instead: the module and factory
import without the driver, the capability declaration is registered and consistent
with the frozen vocabulary, DSN/marker routing is asserted, a DSN read from a marker
file never appears in an error message, the SQLite/DuckDB paths stay driver-free, and
the postgres dialect's capability/bound behaviour is executed against the real
declaration.

The suite proves, against real engines:

* **18-N1** the same fixture (360-row fact table + dimension + date column, built
  from the same DDL and the same deterministic data in both engines) answers the
  same seven queries identically: `count`, `sum`, `group_by_join`, `window`
  (`SUM(...) OVER (PARTITION BY ... ORDER BY ...)`), `cte`, `date_range`,
  `empty` — compared on columns, row count and every row value with no tolerance;
  plus schema-metadata parity and type-conversion parity.
* **18-S1** `INSERT`/`UPDATE`/`DELETE`/`DROP`/`CREATE`/`ATTACH`/`PRAGMA` are
  refused by `AdapterPolicyError` (`decision.rule == "read_only_ast"`) before the
  engine runs, the engine role independently refuses the writes on both backends,
  and the fixture is verified unchanged afterwards.
* **18-R1** the SQLite path (query, schema, preview, explain, policy refusal)
  keeps working while `duckdb` is made unimportable via monkeypatched
  `sys.modules`/`find_spec`, the backend fails loudly only when used, and a
  subprocess asserts that importing `queryforge.infrastructure.db` never imports
  the driver.
* capabilities, typed-error parity (`AdapterQueryError` for an unknown table and
  an unknown column from both backends), `EXPLAIN` plans, and cancellation: a
  deadline (`AdapterTimeoutError`) and a client `cancel()`
  (`AdapterCancelledError`) both interrupt a multi-billion-row join in well under
  a second and leave the connection usable; DuckDB's bounded fetch stops at the
  limit instead of materializing the whole result.

The PostgreSQL suite (`tests/test_postgres_adapter_contract.py`) runs the same
conformance mixin against a server and adds:

* **18-N1 across an embedded and a server engine**: the seven portable queries return
  *identical* normalized rows from SQLite and PostgreSQL, plus logical-schema,
  primary-key, nullability and type-conversion parity (the last one through the
  documented `Decimal` comparison, since the two drivers report different Python
  types for `DECIMAL`).
* **18-S1 with three layers**: 14 write/admin statements (including
  `SET default_transaction_read_only = off`, `COPY`, `VACUUM`, `CREATE ROLE`) are
  refused by `AdapterPolicyError` before the server sees them; the same statements
  through the trusted `execute_sql` are refused by the server with SQLSTATE `25006`
  (read-only transaction) or `42501` (insufficient privilege) — SQLSTATE, not message
  text, so a localized server cannot turn the evidence green; and the
  `SELECT ... INTO` gap in the AST layer is demonstrated to be closed by the engine.
* **the boundary is reported**: `SHOW default_transaction_read_only` is read back from
  the server, the role identity is cross-checked against `current_user`, and
  `readonly_role_verified` is asserted to mean exactly "this session is not a
  superuser" (a run using `QUERYFORGE_TEST_POSTGRES_ALLOW_SUPERUSER=1` asserts that the
  role layer is *not* claimed instead).
* **the bounded read really streams**: a named server cursor returns 4 of 20 000 000
  rows well inside the time budget a client-side materialization could not meet, and
  leaves no cursor allocated (`pg_cursors`), including on a second use of the same
  fixed cursor name.
* **the declaration is probed, not asserted**: every declared date function is run on
  the server, and the always-run half of the module fails if a declaration appears
  without a probe.
