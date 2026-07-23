import { sql } from "drizzle-orm";
import {
  index,
  integer,
  sqliteTable,
  text,
} from "drizzle-orm/sqlite-core";

export const studioSources = sqliteTable(
  "studio_sources",
  {
    id: text("id").primaryKey(),
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
    index("studio_sources_created_at_idx").on(table.createdAt),
  ],
);

export const studioRuns = sqliteTable(
  "studio_runs",
  {
    id: text("id").primaryKey(),
    question: text("question").notNull(),
    status: text("status").notNull(),
    model: text("model").notNull(),
    rowCount: integer("row_count").notNull().default(0),
    duration: text("duration").notNull(),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [index("studio_runs_created_at_idx").on(table.createdAt)],
);
