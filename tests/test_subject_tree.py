"""Offline coverage for declarative subject scope selection and fallback."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode
from queryforge.workflow.node.skill_selection_node import SkillSelectionNode
from queryforge.workflow.node.subject_selection_node import SubjectSelectionNode
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, HistoryMatch, ReferenceExample, SqlTask
from queryforge.domain.skills import SkillManager
from queryforge.infrastructure.tools.database_tool import DatabaseTool


class SubjectSkillLLM:
    def generate_json(self, prompt: str) -> dict:
        return {
            "skills": ["business_rules", "data_quality_testing"],
            "reason": "Engagement rules are relevant.",
        }


class SubjectTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "warehouse.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE fact_watch_session (session_id INTEGER, watch_seconds REAL)")
        connection.execute("CREATE TABLE dim_anime (anime_id INTEGER, name TEXT)")
        connection.execute("CREATE TABLE hr_employees (employee_id INTEGER, name TEXT)")
        connection.commit()
        connection.close()
        self.subject_tree = self.root / "subjects.yml"
        self.subject_tree.write_text(
            """version: "1.0"
default_subject: engagement
subjects:
  - id: engagement
    name: Engagement
    description: Playback and audience analytics
    synonyms: [watch, playback]
    tables: [fact_watch_session, dim_anime]
    metrics: [watch_hours]
    skills: [business_rules]
  - id: inventory
    name: Inventory
    description: Warehouse inventory analytics
    synonyms: [inventory, stock]
    tables: [fact_inventory]
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def context(self, question: str) -> Context:
        return Context(
            task=SqlTask(question=question, database_path=str(self.database))
        )

    def schema_node(self) -> SchemaLinkingNode:
        return SchemaLinkingNode(
            DatabaseTool(SQLiteConnector(str(self.database))),
        )

    def test_keyword_selection_scopes_loaded_schema(self):
        context = self.context("Show watch hours by anime")
        selection = SubjectSelectionNode(
            enabled=True,
            subject_tree_path=str(self.subject_tree),
        ).execute(context)
        self.assertTrue(selection.success)
        self.assertEqual(context.subject_selection.subject.id, "engagement")
        result = self.schema_node().execute(context)
        self.assertTrue(result.success)
        self.assertEqual(
            [schema.table_name for schema in context.relevant_tables],
            ["dim_anime", "fact_watch_session"],
        )

    def test_manual_subject_selection_overrides_keyword_match(self):
        context = self.context("Show watch hours by anime")
        result = SubjectSelectionNode(
            enabled=True,
            subject_tree_path=str(self.subject_tree),
            requested_subject="inventory",
        ).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.subject_selection.subject.id, "inventory")
        self.assertEqual(context.subject_selection.reason, "Subject was selected explicitly.")

    def test_missing_subject_tables_fall_back_to_full_schema(self):
        context = self.context("Show current inventory")
        self.assertTrue(
            SubjectSelectionNode(
                enabled=True,
                subject_tree_path=str(self.subject_tree),
            ).execute(context).success
        )
        result = self.schema_node().execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.subject_selection.status, "fallback_all")
        self.assertEqual(
            {schema.table_name for schema in context.relevant_tables},
            {"fact_watch_session", "dim_anime", "hr_employees"},
        )

    def test_subject_skills_constrain_automatic_skill_selection(self):
        context = self.context("Show watch hours")
        self.assertTrue(
            SubjectSelectionNode(
                enabled=True,
                subject_tree_path=str(self.subject_tree),
            ).execute(context).success
        )
        context.relevant_tables = []
        result = SkillSelectionNode(
            SubjectSkillLLM(),
            SkillManager(),
        ).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.loaded_skill_names, ["business_rules"])

    def test_subject_scope_filters_history_and_reference_sql(self):
        context = self.context("Show watch hours")
        context.history_matches = [
            HistoryMatch(
                id=1,
                question="Watch hours",
                sql="SELECT SUM(watch_seconds) FROM fact_watch_session",
                tables_used=["fact_watch_session"],
                similarity=0.9,
                created_at="2026-01-01T00:00:00Z",
            ),
            HistoryMatch(
                id=2,
                question="Employees",
                sql="SELECT name FROM hr_employees",
                tables_used=["hr_employees"],
                similarity=0.8,
                created_at="2026-01-01T00:00:00Z",
            ),
        ]
        context.reference_examples = [
            ReferenceExample(
                question="Watch hours",
                sql="SELECT SUM(watch_seconds) FROM fact_watch_session",
                similarity=0.9,
            ),
            ReferenceExample(
                question="Employees",
                sql="SELECT name FROM hr_employees",
                similarity=0.8,
            ),
        ]
        self.assertTrue(
            SubjectSelectionNode(
                enabled=True,
                subject_tree_path=str(self.subject_tree),
            ).execute(context).success
        )
        self.assertTrue(self.schema_node().execute(context).success)
        self.assertEqual([match.id for match in context.history_matches], [1])
        self.assertEqual(len(context.reference_examples), 1)

    def test_disabled_subject_tree_leaves_schema_unscoped(self):
        context = self.context("Show watch hours")
        self.assertTrue(
            SubjectSelectionNode(
                enabled=False,
                subject_tree_path=str(self.subject_tree),
            ).execute(context).success
        )
        self.assertIsNone(context.subject_selection)
        self.assertTrue(self.schema_node().execute(context).success)
        self.assertEqual(len(context.relevant_tables), 3)


if __name__ == "__main__":
    unittest.main()
