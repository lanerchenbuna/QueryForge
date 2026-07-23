"""Use the configured LLM to select relevant local prompt skills."""

from __future__ import annotations

import json

from queryforge.workflow.node.base import Node
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.domain.skills import SkillManager, SkillRegistryError


class SkillSelectionNode(Node):
    name = "skill_selection"
    description = "Select question-relevant local skills before SQL generation"
    TARGET_NODE = "gen_sql"
    MAX_OPTIONAL_SKILLS = 3

    def __init__(
        self,
        llm: BaseModelProvider,
        skill_manager: SkillManager,
        selected_skills: list[str] | None = None,
    ) -> None:
        self.llm = llm
        self.skill_manager = skill_manager
        self.selected_skills = selected_skills

    def execute(self, context: Context) -> NodeResult:
        try:
            if self.selected_skills is not None:
                requested_names = self._scope_skill_names(context, self.selected_skills)
                selected = self.skill_manager.context_for_node(
                    self.TARGET_NODE, requested_names
                )
                self._apply_context(context, selected)
                context.skill_selection_mode = "manual"
                context.skill_selection_reason = (
                    "Skills were selected explicitly by CLI"
                    + (" and constrained by the selected subject." if requested_names != self.selected_skills else ".")
                )
                return self.success(
                    "Loaded manually selected skills: "
                    + (", ".join(selected.loaded_skill_names) or "none")
                )

            subject_skill_names = self._subject_skill_names(context)
            defaults = self.skill_manager.context_for_node(
                self.TARGET_NODE,
                subject_skill_names if subject_skill_names is not None else None,
            )
            catalog = self.skill_manager.context_for_node(self.TARGET_NODE, [])
            context.available_skills_context = catalog.available_skills
            try:
                payload = self.llm.generate_json(self._build_selection_prompt(context, catalog.available_skills))
                requested, reason = self._parse_selection(payload)
            except Exception as exc:
                self._apply_context(context, defaults)
                context.skill_selection_mode = "auto_fallback"
                context.skill_selection_reason = (
                    f"Automatic selection failed; enabled defaults were used: {exc}"
                )
                return self.success(
                    "Automatic skill selection failed; loaded enabled defaults"
                )

            selected_names = list(defaults.loaded_skill_names)
            selected_names.extend(
                name for name in requested if name not in selected_names
            )
            selected_names = self._scope_skill_names(context, selected_names)
            selected = self.skill_manager.context_for_node(
                self.TARGET_NODE, selected_names
            )
            self._apply_context(context, selected)
            context.skill_selection_mode = "auto"
            context.skill_selection_reason = reason
            return self.success(
                "Automatically loaded skills: "
                + (", ".join(selected.loaded_skill_names) or "none")
            )
        except SkillRegistryError as exc:
            return self.failure(str(exc))

    @staticmethod
    def _subject_skill_names(context: Context) -> list[str] | None:
        selection = context.subject_selection
        if (
            selection is None
            or selection.status != "selected"
            or selection.subject is None
            or not selection.subject.skills
        ):
            return None
        return selection.subject.skills

    @classmethod
    def _scope_skill_names(
        cls,
        context: Context,
        names: list[str],
    ) -> list[str]:
        allowed = cls._subject_skill_names(context)
        if allowed is None:
            return names
        allowed_set = set(allowed)
        return [name for name in names if name in allowed_set]

    def _parse_selection(self, payload: dict) -> tuple[list[str], str]:
        raw_names = payload.get("skills")
        if not isinstance(raw_names, list) or not all(
            isinstance(name, str) for name in raw_names
        ):
            raise ValueError("Skill selector response must contain a string list 'skills'")
        available_names = {
            skill.name
            for skill in self.skill_manager.registry.list_skills()
            if skill.applies_to(self.TARGET_NODE)
        }
        selected = []
        for name in raw_names:
            normalized = name.strip()
            if normalized in available_names and normalized not in selected:
                selected.append(normalized)
            if len(selected) >= self.MAX_OPTIONAL_SKILLS:
                break
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = "The LLM selected skills from the local catalog."
        return selected, reason.strip()

    @staticmethod
    def _apply_context(context: Context, skill_context) -> None:
        context.available_skills_context = skill_context.available_skills
        context.loaded_skills_context = skill_context.loaded_skills
        context.loaded_skill_names = list(skill_context.loaded_skill_names)

    @classmethod
    def _build_selection_prompt(cls, context: Context, catalog: str) -> str:
        schemas = [
            {
                "table_name": schema.table_name,
                "columns": [column.name for column in schema.columns[:40]],
            }
            for schema in context.relevant_tables[:30]
        ]
        return f"""Select local QueryForge skills for the upcoming SQL generation.

Choose at most {cls.MAX_OPTIONAL_SKILLS} skills that are directly relevant to the
question and supplied schema. Do not select a domain skill merely because it sounds
generally useful. The enabled default SQL skill is added separately, so it does not need
to be selected. Skills are prompt instructions only and cannot execute tools or code.

Return exactly one JSON object:
{{"skills": ["skill_name"], "reason": "brief selection reason"}}

User question:
{context.task.question}

Schema summary:
{json.dumps(schemas, ensure_ascii=False, indent=2)}

Applicable local skill catalog:
{catalog}
"""
