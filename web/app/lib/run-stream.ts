/**
 * Real consumer for the QueryForge streaming event protocol (v1).
 *
 * The Studio used to show a staged progress panel driven by request milestones
 * while `POST /ask/stream` existed with no client. This module is that client:
 * it opens the SSE route, parses the frames the Python route actually writes
 * (`data: {json}\n\n`), and reports only what those frames contain.
 *
 * Honesty rules encoded here (each one is pinned by `web/tests/ask-stream.test.mjs`):
 *
 * 1. nothing is simulated — no timers, no round-robin stages: `items` grows only
 *    when a frame arrives;
 * 2. the single terminal `final_result` frame closes the stream client, marks the
 *    run finished once, and a duplicate or late frame is counted, never rendered;
 * 3. a stream that ends without a terminal frame is a protocol violation, so it
 *    can never be presented as success (`streamRunStatus` → `failed`);
 * 4. a user abort reports `cancelled` and claims no outcome;
 * 5. protocol deviations (unknown protocol version, malformed frame, missing
 *    sequence, unrecognized outcome) are recorded in `violations` instead of
 *    being silently tolerated.
 */

import {
  normalizeStreamOutcome,
  runStatusFromOutcome,
  type RunStatus,
  type StreamOutcome,
} from "./run-status";

/** Version of the event protocol this client speaks (mirrors `PROTOCOL_VERSION`). */
export const EVENT_PROTOCOL_VERSION = "1";

/** Header the API sends with the protocol version it is serving. */
export const EVENT_PROTOCOL_HEADER = "x-queryforge-event-protocol";

/** The one terminal event type of protocol v1. */
export const TERMINAL_EVENT_TYPE = "final_result";

/** Exact wording shown when the stream closed without a terminal frame. */
export const MISSING_TERMINAL_DETAIL = "stream ended without a terminal event";

export type ProgressKind =
  | "run"
  | "node"
  | "phase"
  | "artifact"
  | "retry"
  | "unknown";

/** One rendered progress item: derived from exactly one received frame. */
export interface StreamProgressItem {
  protocolVersion: string;
  eventId: string;
  eventType: string;
  sequence: number;
  runId: string;
  taskId: string | null;
  nodeName: string | null;
  tool: string | null;
  phaseName: string | null;
  artifactType: string | null;
  status: string | null;
  message: string | null;
  timestamp: string;
  kind: ProgressKind;
  /** Human label built only from the frame's own fields. */
  label: string;
  /** Secondary line: tool/step/phase/artifact names plus the frame message. */
  detail: string;
}

/** The terminal frame, plus the payload it delivered. */
export interface StreamTerminal {
  eventId: string;
  sequence: number;
  eventType: typeof TERMINAL_EVENT_TYPE;
  runId: string;
  taskId: string | null;
  /** Validated protocol outcome, or `null` when the frame's value is unknown. */
  outcome: StreamOutcome | null;
  /** The raw `outcome` string as received, for honest diagnostics. */
  outcomeRaw: string | null;
  status: string | null;
  message: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
  /** `data.observability` of the terminal frame (usage/latency summary). */
  observability: Record<string, unknown> | null;
  cancelledAfterCompletion: boolean;
  timestamp: string;
}

export type StreamRunStatus =
  | "streaming"
  | "finished"
  | "cancelled"
  | "protocol_violation"
  | "failed";

export interface StreamRunState {
  /** Protocol version this client requires. */
  protocolVersion: string;
  /** Version the response header announced, when it announced one. */
  headerProtocolVersion: string | null;
  status: StreamRunStatus;
  runId: string;
  taskId: string | null;
  /** Outcome of the accepted terminal frame; `null` until one arrives. */
  outcome: StreamOutcome | null;
  terminal: StreamTerminal | null;
  items: StreamProgressItem[];
  tools: string[];
  phases: string[];
  artifacts: string[];
  framesReceived: number;
  progressFrames: number;
  /** Sequence number of the last accepted frame (0 before the first one). */
  lastSequence: number;
  duplicateTerminalFrames: number;
  lateFramesIgnored: number;
  sequenceGaps: number;
  outOfOrderFrames: number;
  droppedProgressSuspected: boolean;
  malformedFrames: number;
  violations: string[];
  /** Honest human-readable reason for the current status. */
  detail: string;
  cancelledByClient: boolean;
  result: Record<string, unknown> | null;
  observability: Record<string, unknown> | null;
  error: string | null;
}

export interface AskStreamOptions {
  /** Same-origin proxy path, e.g. `/api/queryforge/ask/stream`. */
  url: string;
  /** Request body sent to the API (the same body `POST /ask` receives). */
  body: Record<string, unknown>;
  /** Aborting this signal cancels the run client-side. */
  signal?: AbortSignal;
  /** Injectable for tests; defaults to `fetch`. */
  fetchImpl?: typeof fetch;
  /** Called after every accepted frame (and once for the initial state). */
  onUpdate?: (state: StreamRunState) => void;
}

export function initialStreamState(): StreamRunState {
  return {
    protocolVersion: EVENT_PROTOCOL_VERSION,
    headerProtocolVersion: null,
    status: "streaming",
    runId: "",
    taskId: null,
    outcome: null,
    terminal: null,
    items: [],
    tools: [],
    phases: [],
    artifacts: [],
    framesReceived: 0,
    progressFrames: 0,
    lastSequence: 0,
    duplicateTerminalFrames: 0,
    lateFramesIgnored: 0,
    sequenceGaps: 0,
    outOfOrderFrames: 0,
    droppedProgressSuspected: false,
    malformedFrames: 0,
    violations: [],
    detail: "",
    cancelledByClient: false,
    result: null,
    observability: null,
    error: null,
  };
}

/**
 * Incremental `text/event-stream` frame splitter.
 *
 * A chunk boundary may fall anywhere, including inside the blank line that
 * separates two frames, so the tail is kept until the next `push`. Only `data:`
 * lines carry protocol payloads; comments (`: keep-alive`) and `event:`/`id:`/
 * `retry:` fields are ignored because every payload names its own event type.
 */
export class SseFrameParser {
  private buffer = "";

  private static readonly SEPARATOR = /\r?\n\r?\n/;

  push(chunk: string): string[] {
    this.buffer += chunk;
    const payloads: string[] = [];
    for (;;) {
      const match = SseFrameParser.SEPARATOR.exec(this.buffer);
      if (!match || match.index === undefined) break;
      const rawEvent = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const payload = dataOfEvent(rawEvent);
      if (payload !== null) payloads.push(payload);
    }
    return payloads;
  }

  /** Unparsed remainder; non-empty only for a truncated (never completed) frame. */
  get pending(): string {
    return this.buffer;
  }
}

function dataOfEvent(rawEvent: string): string | null {
  const lines = rawEvent.split(/\r?\n/);
  const data: string[] = [];
  for (const line of lines) {
    if (line.startsWith(":")) continue;
    if (!line.startsWith("data:")) continue;
    const value = line.slice("data:".length);
    data.push(value.startsWith(" ") ? value.slice(1) : value);
  }
  return data.length ? data.join("\n") : null;
}

const PROGRESS_KINDS: Record<string, ProgressKind> = {
  run_started: "run",
  node_started: "node",
  node_completed: "node",
  node_failed: "node",
  phase_started: "phase",
  phase_completed: "phase",
  artifact_created: "artifact",
  retrying: "retry",
};

/**
 * Label one progress frame. Every word comes from the frame itself; an event
 * type this client does not know is shown verbatim rather than guessed at.
 */
export function progressLabel(frame: {
  eventType: string;
  nodeName: string | null;
  tool: string | null;
  phaseName: string | null;
  artifactType: string | null;
  message: string | null;
}): string {
  const suffix = (value: string | null) => (value ? `: ${value}` : "");
  switch (frame.eventType) {
    case "run_started":
      return `Run started${suffix(frame.message)}`;
    case "node_started":
      return `Node started${suffix(frame.nodeName)}`;
    case "node_completed":
      return `Node completed${suffix(frame.nodeName)}`;
    case "node_failed":
      return `Node failed${suffix(frame.nodeName)}`;
    case "phase_started":
      return `Phase started${suffix(frame.phaseName)}`;
    case "phase_completed":
      return `Phase completed${suffix(frame.phaseName)}`;
    case "artifact_created":
      return `Artifact created${suffix(frame.artifactType)}`;
    case "retrying":
      return `Retrying${suffix(frame.nodeName ?? frame.tool)}`;
    default:
      return frame.eventType;
  }
}

/** Secondary line of a progress item: names and message, all from the frame. */
function progressDetail(frame: {
  tool: string | null;
  nodeName: string | null;
  phaseName: string | null;
  artifactType: string | null;
  status: string | null;
  message: string | null;
}): string {
  const parts: string[] = [];
  if (frame.tool) parts.push(`tool ${frame.tool}`);
  if (frame.nodeName && frame.nodeName !== frame.tool) {
    parts.push(`step ${frame.nodeName}`);
  }
  if (frame.phaseName) parts.push(`phase ${frame.phaseName}`);
  if (frame.artifactType) parts.push(`artifact ${frame.artifactType}`);
  if (frame.status) parts.push(`status ${frame.status}`);
  if (frame.message) parts.push(frame.message);
  return parts.join(" · ");
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function pushViolation(state: StreamRunState, violation: string): void {
  if (!state.violations.includes(violation)) state.violations.push(violation);
}

/** Build one rendered progress item from a non-terminal frame. */
export function progressItemOf(record: Record<string, unknown>): StreamProgressItem {
  const eventType = asText(record.event_type);
  const nodeName = asText(record.node_name) || null;
  const tool = asText(record.tool) || null;
  const phaseName = asText(record.phase_name) || null;
  const artifactType = asText(record.artifact_type) || null;
  const message = asText(record.message) || null;
  const status = asText(record.status) || null;
  return {
    protocolVersion: asText(record.protocol_version),
    eventId: asText(record.event_id),
    eventType,
    sequence: asInteger(record.sequence) ?? 0,
    runId: asText(record.run_id),
    taskId: asText(record.task_id) || null,
    nodeName,
    tool,
    phaseName,
    artifactType,
    status,
    message,
    timestamp: asText(record.timestamp),
    kind: PROGRESS_KINDS[eventType] ?? "unknown",
    label: progressLabel({
      eventType,
      nodeName,
      tool,
      phaseName,
      artifactType,
      message,
    }),
    detail: progressDetail({
      tool,
      nodeName,
      phaseName,
      artifactType,
      status,
      message,
    }),
  };
}

function terminalOf(
  record: Record<string, unknown>,
  state: StreamRunState,
): StreamTerminal {
  const outcomeRaw = asText(record.outcome) || null;
  const outcome = normalizeStreamOutcome(record.outcome);
  if (!outcome) {
    // A terminal frame without a recognized outcome is a protocol deviation; it
    // stays `null` so no caller can read a success out of it.
    pushViolation(state, `terminal_outcome_unrecognized:${outcomeRaw ?? "missing"}`);
  }
  const data = asRecord(record.data);
  return {
    eventId: asText(record.event_id),
    sequence: asInteger(record.sequence) ?? 0,
    eventType: TERMINAL_EVENT_TYPE,
    runId: asText(record.run_id),
    taskId: asText(record.task_id) || null,
    outcome,
    outcomeRaw,
    status: asText(record.status) || null,
    message: asText(record.message) || null,
    error: asText(record.error) || null,
    result: asRecord(record.result),
    observability: asRecord(data?.observability),
    cancelledAfterCompletion: data?.cancelled_after_completion === true,
    timestamp: asText(record.timestamp),
  };
}

function trackSequence(state: StreamRunState, sequence: number): void {
  const last = state.lastSequence;
  if (sequence <= last) {
    state.outOfOrderFrames += 1;
    return;
  }
  if (sequence > last + 1) {
    // Protocol v1 permits dropping non-critical progress events under
    // backpressure (the terminal event is never dropped), so a gap is reported
    // as suspected dropped progress rather than as a violation.
    state.sequenceGaps += 1;
    state.droppedProgressSuspected = true;
  }
  state.lastSequence = sequence;
}

/**
 * Accept one SSE payload.
 * Returns `false` exactly once per run: for the terminal `final_result` frame,
 * which is the signal for the caller to close the stream client.
 */
function acceptFrame(
  state: StreamRunState,
  payload: string,
  notify: () => void,
): boolean {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    state.malformedFrames += 1;
    pushViolation(state, "malformed_frame");
    notify();
    return true;
  }
  const record = asRecord(parsed);
  if (!record) {
    state.malformedFrames += 1;
    pushViolation(state, "frame_is_not_an_object");
    notify();
    return true;
  }

  const eventType = asText(record.event_type);
  const runId = asText(record.run_id);
  if (!eventType || !runId) {
    state.malformedFrames += 1;
    pushViolation(state, "frame_missing_identity");
    notify();
    return true;
  }

  state.framesReceived += 1;
  const protocolVersion = asText(record.protocol_version);
  if (protocolVersion && protocolVersion !== EVENT_PROTOCOL_VERSION) {
    pushViolation(state, `frame_protocol_version:${protocolVersion}`);
  }
  const sequence = asInteger(record.sequence);
  if (sequence === null) pushViolation(state, "frame_missing_sequence");
  else trackSequence(state, sequence);

  if (state.terminal !== null) {
    // The terminal frame closes the run: anything after it is counted, never
    // rendered (the emitter refuses to publish these; this is the client's own
    // belt-and-braces rule).
    if (eventType === TERMINAL_EVENT_TYPE) state.duplicateTerminalFrames += 1;
    else state.lateFramesIgnored += 1;
    notify();
    return true;
  }

  if (!state.runId) state.runId = runId;
  const taskId = asText(record.task_id);
  if (taskId && !state.taskId) state.taskId = taskId;

  if (eventType === TERMINAL_EVENT_TYPE) {
    const terminal = terminalOf(record, state);
    state.terminal = terminal;
    state.outcome = terminal.outcome;
    state.result = terminal.result;
    state.observability = terminal.observability;
    state.error = terminal.error;
    if (terminal.taskId && !state.taskId) state.taskId = terminal.taskId;
    notify();
    return false;
  }

  const item = progressItemOf(record);
  state.progressFrames += 1;
  state.items.push(item);
  if (item.tool && !state.tools.includes(item.tool)) state.tools.push(item.tool);
  if (item.phaseName && !state.phases.includes(item.phaseName)) {
    state.phases.push(item.phaseName);
  }
  if (item.artifactType && !state.artifacts.includes(item.artifactType)) {
    state.artifacts.push(item.artifactType);
  }
  notify();
  return true;
}

function finalize(
  state: StreamRunState,
  aborted: boolean,
  readError: unknown,
): void {
  if (state.terminal) {
    state.status = "finished";
    state.outcome = state.terminal.outcome;
    state.detail = state.terminal.error ?? state.terminal.message ?? "";
    return;
  }
  if (aborted) {
    state.status = "cancelled";
    state.cancelledByClient = true;
    state.outcome = "cancelled";
    state.detail =
      "Cancelled by the client: the stream was aborted before a terminal event, so no run outcome is claimed.";
    return;
  }
  if (readError) {
    state.status = "failed";
    state.outcome = null;
    state.detail = `Stream failed before a terminal event: ${
      readError instanceof Error ? readError.message : String(readError)
    }`;
    return;
  }
  state.status = "protocol_violation";
  state.outcome = null;
  pushViolation(state, "missing_terminal_event");
  state.detail = `${MISSING_TERMINAL_DETAIL} — no outcome is claimed.`;
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

async function failureDetail(response: Response): Promise<string> {
  try {
    const payload = asRecord(await response.json());
    const detail = asText(payload?.detail);
    if (detail) return detail;
  } catch {
    // A non-JSON error body carries no reason the UI could show.
  }
  return "";
}

/**
 * Open `/ask/stream` and consume it until the terminal frame, a client abort,
 * or the end of the response. The returned state is the ground truth for the
 * UI: it never contains an item that did not come from a frame.
 */
export async function consumeAskStream(
  options: AskStreamOptions,
): Promise<StreamRunState> {
  const state = initialStreamState();
  const notify = () => {
    options.onUpdate?.(state);
  };
  notify();

  const fetchImpl = options.fetchImpl ?? fetch;
  const signal = options.signal;

  let response: Response;
  try {
    response = await fetchImpl(options.url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "text/event-stream",
      },
      body: JSON.stringify(options.body),
      signal,
    });
  } catch (error) {
    if (signal?.aborted || isAbortError(error)) {
      finalize(state, true, null);
    } else {
      state.status = "failed";
      state.detail = `Stream request failed: ${
        error instanceof Error ? error.message : String(error)
      }`;
    }
    notify();
    return state;
  }

  const headerProtocol = response.headers.get(EVENT_PROTOCOL_HEADER);
  if (headerProtocol) state.headerProtocolVersion = headerProtocol.trim();
  if (
    state.headerProtocolVersion &&
    state.headerProtocolVersion !== EVENT_PROTOCOL_VERSION
  ) {
    pushViolation(state, `unsupported_event_protocol:${state.headerProtocolVersion}`);
  }

  if (!response.ok) {
    const detail = await failureDetail(response);
    state.status = "failed";
    state.detail = `Stream request failed with HTTP ${response.status}${
      response.statusText ? ` ${response.statusText}` : ""
    }${detail ? ` — ${detail}` : ""}.`;
    notify();
    return state;
  }

  if (!response.body) {
    state.status = "failed";
    state.detail = "Stream response contained no body.";
    notify();
    return state;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseFrameParser();
  let readError: unknown = null;
  const abortReader = () => {
    void reader.cancel().catch(() => undefined);
  };
  if (signal) {
    if (signal.aborted) abortReader();
    else signal.addEventListener("abort", abortReader, { once: true });
  }

  try {
    for (;;) {
      const { done, value } = await reader.read();
      let terminalAccepted = false;
      if (value && value.byteLength) {
        for (const payload of parser.push(decoder.decode(value, { stream: true }))) {
          // `false` means this payload was the run's terminal event; the rest of
          // this already-received chunk is still inspected so a duplicate or
          // late frame is *counted* (and rendered nowhere), then reading stops.
          if (!acceptFrame(state, payload, notify)) terminalAccepted = true;
        }
      }
      if (terminalAccepted) break;
      if (done) break;
    }
  } catch (error) {
    readError = error;
  } finally {
    signal?.removeEventListener("abort", abortReader);
    // The terminal frame closes the stream client: nothing may follow it, so the
    // response body is released here. A cancelled or failed consumer releases it
    // too, instead of leaking the connection.
    await reader.cancel().catch(() => undefined);
  }

  finalize(state, Boolean(signal?.aborted), readError);
  notify();
  return state;
}

/**
 * Payload-free usage/latency summary of the terminal frame
 * (`data.observability`, built by the Python side's span recorder).
 */
export interface StreamObservability {
  runId: string | null;
  taskId: string | null;
  modelCalls: number | null;
  totalTokens: number | null;
  /** True when any call reported estimated rather than measured tokens. */
  estimated: boolean;
  endToEndMs: number | null;
  spanCount: number | null;
  priceTableConfigured: boolean;
}

/**
 * Read the observability summary a terminal frame carried. Returns `null` when
 * the frame carried none (progress-less ends, cancellations, failures) instead
 * of inventing zeroes.
 */
export function streamObservability(
  state: StreamRunState,
): StreamObservability | null {
  const observability = state.observability;
  if (!observability) return null;
  const usage = asRecord(observability.usage);
  const latency = asRecord(observability.latency);
  return {
    runId: asText(observability.run_id) || null,
    taskId: asText(observability.task_id) || null,
    modelCalls: asInteger(usage?.model_calls),
    totalTokens: asInteger(usage?.total_tokens),
    estimated: usage?.estimated === true,
    endToEndMs:
      typeof latency?.end_to_end_ms === "number" ? latency.end_to_end_ms : null,
    spanCount: asInteger(latency?.span_count),
    priceTableConfigured: usage?.price_table_configured === true,
  };
}

/**
 * Provenance-free facts of the consumed stream, attached to the run view so the
 * Trust Trace can show what the transport actually did (frames, tools, artifacts
 * and every recorded protocol deviation).
 */
export function streamEvidence(state: StreamRunState): Record<string, unknown> {
  return {
    protocol_version: state.headerProtocolVersion ?? state.protocolVersion,
    run_id: state.terminal?.runId || state.runId,
    task_id: state.terminal?.taskId ?? state.taskId,
    terminal_sequence: state.terminal?.sequence ?? null,
    outcome: state.terminal?.outcome ?? null,
    outcome_raw: state.terminal?.outcomeRaw ?? null,
    status: state.status,
    frames_received: state.framesReceived,
    progress_frames: state.progressFrames,
    tools: [...state.tools],
    phases: [...state.phases],
    artifacts: [...state.artifacts],
    violations: [...state.violations],
    dropped_progress_suspected: state.droppedProgressSuspected,
    cancelled_by_client: state.cancelledByClient,
  };
}

/**
 * Studio status of a consumed stream. Outcome first: a stream that ended without
 * a terminal frame (or with an unrecognized outcome) is `failed`, never success.
 */
export function streamRunStatus(state: StreamRunState): RunStatus {
  if (state.status === "cancelled") return "cancelled";
  if (state.status === "finished" || state.status === "protocol_violation") {
    return runStatusFromOutcome(state.terminal?.outcome ?? null);
  }
  return "failed";
}

/**
 * Honest reason line for a streamed run. Every sentence is backed by a frame,
 * the client's own abort, or a recorded protocol violation.
 */
export function streamRunDetail(state: StreamRunState): string {
  const parts: string[] = [];
  if (state.detail) parts.push(state.detail);
  else if (state.status === "protocol_violation") {
    parts.push(`${MISSING_TERMINAL_DETAIL} — no outcome is claimed.`);
  }
  if (state.terminal?.error) parts.push(state.terminal.error);
  const reason = asText(state.terminal?.result?.reason);
  if (reason && state.outcome !== "success") parts.push(reason);
  if (state.terminal?.cancelledAfterCompletion) {
    parts.push(
      "Cancellation arrived after the workflow returned; the already recorded outcome was preserved.",
    );
  }
  for (const violation of state.violations) {
    parts.push(`Protocol violation: ${violation}.`);
  }
  if (state.droppedProgressSuspected) {
    parts.push(
      `Progress frames were dropped by stream backpressure (${state.sequenceGaps} sequence gap(s)); the terminal event is never dropped.`,
    );
  }
  if (state.duplicateTerminalFrames > 0) {
    parts.push(
      `${state.duplicateTerminalFrames} duplicate terminal frame(s) were ignored.`,
    );
  }
  if (state.lateFramesIgnored > 0) {
    parts.push(
      `${state.lateFramesIgnored} frame(s) after the terminal event were ignored.`,
    );
  }
  if (state.malformedFrames > 0) {
    parts.push(`${state.malformedFrames} malformed frame(s) were skipped.`);
  }
  return parts.join(" ").trim();
}
