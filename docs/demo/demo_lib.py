"""Shared helpers for the step-17 acceptance demos.

Every demo is a self-contained, offline, deterministic script that *proves* the
behaviour it narrates: each claim is checked against the real system (CLI,
service, artifact files) and the script exits non-zero when a claim does not
hold. That makes the demos runnable in CI (`tests/test_demo_scripts.py`) and
honest as user-facing documentation at the same time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

#: The bundled, checked-in sample the demos read from (never write to).
ANIME_ROOT = PROJECT_ROOT / "sample_data" / "anime_streaming"
ANIME_DATABASE = ANIME_ROOT / "anime_streaming.sqlite"
ANIME_MODEL = ANIME_ROOT / "semantic_model.yml"
ANIME_POLICY = ANIME_ROOT / "sql_policy.yml"
ASSET_CONFIG = PROJECT_ROOT / "sample_data" / "data_assets" / "assets.yml"
ASSET_CSV = PROJECT_ROOT / "sample_data" / "data_assets" / "anime_watch_events.csv"


class DemoFailure(AssertionError):
    """A demo claim did not hold; the run must fail loudly, never narrate success."""


def say(step: str, detail: str = "") -> None:
    """Print one narrated step of a demo."""
    line = f"[demo] {step}"
    print(f"{line}: {detail}" if detail else line, flush=True)


def check(condition: bool, claim: str, evidence: str = "") -> None:
    """Assert a narrated claim, printing the evidence either way."""
    marker = "ok" if condition else "FAILED"
    suffix = f" ({evidence})" if evidence else ""
    print(f"[demo]   [{marker}] {claim}{suffix}", flush=True)
    if not condition:
        raise DemoFailure(claim)


def workspace() -> Path:
    """A throwaway workspace so a demo never writes into the repository."""
    root = Path(tempfile.mkdtemp(prefix="queryforge_demo_"))
    return root


def run_cli(args: Sequence[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run the QueryForge CLI and return (exit code, stdout, stderr)."""
    environment = os.environ.copy()
    environment["LOG_LEVEL"] = "CRITICAL"
    environment.setdefault("PYTHONPATH", str(PROJECT_ROOT))
    completed = subprocess.run(
        [PYTHON, "-m", "queryforge", *args],
        cwd=str(cwd or PROJECT_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def run_script(args: Sequence[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a repository script with the same environment conventions."""
    environment = os.environ.copy()
    environment["LOG_LEVEL"] = "CRITICAL"
    environment.setdefault("PYTHONPATH", str(PROJECT_ROOT))
    completed = subprocess.run(
        [PYTHON, *args],
        cwd=str(cwd or PROJECT_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def json_from(stdout: str) -> Any:
    """Parse the JSON payload a CLI command prints (ignoring leading logs)."""
    start = stdout.find("{")
    if start < 0:
        raise DemoFailure(f"expected a JSON payload, got: {stdout[:200]!r}")
    return json.loads(stdout[start:])


def isolated_env(root: Path) -> dict[str, str]:
    """Environment pointing every stateful path at the demo workspace."""
    return {
        "ORCHESTRATION_STATE_ROOT": str(root / "runs"),
        "HISTORY_DB_PATH": str(root / "history.sqlite"),
        "VECTOR_KB_PATH": str(root / "vector_kb"),
    }


def copy_asset_config(root: Path, *, csv_rows: list[str] | None = None) -> Path:
    """Copy the bundled asset contract into the workspace, optional CSV override.

    The contract references its CSV with a relative path, so the CSV has to live
    next to the copied YAML — that is also what makes the "upload decides the
    result" claim (17-E2E1) reproducible: only the CSV changes.
    """
    target = root / "assets.yml"
    csv_target = root / ASSET_CSV.name
    shutil.copy2(ASSET_CONFIG, target)
    if csv_rows is None:
        shutil.copy2(ASSET_CSV, csv_target)
    else:
        header = ASSET_CSV.read_text(encoding="utf-8").splitlines()[0]
        csv_target.write_text("\n".join([header, *csv_rows]) + "\n", encoding="utf-8")
    return target


class Demo:
    """Context manager that cleans up the demo workspace."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.root = workspace()

    def __enter__(self) -> "Demo":
        say(f"{self.name}: workspace", str(self.root))
        return self

    def __exit__(self, *_: object) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
