"""The step-17 demos are executable acceptance tests (17-N1).

Each demo asserts every claim it narrates against the real system, so running
them here keeps "a clean environment reproduces the demos" true: a demo that
stops matching reality fails this suite instead of quietly becoming a story.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = PROJECT_ROOT / "docs" / "demo"
PYTHON = sys.executable


def _run_demo(name: str) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["LOG_LEVEL"] = "CRITICAL"
    environment.setdefault("PYTHONPATH", str(PROJECT_ROOT))
    return subprocess.run(
        [PYTHON, str(DEMO_DIR / name)],
        cwd=str(PROJECT_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


class DemoScriptsTest(unittest.TestCase):
    def assert_demo_passes(self, name: str) -> None:
        completed = _run_demo(name)
        if completed.returncode != 0:
            self.fail(
                f"{name} failed (exit {completed.returncode}); "
                f"the claim that did not hold is the last [demo] line:\n"
                + "\n".join(completed.stdout.splitlines()[-6:])
                + "\nstderr tail:\n"
                + "\n".join(completed.stderr.splitlines()[-3:])
            )

    def test_demo_a_upload_decides_the_answer(self):
        self.assert_demo_passes("run_demo_a.py")

    def test_demo_b_semantic_validation_catches_a_wrong_query(self):
        self.assert_demo_passes("run_demo_b.py")

    def test_demo_c_multi_step_analysis_with_evidence(self):
        self.assert_demo_passes("run_demo_c.py")

    def test_demo_d_transports_refusals_and_recovery(self):
        self.assert_demo_passes("run_demo_d.py")

    def test_demo_e_api_upload_repair_and_attribution(self):
        """Absorbed the earlier `scripts/demo_data_agent.py` scenarios (step 17)."""
        self.assert_demo_passes("run_demo_e.py")

    def test_run_all_covers_every_demo_script(self):
        sys.path.insert(0, str(DEMO_DIR))
        try:
            import run_all  # noqa: PLC0415 - imported for its DEMOS list
        finally:
            sys.path.pop(0)
        on_disk = {path.name for path in DEMO_DIR.glob("run_demo_*.py")}
        self.assertEqual(set(run_all.DEMOS), on_disk)


if __name__ == "__main__":
    unittest.main()
