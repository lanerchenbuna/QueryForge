"""Pre-defined benchmark thresholds and their gate semantics (step 16).

Thresholds are checked in *before* the run they grade: a threshold edited after
seeing the numbers is not a gate.  This module therefore only ever reads
``evaluation/thresholds.json`` -- it never derives a default from results.

:func:`check_metrics` returns human-readable violations instead of a boolean so
a failing gate prints exactly what missed and by how much.  Two rules matter:

* a rule that names an unknown metric is a violation, not a silent pass (a typo
  in a gate must fail loudly);
* a metric that *cannot be measured* (``None``: no tasks, no recorded tool
  calls, no usage, no price table) is a violation too, because "unmeasurable"
  must never look like "100% passed" (16-B1).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Tiers of the benchmark; they are reported separately and never mixed.
TIER_NAMES: tuple[str, ...] = ("tier1_offline", "tier2_integration", "tier3_model_e2e")

#: The checked-in thresholds document (repo relative; this file lives in
#: ``queryforge/evaluation/``, so two parents up is the project root).
DEFAULT_THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[2] / "evaluation" / "thresholds.json"
)

#: Rule keys inside a tier: ``min_<metric>`` / ``max_<metric>``.
_RULE_PREFIXES: tuple[str, ...] = ("min_", "max_")

#: Tier keys that are metadata rather than a rule.
_NON_RULE_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "note",
        "notes",
        "description",
        "title",
        "version",
        "splits",
        "required_dependencies",
        "tier",
        "superseded",
    }
)

#: Rule name -> path inside the aggregate report.  ``clarification_appropriateness``
#: is a counting block, so its rule reads the nested overall ``rate``.
METRIC_PATHS: dict[str, tuple[str, ...]] = {
    "task_success_rate": ("task_success_rate",),
    "multi_step_success_rate": ("multi_step_success_rate",),
    "evidence_coverage_rate": ("evidence_coverage_rate",),
    "unsupported_assertion_rate": ("unsupported_assertion_rate",),
    "tool_legality_rate": ("tool_legality_rate",),
    "tool_validity_rate": ("tool_validity_rate",),
    "clarification_appropriateness": ("clarification_appropriateness", "rate"),
    "avg_tool_calls_per_task": ("avg_tool_calls",),
    "p50_wall_ms": ("p50_wall_ms",),
    "p95_wall_ms": ("p95_wall_ms",),
    "cost_usd": ("cost_usd",),
    "task_count": ("task_count",),
}


@dataclass(frozen=True)
class ThresholdRule:
    """One ``min_``/``max_`` rule with its metric resolved by name."""

    key: str
    op: str
    metric: str
    value: float


def _rules_from(raw: Mapping[str, Any]) -> list[ThresholdRule]:
    """Extract the min/max rules of one tier mapping (stable order)."""

    rules: list[ThresholdRule] = []
    for key in sorted(str(item) for item in raw):
        if key in _NON_RULE_KEYS:
            continue
        prefix = next((item for item in _RULE_PREFIXES if key.startswith(item)), None)
        if prefix is None:
            continue
        value = raw.get(key)
        number = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
        if number is None or not math.isfinite(number):
            # A non-numeric rule is kept so the gate reports it instead of
            # silently dropping a threshold somebody intended to enforce.
            rules.append(ThresholdRule(key=key, op="invalid", metric=key, value=0.0))
            continue
        rules.append(
            ThresholdRule(
                key=key,
                op=prefix.rstrip("_"),
                metric=key[len(prefix):],
                value=number,
            )
        )
    return rules


class TierConfig(BaseModel):
    """One gate tier: threshold rules plus the tier's own metadata."""

    model_config = ConfigDict(extra="allow")

    name: str = ""
    note: str | None = None
    required_dependencies: list[str] = Field(default_factory=list)
    splits: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def rules(self) -> list[ThresholdRule]:
        """Every min/max rule of this tier, including keys added by a new gate."""

        return _rules_from(self.model_dump())

    def split_rules(self, split: str) -> list[ThresholdRule]:
        """Rules that apply to one split only."""

        return _rules_from(self.splits.get(split) or {})


class Thresholds(BaseModel):
    """The whole thresholds document (typed access per contract 四)."""

    model_config = ConfigDict(extra="allow")

    version: str = "1.0"
    tier1_offline: TierConfig = Field(default_factory=TierConfig)
    tier2_integration: TierConfig = Field(default_factory=TierConfig)
    tier3_model_e2e: TierConfig = Field(default_factory=TierConfig)

    @model_validator(mode="after")
    def _label_tiers(self) -> "Thresholds":
        for name in TIER_NAMES:
            tier = getattr(self, name)
            if not tier.name:
                tier.name = name
        return self

    def tier(self, name: str) -> TierConfig:
        """One tier by name; an unknown name raises instead of silently passing."""

        if name not in TIER_NAMES:
            raise ValueError(
                f"unknown threshold tier {name!r}; expected one of "
                f"{', '.join(TIER_NAMES)}"
            )
        return getattr(self, name)

    def names(self) -> tuple[str, ...]:
        return TIER_NAMES

    def required_dependencies(self, tier: str) -> list[str]:
        """Optional dependencies this tier must actually have installed (16-R1)."""

        return list(self.tier(tier).required_dependencies)


def load_thresholds(path: str | Path | None = None) -> Thresholds:
    """Load the checked-in thresholds document (or one given path)."""

    target = Path(path) if path is not None else DEFAULT_THRESHOLDS_PATH
    if not target.is_file():
        raise FileNotFoundError(f"thresholds document does not exist: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"thresholds document is not valid JSON: {target}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"thresholds document must be a JSON object: {target}")
    return Thresholds.model_validate(payload)


def _coerce_tier(tier: str | TierConfig | Mapping[str, Any] | None,
                 thresholds: Thresholds | Mapping[str, Any] | None) -> TierConfig:
    """Resolve the tier to check, from a name, a config, or a tier mapping."""

    if isinstance(tier, TierConfig):
        return tier
    if isinstance(tier, Mapping):
        config = TierConfig.model_validate(dict(tier))
        if not config.name:
            config.name = "thresholds"
        return config
    if isinstance(tier, str):
        if isinstance(thresholds, Thresholds):
            return thresholds.tier(tier)
        if isinstance(thresholds, Mapping):
            entry = thresholds.get(tier)
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"thresholds document has no tier {tier!r} to check against"
                )
            config = TierConfig.model_validate(dict(entry))
            config.name = config.name or tier
            return config
        if thresholds is None:
            return load_thresholds().tier(tier)
        raise ValueError(
            "thresholds must be a mapping, a Thresholds document or None when a "
            "tier name is given"
        )
    raise ValueError(
        "tier must be a tier name, a TierConfig or a tier mapping"
    )


def _resolve(metrics: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = metrics
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _compare(rule: ThresholdRule, actual: Any, label: str) -> str | None:
    """Return a violation message, or ``None`` when the rule is satisfied."""

    if rule.op == "invalid":
        return (
            f"{label}: threshold rule '{rule.key}' has a non-numeric value; a rule "
            "that cannot be compared must be fixed, not ignored"
        )
    if METRIC_PATHS.get(rule.metric) is None:
        return (
            f"{label}: threshold rule '{rule.key}' names an unknown metric "
            f"'{rule.metric}'; it cannot be enforced"
        )
    if actual is None:
        return (
            f"{label}: {rule.metric} is not measurable, so the pre-defined "
            f"({rule.op} {rule.value}) cannot be verified"
        )
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return (
            f"{label}: {rule.metric} is not a number ({actual!r}), so the "
            f"pre-defined ({rule.op} {rule.value}) cannot be verified"
        )
    number = float(actual)
    if not math.isfinite(number):
        return f"{label}: {rule.metric} is not finite"
    if rule.op == "min" and number < rule.value:
        return (
            f"{label}: {rule.metric}={number} is below the pre-defined minimum "
            f"{rule.value}"
        )
    if rule.op == "max" and number > rule.value:
        return (
            f"{label}: {rule.metric}={number} is above the pre-defined maximum "
            f"{rule.value}"
        )
    return None


def _metric_value(metrics: Mapping[str, Any], rule: ThresholdRule) -> Any:
    """Read the metric one rule names, or ``None`` when it is not measurable."""

    path = METRIC_PATHS.get(rule.metric)
    if path is None:
        return None
    return _resolve(metrics, path)


def check_metrics(
    metrics: Mapping[str, Any],
    tier: str | TierConfig | Mapping[str, Any],
    thresholds: Thresholds | Mapping[str, Any] | None = None,
) -> list[str]:
    """Check one aggregate report against one tier's pre-defined thresholds.

    ``metrics`` is the result of :func:`queryforge.evaluation.evaluator.aggregate`
    (or any mapping carrying the same metric names).  ``tier`` may be a tier name
    (then ``thresholds`` -- a loaded document or the parsed JSON -- supplies it),
    a :class:`TierConfig`, or the tier mapping itself.

    Returns the list of human-readable violations; an empty list means the tier's
    every pre-defined rule was verifiable and satisfied.
    """

    report = metrics if isinstance(metrics, Mapping) else {}
    config = _coerce_tier(tier, thresholds)
    label = config.name or "thresholds"
    violations: list[str] = []
    for rule in config.rules():
        message = _compare(rule, _metric_value(report, rule), label)
        if message:
            violations.append(message)
    for split in sorted(config.splits):
        bucket = _resolve(report, ("by_split", split))
        if not isinstance(bucket, Mapping):
            violations.append(
                f"{label}: split '{split}' has pre-defined thresholds but the "
                "report carries no per-split metrics for it"
            )
            continue
        for rule in config.split_rules(split):
            message = _compare(
                rule, _metric_value(bucket, rule), f"{label}.splits.{split}"
            )
            if message:
                violations.append(message)
    return violations


__all__ = [
    "DEFAULT_THRESHOLDS_PATH",
    "METRIC_PATHS",
    "TIER_NAMES",
    "ThresholdRule",
    "Thresholds",
    "TierConfig",
    "check_metrics",
    "load_thresholds",
]
