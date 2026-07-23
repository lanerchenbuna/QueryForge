# Phase 2 Final Acceptance Report

## Status

**Passed.** Phase 2 completes the default Agent Team orchestration evolution while retaining
the governed SQLite SQL core. Final offline acceptance completed with **11/11 checks**, **213**
targeted upgrade tests, and **247** full-suite tests passing; **7** optional-dependency tests
were skipped in the base environment.

## Completed Capabilities

| Phase | Capability | Status |
| --- | --- | --- |
| A | Seven task routes, role decision artifacts, quality gates | Complete |
| B | Conversation Memory and Bounded Tool Loop | Complete |
| C | Structured Reasoning, Parallel + Selection, Subject Tree | Complete |
| D | Streaming events, static Report Artifact, enhanced MCP | Complete |

## Verification

- `scripts/run_acceptance.py --full`: 11/11 offline checks.
- Targeted upgrade suite: 213 passing tests, 7 optional-dependency skips.
- Full suite: 247 passing tests, 7 optional-dependency skips.
- Default workflow benchmark: two model calls per simple request.
- Phase B/C benchmark isolates each scenario's history/run state. Wall-clock values are
  environment observations; the stable regression gate is the two-call default/minimal budget.
  Tool Loop stays bounded and two parallel candidates require one additional model call.
- `tests/test_phase2_final_acceptance.py` verifies all task routes, default compatibility,
  report generation and progress streaming end-to-end.

## Architecture and Safety

All normal query entrypoints use `AgentService -> Router -> Orchestrator -> WorkflowRunner`.
The orchestrator records five compact stages: `analysis`, `candidate`, `execution`,
`completion`, and `delivery`. Metadata and SQL-review requests use direct read-only paths that
intentionally do not execute generated SQL. Every SQL preview and execution path uses
`DatabaseTool` and its AST policy; Governance artifacts are created before normal execution.

Advanced features remain opt-in or minimal by default: no session without an explicit session
request, Tool Loop disabled, single candidate, Subject Tree disabled, and streaming only when
the stream endpoint or CLI option is selected.

## Known Limits and Next Directions

- Report charts prefer the Vega Embed CDN and include an inline SVG fallback; full interactive
  rendering still requires the CDN.
- Streaming exposes workflow progress, not provider token streaming or cancellation.
- MCP has no built-in authentication, tenant isolation or rate limiting; keep it in a controlled
  local environment.
- SQL review performs static syntax, policy, readability and performance checks, plus explicit
  governed metric-alias contract checks. It does not yet prove semantic correctness for arbitrary
  SQL expressions.

Next candidates are provider token streaming/cancellation, authenticated transports, richer
semantic SQL review, report export formats, and domain profiles.
