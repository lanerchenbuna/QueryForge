"""Measure a small offline Phase A baseline without external model calls."""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.core.config import Config
from queryforge.application import AgentOptions, AgentService


class BenchmarkLLM:
    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, prompt: str) -> dict:
        self.calls += 1
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The result answers the question.",
                "suggested_fix": None,
            }
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List item names.",
            "tables_used": ["items"],
        }


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "items.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.executemany(
            "INSERT INTO items VALUES (?)",
            [("alpha",), ("beta",), ("gamma",)],
        )
        connection.commit()
        connection.close()
        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(database),
            history_db_path=str(root / "history.sqlite"),
        )
        llm = BenchmarkLLM()
        service = AgentService(
            config_loader=lambda **_: config,
            llm_factory=lambda _: llm,
        )
        durations_ms: list[float] = []
        artifact_counts: list[int] = []
        runs = 5
        for index in range(runs):
            started = time.perf_counter()
            output = service.ask(
                "List item names",
                AgentOptions(
                    database=str(database),
                    skills=[],
                    run_id=f"phase_a_benchmark_{index}",
                    orchestration_state_root=str(root / ".queryforge" / "runs"),
                ),
            )
            durations_ms.append((time.perf_counter() - started) * 1000)
            state = json.loads(
                Path(output["agent_team"]["state_path"]).read_text(encoding="utf-8")
            )
            artifact_counts.append(len(state["artifacts"]))

    report = {
        "scenario": "offline ask_sql with deterministic provider",
        "runs": runs,
        "median_duration_ms": round(statistics.median(durations_ms), 3),
        "min_duration_ms": round(min(durations_ms), 3),
        "max_duration_ms": round(max(durations_ms), 3),
        "artifact_count": artifact_counts[0],
        "model_calls_per_run": llm.calls / runs,
        "expected_model_calls_per_run": 2,
        "phase_a_role_agents_use_llm": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
