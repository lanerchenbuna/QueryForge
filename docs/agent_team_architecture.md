# QueryForge Architecture

## Runtime Shape

```text
CLI / REST-SSE / MCP / Gateway
              |
              v
        AgentService
              |
              v
      Router + Orchestrator
              |
              v
analysis -> candidate -> execution -> completion -> delivery
              |
              v
       WorkflowRunner
  schema -> SQL -> policy -> execute -> reflect/repair -> output
              |
              v
 DatabaseTool + SQLGlot AST policy
```

Every public transport calls `AgentService`. The router classifies the request; the
orchestrator manages stages, artifacts, state, and delivery; `WorkflowRunner` remains
the only SQL generation and execution kernel.

## Five Stages

| Stage | Responsibility | Main outputs |
| --- | --- | --- |
| `analysis` | Clarify the request, retrieve context, map schema and semantics | analysis, knowledge, schema artifacts |
| `candidate` | Generate or repair SQL and apply Governance | candidate, policy, optional selector artifacts |
| `execution` | Run one governed read-only statement | result or controlled failure |
| `completion` | Data QA, optional visualization/report, operational readiness | QA, chart/report, ops artifacts |
| `delivery` | Persist final state and return stable response metadata | delivery report |

Role agents are stage-local implementations, not additional public workflow stages.
This keeps the observable state machine short while preserving traceability.

## Safety Invariants

- `DatabaseTool` is the only formal SQL execution boundary.
- Both candidate Governance and `DatabaseTool` evaluate SQLGlot AST policy.
- SQL must be one read-only query and may be constrained by table, column, function,
  CTE, result-size, table-count, join-count, and CROSS JOIN rules.
- Metadata and SQL review are direct read-only paths; they never execute generated SQL.
- Failures produce `blocked`, `failed`, or `degraded` artifacts rather than bypassing
  governance.

## Adaptive Work

Simple requests use the single-candidate path. Complexity routing may enable bounded
tool discovery and two concurrent SQL candidates. Plan mode omits execution but keeps
analysis, candidate governance, completion metadata, and delivery.

Sessions, Subject Tree, streaming, reports, vector retrieval, and MCP are adapters or
bounded extensions around the same service and safety boundary.

## Runtime Artifacts

```text
.queryforge/runs/<run_id>/
├── state.json
├── artifacts/
│   ├── routing_decision.json
│   ├── analysis_request.json
│   ├── schema_plan.json
│   ├── sql_candidate.json
│   ├── governance_report.json
│   ├── qa_report.json
│   └── delivery_report.json
└── logs/
```

Artifacts may be `valid`, `warning`, `blocked`, or `degraded`. Session memory stores
structured context only and never result rows.

## Module Responsibilities

- `application`: stable facade, validated options, direct use cases, read-only resources,
  and event streaming.
- `orchestration`: routing, five-stage lifecycle, artifacts, quality gates, sessions.
- `workflow`: SQL nodes, reflection loop, candidate selection, reports.
- `domain`: semantic model, operational contracts, security policy, prompt skills.
- `infrastructure`: SQLite, model providers, storage, and database tools.
- `interfaces`: CLI-facing API, REST/SSE, MCP, and Gateway transports.

### Internal Package Boundaries

```text
application/
├── agent_service.py   # ask/plan/stream orchestration facade
├── options.py         # request contracts and budget validation
├── resources.py       # schema, metric, history, preview, health resources
├── direct_tasks.py    # metadata and static SQL-review routes
└── event_stream.py

domain/semantic/
├── schemas.py         # strict semantic and contract data structures
├── model.py           # loading, physical validation, matching, Join Path resolution
├── contract_validator.py
└── subject.py

data_assets/
├── models.py          # declarative asset contracts
├── sources.py         # CSV, Parquet, JSON API adapters
├── transforms.py      # normalization, quality, watermark helpers
└── pipeline.py        # build transaction and publication coordination

orchestration/
├── gates.py           # deterministic stage-gate evaluation
├── quality.py         # artifact payload validation
└── orchestrator/      # lifecycle and delivery coordination
```

The public compatibility namespaces remain as re-export shims. New code should import
from the modular paths above.
