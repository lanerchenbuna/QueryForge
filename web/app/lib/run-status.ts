/**
 * Studio run status + provenance protocol.
 *
 * Business status and provenance are independent: a `success` run can only be
 * `live` when it was produced by the configured backend, and demo evidence is
 * always tagged `demo`. Unknown statuses never normalize to `success`, so a
 * malformed or truncated response can never be presented as a passed run.
 */

export type RunStatus =
  | "success"
  | "partial"
  | "blocked"
  | "failed"
  | "planned"
  | "cancelled"
  | "needs_clarification";

export type Provenance = "live" | "demo";

/**
 * Transport that produced a run. The Studio labels this instead of implying that
 * every run came from the same path: `live-request` is the single-response
 * `POST /ask` call, `live-stream` is the SSE `POST /ask/stream` consumer, and
 * `demo` is the deterministic offline fixture.
 */
export type RunMode = "live-request" | "live-stream" | "demo";

export const RUN_STATUSES: readonly RunStatus[] = [
  "success",
  "partial",
  "blocked",
  "failed",
  "planned",
  "cancelled",
  "needs_clarification",
];

const RUN_STATUS_SET = new Set<string>(RUN_STATUSES);

/**
 * Termination vocabulary of streaming event protocol v1
 * (`queryforge/workflow/event_emitter.py`). It is deliberately a subset of
 * `RUN_STATUSES`, so a streamed outcome maps onto a Studio status without a
 * translation that could soften it.
 */
export type StreamOutcome =
  | "success"
  | "partial"
  | "blocked"
  | "failed"
  | "cancelled";

export const STREAM_OUTCOMES: readonly StreamOutcome[] = [
  "success",
  "partial",
  "blocked",
  "failed",
  "cancelled",
];

const STREAM_OUTCOME_SET = new Set<string>(STREAM_OUTCOMES);

/**
 * Normalize a terminal frame's `outcome`.
 * Unknown, empty, or non-string values return `null` — never a guess, and never
 * `success`.
 */
export function normalizeStreamOutcome(raw: unknown): StreamOutcome | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim().toLowerCase();
  return STREAM_OUTCOME_SET.has(value) ? (value as StreamOutcome) : null;
}

/** Display label for a protocol outcome; unknown outcomes say so. */
export function outcomeLabel(outcome: unknown): string {
  const normalized = normalizeStreamOutcome(outcome);
  if (!normalized) return "Unknown outcome";
  return statusLabel(normalized);
}

/**
 * Studio status of a streamed run, taken from the protocol outcome only.
 *
 * A terminal frame whose outcome is missing or outside the vocabulary is a
 * protocol deviation: it maps to `failed` because an unrecognized termination
 * must never be rendered as a passed run.
 */
export function runStatusFromOutcome(raw: unknown): RunStatus {
  const outcome = normalizeStreamOutcome(raw);
  return outcome ?? "failed";
}

/** Label for the transport that produced a result. */
export function runModeLabel(mode: RunMode): string {
  switch (mode) {
    case "live-stream":
      return "Live stream";
    case "live-request":
      return "Live request";
    case "demo":
      return "Offline demo";
    default:
      return "Unknown mode";
  }
}

/** Run history labels: display strings mapped from the real protocol status. */
export type RunRecordStatus =
  | "Passed"
  | "Partial"
  | "Blocked"
  | "Failed"
  | "Cancelled"
  | "Planned"
  | "Needs clarification"
  | "Running";

/** A run result as rendered by the Studio. */
export interface RunView {
  /** Where the data came from. Demo evidence is never presented as live. */
  provenance: Provenance;
  /** Which live transport (or the offline fixture) produced this result. */
  mode: RunMode;
  /** Real backend status; never inferred from the HTTP status code. */
  status: RunStatus;
  runId: string;
  explanation: string;
  /** Plan-only or blocked text returned instead of a result table. */
  planText: string;
  /** Honest failure reason: API `detail`, policy reason, or HTTP status text. */
  detail: string;
  sql: string;
  columns: string[];
  rows: Array<Array<string | number>>;
  rowCount: number;
  /** Raw backend output kept for the Trust Trace evidence. */
  output: Record<string, unknown> | null;
}

/**
 * Map an unknown backend value to the protocol status.
 * Unknown, empty, or non-string values normalize to `failed` — never `success`.
 */
export function normalizeRunStatus(raw: unknown): RunStatus {
  if (typeof raw === "string") {
    const value = raw.trim().toLowerCase();
    if (RUN_STATUS_SET.has(value)) return value as RunStatus;
  }
  return "failed";
}

/** Provenance is a property of the connection used to produce the run. */
export function provenanceOf(connection: "live" | "demo"): Provenance {
  return connection === "live" ? "live" : "demo";
}

/** Statuses that must never render rows invented by the client. */
export function isFailureStatus(status: RunStatus): boolean {
  return status !== "success";
}

export function statusLabel(status: RunStatus): string {
  switch (status) {
    case "success":
      return "Success";
    case "partial":
      return "Partial";
    case "blocked":
      return "Blocked";
    case "failed":
      return "Failed";
    case "planned":
      return "Plan only";
    case "cancelled":
      return "Cancelled";
    case "needs_clarification":
      return "Needs clarification";
    default:
      return "Failed";
  }
}

/** Display label for run history. Real statuses keep their real meaning. */
export function runRecordStatus(status: RunStatus): RunRecordStatus {
  switch (status) {
    case "success":
      return "Passed";
    case "partial":
      return "Partial";
    case "blocked":
      return "Blocked";
    case "planned":
      return "Planned";
    case "cancelled":
      return "Cancelled";
    case "needs_clarification":
      return "Needs clarification";
    default:
      return "Failed";
  }
}

/**
 * Status accepted by `POST /api/studio/runs`.
 * The runs route stores the real protocol vocabulary (`partial` included), but
 * does not accept `needs_clarification`, so that one is stored as `blocked`
 * (still a genuine non-success) instead of silently becoming success.
 */
export function persistableRunStatus(status: RunStatus): RunStatus {
  return status === "needs_clarification" ? "blocked" : status;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
/** Pull the honest error/blocked reason out of a backend payload. */
export function extractRunDetail(payload: unknown): string {
  if (!isRecord(payload)) return "";
  const candidates = [payload.detail, payload.error, payload.reason, payload.message];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  const security = payload.sql_security;
  if (isRecord(security)) {
    const decisions = security.decisions;
    if (Array.isArray(decisions)) {
      const denied = decisions.filter(
        (decision) => isRecord(decision) && decision.allowed === false,
      );
      const first = denied[0];
      if (isRecord(first)) {
        const rule = typeof first.rule === "string" ? first.rule : "";
        const reason = typeof first.reason === "string" ? first.reason : "";
        const text = [rule, reason].filter(Boolean).join(" — ");
        if (text) return text;
      }
    }
  }
  const status = payload.status;
  if (typeof status === "string" && status.trim() && status.trim() !== "success") {
    return `Backend returned status "${status.trim()}".`;
  }
  return "";
}

/** Text extracted for plan-only / blocked / clarification responses. */
export function extractRunText(payload: unknown): string {
  if (!isRecord(payload)) return "";
  const plan = payload.plan;
  if (isRecord(plan) && typeof plan.summary === "string" && plan.summary.trim()) {
    return plan.summary.trim();
  }
  if (typeof plan === "string" && plan.trim()) return plan.trim();
  const clarification = payload.clarification ?? payload.unresolved_questions;
  if (Array.isArray(clarification) && clarification.length) {
    return clarification.map(String).join("\n");
  }
  if (typeof clarification === "string" && clarification.trim()) {
    return clarification.trim();
  }
  return "";
}
