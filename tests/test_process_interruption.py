"""A real process interruption: SIGKILL a running plan, then resume it (step 15).

Unlike the in-process tests (which simulate a crash by editing the journal), this
test starts a *separate operating-system process* that executes a plan through the
real :class:`~queryforge.orchestration.planner.executor.AnalysisExecutor` and the
real journal, kills it with ``SIGKILL`` in the middle of the second step, and then
resumes the run from the journal. Nothing about the first step's committed state
is faked, so this is the strongest available evidence for 15-N1/15-E1.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from queryforge.orchestration.runtime.execution_journal import ExecutionJournal
from queryforge.orchestration.runtime.resume import RunResumer
from queryforge.orchestration.tools.budget import BudgetManager
from queryforge.orchestration.tools.registry import ToolRegistry, build_default_registry
from queryforge.orchestration.tools.specs import ToolSpec

RUN_ID = "qf_killed_run"

#: The child executes a fresh copy of this plan; the parent resumes the same one.
CHILD_DRIVER = '''
"""Executes a two-step plan with a slow second step, then exits."""
import json
import sys
import time
from pathlib import Path

state_root = Path(sys.argv[1])
run_id = sys.argv[2]
sentinel = Path(sys.argv[3])

from queryforge.orchestration.planner import plan as plan_module
from queryforge.orchestration.planner.executor import AnalysisExecutor
from queryforge.orchestration.planner.plan import AnalysisPlan, PlanStep
from queryforge.orchestration.runtime.execution_journal import ExecutionJournal
from queryforge.orchestration.tools.budget import BudgetManager
from queryforge.orchestration.tools.registry import build_default_registry
from queryforge.orchestration.tools.specs import ToolSpec

plan_module.PLAN_ACTIONS = plan_module.PLAN_ACTIONS + ("echo_step",)
plan_module.ACTION_TOOL_MAP["echo_step"] = "echo_step"

registry = build_default_registry(None, BudgetManager())
def _slow_handler(params, context=None):
    """Fast for the first step, deliberately slow for the second one."""
    if params.get("value") == 2:
        time.sleep(60)
    return {"echo": params.get("value")}


registry.register(
    ToolSpec(
        name="echo_step",
        description="slow echo",
        parameter_schema={
            "type": "object",
            "properties": {"value": {}},
            "additionalProperties": False,
        },
        modes=["execute"],
        budget_category="compute",
    ),
    _slow_handler,
)

plan = AnalysisPlan(
    plan_id="plan_killed",
    question="q",
    version=1,
    steps=[
        PlanStep(id="first", action="echo_step", inputs={"value": 1},
                 expected_evidence=["echo_step"]),
        PlanStep(id="second", action="echo_step", inputs={"value": 2},
                 depends_on=["first"], expected_evidence=["echo_step"]),
        PlanStep(id="answer", action="compose_answer", depends_on=["first", "second"]),
    ],
)

sentinel.write_text("started", encoding="utf-8")
from queryforge.orchestration.runtime.resume import RunResumer

resumer = RunResumer(state_root, run_id)
resumer.save_plan(plan)
executor = AnalysisExecutor(
    registry,
    budget_manager=BudgetManager(),
    journal=resumer.journal,
)
result = executor.execute(plan)
sentinel.write_text(json.dumps(result.to_payload()), encoding="utf-8")
'''


def _fast_registry(counter: list[str]) -> ToolRegistry:
    registry = build_default_registry(None, BudgetManager())
    registry.register(
        ToolSpec(
            name="echo_step",
            description="fast echo",
            parameter_schema={
                "type": "object",
                "properties": {"value": {}},
                "additionalProperties": False,
            },
            modes=["execute"],
            budget_category="compute",
        ),
        lambda params, context=None: (
            counter.append(str(params.get("value"))),
            {"echo": params.get("value")},
        )[1],
    )
    return registry


class ProcessInterruptionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.directory.name) / "runs"
        self.run_dir = self.state_root / RUN_ID
        self.sentinel = Path(self.directory.name) / "started.txt"
        self.driver = Path(self.directory.name) / "child_driver.py"
        self.driver.write_text(CHILD_DRIVER, encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def _wait_for_first_step(self, process: subprocess.Popen, timeout: float = 60.0) -> dict:
        """Poll the journal until the child committed its first step."""
        deadline = time.monotonic() + timeout
        journal_path = self.run_dir / "execution.json"
        last: dict = {}
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(
                    "the child exited before the kill landed; the interruption "
                    "window was missed (this is a test-environment failure, not a "
                    "product failure)"
                )
            if journal_path.is_file():
                try:
                    last = json.loads(journal_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:  # mid-write; read again
                    last = {}
                steps = last.get("steps") or {}
                if (steps.get("first") or {}).get("status") == "succeeded":
                    return last
            time.sleep(0.02)
        self.fail(f"the child never committed its first step; last journal={last}")

    def test_sigkill_mid_plan_then_resume_reuses_the_committed_step(self):
        process = subprocess.Popen(
            [sys.executable, str(self.driver), str(self.state_root), RUN_ID, str(self.sentinel)],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        try:
            journal_state = self._wait_for_first_step(process)
            # The second step is inside its 60s tool call when the kill lands.
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=30)
        finally:
            if process.poll() is None:  # pragma: no cover - defensive
                process.kill()
                process.wait(timeout=30)

        self.assertLess(process.returncode, 0)  # killed by a signal, not a clean exit
        self.assertFalse(self.sentinel.read_text(encoding="utf-8").startswith("{"))
        steps = journal_state["steps"]
        self.assertEqual(steps["first"]["status"], "succeeded")
        self.assertEqual(steps["first"]["attempt"], 1)
        self.assertIn(steps["second"]["status"], {"running", "pending"})
        # The process died before writing a terminal outcome: the run is resumable.
        self.assertIsNone(journal_state["terminal_outcome"])
        self.assertTrue(RunResumer(self.state_root, RUN_ID).journal.resumable())

        # Resume in a fresh process image (this test process), same journal.
        counter: list[str] = []
        from queryforge.orchestration.planner import plan as plan_module
        from queryforge.orchestration.planner.executor import AnalysisExecutor
        from queryforge.orchestration.planner.plan import AnalysisPlan

        saved = plan_module.PLAN_ACTIONS
        plan_module.PLAN_ACTIONS = plan_module.PLAN_ACTIONS + ("echo_step",)
        plan_module.ACTION_TOOL_MAP["echo_step"] = "echo_step"
        try:
            plan = AnalysisPlan.model_validate(
                RunResumer(self.state_root, RUN_ID).load_plan().model_dump(mode="json")
            )
            result = AnalysisExecutor(
                _fast_registry(counter),
                budget_manager=BudgetManager(),
                journal=ExecutionJournal(self.run_dir, run_id=RUN_ID),
            ).execute(plan)
        finally:
            plan_module.PLAN_ACTIONS = saved
            plan_module.ACTION_TOOL_MAP.pop("echo_step", None)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.terminal_outcome, "success")
        self.assertEqual(result.reused_steps, ["first"])
        self.assertEqual(result.recomputed_steps, ["second", "answer"])
        # The slow step was never re-run; only the unfinished step hit the tool.
        self.assertEqual(counter, ["2"])
        self.assertEqual(
            [record.status for record in RunResumer(self.state_root, RUN_ID).journal.journal.steps.values()],
            ["succeeded", "succeeded", "succeeded"],
        )
        self.assertEqual(
            RunResumer(self.state_root, RUN_ID).status().terminal_outcome, "success"
        )


if __name__ == "__main__":
    unittest.main()
