"""Optional, declarative semantic model support."""

from queryforge.domain.semantic.schemas import (
    CardinalityContract,
    ContractQualityRule,
    OperationalContract,
    ResolvedJoinPath,
    ResolvedJoinStep,
    SemanticDimension,
    SemanticEntity,
    SemanticMatch,
    SemanticMetric,
    MetricMatch,
    SemanticModel,
    SemanticModelContext,
    SemanticRelationship,
    SemanticJoinPath,
)
from queryforge.domain.semantic.model import SemanticModelError, SemanticModelLoader
from queryforge.domain.semantic.contract_validator import (
    ContractCheck,
    ContractValidationReport,
    SemanticContractValidator,
)
from queryforge.domain.semantic.subject import (
    Subject,
    SubjectError,
    SubjectSelection,
    SubjectTree,
    SubjectTreeLoader,
)
from queryforge.domain.semantic.discovery import discover_semantic_model

__all__ = [
    "CardinalityContract",
    "ContractCheck",
    "ContractQualityRule",
    "ContractValidationReport",
    "OperationalContract",
    "ResolvedJoinPath",
    "ResolvedJoinStep",
    "SemanticDimension",
    "SemanticEntity",
    "SemanticMatch",
    "SemanticMetric",
    "MetricMatch",
    "SemanticModel",
    "SemanticModelContext",
    "SemanticModelError",
    "SemanticModelLoader",
    "SemanticRelationship",
    "SemanticJoinPath",
    "SemanticContractValidator",
    "discover_semantic_model",
    "Subject",
    "SubjectError",
    "SubjectSelection",
    "SubjectTree",
    "SubjectTreeLoader",
]
