"""Select local skills for a node and render prompt-safe context blocks."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from collections import OrderedDict
from hashlib import sha256
from threading import RLock

from queryforge.domain.skills.registry import (
    SkillDefinition,
    SkillRegistry,
    SkillRegistryError,
)


MAX_SKILL_CONTENT_CHARS = 64_000


@dataclass(frozen=True, slots=True)
class SkillContext:
    node_name: str
    available_skills: str
    loaded_skills: str
    loaded_skill_names: tuple[str, ...]

    @property
    def prompt_context(self) -> str:
        return f"{self.available_skills}\n\n{self.loaded_skills}"


class SkillManager:
    """Resolve automatic or explicitly selected prompt-only skills."""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or SkillRegistry()
        self._context_cache: OrderedDict[tuple, SkillContext] = OrderedDict()
        self._cache_lock = RLock()

    def context_for_node(
        self,
        node_name: str,
        selected_names: list[str] | tuple[str, ...] | None = None,
        *,
        available_names: list[str] | None = None,
    ) -> SkillContext:
        all_skills = self.registry.list_skills()
        available = [skill for skill in all_skills if skill.applies_to(node_name)]
        if available_names is not None:
            available = [skill for skill in available if skill.name in available_names]
        if selected_names is None:
            loaded = [skill for skill in available if skill.metadata.enabled]
        else:
            requested = list(dict.fromkeys(selected_names))
            by_name = {skill.name: skill for skill in all_skills}
            unknown = [name for name in requested if name not in by_name]
            if unknown:
                known = ", ".join(sorted(by_name)) or "none"
                raise SkillRegistryError(
                    f"Unknown skill(s): {', '.join(unknown)}. Available: {known}"
                )
            requested_set = set(requested)
            loaded = [skill for skill in available if skill.name in requested_set]

        contents = [self._read_content(skill) for skill in loaded]
        key = (str(self.registry.skills_dir), node_name,
               tuple(skill.metadata for skill in available),
               tuple((skill.name, sha256(content.encode()).hexdigest()) for skill, content in zip(loaded, contents)))
        with self._cache_lock:
            cached = self._context_cache.get(key)
            if cached is not None:
                self._context_cache.move_to_end(key)
                return cached
        result = SkillContext(
            node_name=node_name,
            available_skills=self._render_available(node_name, available),
            loaded_skills=self._render_loaded(node_name, loaded, contents),
            loaded_skill_names=tuple(skill.name for skill in loaded),
        )
        with self._cache_lock:
            self._context_cache[key] = result
            if len(self._context_cache) > 32:
                self._context_cache.popitem(last=False)
        return result

    @staticmethod
    def _render_available(node_name: str, skills: list[SkillDefinition]) -> str:
        lines = [f'<available_skills node="{escape(node_name)}">']
        for skill in skills:
            metadata = skill.metadata
            lines.append(
                f'  <skill name="{escape(metadata.name)}" '
                f'enabled="{str(metadata.enabled).lower()}" '
                f'priority="{metadata.priority}">'
                f"{escape(metadata.description)}</skill>"
            )
        lines.append("</available_skills>")
        return "\n".join(lines)

    @staticmethod
    def _read_content(skill: SkillDefinition) -> str:
        try:
            content = skill.instruction_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SkillRegistryError(f"Could not read skill instructions {skill.instruction_path}: {exc}") from exc
        if len(content) > MAX_SKILL_CONTENT_CHARS:
            raise SkillRegistryError(f"Skill {skill.name!r} exceeds {MAX_SKILL_CONTENT_CHARS} characters")
        return content

    @staticmethod
    def _render_loaded(node_name: str, skills: list[SkillDefinition], contents: list[str]) -> str:
        lines = [f'<loaded_skills node="{escape(node_name)}">']
        for skill, content in zip(skills, contents):
            lines.extend(
                [
                    f'  <skill name="{escape(skill.name)}">',
                    content,
                    "  </skill>",
                ]
            )
        lines.append("</loaded_skills>")
        return "\n".join(lines)
