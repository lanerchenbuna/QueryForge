import { sql } from "drizzle-orm";
import {
  index,
  integer,
  sqliteTable,
  text,
  uniqueIndex,
} from "drizzle-orm/sqlite-core";

export const studioDomains = sqliteTable(
  "studio_domains",
  {
    id: text("id").primaryKey(),
    name: text("name").notNull(),
    slug: text("slug").notNull(),
    description: text("description").notNull().default(""),
    owner: text("owner").notNull().default("Workspace admin"),
    status: text("status").notNull().default("draft"),
    isSample: integer("is_sample", { mode: "boolean" })
      .notNull()
      .default(false),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
    updatedAt: text("updated_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    uniqueIndex("studio_domains_slug_idx").on(table.slug),
    index("studio_domains_created_at_idx").on(table.createdAt),
  ],
);

export const studioSources = sqliteTable(
  "studio_sources",
  {
    id: text("id").primaryKey(),
    domainId: text("domain_id")
      .notNull()
      .default("domain_anime_streaming"),
    name: text("name").notNull(),
    sourceType: text("source_type").notNull(),
    objectKey: text("object_key").notNull(),
    sizeBytes: integer("size_bytes").notNull(),
    tableCount: integer("table_count").notNull().default(1),
    status: text("status").notNull().default("ready"),
    semanticJson: text("semantic_json").notNull(),
    reviewed: integer("reviewed", { mode: "boolean" }).notNull().default(false),
    contractStatus: text("contract_status").notNull().default("pending"),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("studio_sources_domain_id_idx").on(table.domainId),
    index("studio_sources_created_at_idx").on(table.createdAt),
  ],
);

export const studioRuns = sqliteTable(
  "studio_runs",
  {
    id: text("id").primaryKey(),
    domainId: text("domain_id")
      .notNull()
      .default("domain_anime_streaming"),
    question: text("question").notNull(),
    status: text("status").notNull(),
    model: text("model").notNull(),
    rowCount: integer("row_count").notNull().default(0),
    duration: text("duration").notNull(),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("studio_runs_domain_id_idx").on(table.domainId),
    index("studio_runs_created_at_idx").on(table.createdAt),
  ],
);
