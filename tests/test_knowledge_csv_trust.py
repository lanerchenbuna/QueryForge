"""E-05: the CSV knowledge path must not mint trusted material unconditionally.

``KnowledgeBaseBuilder._csv_documents`` stamped every row ``human_reviewed`` with
``review_status: reviewed`` regardless of whether anyone had reviewed it. Any CSV
with ``question``/``sql`` columns therefore entered the retrieval index as trusted
few-shot material, which inverts the three-tier verification model in
``domain/knowledge/governance.py`` where only an explicit human review counts as
trusted. ``SQLHistoryStore.import_success_stories`` already gated trust on a named
reviewer; this path did not.

These tests exercise the real document builder, not a double.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from queryforge.infrastructure.storage.knowledge_base import KnowledgeBaseBuilder


def _write_csv(path: Path, header: str, rows: list[str]) -> Path:
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


class CsvKnowledgeTrustTest(unittest.TestCase):
    def _documents(self, header: str, rows: list[str]) -> list:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_csv(Path(directory) / "source.csv", header, rows)
            return KnowledgeBaseBuilder._csv_documents(path)

    def test_rows_without_a_reviewer_are_not_marked_human_reviewed(self):
        documents = self._documents(
            "question,sql,evidence",
            [
                'How many anime are there?,"SELECT COUNT(*) AS n FROM dim_anime",checked',
                'How many studios?,"SELECT COUNT(*) AS n FROM dim_studio",checked',
            ],
        )
        self.assertEqual(len(documents), 2)
        for document in documents:
            metadata = document.metadata
            with self.subTest(question=metadata["question"]):
                # Execution success is not business correctness.
                self.assertEqual(
                    metadata["verification_level"], "execution_success"
                )
                self.assertEqual(metadata["review_status"], "draft")
                self.assertIsNone(metadata["reviewer"])

    def test_a_named_reviewer_earns_human_reviewed(self):
        documents = self._documents(
            "question,sql,reviewer",
            [
                'How many anime are there?,"SELECT COUNT(*) AS n FROM dim_anime",data-platform',
            ],
        )
        self.assertEqual(len(documents), 1)
        metadata = documents[0].metadata
        self.assertEqual(metadata["verification_level"], "human_reviewed")
        self.assertEqual(metadata["review_status"], "reviewed")
        self.assertEqual(metadata["reviewer"], "data-platform")
        self.assertEqual(metadata["owner"], "data-platform")

    def test_reviewed_by_is_accepted_as_an_alias(self):
        documents = self._documents(
            "question,sql,reviewed_by",
            ['How many genres?,"SELECT COUNT(*) AS n FROM dim_genre",analytics-lead'],
        )
        metadata = documents[0].metadata
        self.assertEqual(metadata["verification_level"], "human_reviewed")
        self.assertEqual(metadata["reviewer"], "analytics-lead")

    def test_a_blank_reviewer_does_not_grant_trust(self):
        documents = self._documents(
            "question,sql,reviewer,owner",
            ['How many users?,"SELECT COUNT(*) AS n FROM dim_user",,business'],
        )
        metadata = documents[0].metadata
        self.assertEqual(metadata["verification_level"], "execution_success")
        self.assertEqual(metadata["review_status"], "draft")
        # Falls back to the declared owner rather than losing it.
        self.assertEqual(metadata["owner"], "business")

    def test_checked_in_sample_is_untrusted_until_a_reviewer_is_added(self):
        """The shipped sample CSV has no reviewer column, so it must not be trusted."""
        sample = (
            Path(__file__).resolve().parents[1]
            / "sample_data"
            / "anime_streaming"
            / "success_story.csv"
        )
        self.assertTrue(sample.is_file(), "sample CSV is part of the repo")
        documents = KnowledgeBaseBuilder._csv_documents(sample)
        self.assertTrue(documents)
        for document in documents:
            self.assertEqual(
                document.metadata["verification_level"], "execution_success"
            )
            self.assertEqual(document.metadata["review_status"], "draft")


if __name__ == "__main__":
    unittest.main()
