"""Declarative subject scopes for bounded analytical query context."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class SubjectError(ValueError):
    """Raised when a subject tree cannot be loaded or used safely."""


class Subject(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    knowledge_sources: list[str] = Field(default_factory=list)
    default_time_field: str | None = None
    default_grain: str | None = None
    priority: int = 0


class SubjectTree(BaseModel):
    version: str = "1.0"
    subjects: list[Subject] = Field(min_length=1)
    default_subject: str | None = None

    def subject_by_id(self, subject_id: str) -> Subject | None:
        return next((item for item in self.subjects if item.id == subject_id), None)


class SubjectSelection(BaseModel):
    enabled: bool = False
    status: Literal["disabled", "selected", "fallback_all"] = "disabled"
    subject: Subject | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    fallback_reason: str | None = None


class SubjectTreeLoader:
    """Load a compact YAML subject tree and select a scope without an LLM."""

    @classmethod
    def load(cls, path: str | Path) -> SubjectTree:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise SubjectError(f"Subject tree does not exist: {source}")
        try:
            payload = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SubjectError(f"Could not read subject tree {source}: {exc}") from exc
        try:
            tree = SubjectTree.model_validate(payload)
        except Exception as exc:
            raise SubjectError(f"Invalid subject tree {source}: {exc}") from exc
        ids = [subject.id for subject in tree.subjects]
        if len(ids) != len(set(ids)):
            raise SubjectError(f"Subject tree has duplicate subject IDs: {source}")
        if tree.default_subject and tree.subject_by_id(tree.default_subject) is None:
            raise SubjectError(
                f"default_subject {tree.default_subject!r} is not defined in {source}"
            )
        return tree

    @classmethod
    def select(
        cls,
        tree: SubjectTree,
        question: str,
        *,
        requested_subject: str | None = None,
        default_subject: str | None = None,
    ) -> SubjectSelection:
        if requested_subject:
            subject = tree.subject_by_id(requested_subject)
            if subject is None:
                raise SubjectError(
                    f"Unknown subject {requested_subject!r}. Available: "
                    + ", ".join(item.id for item in tree.subjects)
                )
            return SubjectSelection(
                enabled=True,
                status="selected",
                subject=subject,
                candidate_ids=[subject.id],
                reason="Subject was selected explicitly.",
            )

        scored = [
            (cls._score(question, subject), subject)
            for subject in tree.subjects
        ]
        matched = [(score, subject) for score, subject in scored if score > 0]
        if matched:
            matched.sort(key=lambda item: (-item[0], -item[1].priority, item[1].id))
            score, subject = matched[0]
            return SubjectSelection(
                enabled=True,
                status="selected",
                subject=subject,
                candidate_ids=[item.id for _, item in matched],
                reason=f"Matched subject keywords and metadata (score={score}).",
            )

        selected_default = default_subject or tree.default_subject
        if selected_default:
            subject = tree.subject_by_id(selected_default)
            if subject is not None:
                return SubjectSelection(
                    enabled=True,
                    status="selected",
                    subject=subject,
                    candidate_ids=[subject.id],
                    reason="No subject keywords matched; selected the default subject.",
                )
        return SubjectSelection(
            enabled=True,
            status="fallback_all",
            reason="No subject matched and no default subject is configured.",
            fallback_reason="unmatched_subject",
        )

    @staticmethod
    def _score(question: str, subject: Subject) -> int:
        normalized = question.lower()
        score = 0
        terms = [
            (subject.name, 3),
            *[(term, 3) for term in subject.synonyms],
            *[(term, 2) for term in subject.metrics],
            *[(term, 1) for term in subject.entities],
            *[(term, 1) for term in subject.tables],
        ]
        for term, weight in terms:
            normalized_term = term.strip().lower()
            if not normalized_term:
                continue
            if re.search(rf"(?<!\w){re.escape(normalized_term)}(?!\w)", normalized):
                score += weight
        return score
