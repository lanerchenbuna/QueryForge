"""Discover and validate local, prompt-only QueryForge skills."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILLS_DIR = PACKAGE_ROOT / "bundled_skills"
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class SkillRegistryError(ValueError):
    """Raised when a local skill is missing or has invalid metadata."""


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    allowed_nodes: tuple[str, ...]
    enabled: bool
    priority: int


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    metadata: SkillMetadata
    directory: Path
    instruction_path: Path

    @property
    def name(self) -> str:
        return self.metadata.name

    def applies_to(self, node_name: str) -> bool:
        return node_name in self.metadata.allowed_nodes


class SkillRegistry:
    """Scan one local directory; skills are data and never imported as code."""

    def __init__(self, skills_dir: str | Path = DEFAULT_SKILLS_DIR) -> None:
        self.skills_dir = Path(skills_dir).expanduser().resolve()

    def list_skills(self) -> list[SkillDefinition]:
        if not self.skills_dir.exists():
            return []
        if not self.skills_dir.is_dir():
            raise SkillRegistryError(
                f"Skills path is not a directory: {self.skills_dir}"
            )

        skills: list[SkillDefinition] = []
        names: set[str] = set()
        for directory in sorted(path for path in self.skills_dir.iterdir() if path.is_dir()):
            metadata_path = directory / "skill.yml"
            instruction_path = directory / "SKILL.md"
            if not metadata_path.is_file():
                raise SkillRegistryError(
                    f"Skill directory is missing skill.yml: {directory}"
                )
            if not instruction_path.is_file():
                raise SkillRegistryError(
                    f"Skill {directory.name!r} is missing SKILL.md: {instruction_path}"
                )
            metadata = self._read_metadata(metadata_path)
            if metadata.name != directory.name:
                raise SkillRegistryError(
                    f"Skill name {metadata.name!r} must match directory {directory.name!r}"
                )
            if metadata.name in names:
                raise SkillRegistryError(f"Duplicate skill name: {metadata.name}")
            names.add(metadata.name)
            skills.append(
                SkillDefinition(
                    metadata=metadata,
                    directory=directory,
                    instruction_path=instruction_path,
                )
            )
        return sorted(
            skills,
            key=lambda skill: (-skill.metadata.priority, skill.metadata.name),
        )

    def get(self, name: str) -> SkillDefinition:
        for skill in self.list_skills():
            if skill.name == name:
                return skill
        available = ", ".join(skill.name for skill in self.list_skills()) or "none"
        raise SkillRegistryError(
            f"Unknown skill {name!r}. Available local skills: {available}"
        )

    @staticmethod
    def _read_metadata(path: Path) -> SkillMetadata:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SkillRegistryError(f"Could not read skill metadata {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SkillRegistryError(f"Skill metadata must be a mapping: {path}")

        name = _required_string(payload, "name", path)
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise SkillRegistryError(
                f"Skill name must match {SKILL_NAME_PATTERN.pattern!r}: {name!r}"
            )
        description = _required_string(payload, "description", path)
        raw_nodes = payload.get("allowed_nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes or not all(
            isinstance(node, str) and node.strip() for node in raw_nodes
        ):
            raise SkillRegistryError(
                f"allowed_nodes must be a non-empty string list: {path}"
            )
        allowed_nodes = tuple(dict.fromkeys(node.strip() for node in raw_nodes))
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise SkillRegistryError(f"enabled must be true or false: {path}")
        priority = payload.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise SkillRegistryError(f"priority must be an integer: {path}")
        return SkillMetadata(
            name=name,
            description=description,
            allowed_nodes=allowed_nodes,
            enabled=enabled,
            priority=priority,
        )


def _required_string(payload: dict[str, Any], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillRegistryError(f"{key} must be a non-empty string: {path}")
    return value.strip()
