"""Paired summaries with task-cluster uncertainty, independent of the agent runtime."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import mean


def summarize_pairs(records, *, repeats: int, seed: int = 17, bootstrap_samples: int = 2000):
    if repeats < 2 or bootstrap_samples < 100:
        raise ValueError("paired experiments need at least two repeats and 100 bootstrap samples")
    groups = defaultdict(dict)
    datasets = {}
    for record in records:
        if type(record.get("passed")) is not bool:
            raise ValueError("paired verdict must be a boolean")
        task, repeat, arm = record["task_id"], record["repeat"], record["arm"]
        if arm not in {"baseline", "treatment"} or type(repeat) is not int or not 0 <= repeat < repeats:
            raise ValueError("invalid pair arm or repeat")
        if task in datasets and datasets[task] != record["dataset"]:
            raise ValueError("paired dataset mismatch")
        datasets[task] = record["dataset"]
        key = (task, repeat)
        if arm in groups[key]:
            raise ValueError("duplicate pair arm")
        groups[key][arm] = record
    if not groups:
        raise ValueError("no paired observations")
    for task in datasets:
        for repeat in range(repeats):
            if set(groups.get((task, repeat), {})) != {"baseline", "treatment"}:
                raise ValueError(f"missing pair: {task}, repeat {repeat}")
    eligible = [pair for pair in groups.values() if not any(
        r.get("environment_error") or r.get("provider_contract_error") for r in pair.values())]
    excluded = [{"task_id": task, "repeat": repeat,
                 "arms": [arm for arm, r in pair.items() if r.get("environment_error")]}
                for (task, repeat), pair in groups.items() if any(r.get("environment_error") for r in pair.values())]
    contract_errors = [{"task_id": task, "repeat": repeat,
                        "arms": [arm for arm, r in pair.items() if r.get("provider_contract_error")]}
                       for (task, repeat), pair in groups.items() if any(r.get("provider_contract_error") for r in pair.values())]

    def cluster_summary(pairs, value):
        clusters = defaultdict(list)
        baselines, treatments = defaultdict(list), defaultdict(list)
        for pair in pairs:
            b, t = value(pair["baseline"]), value(pair["treatment"])
            if b is None or t is None or not all(math.isfinite(x) for x in (b, t)):
                continue
            task = pair["baseline"]["task_id"]
            clusters[task].append(t - b)
            baselines[task].append(b)
            treatments[task].append(t)
        if not clusters:
            return {"pairs": 0, "unique_tasks": 0, "baseline_mean": None, "treatment_mean": None,
                    "delta": None, "ci95": None}
        differences = [mean(v) for _, v in sorted(clusters.items())]
        rng = random.Random(seed)
        samples = sorted(mean(rng.choices(differences, k=len(differences))) for _ in range(bootstrap_samples))
        return {"pairs": sum(map(len, clusters.values())), "unique_tasks": len(clusters),
                "baseline_mean": mean(mean(v) for v in baselines.values()),
                "treatment_mean": mean(mean(v) for v in treatments.values()), "delta": mean(differences),
                "ci95": [samples[int(.025 * bootstrap_samples)], samples[int(.975 * bootstrap_samples) - 1]]}

    return {"total_pairs": len(groups), "evaluable_pairs": len(eligible), "excluded_environment_pairs": excluded,
            "excluded_provider_contract_pairs": contract_errors,
            "unique_tasks": len(datasets), "datasets": sorted(set(datasets.values())), "repeats": repeats,
            "accuracy": cluster_summary(eligible, lambda r: float(r["passed"])),
            "wall_ms": cluster_summary(eligible, lambda r: r.get("wall_ms")),
            "measured_tokens": cluster_summary(eligible, lambda r: r.get("measured_tokens") or None),
            "cost_usd": cluster_summary(eligible, lambda r: r.get("cost_usd") if r.get("cost_basis") in {"reported", "priced"} else None),
            "uncertainty": {"method": "paired task-cluster percentile bootstrap", "samples": bootstrap_samples,
                            "seed": seed, "estimand": "equal-weight task means; treatment minus baseline",
                            "note": "Repeats stay inside their task cluster. Small curated task sets do not establish generalization; a zero-width interval is not proof of no effect."},
            "complete": not (excluded or contract_errors)}
