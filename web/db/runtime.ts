import { env } from "cloudflare:workers";

type StudioBindings = {
  DB?: D1Database;
  UPLOADS?: R2Bucket;
};

export function getStudioBindings() {
  const bindings = env as unknown as StudioBindings;
  if (!bindings.DB || !bindings.UPLOADS) {
    throw new Error(
      "Studio persistence is unavailable. Configure the DB and UPLOADS bindings.",
    );
  }
  return { db: bindings.DB, uploads: bindings.UPLOADS };
}

export async function ensureStudioSchema(db: D1Database) {
  await db.batch([
    db.prepare(`
      CREATE TABLE IF NOT EXISTS studio_sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        source_type TEXT NOT NULL,
        object_key TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        table_count INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'ready',
        semantic_json TEXT NOT NULL,
        reviewed INTEGER NOT NULL DEFAULT 0,
        contract_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
    db.prepare(`
      CREATE INDEX IF NOT EXISTS studio_sources_created_at_idx
      ON studio_sources(created_at DESC)
    `),
    db.prepare(`
      CREATE TABLE IF NOT EXISTS studio_runs (
        id TEXT PRIMARY KEY,
        question TEXT NOT NULL,
        status TEXT NOT NULL,
        model TEXT NOT NULL,
        row_count INTEGER NOT NULL DEFAULT 0,
        duration TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
    db.prepare(`
      CREATE INDEX IF NOT EXISTS studio_runs_created_at_idx
      ON studio_runs(created_at DESC)
    `),
  ]);
}
