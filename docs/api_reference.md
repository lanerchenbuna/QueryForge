# API Reference

Start the optional REST server:

```bash
queryforge --serve-api --api-host 127.0.0.1 --api-port 8000
```

All request bodies use the `AskRequest` contract. The common fields are `question`,
`database`, `semantic_model_path`, `sql_policy_path`, `skills`, session options, Tool Loop,
parallel candidates, Subject Tree and report options.

A semantic model is required by default and is discovered beside the selected
database. `allow_schema_only: true` is an explicit diagnostics-only escape hatch;
normal analytical clients should never set it.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/health` | GET | Service health and capability summary |
| `/models` | GET | Configured model providers |
| `/skills` | GET | Local prompt-only Skills |
| `/ask` | POST | Execute the full governed NL2SQL workflow |
| `/ask/stream` | POST | SSE progress events; no SQL, prompts, or result rows in events |
| `/plan` | POST | Generate and preflight a plan without executing SQL |
| `/report/{run_id}` | GET | Download static HTML from the configured report directory |
| `/gateway/webhook` | POST | Gateway adapter with a stable user/channel session |

Example:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question":"Top anime by watch hours","database":"sample_data/anime_streaming/anime_streaming.sqlite"}'
```

For SSE, send the same body to `/ask/stream`. Each `data:` line is a `WorkflowEvent` containing
only progress metadata. Use `report: true` to create a static report after a successful execution,
then retrieve it from `/report/<run_id>`.

Errors caused by invalid inputs return `400`; workflow, policy, or execution failures return
`422`. SQL policy denials include a named `SQL_SECURITY_ERROR` and occur before SQLite execution.
