import unittest

from queryforge.workflow.node.skill_selection_node import SkillSelectionNode
from queryforge.core.schemas.models import Context, SqlTask, TableColumn, TableSchema
from queryforge.domain.skills import SkillManager


def sample_context(question: str = "Show completed order revenue") -> Context:
    return Context(
        task=SqlTask(question=question, database_path="orders.sqlite"),
        relevant_tables=[
            TableSchema(
                table_name="orders",
                columns=[
                    TableColumn(name="order_id"),
                    TableColumn(name="net_amount"),
                    TableColumn(name="status"),
                ],
            )
        ],
    )


class StaticSelectorLLM:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0

    def generate_json(self, prompt: str):
        self.calls += 1
        self.prompt = prompt
        if self.error:
            raise self.error
        return self.payload


class SkillSelectionNodeTest(unittest.TestCase):
    def test_llm_selects_domain_skill_and_keeps_enabled_default(self) -> None:
        llm = StaticSelectorLLM(
            {
                "skills": ["business_rules", "data_modeling"],
                "reason": "The question concerns order revenue and grain.",
            }
        )
        context = sample_context()
        result = SkillSelectionNode(llm, SkillManager()).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.skill_selection_mode, "auto")
        self.assertEqual(
            context.loaded_skill_names,
            ["sql_best_practices", "business_rules", "data_modeling"],
        )
        self.assertIn("Example Commerce Business Rules", context.loaded_skills_context)
        self.assertIn("order revenue", context.skill_selection_reason)
        self.assertIn("Schema summary", llm.prompt)

    def test_hallucinated_skill_is_ignored(self) -> None:
        llm = StaticSelectorLLM(
            {"skills": ["remote_code_execution"], "reason": "invalid choice"}
        )
        context = sample_context()
        result = SkillSelectionNode(llm, SkillManager()).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.loaded_skill_names, ["sql_best_practices"])

    def test_selection_failure_falls_back_to_enabled_defaults(self) -> None:
        llm = StaticSelectorLLM(error=RuntimeError("selector unavailable"))
        context = sample_context()
        result = SkillSelectionNode(llm, SkillManager()).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.skill_selection_mode, "auto_fallback")
        self.assertEqual(context.loaded_skill_names, ["sql_best_practices"])
        self.assertIn("selector unavailable", context.skill_selection_reason)

    def test_manual_selection_skips_llm_router(self) -> None:
        llm = StaticSelectorLLM(error=AssertionError("LLM must not be called"))
        context = sample_context()
        result = SkillSelectionNode(
            llm,
            SkillManager(),
            selected_skills=["business_rules"],
        ).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(context.skill_selection_mode, "manual")
        self.assertEqual(context.loaded_skill_names, ["business_rules"])

    def test_manual_empty_list_disables_all_skills(self) -> None:
        llm = StaticSelectorLLM(error=AssertionError("LLM must not be called"))
        context = sample_context()
        result = SkillSelectionNode(
            llm, SkillManager(), selected_skills=[]
        ).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.loaded_skill_names, [])


if __name__ == "__main__":
    unittest.main()
