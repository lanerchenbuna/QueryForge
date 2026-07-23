# Old and Current Workflow Comparison

This document compares the original QueryForge MVP workflow with the current
governed analytics workflow. "Old" refers to the fixed MVP described in
`docs/reproduction/vibe-coding-reproduce/`, not the upstream Datus-Agent project.

## 1. Flow Shape

### Old MVP

```text
CLI
  -> WorkflowRunner
  -> SchemaLinkingNode
  -> GenSqlNode
  -> ExecuteSqlNode
  -> OutputNode
```

The workflow had one request type: generate one SQLite `SELECT`, execute it, and
format the result. A node failure stopped the request.

### Current Workflow

```text
CLI / REST-SSE / MCP / Gateway
  -> AgentService
  -> EntryRouterAgent
  -> OrchestratorAgent
  -> analysis -> candidate -> execution -> completion -> delivery
  -> WorkflowRunner / ReflectiveWorkflow
```

The SQL execution kernel remains node-based:

```text
scope -> date/schema/skill/metric context
      -> optional Tool Loop or parallel candidates
      -> SQL generation
      -> Governance
      -> DatabaseTool execution
      -> reflection / bounded repair
      -> output
```

The five stages are orchestration state, not five extra model calls. The default
simple path still uses one SQL candidate and two model calls: generation and
reflection.

## 2. Code Mapping

| Concern | Old MVP | Current implementation | Behavioral change |
| --- | --- | --- | --- |
| Entry | `main.py` created `WorkflowRunner` directly | `application/agent_service.py` is the shared service facade | All transports use the same service behavior. |
| Request type | One implicit NL2SQL request | `orchestration/agents/entry_router.py` selects task type | Metadata and SQL review do not need to execute generated SQL. |
| Orchestration | Fixed node order in `WorkflowRunner` | `orchestration/orchestrator/orchestrator.py` plus `pipeline_registry.py` | Persisted stage state, artifacts, quality gates, and delivery. |
| SQL loop | One generate/execute/output pass | `workflow/workflow.py:ReflectiveWorkflow` | Bounded reflection, repair, regeneration, Tool Loop, and candidate selection. |
| Schema context | Physical SQLite schema | `SchemaLinkingNode` plus semantic model and Subject Tree | Business names, metrics, Join Paths, grain, and fan-out constraints. |
| Execution | Connector call after basic read-only validation | `ExecuteSqlNode -> DatabaseTool -> SQLiteConnector` | Semantic join guard, policy decision capture, and read-only execution revalidation. |
| Output | SQL, explanation, rows | `OutputNode`, artifacts, report/SSE/MCP adapters | Stable response plus optional audit and delivery outputs. |

## 3. Router Changes

### Old behavior

There was no Router module. Every non-empty CLI question was treated as an analytical
SQL request and entered the fixed workflow. There was no deterministic complexity
profile, request classification, custom task registration, or route-specific direct
path.

### Current behavior

`EntryRouterAgent` in
[`queryforge/orchestration/agents/entry_router.py`](../queryforge/orchestration/agents/entry_router.py)
is intentionally isolated from model, connector, `WorkflowRunner`, and SQL tool
dependencies. It cannot generate or execute SQL.

It performs three tasks:

1. Classifies requests using deterministic marker rules:
   - `ask_sql`
   - `sql_review`
   - `troubleshoot_sql`
   - `metadata_query`
   - `explain_result`
   - `build_report`
   - `unknown`
2. Assigns `simple` or `complex` execution profiles using cross-table, metric,
   comparison, ranking, grouping, filtering, request-length, and multi-question
   signals.
3. Supports deployment-specific rules through `TaskRoute` and pipeline registration.

`AgentService._run()` uses the decision to enable expensive mechanisms only when
needed:

```text
simple  -> one candidate, no Tool Loop
complex -> bounded Tool Loop + at least two candidates
```

The result is persisted as `routing_decision.json`, returned in `agent_team`, and
used to select one compact stage pipeline:

| Task type | Pipeline |
| --- | --- |
| `ask_sql`, `troubleshoot_sql`, `explain_result`, `build_report`, `unknown` | `analysis -> candidate -> execution -> completion -> delivery` |
| `sql_review` | `analysis -> candidate -> review -> completion -> delivery` |
| `metadata_query` | `analysis -> delivery` |

### Why this is different

The Router is not an LLM planner. It is a cheap, deterministic admission and
execution-profile decision. This prevents simple metadata or review requests from
paying for a full SQL-generation loop, while keeping classification explainable and
testable.

## 4. Governance Changes

### Old behavior

The MVP `DatabaseTool` performed one basic read-only check before calling SQLite. The
original reproduction specification explicitly allowed simple checks for empty SQL,
non-`SELECT`, multiple statements, and obvious DDL/DML. There was no separate
Governance artifact, table/column allowlist, function blacklist, recursive CTE
control, query-shape budget, or candidate-stage decision.

### Current behavior

Governance is split deliberately into two layers:

```text
candidate stage
  -> GovernanceAgent
  -> SQLPolicyEngine.evaluate(sql)
  -> governance_report artifact

execution boundary
  -> ExecuteSqlNode
  -> DatabaseTool.execute_sql(sql)
  -> SQLPolicyEngine.evaluate(sql) again
  -> SQLiteConnector (query_only connection)
```

`GovernanceAgent` in
[`queryforge/orchestration/agents/governance.py`](../queryforge/orchestration/agents/governance.py)
runs before normal execution. It:

- requires an SQL candidate;
- evaluates the shared SQLGlot policy engine;
- writes `governance_report` with the full decision;
- blocks the candidate stage on policy denial or policy-engine failure;
- adds non-blocking warnings for high join count, unbounded scans, and
  sensitive-looking columns.

`SQLPolicyEngine` in
[`queryforge/domain/security/sql_policy.py`](../queryforge/domain/security/sql_policy.py)
enforces:

- one parsable SQLite query AST;
- read-only query roots and forbidden AST nodes;
- no recursive CTE;
- dangerous function blacklist;
- table and column scope;
- required or maximum `LIMIT`;
- maximum tables and joins;
- optional CROSS JOIN denial.

`DatabaseTool` in
[`queryforge/infrastructure/tools/database_tool.py`](../queryforge/infrastructure/tools/database_tool.py)
is the non-bypassable execution boundary. It applies the same policy again during
`execute_sql()` and uses a read-only SQLite connector with `PRAGMA query_only = ON`.

### Why policy is checked twice

The first check makes the orchestration decision observable and prevents unsafe SQL
from continuing into the normal execution stage. The second check is defense in depth:
any caller using `DatabaseTool`, including previews and transport resources, must pass
the policy even if it bypasses orchestration code by mistake.

## 5. State and Failure Handling

| Aspect | Old MVP | Current workflow |
| --- | --- | --- |
| State | In-memory `Context` only | `Context` plus persisted `TaskState` and artifacts |
| Failure | Stop at failed node | Stop, repair within budget when eligible, or emit `blocked`/`degraded` evidence |
| SQL errors | Execution failure ends request | Reflection chooses `SUCCESS`, `FIX_SQL`, `REGENERATE`, or human review |
| Policy failure | Basic validation error | Named AST rule, policy decision, Governance artifact, no execution |
| Session | None | Explicit opt-in structured session memory; no result rows |
| Delivery | CLI JSON | Shared JSON contract, reports, SSE progress, MCP tools/resources |

## 6. What Was Intentionally Not Changed

- SQLite remains the only formal execution backend.
- `WorkflowRunner`, node contracts, and `Context` remain the SQL execution core.
- Every generated formal SQL statement still runs through `DatabaseTool`.
- The default simple path remains bounded and avoids Tool Loop and multi-candidate
  overhead.
- Legacy Python import shims remain for external callers during the modular-layout
  transition.

## 7. Practical Reading Order

1. `application/agent_service.py`: transport-neutral request assembly.
2. `orchestration/agents/entry_router.py`: deterministic routing and complexity.
3. `orchestration/orchestrator/orchestrator.py`: stage lifecycle and artifacts.
4. `workflow/workflow.py`: reflective SQL loop.
5. `orchestration/agents/governance.py`: pre-execution Governance artifact.
6. `domain/security/sql_policy.py`: AST rules.
7. `infrastructure/tools/database_tool.py`: final execution boundary.
