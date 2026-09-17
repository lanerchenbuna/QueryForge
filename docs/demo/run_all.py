"""Run every step-17 demo and report a single exit code.

Each demo is a narrated, asserting script; this runner keeps the "clean
environment reproduces the demos" claim (17-N1) checkable in one command
(``make demo``), and stops at the first failure so the output stays readable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEMOS = (
    "run_demo_a.py",
    "run_demo_b.py",
    "run_demo_c.py",
    "run_demo_d.py",
    "run_demo_e.py",
)
HERE = Path(__file__).resolve().parent


def main() -> int:
    for name in DEMOS:
        print(f"\n=== {name} " + "=" * (60 - len(name)), flush=True)
        completed = subprocess.run(
            [sys.executable, str(HERE / name)], cwd=str(HERE.parents[1]), check=False
        )
        if completed.returncode != 0:
            print(f"\n[demo] {name} FAILED (exit {completed.returncode})", file=sys.stderr)
            return completed.returncode
    print(f"\n[demo] all {len(DEMOS)} demos passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
