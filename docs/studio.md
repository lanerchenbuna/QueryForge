# QueryForge Studio

QueryForge Studio is the browser interface for the governed analytics runtime.
It is designed to make the semantic layer visible and editable instead of
hiding it behind an opaque chat box.

## Start the interface

From the repository root:

```bash
make web-install
make web-dev
```

Open <http://localhost:3000>. The interface uses a deterministic demo response
when the QueryForge API is offline, which is useful for exploring the product.

For live execution, start the Python API in another terminal:

```bash
queryforge --serve-api
```

The Studio sends requests through its same-origin `/api/queryforge/*` proxy. Set
`QUERYFORGE_API_URL` in `web/.env.local` if the API is not available at
`http://127.0.0.1:8000`.

## Mandatory semantic onboarding

The Data Sources workflow profiles an uploaded SQLite, CSV, or Parquet asset,
generates a semantic draft, and then stops at a human review gate. Publication
remains disabled until:

1. entities, dimensions, metrics, relationships, and join paths have been
   reviewed;
2. contract validation succeeds; and
3. the reviewer explicitly confirms the semantic contract.

The server enforces the same rule. Calling the upload endpoint without
`reviewed=true` returns an error, so the requirement cannot be bypassed through
the UI.

## Runtime persistence

Hosted deployments use D1 for source metadata and run history and R2 for uploaded
files. The Python runtime still owns SQL planning, policy enforcement, read-only
execution, and analytical artifacts.

The bundled anime dataset under `sample_data/anime_streaming/` is read-only
showcase data for the Studio. Upload tests and hosted storage never modify or
delete it.

## Validate the frontend

```bash
make web-check
```

The check covers linting, TypeScript types, production compilation, server
rendering, mandatory semantic-review enforcement, and branded social assets.
