import tempfile
import unittest
from pathlib import Path

from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.core.schemas.models import Context, SqlTask, TableColumn, TableSchema
from queryforge.domain.skills import SkillManager, SkillRegistry, SkillRegistryError


class SkillsSystemTest(unittest.TestCase):
    def test_bundled_registry_covers_data_engineering_lifecycle(self) -> None:
        skills = SkillRegistry().list_skills()
        names = {skill.name for skill in skills}
        self.assertEqual(len(skills), 10)
        self.assertTrue(
            {
                "requirements_and_contracts",
                "ingestion_and_cdc",
                "data_modeling",
                "transformation_and_orchestration",
                "sql_best_practices",
                "business_rules",
                "data_quality_testing",
                "performance_optimization",
                "observability_and_operations",
                "governance_and_security",
            }.issubset(names)
        )

    def test_default_loads_only_enabled_applicable_skills(self) -> None:
        context = SkillManager().context_for_node("gen_sql")
        self.assertEqual(context.loaded_skill_names, ("sql_best_practices",))
        self.assertIn("SQL Best Practices", context.loaded_skills)
        self.assertNotIn("Example Commerce Business Rules", context.loaded_skills)
        self.assertIn("business_rules", context.available_skills)

    def test_manual_selection_can_load_disabled_skill(self) -> None:
        context = SkillManager().context_for_node(
            "gen_sql", ["business_rules", "sql_best_practices"]
        )
        self.assertEqual(
            context.loaded_skill_names,
            ("sql_best_practices", "business_rules"),
        )
        self.assertIn("cancelled orders", context.loaded_skills)

    def test_manual_selection_is_filtered_by_node(self) -> None:
        context = SkillManager().context_for_node(
            "gen_sql", ["observability_and_operations"]
        )
        self.assertEqual(context.loaded_skill_names, ())
        self.assertNotIn("observability_and_operations", context.available_skills)

    def test_unknown_manual_skill_has_friendly_error(self) -> None:
        with self.assertRaisesRegex(SkillRegistryError, "Unknown skill"):
            SkillManager().context_for_node("gen_sql", ["does_not_exist"])

    def test_missing_skill_markdown_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_dir = Path(directory) / "broken"
            skill_dir.mkdir()
            (skill_dir / "skill.yml").write_text(
                """name: broken
description: Missing instructions
allowed_nodes: [gen_sql]
enabled: true
priority: 1
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SkillRegistryError, "missing SKILL.md"):
                SkillRegistry(directory).list_skills()

    def test_disabled_skill_is_not_injected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_dir = Path(directory) / "disabled_example"
            skill_dir.mkdir()
            (skill_dir / "skill.yml").write_text(
                """name: disabled_example
description: Disabled test skill
allowed_nodes: [gen_sql]
enabled: false
priority: 10
""",
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                "THIS_DISABLED_TEXT_MUST_NOT_LOAD", encoding="utf-8"
            )
            skill_context = SkillManager(
                SkillRegistry(directory)
            ).context_for_node("gen_sql")
        self.assertEqual(skill_context.loaded_skill_names, ())
        self.assertNotIn("THIS_DISABLED_TEXT_MUST_NOT_LOAD", skill_context.loaded_skills)

    def test_gen_sql_prompt_contains_skill_catalog_and_loaded_instructions(self) -> None:
        skill_context = SkillManager().context_for_node("gen_sql")
        context = Context(
            task=SqlTask(question="How many rows?", database_path="sample.sqlite"),
            relevant_tables=[
                TableSchema(
                    table_name="example",
                    columns=[TableColumn(name="id", data_type="INTEGER")],
                )
            ],
            available_skills_context=skill_context.available_skills,
            loaded_skills_context=skill_context.loaded_skills,
            loaded_skill_names=list(skill_context.loaded_skill_names),
        )
        prompt = GenSqlNode._build_prompt(context)
        self.assertIn('<available_skills node="gen_sql">', prompt)
        self.assertIn('<loaded_skills node="gen_sql">', prompt)
        self.assertIn("never use `SELECT *`", prompt)
        self.assertIn("Skills are not database tables", prompt)


if __name__ == "__main__":
    unittest.main()
