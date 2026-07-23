# QueryForge Studio

The visual workspace for QueryForge. It brings data onboarding, mandatory
semantic review, governed natural-language analysis, SQL inspection, trust
evidence, and run history into one responsive interface.

## Run locally

Requirements: Node.js 22.13+ and npm.

```bash
npm ci
npm run dev
```

Open <http://localhost:3000>. The Studio starts in demo mode when the Python API
is unavailable, so the full product tour remains interactive.

For live answers, start the QueryForge API from the repository root in a second
terminal:

```bash
queryforge --serve-api
```

If the API uses another address, copy `.env.example` to `.env.local` and change
`QUERYFORGE_API_URL`.

## Product areas

- **Overview** — platform metrics, engagement trends, semantic contract health,
  and recent activity.
- **Data Sources** — anime dataset inventory plus SQLite, CSV, and Parquet
  onboarding.
- **Semantic Studio** — entities, metrics, relationships, join paths, and
  contract status in one graph-and-editor workspace.
- **Ask & Analyze** — natural language to governed SQL with progress, results,
  export, and a complete Trust Trace.
- **Run History** — filterable, persistent history for audit and replay.

Data uploads are intentionally atomic: the upload endpoint refuses publication
until a semantic contract has been reviewed and validated.

## Quality checks

```bash
npm run check
```

This runs linting, TypeScript checking, a production build, and rendered-output
tests.

## Runtime storage

The hosted build uses Cloudflare-compatible bindings:

- `DB` (D1) stores source metadata and Studio run history.
- `UPLOADS` (R2) stores uploaded source files.

Database migrations live in `drizzle/`. The bundled anime source data remains in
the repository-level `sample_data/` directory and is not copied or modified by
the Studio.
