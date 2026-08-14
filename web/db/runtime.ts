import { env } from "cloudflare:workers";

type StudioBindings = {
  DB?: D1Database;
  UPLOADS?: R2Bucket;
};

export const SAMPLE_DOMAIN_ID = "domain_anime_streaming";

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
      CREATE TABLE IF NOT EXISTS studio_domains (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT 'Workspace admin',
        status TEXT NOT NULL DEFAULT 'draft',
        is_sample INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
    db.prepare(`
      CREATE UNIQUE INDEX IF NOT EXISTS studio_domains_slug_idx
      ON studio_domains(slug)
    `),
    db.prepare(`
      CREATE INDEX IF NOT EXISTS studio_domains_created_at_idx
      ON studio_domains(created_at DESC)
    `),
    db.prepare(`
      CREATE TABLE IF NOT EXISTS studio_sources (
        id TEXT PRIMARY KEY,
        domain_id TEXT NOT NULL DEFAULT 'domain_anime_streaming',
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
      CREATE TABLE IF NOT EXISTS studio_runs (
        id TEXT PRIMARY KEY,
        domain_id TEXT NOT NULL DEFAULT 'domain_anime_streaming',
        question TEXT NOT NULL,
        status TEXT NOT NULL,
        model TEXT NOT NULL,
        row_count INTEGER NOT NULL DEFAULT 0,
        duration TEXT NOT NULL,
        is_demo INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
  ]);

  await ensureColumn(
    db,
    "studio_sources",
    "domain_id",
    `ALTER TABLE studio_sources
     ADD COLUMN domain_id TEXT NOT NULL DEFAULT '${SAMPLE_DOMAIN_ID}'`,
  );
  await ensureColumn(
    db,
    "studio_runs",
    "domain_id",
    `ALTER TABLE studio_runs
     ADD COLUMN domain_id TEXT NOT NULL DEFAULT '${SAMPLE_DOMAIN_ID}'`,
  );
  await ensureColumn(
    db,
    "studio_runs",
    "is_demo",
    `ALTER TABLE studio_runs
     ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0`,
  );

  await db.batch([
    db.prepare(
      `INSERT OR IGNORE INTO studio_domains (
        id, name, slug, description, owner, status, is_sample
      ) VALUES (?, ?, ?, ?, ?, 'ready', 1)`,
    ).bind(
      SAMPLE_DOMAIN_ID,
      "Anime Streaming",
      "anime-streaming",
      "Synthetic streaming analytics showcase with engagement, revenue, advertising, and merchandising data.",
      "Content analytics",
    ),
    db.prepare(`
      UPDATE studio_sources
      SET domain_id = '${SAMPLE_DOMAIN_ID}'
      WHERE domain_id IS NULL OR domain_id = ''
    `),
    db.prepare(`
      UPDATE studio_runs
      SET domain_id = '${SAMPLE_DOMAIN_ID}'
      WHERE domain_id IS NULL OR domain_id = ''
    `),
    db.prepare(`
      CREATE INDEX IF NOT EXISTS studio_sources_domain_id_idx
      ON studio_sources(domain_id)
    `),
    db.prepare(`
      CREATE INDEX IF NOT EXISTS studio_sources_created_at_idx
      ON studio_sources(created_at DESC)
    `),
    db.prepare(`
      CREATE INDEX IF NOT EXISTS studio_runs_domain_id_idx
      ON studio_runs(domain_id)
    `),
    db.prepare(`
      CREATE INDEX IF NOT EXISTS studio_runs_created_at_idx
      ON studio_runs(created_at DESC)
    `),
  ]);
}

async function ensureColumn(
  db: D1Database,
  table: "studio_sources" | "studio_runs",
  column: string,
  alteration: string,
) {
  const result = await db.prepare(`PRAGMA table_info(${table})`).all();
  const hasColumn = result.results.some(
    (item) => String((item as Record<string, unknown>).name) === column,
  );
  if (!hasColumn) {
    await db.prepare(alteration).run();
  }
}
