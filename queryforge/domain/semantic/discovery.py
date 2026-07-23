"""Convention-based semantic-model discovery for a selected database."""

from __future__ import annotations

from pathlib import Path


def discover_semantic_model(
    database_path: str | Path,
    explicit_path: str | Path | None = None,
) -> str | None:
    """Return an explicit model or the first conventional sibling model.

    Discovery is intentionally local to the database directory. This prevents a
    model for one database from being silently applied to an unrelated source.
    """

    if explicit_path:
        return str(Path(explicit_path).expanduser())
    database = Path(database_path).expanduser().resolve()
    candidates = (
        database.with_suffix(".semantic.yml"),
        database.parent / "semantic_model.yml",
        database.parent / f"{database.stem}_semantic_model.yml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None
