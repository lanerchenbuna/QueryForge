"""Serve the scripted-model API on loopback for local Studio validation.

The model is a fixture: every SQL statement is supplied by the scenario, so this
never measures model quality. It exists so the Studio UI can be exercised against
real HTTP handlers and real SQLite without credentials or network access.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scenarios_api_and_repair import config_at  # noqa: E402

PROJECT_ROOT = HERE.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.application import AgentService  # noqa: E402
from queryforge.interfaces.api.app import create_app  # noqa: E402
from scripts.benchmark_runners import ScriptedModel  # noqa: E402

SQL = "SELECT COUNT(items.id) AS item_count FROM items"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18000)
    args = parser.parse_args()
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="qf_demo_api_") as tmp:
        config = replace(config_at(Path(tmp)), api_key=None)
        service = AgentService(
            config_loader=lambda **_: config,
            llm_factory=lambda _: ScriptedModel(SQL),
        )
        print(
            "OFFLINE SCRIPTED MODEL: real API and SQL, no model-quality claim.",
            flush=True,
        )
        uvicorn.run(
            create_app(service), host="127.0.0.1", port=args.port, log_level="warning"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
