"""Measure offline Phase B/C feature overhead without external model calls."""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.core.config import Config
from queryforge.application import AgentOptions, AgentService


class BenchmarkLLM:
    def __init__(self, tool_loop: bool = False, parallel: bool = False) -> None:
        self.calls = 0
        self.tool_loop = tool_loop
        self.parallel = parallel

    def generate_json(self, prompt: str) -> dict:
        self.calls += 1
        if "Choose one bounded read-only action" in prompt:
            return {
                "action": "final_answer",
                "params": {
                    "sql": "SELECT name FROM items ORDER BY name",
                    "explanation": "List item names.",
                    "tables_used": ["items"],
                },
            }
        if "Generate candidate" in prompt:
            return {
                "sql": "SELECT name FROM items ORDER BY name",
                "explanation": "List item names.",
                "tables_used": ["items"],
            }
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


def measure(service: AgentService, database: Path, options: dict) -> dict:
    durations: list[float] = []
    runs = 7
    for index in range(runs):
        started = time.perf_counter()
        service.ask(
            "List item names",
            AgentOptions(
                database=str(database),
                skills=[],
                run_id=f"phase_bc_{options['name']}_{index}",
                **options["values"],
            ),
        )
        durations.append((time.perf_counter() - started) * 1000)
    return {
        "median_duration_ms": round(statistics.median(durations), 3),
        "min_duration_ms": round(min(durations), 3),
        "max_duration_ms": round(max(durations), 3),
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
            orchestration_state_root=str(root / ".queryforge" / "runs"),
        )
        scenarios = (
            {"name": "default", "values": {}},
            {
                "name": "explicit_minimal",
                "values": {
                    "tool_loop_enabled": False,
                    "parallel_candidates": 1,
                    "subject_tree_enabled": False,
                },
            },
            {
                "name": "tool_loop",
                "values": {"tool_loop_enabled": True, "tool_loop_max_rounds": 1},
            },
            {
                "name": "parallel_candidates",
                "values": {"parallel_candidates": 2, "parallel_max_preview": 2},
            },
        )
        results = {}
        for scenario in scenarios:
            llm = BenchmarkLLM()
            scenario_root = root / scenario["name"]
            scenario_config = replace(
                config,
                history_db_path=str(scenario_root / "history.sqlite"),
                orchestration_state_root=str(scenario_root / ".queryforge" / "runs"),
            )

            def scenario_config_loader(**_: object) -> Config:
                return scenario_config

            service = AgentService(
                config_loader=scenario_config_loader,
                llm_factory=lambda _, provider=llm: provider,
            )
            measurement = measure(service, database, scenario)
            measurement["model_calls_per_run"] = round(llm.calls / 7, 2)
            results[scenario["name"]] = measurement

    if results["default"]["model_calls_per_run"] != 2:
        raise AssertionError("Default configuration must retain two model calls per run")
    if results["explicit_minimal"]["model_calls_per_run"] != 2:
        raise AssertionError("Explicit minimal configuration must retain two model calls")
    print(
        json.dumps(
            {
                "scenario": "offline Phase B/C feature comparison",
                "runs_per_scenario": 7,
                "timing_note": (
                    "Wall-clock values are environment observations; model-call "
                    "budgets are the stable regression gate."
                ),
                "default_preserves_minimal_path": True,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
