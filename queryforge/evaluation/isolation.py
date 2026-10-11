"""Fail closed on split leakage before running any task or model."""
import hashlib
import json
import re
from pathlib import Path


def fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().casefold())
    return hashlib.sha256(normalized.encode()).hexdigest()


def audit_splits(specs) -> None:
    groups = {}
    questions = {}
    for spec in specs:
        # schema split and template split are explicit dataset design constraints.
        for key in ("schema:" + spec.dataset, "template:" + str(getattr(spec, "template_id", spec.task_id))):
            if key in groups and groups[key] != spec.split:
                raise ValueError(f"split leakage: {key}")
            groups[key] = spec.split
        key = fingerprint(spec.question)
        if key in questions and questions[key] != spec.split:
            raise ValueError("duplicate question across splits")
        questions[key] = spec.split


def audit_corpus(specs, path: Path) -> None:
    # Scan question and SQL independently; extra metadata cannot hide a leak.
    protected = {fingerprint(text) for s in specs if s.split == "holdout"
                 for text in (s.question, getattr(s, "reference_sql", "")) if text}
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip() and any(fingerprint(s) in protected for s in strings(json.loads(line))):
            raise ValueError(f"holdout contamination: {path}:{number}")


def audit_dataset_schemas(specs, datasets, root: Path) -> dict[str, str]:
    """Compare physical schemas, so renaming a dataset cannot evade holdout isolation."""
    from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector

    signatures, splits = {}, {}
    for spec in specs:
        dataset = datasets.get(spec.dataset)
        if dataset is None:
            raise ValueError(f"unknown dataset: {spec.dataset}")
        if dataset.get("split_scope") != spec.split:
            raise ValueError(f"dataset split_scope mismatch: {spec.dataset}")
        if spec.dataset not in signatures:
            database = Path(dataset["database"])
            connector = SQLiteConnector(str(database if database.is_absolute() else root / database))
            try:
                schemas = [connector.describe_table(name).model_dump() for name in connector.list_tables()]
                # Rows are deliberately excluded: copied data on the same schema still leaks.
                signatures[spec.dataset] = hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()
            finally:
                connector.close()
        key = signatures[spec.dataset]
        if key in splits and splits[key] != spec.split:
            raise ValueError(f"physical schema split leakage: {spec.dataset}")
        splits[key] = spec.split
    return signatures
