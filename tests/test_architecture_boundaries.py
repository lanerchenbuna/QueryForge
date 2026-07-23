import unittest
from pathlib import Path

from queryforge.application import AgentOptions, AgentService
from queryforge.application.direct_tasks import DirectTaskExecutor
from queryforge.application.resources import ResourceService
from queryforge.data_assets.sources import read_source
from queryforge.data_assets.transforms import normalize_identifier
from queryforge.domain.semantic import SemanticModel
from queryforge.domain.semantic.schemas import ContractQualityRule
from queryforge.orchestration.gates import QualityGateEvaluator


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ArchitectureBoundaryTest(unittest.TestCase):
    def test_stable_application_exports_remain_available(self):
        self.assertTrue(issubclass(AgentService, ResourceService))
        self.assertTrue(callable(AgentOptions))
        self.assertTrue(callable(DirectTaskExecutor))

    def test_semantic_schemas_are_separate_from_algorithms(self):
        self.assertTrue(callable(SemanticModel))
        self.assertTrue(callable(ContractQualityRule))
        self.assertLess(
            len(
                (
                    PROJECT_ROOT
                    / "queryforge/domain/semantic/model.py"
                ).read_text(encoding="utf-8").splitlines()
            ),
            800,
        )

    def test_data_asset_sources_and_transforms_are_separate(self):
        self.assertTrue(callable(read_source))
        self.assertEqual(normalize_identifier("Order ID"), "order_id")

    def test_orchestration_gate_has_its_own_component(self):
        self.assertTrue(callable(QualityGateEvaluator))


if __name__ == "__main__":
    unittest.main()
