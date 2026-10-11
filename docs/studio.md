# QueryForge Studio

QueryForge Studio is the browser interface for the governed analytics runtime.
It is designed around a domain-first rule: business context must be selected
before data, semantics, or analysis can exist. The semantic layer stays visible
and editable instead of hiding behind an opaque chat box.

## Start the interface

From the repository root:

```bash
make web-install
make web-dev
```

Open <http://localhost:3000>. Studio supports local single-user use.
Demo results are labelled explicitly; a failed live request cannot become a demo success.

For live execution, start the Python API in another terminal:

```bash
queryforge --serve-api
```

The Studio sends requests through its same-origin `/api/queryforge/*` proxy. Set
`QUERYFORGE_API_URL` in `web/.env.local` if the API is not available at
`http://127.0.0.1:8000`. The configured upstream must remain a loopback HTTP(S)
address. The development server binds to `127.0.0.1`, disables the public worker
inspector, and rejects foreign origins on Studio routes in local mode.

## Domain-first workflow

1. Open **Data Domains**.
2. Select the bundled Anime Streaming sample or create a clean business domain.
3. Add CSV or Parquet files inside the active domain; database files are rejected.
4. Inspect actual CSV columns and sampled missing values (up to 2 MiB / 1000 rows).
5. Review the starter draft's entity, grain, key, dimensions and owner.
6. Edit metric names, descriptions, count/sum/ratio aggregations and qualified
   `table.column` expressions. Editing resets review and draft validation.
7. Classify sensitivity and confirm the semantic review. Draft field checks
   are not physical quality or publication verdicts.
8. Publish data and semantics atomically.
9. Ask questions and inspect Trust Trace evidence within that same domain.

The top-bar domain switcher changes the context for Overview, Data Sources,
Semantic Studio, Ask & Analyze, and Run History. Sources and runs carry a
`domain_id`; uploaded objects use
`domains/{domainId}/sources/{sourceId}/...`. A new domain never reuses semantic
definitions from the sample.

## Mandatory semantic onboarding

The upload wizard inspects a bounded CSV sample and presents an editable starter
contract. It does not infer unique keys, business grain or a dataset quality
score. Parquet schema remains unverified in the browser and is checked by the
Python publisher. The local contract editor supports one entity with named
dimensions and measures; full relationship/time/unit authoring remains in YAML.
Physical column names are evidence. Publication remains disabled until:

1. business entity identity and row grain are explicit;
2. primary key, dimensions and metric expressions are reviewed;
3. metric expressions refer to the publication table's qualified columns;
4. owner and sensitivity are complete;
5. draft fields are complete; and
6. the reviewer explicitly confirms the contract.

The server enforces both boundaries. Calling the upload endpoint without a valid
`domain_id` or without `reviewed=true` returns an error, so neither domain
selection nor semantic review can be bypassed through the UI. The Python
publisher then checks actual data, keys and semantic contracts. Only an
executable `data_version` marks a source/domain ready. Failed and unknown source
states remain visible. Published domains can query by `domain_id`; switching
domains clears prior results and is refused while a run is in progress.

## Runtime persistence

Local Studio uses Miniflare's D1/R2-compatible storage for domains, sources and
history. Set `STUDIO_STATE_DIR` to isolate a test instance. The Python runtime
owns SQL planning, policy enforcement, read-only execution and reports. The deployment assumes a local
single user; multi-user hosting is outside the supported deployment model.

Run history retains result snapshots (2 MiB maximum, 30-day access). Replay is
bound to the saved domain/data version and makes no model call. Legacy history
without a snapshot and expired snapshots are unavailable rather than re-executed.
SQL errors, publication failures and history storage errors are shown to the
user. Cancellation is cooperative: an in-flight model request can finish before
the worker records `cancelled`, but the next SQL boundary remains blocked.

The bundled anime dataset under `sample_data/anime_streaming/` is read-only
showcase data attached to the Anime Streaming sample domain. It is not the
platform schema. Upload tests and hosted storage never modify or delete it.

## Validate the frontend

```bash
make web-check
```

The check covers linting, TypeScript types, production compilation, server
rendering, mandatory semantic-review enforcement, and branded social assets.

For a disposable offline end-to-end fixture, start these in separate terminals:

```bash
.venv/bin/python -B -m tests.studio_fixture_server
STUDIO_STATE_DIR=/tmp/queryforge-studio-test QUERYFORGE_API_URL=http://127.0.0.1:8000 npm --prefix web run dev
.venv/bin/python -B scripts/check_local_studio.py --report /tmp/queryforge-studio-acceptance.json
```

The checker requires PyArrow for its genuine Parquet fixture and refuses a
backend without the explicit offline-fixture health marker before any workflow.
It verifies real Studio HTTP handlers, local D1/R2, Python publication, governed
SQLite results, refusals, SSE disconnect cancellation, replay and report expiry.
