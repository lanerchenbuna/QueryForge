"""Lightweight lexical lookup for bundled, validated SQL examples."""

from __future__ import annotations

import csv
import re
from difflib import SequenceMatcher
from pathlib import Path

from queryforge.core.schemas.models import ReferenceExample


class ReferenceSqlTool:
    """Read an optional success_story.csv next to a SQLite database."""

    def __init__(self, database_path: str) -> None:
        self.reference_path = Path(database_path).expanduser().resolve().parent / "success_story.csv"

    def find_similar(
        self,
        question: str,
        limit: int = 3,
        minimum_similarity: float = 0.55,
    ) -> list[ReferenceExample]:
        if not self.reference_path.is_file() or limit <= 0:
            return []

        normalized_question = self._normalize(question)
        question_tokens = set(normalized_question.split())
        candidates: list[ReferenceExample] = []
        try:
            with self.reference_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    candidate_question = (row.get("question") or "").strip()
                    sql = (row.get("sql") or "").strip()
                    if not candidate_question or not sql:
                        continue
                    normalized_candidate = self._normalize(candidate_question)
                    candidate_tokens = set(normalized_candidate.split())
                    union = question_tokens | candidate_tokens
                    token_score = (
                        len(question_tokens & candidate_tokens) / len(union)
                        if union
                        else 0.0
                    )
                    sequence_score = SequenceMatcher(
                        None, normalized_question, normalized_candidate
                    ).ratio()
                    similarity = max(token_score, sequence_score)
                    if normalized_question == normalized_candidate:
                        similarity = 1.0
                    if similarity >= minimum_similarity:
                        candidates.append(
                            ReferenceExample(
                                question=candidate_question,
                                sql=sql,
                                similarity=round(similarity, 4),
                            )
                        )
        except (OSError, csv.Error):
            return []

        candidates.sort(key=lambda example: example.similarity, reverse=True)
        return candidates[:limit]

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", text.lower()))
