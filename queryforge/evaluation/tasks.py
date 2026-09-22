"""Gold task specifications: the *what* of the step-16 agent benchmark.

The evaluator grades a recorded trace against a gold task, so this module owns
exactly one job: turn ``evaluation/tasks/<split>.jsonl`` into validated
:class:`TaskSpec` objects.

Three decisions are deliberate:

* unknown fields are kept (``extra="allow"``) so a gold set that grows faster
  than the evaluator never crashes scoring -- the evaluator only reads the
  fields the frozen interface fixes;
* a malformed row raises immediately with its ``path:line``, because a benchmark
  that silently skips an unparsable gold row reports a success rate over an
  unknown denominator;
* the declared ``split`` must agree with the file name, so a task cannot be
  graded against the wrong split (holdout isolation depends on it).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: Splits of the benchmark; the file name is the split (contract 2.2/2.4).
KNOWN_SPLITS: tuple[str, ...] = ("dev", "regression", "holdout")

Split = Literal["dev", "regression", "holdout"]

#: Outcome kinds a gold task may expect (contract 2.2).
OutcomeKind = Literal["analysis", "clarification", "query", "policy_rejection"]

#: Default *relative* tolerance for ``expected_values``; a per-key override in
#: ``values_tolerance`` replaces it for that key, ``"*"`` replaces it globally.
DEFAULT_VALUES_TOLERANCE = 1e-6


class TaskSpecError(ValueError):
    """A gold task row could not be loaded (kept distinct for clear gating)."""


def _clean_text_list(value: Any) -> list[str]:
    """Normalize a gold string list: strip, drop blanks, de-duplicate in order."""

    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise ValueError("expected a list of strings, not a single string")
    if isinstance(value, (set, frozenset)):
        # Sets have no stable order; sorting keeps loading deterministic.
        value = sorted(value, key=str)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"expected a list of strings, got {type(value).__name__}")
    cleaned: list[str] = []
    for item in value:
        text = "" if item is None else str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_replaceable(value: Any) -> dict[str, list[str]]:
    """Normalize ``replaceable_steps`` (action -> accepted equivalent actions)."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("replaceable_steps must be an object of action -> actions")
    cleaned: dict[str, list[str]] = {}
    for key, alternatives in value.items():
        action = str(key).strip()
        if not action:
            raise ValueError("replaceable_steps keys must be non-empty action names")
        cleaned[action] = _clean_text_list(alternatives)
    return cleaned


def _clean_tolerance(value: Any) -> dict[str, float]:
    """Normalize per-key tolerances; a non-finite or negative tolerance is a bug."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("values_tolerance must be an object of key -> tolerance")
    cleaned: dict[str, float] = {}
    for key, raw in value.items():
        try:
            tolerance = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"values_tolerance[{key!r}] must be a number") from exc
        if not math.isfinite(tolerance) or tolerance < 0.0:
            raise ValueError(
                f"values_tolerance[{key!r}] must be a finite, non-negative number"
            )
        cleaned[str(key)] = tolerance
    return cleaned


class TaskSpec(BaseModel):
    """One gold task (contract 2.2 field by field)."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    split: Split
    dataset: str
    question: str
    coverage: list[str] = Field(default_factory=list)
    expected_outcome: OutcomeKind
    expected_status: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    required_steps: list[str] = Field(default_factory=list)
    replaceable_steps: dict[str, list[str]] = Field(default_factory=dict)
    required_evidence: list[str] = Field(default_factory=list)
    forbidden_evidence: list[str] = Field(default_factory=list)
    expected_values: dict[str, Any] = Field(default_factory=dict)
    values_tolerance: dict[str, float] = Field(default_factory=dict)
    answer_must_reference_evidence: bool = True
    forbidden_claims: list[str] = Field(default_factory=list)
    required_claims: list[str] = Field(default_factory=list)
    acceptable_stop_reasons: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = None
    holdout_fingerprint: str | None = None
    notes: str | None = None

    @field_validator(
        "coverage",
        "expected_status",
        "allowed_tools",
        "required_steps",
        "required_evidence",
        "forbidden_evidence",
        "forbidden_claims",
        "required_claims",
        "acceptable_stop_reasons",
        mode="before",
    )
    @classmethod
    def _normalize_lists(cls, value: Any) -> list[str]:
        return _clean_text_list(value)

    @field_validator("task_id", "dataset", "question")
    @classmethod
    def _require_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("task_id, dataset and question must be non-empty")
        return text

    @field_validator("replaceable_steps", mode="before")
    @classmethod
    def _normalize_replaceable(cls, value: Any) -> dict[str, list[str]]:
        return _clean_replaceable(value)

    @field_validator("values_tolerance", mode="before")
    @classmethod
    def _normalize_tolerance(cls, value: Any) -> dict[str, float]:
        return _clean_tolerance(value)

    @field_validator("max_tool_calls")
    @classmethod
    def _require_non_negative_budget(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError("max_tool_calls must be zero or greater")
        return int(value)

    # -- helpers the evaluator relies on ----------------------------------

    def tolerance_for(self, key: str) -> float:
        """Relative tolerance for one ``expected_values`` key.

        A per-key entry wins, then a ``"*"`` entry, then the documented default;
        the tolerance is *relative* so a big number is not compared as if it were
        a small one.
        """

        if key in self.values_tolerance:
            return self.values_tolerance[key]
        if "*" in self.values_tolerance:
            return self.values_tolerance["*"]
        return DEFAULT_VALUES_TOLERANCE

    def requires_evidence(self) -> bool:
        """Whether this task's success depends on evidence anchoring."""

        return bool(self.required_evidence) or bool(self.answer_must_reference_evidence)

    def is_multi_step(self) -> bool:
        """Whether the task is scored as a multi-step analysis (contract 3.6)."""

        return len(self.required_steps) > 1 or "multi_step_analysis" in self.coverage


def spec_index(specs: Iterable[TaskSpec]) -> dict[str, TaskSpec]:
    """Index specs by ``task_id`` (later duplicates are ignored, kept explicit)."""

    index: dict[str, TaskSpec] = {}
    for spec in specs:
        index.setdefault(spec.task_id, spec)
    return index


def _split_from_stem(stem: str) -> str | None:
    return stem if stem in KNOWN_SPLITS else None


def _load_documents(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Parse one gold file as JSONL, or as a JSON array/object of tasks."""

    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped[0] in "[{":
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        if payload is not None:
            if isinstance(payload, dict):
                payload = payload["tasks"] if "tasks" in payload else [payload]
            if isinstance(payload, list):
                documents: list[tuple[int, dict[str, Any]]] = []
                for index, item in enumerate(payload, start=1):
                    if not isinstance(item, dict):
                        raise TaskSpecError(
                            f"{path}:{index}: every task must be a JSON object"
                        )
                    documents.append((index, item))
                return documents
    documents = []
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            item = json.loads(entry)
        except ValueError as exc:
            raise TaskSpecError(f"{path}:{number}: invalid JSON ({exc})") from exc
        if not isinstance(item, dict):
            raise TaskSpecError(f"{path}:{number}: every task must be a JSON object")
        documents.append((number, item))
    return documents


def _validate(path: Path, number: int, document: dict[str, Any]) -> TaskSpec:
    try:
        return TaskSpec.model_validate(document)
    except ValidationError as exc:
        raise TaskSpecError(f"{path}:{number}: invalid gold task: {exc}") from exc


def load_specs(path: str | Path) -> list[TaskSpec]:
    """Load every gold task of one file (JSONL, one task per line)."""

    target = Path(path)
    documents = _load_documents(target)
    expected_split = _split_from_stem(target.stem)
    specs: list[TaskSpec] = []
    for number, document in documents:
        if expected_split and "split" not in document:
            document = {**document, "split": expected_split}
        spec = _validate(target, number, document)
        if expected_split and spec.split != expected_split:
            raise TaskSpecError(
                f"{target}:{number}: split {spec.split!r} does not match the file "
                f"name ({expected_split!r}); a task must be graded in its own split"
            )
        specs.append(spec)
    _require_unique_ids(specs)
    return specs


def load_spec_splits(directory: str | Path = "evaluation/tasks") -> list[TaskSpec]:
    """Load every split of the gold task directory (file name == split)."""

    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"gold task directory does not exist: {root}")
    files = sorted(root.glob("*.jsonl"), key=lambda item: item.name)
    if not files:
        raise FileNotFoundError(f"no gold task files (*.jsonl) in {root}")
    specs: list[TaskSpec] = []
    for path in files:
        if _split_from_stem(path.stem) is None:
            raise TaskSpecError(
                f"{path}: unexpected gold file name; expected one of "
                f"{', '.join(name + '.jsonl' for name in KNOWN_SPLITS)}"
            )
        specs.extend(load_specs(path))
    _require_unique_ids(specs)
    return specs


def _require_unique_ids(specs: Sequence[TaskSpec]) -> None:
    seen: dict[str, str] = {}
    for spec in specs:
        previous = seen.get(spec.task_id)
        if previous is not None:
            raise TaskSpecError(
                f"duplicate task_id {spec.task_id!r} in {spec.split} and {previous}"
            )
        seen[spec.task_id] = spec.split


__all__ = [
    "DEFAULT_VALUES_TOLERANCE",
    "KNOWN_SPLITS",
    "OutcomeKind",
    "Split",
    "TaskSpec",
    "TaskSpecError",
    "load_spec_splits",
    "load_specs",
    "spec_index",
]
