"""Demo E — the API upload path, the real repair loop, and data-driven attribution.

Narrated, offline, reproducible. This demo absorbed the earlier
``scripts/demo_data_agent.py`` scenarios so step 17 has **one** demo surface; it
adds the properties the other demos do not cover:

1. the whole upload → quality-reject → publish → query loop runs through **real
   HTTP handlers** with a published data domain (401 without the key, 400 for a
   path outside the domain);
2. a wrong-but-executable SQL statement is repaired by the **real workflow fix
   node** into the governed definition, with the wrong value and the gold value
   both shown;
3. attribution **follows the data**: changing the uploaded/inserted rows changes
   the measured decline and the per-channel direction, and the answer's evidence
   ids track it;
4. a published domain cannot be relabelled by a caller-supplied path, and a
   revoked domain is refused.

Run: `.venv/bin/python docs/demo/run_demo_e.py`
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PROJECT_ROOT = HERE.parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from demo_lib import Demo, check, say  # noqa: E402
from scenarios_api_and_repair import (  # noqa: E402
    config_at,
    demo_analysis,
    demo_repair,
    demo_upload,
)


def main() -> int:
    with Demo("Demo E (API upload, repair loop, data-driven attribution)") as demo:
        root = demo.root

        say("E1: the upload → quality → publish → query loop over real HTTP handlers")
        upload = demo_upload(root / "upload" if (root / "upload").mkdir(parents=True) is None else root)
        published = upload["published_version"]
        check(
            upload["rows"] == [[3]],
            "the published data answers the governed metric (3 rows uploaded → count 3)",
            str(upload["rows"]),
        )
        check(
            upload["old_version_preserved"],
            "a quality-rejected upload leaves the previously published version intact (17-E2E1)",
            f"published_version={published}",
        )
        rejection = upload["quality_rejection"]
        check(
            bool(rejection),
            "the rejection carries an actionable reason",
            str(rejection)[:110],
        )
        say("   published data version", str(published))

        say("E2: an executable-but-wrong statement is repaired by the real fix node")
        repair = demo_repair(root / "repair" if (root / "repair").mkdir(parents=True) is None else root)
        trace = repair["trace"]
        check(
            repair["wrong_executable_value"] == 99,
            "the wrong statement runs and returns a plausible value (99, filtered on valid=0)",
            f"wrong={repair['wrong_executable_value']}",
        )
        check(
            trace["rows"] == [[30.0]],
            "the repaired statement returns the governed value (30, valid=1)",
            f"rows={trace['rows']}",
        )
        nodes = [node["name"] for node in (trace.get("run_summary") or {}).get("workflow_nodes") or []]
        check(
            "fix" in nodes,
            "the repair happened in the real workflow fix node, not in the demo",
            ", ".join(nodes),
        )

        say("E3: attribution follows the data, not the question")
        seen: list[tuple[int, int, str]] = []
        for paid, expected_delta, direction in ((20, -20, "decrease"), (40, 0, "flat"), (60, 20, "increase")):
            scenario_root = root / f"growth_{paid}"
            scenario_root.mkdir(parents=True, exist_ok=True)
            result = demo_analysis(scenario_root, paid_last=paid)
            contribution = result["contribution"]
            paid_row = next(
                row for row in contribution["contributions"] if row["category"] == "paid"
            )
            check(
                contribution["total_delta"] == expected_delta
                and paid_row["direction"] == direction
                and paid_row["delta"] == expected_delta
                and contribution["residual"] == 0,
                f"paid={paid} → total_delta={expected_delta}, direction={direction}, residual=0",
                f"delta={paid_row['delta']}",
            )
            answer = result["trace"]["final_answer"]
            check(
                bool(answer["evidence_ids"]),
                f"the answer for paid={paid} is anchored to evidence",
                f"{len(answer['evidence_ids'])} ids",
            )
            seen.append((paid, contribution["total_delta"], paid_row["direction"]))
        check(
            [item[2] for item in seen] == ["decrease", "flat", "increase"],
            "the conclusion changes with the data instead of matching the question's premise",
            str(seen),
        )

        say("E4: a published domain cannot be relabelled, and a revoked domain is refused")
        planner, resolver = _domain_probe(root)
        from queryforge.domain.domains import DomainContext

        database = root / "owned.sqlite"
        sqlite3.connect(database).close()
        resolver.publish(
            DomainContext(
                domain_id="owned",
                data_version="1",
                schema_fingerprint="fixture",
                database_path=str(database),
            )
        )
        try:
            planner.analyze("anything", domain_id="owned", database="/tmp/another.sqlite")
        except ValueError as exc:
            check("conflicts" in str(exc), "a caller-supplied path may not relabel a domain", str(exc)[:90])
        else:  # pragma: no cover - the refusal is the contract
            check(False, "a caller-supplied path may not relabel a domain", "no error raised")

        resolver.revoke("owned")
        try:
            planner.analyze("anything", domain_id="owned")
        except ValueError as exc:
            check(True, "a revoked domain is refused", str(exc)[:80])
        else:  # pragma: no cover - the refusal is the contract
            check(False, "a revoked domain is refused", "no error raised")
    print("\n[demo] Demo E complete: API upload, repair loop and data-driven attribution verified.")
    return 0


def _domain_probe(root: Path):
    from queryforge.application.analysis_planner import AnalysisPlannerService
    from queryforge.domain.domains import DomainResolver

    config = config_at(root)
    return (
        AnalysisPlannerService(config_loader=lambda: config),
        DomainResolver.from_config(config),
    )


if __name__ == "__main__":
    raise SystemExit(main())
