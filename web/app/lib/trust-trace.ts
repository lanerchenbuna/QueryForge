/**
 * Trust Trace built from real backend artifacts.
 *
 * Every row is backed by a field that the backend actually returned. When the
 * field is missing the row reports "Not evaluated" with `evidence: false`, so
 * the Studio never shows a fixed score or a fabricated verdict.
 */

import type { Provenance } from "./run-status";

export interface TrustTraceRow {
  label: string;
  value: string;
  /** True only when a backing field exists in the live output. */
  evidence: boolean;
}

export interface TrustTrace {
  rows: TrustTraceRow[];
}

export const NOT_EVALUATED = "Not evaluated";
export const DEMO_TRUST_NOTICE = "Demo evidence — not from a live run";

const MAX_POLICY_ROWS = 5;

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function join(...parts: Array<string | undefined>): string {
  return parts.filter((part) => part && part.length > 0).join(" · ");
}

/** Live checks evaluated below; absent artifacts resolve to "Not evaluated". */
const LIVE_TRACE_LABELS = [
  "Generated SQL",
  "SQL policy decisions",
  "Reflection",
  "Retries",
  "SQL attempts",
  "Semantic metric match",
  "Agent team delivery",
  "Tool loop",
];

/** Demo rows keep the demo UX, but they are explicitly not live evidence. */
const DEMO_ROWS: TrustTraceRow[] = [
  { label: "semantic match · watch_hours", value: "SUM · watch_session (sample)", evidence: false },
  { label: "semantic match · completion_rate", value: "RATIO · watch_session (sample)", evidence: false },
  { label: "join path", value: "Watch → Episode → Anime (sample path)", evidence: false },
  { label: "SQL policy · read-only AST", value: "sample only", evidence: false },
  { label: "SQL policy · table scope", value: "sample only", evidence: false },
  { label: "quality score", value: "Not evaluated — no live run", evidence: false },
];

/**
 * Build Trust Trace rows from a raw backend output object.
 * `provenance` must be `demo` for demo evidence (rows are marked unevidenced).
 */
export function buildTrustTrace(
  output: Record<string, unknown> | null | undefined,
  provenance: Provenance = "live",
): TrustTrace {
  if (provenance === "demo") {
    return { rows: DEMO_ROWS.map((row) => ({ ...row })) };
  }
  // A live run without artifacts reports "Not evaluated" rows, never demo rows.
  if (!output) {
    return {
      rows: LIVE_TRACE_LABELS.map((label) => ({
        label,
        value: NOT_EVALUATED,
        evidence: false,
      })),
    };
  }

  const rows: TrustTraceRow[] = [];

  const sql = asText(output.sql);
  rows.push({
    label: "Generated SQL",
    value: sql ? `Present · ${sql.split("\n").length} lines` : NOT_EVALUATED,
    evidence: sql.length > 0,
  });

  const security = asRecord(output.sql_security);
  const decisions = Array.isArray(security?.decisions)
    ? (security?.decisions as unknown[])
    : null;
  if (decisions && decisions.length) {
    for (const decision of decisions.slice(0, MAX_POLICY_ROWS)) {
      const record = asRecord(decision);
      const rule = asText(record?.rule) || asText(record?.policy_name) || "policy rule";
      const allowed = record?.allowed === true;
      const reason = asText(record?.reason);
      rows.push({
        label: rule,
        value: join(allowed ? "Allowed" : "Denied", reason) || (allowed ? "Allowed" : "Denied"),
        evidence: true,
      });
    }
  } else {
    rows.push({ label: "SQL policy decisions", value: NOT_EVALUATED, evidence: false });
  }

  const reflection = asRecord(output.reflection);
  const strategy = asText(reflection?.strategy);
  const reflectionReason = asText(reflection?.reason);
  rows.push({
    label: "Reflection",
    value: join(strategy, reflectionReason) || NOT_EVALUATED,
    evidence: Boolean(strategy || reflectionReason),
  });

  const retryCount = output.retry_count;
  rows.push({
    label: "Retries",
    value: typeof retryCount === "number" ? String(retryCount) : NOT_EVALUATED,
    evidence: typeof retryCount === "number",
  });

  const attempts = output.sql_attempt_history;
  rows.push({
    label: "SQL attempts",
    value: Array.isArray(attempts) ? `${attempts.length}` : NOT_EVALUATED,
    evidence: Array.isArray(attempts),
  });

  const metricSearch = asRecord(output.metric_search);
  const metricStatus = asText(metricSearch?.status);
  const metricMatches = Array.isArray(metricSearch?.matches)
    ? (metricSearch?.matches as unknown[]).length
    : null;
  rows.push({
    label: "Semantic metric match",
    value:
      metricSearch && (metricStatus || metricMatches !== null)
        ? join(
            metricStatus || "reported",
            metricMatches !== null ? `${metricMatches} match(es)` : undefined,
          )
        : NOT_EVALUATED,
    evidence: Boolean(metricSearch && (metricStatus || metricMatches !== null)),
  });

  const team = asRecord(output.agent_team);
  const delivery = asRecord(team?.delivery_report);
  const deliveryStatus = asText(delivery?.status);
  rows.push({
    label: "Agent team delivery",
    value: deliveryStatus || NOT_EVALUATED,
    evidence: Boolean(deliveryStatus),
  });

  const toolLoop = asRecord(output.tool_loop);
  const loopStatus = asText(toolLoop?.status);
  rows.push({
    label: "Tool loop",
    value: loopStatus || NOT_EVALUATED,
    evidence: Boolean(loopStatus),
  });

  return { rows };
}

/** Number of rows backed by real evidence. */
export function trustEvidenceCount(trace: TrustTrace): number {
  return trace.rows.filter((row) => row.evidence).length;
}
