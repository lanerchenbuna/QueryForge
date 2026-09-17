/**
 * Real SSE consumer tests.
 *
 * The frames below are **recorded** ones: they were produced by the Python side
 * itself (the real `EventEmitter` + `WorkflowEventStream` + the route's own
 * `sse_event_generator` from `queryforge/interfaces/api/app.py`), then pasted
 * here verbatim. Recording command (run from the repository root, no server
 * needed, nothing written to the repository):
 *
 *   .venv/bin/python - <<'PY'
 *   from queryforge.workflow.event_emitter import EventEmitter, emit_event
 *   from queryforge.application.event_stream import WorkflowEventStream
 *   from queryforge.interfaces.api.app import sse_event_generator
 *   # ... emit progress events, then exactly one final_result; print each frame
 *   PY
 *
 * So the wire shape (field set and order, `data: {...}\n\n` framing, the
 * `sequence` counter, the terminal `result`/`data.observability` payloads) is the
 * real protocol, not an invented one. Only the *values* inside that recording
 * (run id, tokens, latency) are from that capture run. Nothing here performs a
 * network call: the consumer is driven through an injected `fetchImpl` over an
 * in-memory `ReadableStream`.
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const moduleCache = new Map();

function dataUrl(code) {
  return `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
}

/**
 * Transpile an `app/lib` module (and its relative imports) into an importable
 * data URL, the same technique `tests/publication-status.test.mjs` uses. Data
 * URLs cannot resolve relative specifiers, so each dependency is inlined as its
 * own data URL first.
 */
async function loadModule(fileUrl) {
  const cached = moduleCache.get(fileUrl.href);
  if (cached) return cached;
  const source = await readFile(fileUrl, "utf8");
  let code = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const specifiers = [
    ...new Set([...code.matchAll(/from\s+"(\.[^"]+)"/g)].map((match) => match[1])),
  ];
  for (const specifier of specifiers) {
    const dependencyUrl = new URL(
      specifier.endsWith(".ts") ? specifier : `${specifier}.ts`,
      fileUrl,
    );
    code = code.replaceAll(`"${specifier}"`, `"${await loadModule(dependencyUrl)}"`);
  }
  const url = dataUrl(code);
  moduleCache.set(fileUrl.href, url);
  return url;
}

const runStreamUrl = await loadModule(
  new URL("../app/lib/run-stream.ts", import.meta.url),
);
const runStatusUrl = await loadModule(
  new URL("../app/lib/run-status.ts", import.meta.url),
);

const {
  EVENT_PROTOCOL_VERSION,
  MISSING_TERMINAL_DETAIL,
  SseFrameParser,
  consumeAskStream,
  initialStreamState,
  streamEvidence,
  streamObservability,
  streamRunDetail,
  streamRunStatus,
} = await import(runStreamUrl);
const {
  RUN_STATUSES,
  STREAM_OUTCOMES,
  normalizeStreamOutcome,
  outcomeLabel,
  persistableRunStatus,
  runModeLabel,
  runStatusFromOutcome,
  statusLabel,
} = await import(runStatusUrl);

const RUN_ID = "qf_9f2c1d4e7a5b48c3ab6d0e1f2a3b4c5d";

/** Recorded happy-path frames: 9 progress frames then one terminal frame. */
const RECORDED_FRAMES = [
  {
    protocol_version: "1",
    event_id: "evt_cc6dceb42d214b299ab6efb4688d9810",
    event_type: "run_started",
    timestamp: "2026-09-17T03:29:23.297930+00:00",
    run_id: RUN_ID,
    sequence: 1,
    task_id: "task_3b7e1a90",
    node_name: null,
    tool: null,
    phase_name: null,
    artifact_type: null,
    status: "running",
    outcome: null,
    message: "Started QueryForge workflow.",
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_f0a6ffb772904a78be844c1befe32b1c",
    event_type: "node_started",
    timestamp: "2026-09-17T03:29:23.297964+00:00",
    run_id: RUN_ID,
    sequence: 2,
    task_id: "task_3b7e1a90",
    node_name: "gen_sql",
    tool: "generate_sql",
    phase_name: null,
    artifact_type: null,
    status: "running",
    outcome: null,
    message: null,
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_5cf7345d11144be6a6f2d6a3f6d874a4",
    event_type: "node_completed",
    timestamp: "2026-09-17T03:29:23.297977+00:00",
    run_id: RUN_ID,
    sequence: 3,
    task_id: "task_3b7e1a90",
    node_name: "gen_sql",
    tool: "generate_sql",
    phase_name: null,
    artifact_type: null,
    status: "completed",
    outcome: null,
    message: null,
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_bfb2413ea7eb4085a81a0c5c9203a7a9",
    event_type: "phase_started",
    timestamp: "2026-09-17T03:29:23.297985+00:00",
    run_id: RUN_ID,
    sequence: 4,
    task_id: "task_3b7e1a90",
    node_name: null,
    tool: null,
    phase_name: "execution",
    artifact_type: null,
    status: null,
    outcome: null,
    message: null,
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_751a02367717449a8b6aa574aff89261",
    event_type: "node_started",
    timestamp: "2026-09-17T03:29:23.297992+00:00",
    run_id: RUN_ID,
    sequence: 5,
    task_id: "task_3b7e1a90",
    node_name: "execute_sql",
    tool: "execute_sql",
    phase_name: null,
    artifact_type: null,
    status: "running",
    outcome: null,
    message: null,
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_f48d87f075144e639612d1d64a040800",
    event_type: "artifact_created",
    timestamp: "2026-09-17T03:29:23.297998+00:00",
    run_id: RUN_ID,
    sequence: 6,
    task_id: "task_3b7e1a90",
    node_name: null,
    tool: null,
    phase_name: null,
    artifact_type: "csv",
    status: null,
    outcome: null,
    message: "result.csv",
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_dcac451bd7604b57bda0f041b3393023",
    event_type: "retrying",
    timestamp: "2026-09-17T03:29:23.298005+00:00",
    run_id: RUN_ID,
    sequence: 7,
    task_id: "task_3b7e1a90",
    node_name: "fix_sql",
    tool: "fix_sql",
    phase_name: null,
    artifact_type: null,
    status: "retrying",
    outcome: null,
    message: "Repairing a failed statement.",
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_e683891ecd1f4433bc8acf40a865e18c",
    event_type: "node_completed",
    timestamp: "2026-09-17T03:29:23.298011+00:00",
    run_id: RUN_ID,
    sequence: 8,
    task_id: "task_3b7e1a90",
    node_name: "execute_sql",
    tool: "execute_sql",
    phase_name: null,
    artifact_type: null,
    status: "completed",
    outcome: null,
    message: null,
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_1289d4590fdd4c72b7af090440ba2765",
    event_type: "phase_completed",
    timestamp: "2026-09-17T03:29:23.298017+00:00",
    run_id: RUN_ID,
    sequence: 9,
    task_id: "task_3b7e1a90",
    node_name: null,
    tool: null,
    phase_name: "execution",
    artifact_type: null,
    status: null,
    outcome: null,
    message: null,
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_f74514fd1df94bd9b64fb49cb5ee2001",
    event_type: "final_result",
    timestamp: "2026-09-17T03:29:23.298023+00:00",
    run_id: RUN_ID,
    sequence: 10,
    task_id: "task_3b7e1a90",
    node_name: null,
    tool: null,
    phase_name: null,
    artifact_type: null,
    status: "success",
    outcome: "success",
    message: "QueryForge workflow completed.",
    data: {
      cancelled_after_completion: false,
      observability: {
        run_id: RUN_ID,
        task_id: "task_3b7e1a90",
        usage: {
          run_id: RUN_ID,
          task_id: "task_3b7e1a90",
          model_calls: 2,
          measured_calls: 1,
          prompt_tokens: 812,
          completion_tokens: 133,
          total_tokens: 945,
          estimated: true,
          estimated_cost_usd: null,
          price_table_configured: false,
          by_model: {
            "dashscope/qwen-plus": {
              calls: 2,
              prompt_tokens: 812,
              completion_tokens: 133,
              total_tokens: 945,
              estimated_calls: 1,
            },
          },
        },
        latency: {
          end_to_end_ms: 4312.75,
          by_kind: {
            model: { count: 2, duration_ms: 1180.503, max_duration_ms: 1180.5 },
            tool: { count: 1, duration_ms: 0.002, max_duration_ms: 0.002 },
            sql: { count: 1, duration_ms: 0.001, max_duration_ms: 0.001 },
            retrieval: { count: 0, duration_ms: 0.0, max_duration_ms: 0.0 },
            step: { count: 1, duration_ms: 0.001, max_duration_ms: 0.001 },
          },
          span_count: 5,
        },
      },
    },
    result: {
      status: "success",
      run_id: RUN_ID,
      question: "Compare watch hours and completion rate by genre",
      explanation: "Governed answer built from the reviewed semantic model.",
      columns: ["genre", "watch_hours", "completion_rate"],
      rows: [
        ["Action", 184223, 0.71],
        ["Drama", 121887, 0.64],
      ],
      row_count: 2,
      sql: "SELECT d.genre, SUM(f.watch_minutes) / 60.0 AS watch_hours\nFROM fact_watch_session f\nJOIN dim_anime d ON d.anime_id = f.anime_id\nGROUP BY d.genre\nORDER BY watch_hours DESC",
      sql_security: {
        decisions: [{ rule: "read_only_ast", allowed: true, reason: "SELECT only" }],
      },
      reflection: { strategy: "accepted", reason: "row count within bounds" },
      retry_count: 1,
      sql_attempt_history: [{ attempt: 1, status: "ok" }],
      metric_search: { status: "matched", matches: [{ metric: "watch_hours" }] },
      agent_team: { delivery_report: { status: "delivered" } },
      tool_loop: { status: "completed" },
    },
    error: null,
  },
];

const TERMINAL_FRAME = RECORDED_FRAMES[RECORDED_FRAMES.length - 1];
const PROGRESS_FRAMES = RECORDED_FRAMES.slice(0, -1);

/** Recorded cancelled-path frames, as emitted for a cancelled run. */
const RECORDED_CANCELLED_FRAMES = [
  RECORDED_FRAMES[0],
  {
    protocol_version: "1",
    event_id: "evt_131553367d66488ebf256f04db4f839d",
    event_type: "node_started",
    timestamp: "2026-09-17T03:29:23.299483+00:00",
    run_id: RUN_ID,
    sequence: 2,
    task_id: "task_3b7e1a90",
    node_name: "execute_sql",
    tool: "execute_sql",
    phase_name: null,
    artifact_type: null,
    status: "running",
    outcome: null,
    message: null,
    data: null,
    result: null,
    error: null,
  },
  {
    protocol_version: "1",
    event_id: "evt_98fa139443214d549e458d2421789bf1",
    event_type: "final_result",
    timestamp: "2026-09-17T03:29:23.299493+00:00",
    run_id: RUN_ID,
    sequence: 3,
    task_id: "task_3b7e1a90",
    node_name: null,
    tool: null,
    phase_name: null,
    artifact_type: null,
    status: "cancelled",
    outcome: "cancelled",
    message: "QueryForge workflow was cancelled.",
    data: null,
    result: {
      status: "cancelled",
      outcome: "cancelled",
      run_id: RUN_ID,
      question: "Compare watch hours and completion rate by genre",
      reason:
        "Client disconnected before the workflow completed (stopped at execute_sql).",
    },
    error: null,
  },
];

/** Encode frames exactly the way the route writes them (`data: {...}\n\n`). */
function sse(...frames) {
  return frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("");
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

/**
 * How many times a terminal frame was *accepted*. Every later update keeps
 * reporting the same (single) terminal, so counting updates that carry one would
 * not prove the "finished exactly once" contract; the transition does.
 */
function terminalAcceptances(updates) {
  let accepted = 0;
  let seen = false;
  for (const update of updates) {
    if (update.terminal && !seen) {
      accepted += 1;
      seen = true;
    }
  }
  return accepted;
}

/**
 * An in-memory SSE response that the test drives frame by frame. `cancelled`
 * records that the consumer closed the response body (the "terminal closes the
 * client" contract), never a real socket.
 */
function recordedResponse({ protocolHeader = "1" } = {}) {
  const encoder = new TextEncoder();
  let controller;
  let cancelled = false;
  const body = new ReadableStream({
    start(next) {
      controller = next;
    },
    cancel() {
      cancelled = true;
    },
  });
  const headers = { "content-type": "text/event-stream" };
  if (protocolHeader !== null) {
    headers["x-queryforge-event-protocol"] = protocolHeader;
  }
  return {
    response: new Response(body, { headers }),
    push(text) {
      controller.enqueue(encoder.encode(text));
    },
    close() {
      controller.close();
    },
    get cancelled() {
      return cancelled;
    },
  };
}

/** Start the consumer against a controllable recorded stream. */
function harness(options = {}) {
  const source = recordedResponse(options);
  const controller = new AbortController();
  const updates = [];
  const done = consumeAskStream({
    url: "/api/queryforge/ask/stream",
    body: { question: "Compare watch hours and completion rate by genre" },
    signal: controller.signal,
    fetchImpl: async () => source.response,
    // Snapshots are deep copies: the consumer mutates one state object in place,
    // so a snapshot must capture the moment the frame arrived.
    onUpdate: (state) =>
      updates.push(
        JSON.parse(
          JSON.stringify({
            route: state.status,
            items: state.items,
            terminal: state.terminal,
            frames: state.framesReceived,
            detail: state.detail,
          }),
        ),
      ),
  });
  return {
    source,
    controller,
    updates,
    done,
    push: (text) => source.push(text),
    close: () => source.close(),
    abort: () => controller.abort(),
  };
}

test("the recorded frames carry exactly the protocol v1 field set", () => {
  // Guards against silently drifting the recording away from the real shape:
  // these are the fields of `WorkflowEvent` in
  // `queryforge/workflow/event_emitter.py`, in the order the Python side
  // serializes them.
  const protocolFields = [
    "protocol_version",
    "event_id",
    "event_type",
    "timestamp",
    "run_id",
    "sequence",
    "task_id",
    "node_name",
    "tool",
    "phase_name",
    "artifact_type",
    "status",
    "outcome",
    "message",
    "data",
    "result",
    "error",
  ];
  for (const frame of [...RECORDED_FRAMES, ...RECORDED_CANCELLED_FRAMES]) {
    assert.deepEqual(Object.keys(frame), protocolFields);
    assert.equal(frame.protocol_version, "1");
  }
  // Only the terminal frame carries a result payload or the outcome field.
  assert.equal(RECORDED_FRAMES.filter((frame) => frame.result !== null).length, 1);
  assert.deepEqual(
    RECORDED_FRAMES.filter((frame) => frame.outcome !== null).map(
      (frame) => frame.outcome,
    ),
    ["success"],
  );
});

test("consumes recorded frames: progress items, tools, artifacts, one terminal outcome", async () => {
  const h = harness();
  // One chunk per frame: the number of rendered items must follow the frames.
  for (const frame of RECORDED_FRAMES) {
    h.push(sse(frame));
    await settle();
  }
  const state = await h.done;

  assert.equal(state.status, "finished");
  assert.equal(state.outcome, "success");
  assert.equal(state.runId, RUN_ID);
  assert.equal(state.taskId, "task_3b7e1a90");

  // Node/phase progress, plus the tool/step names and artifact of the frames.
  assert.deepEqual(
    state.items.map((item) => item.eventType),
    [
      "run_started",
      "node_started",
      "node_completed",
      "phase_started",
      "node_started",
      "artifact_created",
      "retrying",
      "node_completed",
      "phase_completed",
    ],
  );
  assert.deepEqual(
    state.items.map((item) => item.sequence),
    [1, 2, 3, 4, 5, 6, 7, 8, 9],
  );
  assert.deepEqual(state.tools, ["generate_sql", "execute_sql", "fix_sql"]);
  assert.deepEqual(state.artifacts, ["csv"]);
  assert.deepEqual(state.phases, ["execution"]);
  assert.equal(state.items[1].label, "Node started: gen_sql");
  assert.match(state.items[1].detail, /tool generate_sql/);
  assert.equal(state.items[5].label, "Artifact created: csv");
  assert.equal(state.items[5].detail, "artifact csv · result.csv");

  // The terminal frame carries the result and the observability summary.
  assert.equal(state.terminal.sequence, 10);
  assert.equal(state.terminal.outcome, "success");
  assert.equal(state.result.row_count, 2);
  assert.equal(state.result.rows[0][0], "Action");
  assert.equal(state.observability.usage.total_tokens, 945);

  // Rendering: one item per frame, the terminal accepted exactly once, and the
  // "finished" transition happening exactly once.
  const rendered = h.updates.map((update) => update.items.length);
  assert.deepEqual(rendered.slice(0, 10), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.ok(rendered.slice(10).every((count) => count === 9));
  assert.equal(terminalAcceptances(h.updates), 1);
  assert.equal(h.updates.filter((update) => update.route === "finished").length, 1);
  assert.equal(h.updates.at(-1).route, "finished");
  // A terminal frame is never rendered as a progress item.
  assert.ok(
    state.items.every((item) => item.eventType !== "final_result"),
  );

  // The terminal event closes the stream client.
  assert.equal(h.source.cancelled, true);
  assert.equal(state.violations.length, 0);
  assert.equal(streamRunStatus(state), "success");
});

test("frames split across chunk boundaries are still parsed exactly once", async () => {
  const h = harness();
  const wire = sse(...RECORDED_FRAMES);
  // Cut inside frames, inside `data:` lines and inside the blank separator.
  for (let index = 0; index < wire.length; index += 37) {
    h.push(wire.slice(index, index + 37));
  }
  const state = await h.done;
  assert.equal(state.status, "finished");
  assert.equal(state.items.length, PROGRESS_FRAMES.length);
  assert.equal(state.terminal.sequence, 10);
  assert.equal(state.malformedFrames, 0);
  assert.equal(state.framesReceived, RECORDED_FRAMES.length);
});

test("a client cancel aborts the stream and reports cancelled, never a guessed outcome", async () => {
  const h = harness();
  h.push(sse(...RECORDED_CANCELLED_FRAMES.slice(0, 2)));
  await settle();
  assert.equal(h.updates.at(-1).route, "streaming");

  h.abort();
  const state = await h.done;

  assert.equal(state.status, "cancelled");
  assert.equal(state.cancelledByClient, true);
  assert.equal(state.outcome, "cancelled");
  assert.equal(state.terminal, null);
  assert.equal(state.result, null);
  assert.equal(streamRunStatus(state), "cancelled");
  assert.match(streamRunDetail(state), /aborted before a terminal event/);
  // Only the frames that really arrived are rendered.
  assert.equal(state.items.length, 2);
  assert.deepEqual(
    state.items.map((item) => item.eventType),
    ["run_started", "node_started"],
  );
  // The client released the response body instead of leaking the connection.
  assert.equal(h.source.cancelled, true);
});

test("a recorded cancelled terminal event renders the cancelled outcome", async () => {
  const h = harness();
  h.push(sse(...RECORDED_CANCELLED_FRAMES));
  const state = await h.done;
  assert.equal(state.status, "finished");
  assert.equal(state.outcome, "cancelled");
  assert.equal(streamRunStatus(state), "cancelled");
  assert.match(streamRunDetail(state), /Client disconnected before the workflow completed/);
  assert.equal(state.items.length, 2);
});

test("a stream closed without a terminal event is a protocol violation, never success", async () => {
  const h = harness();
  h.push(sse(...RECORDED_FRAMES.slice(0, 3)));
  h.close();
  const state = await h.done;

  assert.equal(state.status, "protocol_violation");
  assert.equal(state.terminal, null);
  assert.equal(state.outcome, null);
  assert.ok(state.violations.includes("missing_terminal_event"));
  assert.match(streamRunDetail(state), new RegExp(MISSING_TERMINAL_DETAIL));
  assert.match(streamRunDetail(state), /no outcome is claimed/);
  // Honest status: no terminal event means no success.
  assert.equal(streamRunStatus(state), "failed");
  // The frames that did arrive stay visible.
  assert.equal(state.items.length, 3);
  assert.equal(terminalAcceptances(h.updates), 0);
  assert.equal(h.updates.at(-1).route, "protocol_violation");
});

test("duplicate terminal frames and post-terminal frames are counted, never re-rendered", async () => {
  const h = harness();
  h.push(sse(...RECORDED_FRAMES, TERMINAL_FRAME, RECORDED_FRAMES[1]));
  const state = await h.done;

  assert.equal(state.status, "finished");
  assert.equal(state.outcome, "success");
  assert.equal(state.duplicateTerminalFrames, 1);
  assert.equal(state.lateFramesIgnored, 1);
  assert.equal(state.items.length, PROGRESS_FRAMES.length);
  assert.equal(state.progressFrames, PROGRESS_FRAMES.length);
  assert.equal(terminalAcceptances(h.updates), 1);
  assert.equal(h.updates.filter((update) => update.route === "finished").length, 1);
  assert.match(streamRunDetail(state), /duplicate terminal frame\(s\) were ignored/);
  assert.match(streamRunDetail(state), /after the terminal event were ignored/);
});

test("no timer-based progress: nothing renders before a frame arrives", async () => {
  const h = harness();
  for (let tick = 0; tick < 5; tick += 1) {
    await settle();
  }
  assert.equal(h.updates.length, 1);
  assert.equal(h.updates[0].route, "streaming");
  assert.deepEqual(h.updates[0].items, []);
  assert.equal(h.updates[0].frames, 0);

  h.push(sse(RECORDED_FRAMES[0]));
  await settle();
  assert.equal(h.updates.length, 2);
  assert.deepEqual(
    h.updates[1].items.map((item) => item.eventType),
    ["run_started"],
  );

  h.push(sse(TERMINAL_FRAME));
  const state = await h.done;
  assert.equal(state.items.length, 1);
  assert.equal(state.status, "finished");
});

test("the consumer module contains no timer or wall-clock driven progress", async () => {
  const source = await readFile(
    new URL("../app/lib/run-stream.ts", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(
    source,
    /setTimeout|setInterval|requestAnimationFrame|Date\.now|performance\.now/,
  );
  assert.match(source, /MISSING_TERMINAL_DETAIL = "stream ended without a terminal event"/);
});

test("the progressive parser only emits complete frames and ignores comments", () => {
  const parser = new SseFrameParser();
  assert.deepEqual(parser.push(": keep-alive\n\n"), []);
  assert.deepEqual(parser.push('data: {"event_type":'), []);
  assert.deepEqual(parser.push('"run_started"}\n'), []);
  assert.deepEqual(parser.push("\n"), ['{"event_type":"run_started"}']);
  assert.equal(parser.pending, "");
  assert.deepEqual(parser.push("event: ping\nid: 7\nretry: 100\ndata: one\ndata: two\n\n"), [
    "one\ntwo",
  ]);
});

test("a progress frame dropped by backpressure is reported, not fabricated", async () => {
  const h = harness();
  // Backpressure may drop a non-critical progress frame, which leaves a sequence
  // gap in what the client sees (protocol v1 never drops the terminal event).
  const withoutArtifactFrame = RECORDED_FRAMES.filter(
    (frame) => frame.event_id !== "evt_f48d87f075144e639612d1d64a040800",
  );
  h.push(sse(...withoutArtifactFrame));
  const state = await h.done;

  assert.equal(state.status, "finished");
  assert.equal(state.outcome, "success");
  assert.equal(state.sequenceGaps, 1);
  assert.equal(state.droppedProgressSuspected, true);
  assert.equal(state.artifacts.length, 0);
  assert.equal(state.items.length, PROGRESS_FRAMES.length - 1);
  assert.match(streamRunDetail(state), /dropped by stream backpressure/);
  // A gap is a permitted drop, not a violation of the protocol.
  assert.equal(
    state.violations.filter((violation) => violation.includes("sequence")).length,
    0,
  );
});

test("malformed and unknown frames are recorded as violations, never rendered", async () => {
  const h = harness();
  h.push("data: {not json}\n\n");
  h.push('data: {"protocol_version":"1","event_type":"node_started"}\n\n'); // no run_id
  h.push(sse(RECORDED_FRAMES[0], TERMINAL_FRAME));
  const state = await h.done;

  assert.equal(state.status, "finished");
  assert.equal(state.malformedFrames, 2);
  assert.ok(state.violations.includes("malformed_frame"));
  assert.ok(state.violations.includes("frame_missing_identity"));
  assert.equal(state.items.length, 1);
  assert.match(streamRunDetail(state), /malformed frame\(s\) were skipped/);
});

test("a frame from another protocol version is flagged instead of trusted", async () => {
  assert.equal(EVENT_PROTOCOL_VERSION, "1");
  const h = harness({ protocolHeader: "2" });
  h.push(sse({ ...RECORDED_FRAMES[1], protocol_version: "2" }, TERMINAL_FRAME));
  const state = await h.done;

  assert.equal(state.headerProtocolVersion, "2");
  assert.ok(state.violations.includes("unsupported_event_protocol:2"));
  assert.ok(state.violations.includes("frame_protocol_version:2"));
  assert.match(streamRunDetail(state), /Protocol violation: unsupported_event_protocol:2/);
});

test("a terminal frame without a recognized outcome is never a success", async () => {
  const h = harness();
  h.push(
    sse(RECORDED_FRAMES[0], {
      ...TERMINAL_FRAME,
      outcome: "mostly_fine",
      status: "success",
    }),
  );
  const state = await h.done;

  assert.equal(state.status, "finished");
  assert.equal(state.terminal.outcome, null);
  assert.equal(state.terminal.outcomeRaw, "mostly_fine");
  assert.ok(
    state.violations.includes("terminal_outcome_unrecognized:mostly_fine"),
  );
  assert.equal(streamRunStatus(state), "failed");
});

test("a rejected request surfaces the HTTP status and backend detail", async () => {
  const state = await consumeAskStream({
    url: "/api/queryforge/ask/stream",
    body: { question: "no such database" },
    fetchImpl: async () =>
      new Response(JSON.stringify({ detail: "database path does not exist" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      }),
  });
  assert.equal(state.status, "failed");
  assert.match(state.detail, /HTTP 400/);
  assert.match(state.detail, /database path does not exist/);
  assert.equal(state.items.length, 0);
  assert.equal(streamRunStatus(state), "failed");
});

test("the terminal frame's observability summary is read from the frame only", async () => {
  const h = harness();
  h.push(sse(...RECORDED_FRAMES));
  const successState = await h.done;
  const usage = streamObservability(successState);

  assert.equal(usage.runId, RUN_ID);
  assert.equal(usage.taskId, "task_3b7e1a90");
  assert.equal(usage.modelCalls, 2);
  assert.equal(usage.totalTokens, 945);
  assert.equal(usage.estimated, true);
  assert.equal(usage.endToEndMs, 4312.75);
  assert.equal(usage.spanCount, 5);
  assert.equal(usage.priceTableConfigured, false);

  const evidence = streamEvidence(successState);
  assert.equal(evidence.protocol_version, "1");
  assert.equal(evidence.terminal_sequence, 10);
  assert.equal(evidence.outcome, "success");
  assert.equal(evidence.frames_received, 10);
  assert.deepEqual(evidence.tools, ["generate_sql", "execute_sql", "fix_sql"]);
  assert.deepEqual(evidence.artifacts, ["csv"]);

  // A cancelled run carries no summary: null, never zeroes.
  const cancelled = harness();
  cancelled.push(sse(...RECORDED_CANCELLED_FRAMES));
  const cancelledState = await cancelled.done;
  assert.equal(streamObservability(cancelledState), null);
  assert.equal(streamObservability(initialStreamState()), null);
});

test("stream outcomes map onto Studio statuses without softening", () => {
  assert.deepEqual(STREAM_OUTCOMES, [
    "success",
    "partial",
    "blocked",
    "failed",
    "cancelled",
  ]);
  for (const outcome of STREAM_OUTCOMES) {
    assert.equal(normalizeStreamOutcome(outcome), outcome);
    assert.equal(runStatusFromOutcome(outcome), outcome);
    assert.ok(RUN_STATUSES.includes(outcome));
  }
  assert.equal(normalizeStreamOutcome(" SUCCESS "), "success");
  assert.equal(normalizeStreamOutcome(7), null);
  assert.equal(normalizeStreamOutcome(undefined), null);
  // Unknown or absent outcomes never become success.
  assert.equal(runStatusFromOutcome("unknown"), "failed");
  assert.equal(runStatusFromOutcome(null), "failed");
  assert.equal(runStatusFromOutcome(undefined), "failed");
  assert.equal(statusLabel("partial"), "Partial");
  assert.equal(persistableRunStatus("partial"), "partial");
  assert.equal(outcomeLabel("success"), "Success");
  assert.equal(outcomeLabel("cancelled"), "Cancelled");
  assert.equal(outcomeLabel("who_knows"), "Unknown outcome");
  assert.equal(outcomeLabel(null), "Unknown outcome");
  assert.equal(runModeLabel("live-stream"), "Live stream");
  assert.equal(runModeLabel("live-request"), "Live request");
  assert.equal(runModeLabel("demo"), "Offline demo");
});

test("the Studio wires the real stream, labels the mode and keeps the non-streaming path", async () => {
  const [page, proxy, runsRoute] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(
      new URL("../app/api/queryforge/[...path]/route.ts", import.meta.url),
      "utf8",
    ),
    readFile(new URL("../app/api/studio/runs/route.ts", import.meta.url), "utf8"),
  ]);

  // The real consumer is used, with the real route, and its verdicts are used.
  assert.match(page, /\/api\/queryforge\/ask\/stream/);
  assert.match(page, /consumeAskStream\(/);
  assert.match(page, /streamRunStatus\(state\)/);
  assert.match(page, /streamRunDetail\(state\)/);
  assert.match(page, /const usage = streamObservability\(state\);/);
  assert.match(page, /streamEvidence\(state\)/);
  assert.match(page, /setStreamState\(initialStreamState\(\)\)/);
  // The non-streaming path is still reachable and labelled.
  assert.match(page, /\/api\/queryforge\/ask",/);
  assert.match(page, /data-transport=\{useStreaming \? "stream" : "request"\}/);
  assert.match(page, /data-run-mode=\{result\.mode\}/);
  assert.match(page, /runModeLabel\(result\.mode\)\.toUpperCase\(\)/);
  assert.match(page, /useStreaming/);
  // Cancel, empty-state and progress evidence are visible in the UI.
  assert.match(page, /data-action="cancel-run"/);
  assert.match(page, /cancelStreamRun/);
  assert.match(page, /Waiting for the first frame/);
  assert.match(page, /data-frame-count=\{streamState\?\.framesReceived \?\? 0\}/);
  assert.match(page, /data-observability="terminal-frame"/);
  assert.match(
    page,
    /data-terminal-outcome=\{terminal\?\.outcome \?\? \(terminal \? "unknown" : "none"\)\}/,
  );
  assert.match(page, /outcomeLabel\(terminal\.outcome\)/);
  assert.match(page, /no terminal frame received/);
  assert.match(page, /function StreamEvidenceLine/);
  assert.match(page, /data-note="violations"/);
  // The proxy forwards the protocol version and does not time out SSE runs.
  assert.match(proxy, /x-queryforge-event-protocol/);
  assert.match(proxy, /isEventStreamPath/);
  assert.match(proxy, /AbortSignal\.timeout\(120_000\)/);
  // Run history stores the real protocol vocabulary.
  assert.match(runsRoute, /"partial"/);
});
