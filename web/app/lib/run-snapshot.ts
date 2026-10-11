import { normalizeRunStatus, persistableRunStatus, type RunView } from "./run-status";

export const SNAPSHOT_RETENTION_DAYS = 30;
export const MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024;

export function encodeSnapshot(value: unknown, runId: string, status: string): string | null {
  if (value == null) return null;
  const view = value as Partial<RunView>;
  if (view.runId !== runId || !view.status || persistableRunStatus(view.status) !== status || normalizeRunStatus(view.status) !== view.status
      || !["live", "demo"].includes(String(view.provenance))
      || !Array.isArray(view.columns) || !Array.isArray(view.rows)) {
    throw new Error("Run snapshot does not match its run id, status or result shape.");
  }
  const encoded = JSON.stringify(view);
  if (new TextEncoder().encode(encoded).length > MAX_SNAPSHOT_BYTES) throw new Error("Run snapshot exceeds 2 MiB.");
  return encoded;
}

export function expiredSnapshot(expiresAt: string | null, now = Date.now()): boolean {
  return !expiresAt || !Number.isFinite(Date.parse(expiresAt)) || Date.parse(expiresAt) <= now;
}
