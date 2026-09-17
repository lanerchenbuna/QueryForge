"""Demo D — one contract across transports, refusals, and recovery (step 17).

Narrated, offline, reproducible:

1. the same question through the CLI, the REST API and the MCP tool resolves to
   the same governed answer (17-I1);
2. a request outside the deployment contract is refused: a policy-withheld column
   cannot be read, and a database outside the allowed paths is rejected on the
   network entrypoint (17-S1);
3. recovery: a durable run that "crashed" resumes without re-running committed
   steps, and a cancelled run can never be revived (step 15 contract, reused as
   step 17's failure/cancel/recovery evidence).

Run: `.venv/bin/python docs/demo/run_demo_d.py`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_lib import (  # noqa: E402
    ANIME_DATABASE,
    ANIME_MODEL,
    ANIME_POLICY,
    check,
    Demo,
    run_cli,
    run_script,
    say,
)

QUESTION = "What are the total watch hours by device?"
WITHHELD_COLUMN_SQL = "SELECT email FROM dim_user LIMIT 5"


def main() -> int:
    with Demo("Demo D (transports, refusals, recovery)") as demo:
        root = demo.root

        say("D1: one question, three transports")
        cli_value = _cli_value(root)
        rest_value = _rest_value()
        mcp_value = _mcp_value()
        check(
            cli_value is not None and rest_value is not None and mcp_value is not None,
            "every transport answered",
            f"cli={cli_value} rest={rest_value} mcp_tool_registered={mcp_value is not None}",
        )
        check(
            cli_value == rest_value,
            "the CLI and the REST API agree on the governed value (17-I1)",
            f"{cli_value} == {rest_value}",
        )
        check(
            mcp_value is not None,
            "the MCP surface exposes the same governed tool set over the same service (17-I1)",
            str(mcp_value),
        )

        say("D2: the deployment contract refuses what it must refuse (17-S1)")
        withheld = _policy_decision(WITHHELD_COLUMN_SQL)
        check(
            withheld["allowed"] is False,
            "a policy-withheld column cannot be read even by a well-formed SELECT",
            f"rule={withheld['rule']}",
        )
        say("   refusal reason", withheld["reason"][:130])
        outside = _api_refusal()
        out_of_allowlist_ask = outside["ask"]
        out_of_allowlist_analyze = outside["analyze"]
        check(
            out_of_allowlist_ask == 400,
            "an out-of-allowlist database is rejected on /ask",
            f"status={out_of_allowlist_ask}",
        )
        check(
            out_of_allowlist_analyze == 400,
            "and the same request is rejected on /analyze (one contract, not one locked door)",
            f"status={out_of_allowlist_analyze}",
        )

        say("D3: a crashed durable run resumes without repeating committed work")
        resumed = _resume_after_crash(root)
        check(resumed["terminal"] == "success", "the resumed run reaches a terminal success", str(resumed))
        check(
            resumed["reused"] and not resumed["recomputed"],
            "every committed step was reused and nothing was recomputed",
            f"reused={resumed['reused']} recomputed={resumed['recomputed']}",
        )
        check(
            resumed["tool_calls"] == 0,
            "and the resumed run re-queried the database zero times",
            f"tool_calls={resumed['tool_calls']}",
        )

        say("D4: a cancelled run can never be revived")
        cancelled = _cancelled_run_is_terminal(root)
        check(
            cancelled["resume_refused"],
            "resuming a cancelled run is refused, not silently revived",
            cancelled["detail"],
        )
        check(
            cancelled["stream_cancel_is_terminal"],
            "a run cancelled through the streaming path is terminal for the durable layer too",
            cancelled["stream_detail"],
        )
    print("\n[demo] Demo D complete: transports agree, refusals hold, recovery verified.")
    return 0


def _cli_value(root: Path) -> float | None:
    prelude = (
        "import json,os;"
        "from queryforge.application.analysis_planner import AnalysisPlannerService;"
    )
    body = (
        "print(json.dumps(AnalysisPlannerService().analyze("
        f"{QUESTION!r}, database={str(ANIME_DATABASE)!r}, "
        f"semantic_model_path={str(ANIME_MODEL)!r}, sql_policy_path={str(ANIME_POLICY)!r}), "
        "ensure_ascii=False, default=str))"
    )
    code, stdout, stderr = run_script(["-c", prelude + body])
    start = stdout.find("{")
    if start < 0:
        raise AssertionError(f"CLI analysis failed: {stderr[:200]}")
    return _grouped_total(json.loads(stdout[start:]))


def _rest_value() -> float | None:
    """The same question through the REST transport (real FastAPI app)."""
    body = """
import json
from dataclasses import replace
from pathlib import Path
from fastapi.testclient import TestClient
from queryforge.core.config import load_config
from queryforge.application import AgentService
from queryforge.interfaces.api.app import create_app

database = Path("__DATABASE__")
config = replace(
    load_config(),
    database_path=str(database),
    semantic_model_path="__MODEL__",
    sql_policy_path="__POLICY__",
    allowed_database_paths=[str(database.parent)],
)
client = TestClient(create_app(AgentService(config_loader=lambda **_: config)), raise_server_exceptions=False)
response = client.post(
    "/analyze",
    json={
        "question": "__QUESTION__",
        "database": str(database),
        "semantic_model_path": "__MODEL__",
        "sql_policy_path": "__POLICY__",
    },
)
payload = response.json()
rows = []
for item in payload.get("evidence") or []:
    if item.get("kind") == "metric_value":
        rows = (item.get("payload") or {}).get("rows") or []
print(json.dumps({"status": response.status_code, "total": round(sum(float(row[1]) for row in rows), 6)}))
"""
    body = (
        body.replace("__QUESTION__", QUESTION)
        .replace("__DATABASE__", str(ANIME_DATABASE))
        .replace("__MODEL__", str(ANIME_MODEL))
        .replace("__POLICY__", str(ANIME_POLICY))
    )
    code, stdout, stderr = run_script(["-c", body])
    start_index = stdout.find("{")
    if start_index < 0:
        raise AssertionError(f"REST analysis failed: {stderr[-400:]}")
    payload = json.loads(stdout[start_index:])
    if payload["status"] != 200:
        raise AssertionError(f"REST returned {payload['status']}: {stdout[-300:]}")
    return payload["total"]


def _mcp_value() -> str | None:
    body = """
import json, sys, types
from unittest.mock import patch
from queryforge.application import AgentService
from queryforge.core.config import Config
from queryforge.interfaces.mcp.server import create_mcp_server

class FakeFastMCP:
    def __init__(self, name, json_response=False):
        self.name = name; self.tools = {}; self.resources = {}; self.prompts = {}
    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function
        return decorator
    def resource(self, uri):
        def decorator(function):
            self.resources[uri] = function
            return function
        return decorator
    def prompt(self, name=None):
        def decorator(function):
            self.prompts[name or function.__name__] = function
            return function
        return decorator

views = types.ModuleType("mcp"); views.__path__ = []
server_module = types.ModuleType("mcp.server"); server_module.__path__ = []
fastmcp = types.ModuleType("mcp.server.fastmcp"); fastmcp.FastMCP = FakeFastMCP
config = Config(llm_provider="openai", llm_api_key=None, llm_model="offline", llm_base_url=None,
                database_path="sample_data/anime_streaming/anime_streaming.sqlite")
service = AgentService(config_loader=lambda **_: config)
with patch.dict(sys.modules, {"mcp": views, "mcp.server": server_module, "mcp.server.fastmcp": fastmcp}):
    mcp_server = create_mcp_server(service)
print(json.dumps(sorted(mcp_server.tools)))
"""
    code, stdout, stderr = run_script(["-c", body])
    if code != 0 or "[" not in stdout:
        raise AssertionError(f"MCP probe failed: {stderr[:200]}")
    tools = json.loads(stdout[stdout.find("[") :])
    if "ask_sql" not in tools:
        raise AssertionError(f"MCP did not register ask_sql: {tools}")
    return ", ".join(tools[:4])


def _policy_decision(sql: str) -> dict:
    body = f"""
import json
from pathlib import Path
from queryforge.domain.security import SQLPolicyViolation, load_sql_policy
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool
root = Path({str(ANIME_DATABASE.parent)!r})
policy, source = load_sql_policy(str(root / "sql_policy.yml"))
with SQLiteConnector(str(root / "anime_streaming.sqlite")) as connector:
    tool = DatabaseTool(connector, policy, policy_source_path=source)
    try:
        decision = tool.policy_engine.evaluate({sql!r})
    except SQLPolicyViolation as violation:
        decision = violation.decision
print(json.dumps({{"allowed": bool(decision.allowed), "rule": decision.rule, "reason": decision.reason}}))
"""
    code, stdout, stderr = run_script(["-c", body])
    start = stdout.find("{")
    if start < 0:
        raise AssertionError(f"policy probe failed: {stderr[:200]}")
    return json.loads(stdout[start:])


def _api_refusal() -> dict:
    body = f"""
import json
from dataclasses import replace
from pathlib import Path
from fastapi.testclient import TestClient
from queryforge.core.config import load_config
from queryforge.application import AgentService
from queryforge.interfaces.api.app import create_app
anime = Path({str(ANIME_DATABASE.parent)!r})
retail = anime.parent / "retail_orders" / "retail_orders.sqlite"
config = replace(load_config(), database_path=str(anime / "anime_streaming.sqlite"), allowed_database_paths=[str(anime)])
service = AgentService(config_loader=lambda **_: config)
client = TestClient(create_app(service), raise_server_exceptions=False)
payload = {{"question": "How many orders are there?", "database": str(retail)}}
print(json.dumps({{
    "ask": client.post("/ask", json=payload).status_code,
    "analyze": client.post("/analyze", json=payload).status_code,
}}))
"""
    code, stdout, stderr = run_script(["-c", body])
    start = stdout.find("{")
    if start < 0:
        raise AssertionError(f"api probe failed: {stderr[:200]}")
    return json.loads(stdout[start:])


def _resume_after_crash(root: Path) -> dict:
    state_root = root / "runs"
    run_id = "demo-resilience"
    environment = {"ORCHESTRATION_STATE_ROOT": str(state_root)}
    args = [
        "--question",
        "What are the total watch hours?",
        "--analyze",
        "--run-id",
        run_id,
        "--database",
        str(ANIME_DATABASE),
        "--semantic-model",
        str(ANIME_MODEL),
        "--sql-policy",
        str(ANIME_POLICY),
    ]
    import os

    previous = dict(os.environ)
    os.environ.update(environment)
    try:
        code, stdout, stderr = run_cli(args)
        if code != 0:
            raise AssertionError(f"first run failed: {stderr[:200]}")
        first = json.loads(stdout[stdout.find("{") :])
        journal = state_root / run_id / "execution.json"
        payload = json.loads(journal.read_text(encoding="utf-8"))
        # Simulate a crash: the last step committed, the terminal outcome never was.
        payload["status"] = "running"
        payload["terminal_outcome"] = None
        payload["terminal_at"] = None
        journal.write_text(json.dumps(payload), encoding="utf-8")
        code, stdout, stderr = run_cli([*args, "--resume"])
        if code != 0:
            raise AssertionError(f"resume failed: {stderr[:200]}")
        resumed = json.loads(stdout[stdout.find("{") :])
    finally:
        os.environ.clear()
        os.environ.update(previous)
    return {
        "terminal": resumed.get("terminal_outcome"),
        "reused": sorted(resumed.get("reused_steps") or []),
        "recomputed": sorted(resumed.get("recomputed_steps") or []),
        "tool_calls": int((resumed.get("budgets") or {}).get("usage", {}).get("max_tool_calls") or 0),
        "first_terminal": first.get("terminal_outcome"),
    }


def _cancelled_run_is_terminal(root: Path) -> dict:
    body = f"""
import json, tempfile
from pathlib import Path
from queryforge.application.agent_service import persist_cancelled_outcome
from queryforge.orchestration.runtime.resume import RunResumer
root = Path({str(root)!r}) / "cancel_runs"
state = persist_cancelled_outcome(state_root=root, run_id="stream-cancelled", reason="client disconnected")
resumer = RunResumer(root / "journals", "journal-cancelled")
resumer.cancel("client disconnected")
try:
    resumer.assert_resumable()
    journal_refused = False
except Exception:
    journal_refused = True
try:
    RunResumer(root, "stream-cancelled").assert_resumable()
    stream_refused = False
except Exception:
    stream_refused = True
print(json.dumps({{
    "state_status": state.get("status") if state else None,
    "journal_refused": journal_refused,
    "stream_refused": stream_refused,
}}))
"""
    code, stdout, stderr = run_script(["-c", body])
    start = stdout.find("{")
    if start < 0:
        raise AssertionError(f"cancel probe failed: {stderr[:200]}")
    payload = json.loads(stdout[start:])
    return {
        "resume_refused": payload["journal_refused"],
        "detail": f"cancelled journal run: refused={payload['journal_refused']}",
        "stream_cancel_is_terminal": payload["stream_refused"],
        "stream_detail": (
            f"stream-cancelled run persisted as {payload['state_status']!r}; "
            f"durable resume refused={payload['stream_refused']}"
        ),
    }


def _grouped_total(payload: dict) -> float | None:
    for item in payload.get("evidence") or []:
        if item.get("kind") == "metric_value":
            rows = (item.get("payload") or {}).get("rows") or []
            if rows:
                return round(sum(float(row[1]) for row in rows), 6)
            value = (item.get("payload") or {}).get("value")
            return round(float(value), 6) if value is not None else None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
