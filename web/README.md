# QueryForge Studio

The domain-first visual workspace for QueryForge. Users create or select a data
domain, then onboard that domain's sources, build its mandatory semantic
contract, run governed analysis, inspect SQL and trust evidence, and audit
domain-scoped history in one responsive interface.

## Run locally

Requirements: Node.js 22.13+ and npm.

```bash
npm ci
npm run dev
```

Open <http://localhost:3000>. The bundled Anime Streaming domain remains
interactive in demo mode when the Python API is unavailable. Newly created
domains start empty and never inherit sample entities or metrics.

For live answers, start the QueryForge API from the repository root in a second
terminal:

```bash
queryforge --serve-api
```

If the API uses another address, copy `.env.example` to `.env.local` and change
`QUERYFORGE_API_URL`.

## Product areas

- **Data Domains** — create, select, and manage isolated business contexts.
- **Overview** — active-domain readiness, governed metrics, contract health, and
  recent activity.
- **Data Sources** — domain-scoped SQLite, CSV, and Parquet onboarding.
- **Semantic Studio** — a required contract builder for identity, grain,
  dimensions, measures, metrics, relationships, Join Paths, policy, and quality.
- **Ask & Analyze** — natural language to governed SQL with progress, results,
  export, and a complete Trust Trace.
- **Run History** — filterable, domain-scoped history for audit and replay.

Data uploads are intentionally atomic: the upload endpoint refuses publication
until a data domain is selected and its semantic contract has been reviewed and
validated.

## Quality checks

```bash
npm run check
```

This runs linting, TypeScript checking, a production build, and rendered-output
tests.

## Runtime storage

The hosted build uses Cloudflare-compatible bindings:

- `DB` (D1) stores data domains, source metadata, and domain-scoped run history.
- `UPLOADS` (R2) stores files under
  `domains/{domainId}/sources/{sourceId}/...`.

Database migrations live in `drizzle/`. The bundled anime source data remains in
the repository-level `sample_data/` directory and is not copied or modified by
the Studio.
