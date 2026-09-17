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
from queryforge.domain.semantic.schema_retrieval import (
    QuestionTerms,
    SchemaRetrievalResult,
    SchemaRetriever,
    TableRetrievalSelection,
)
from queryforge.domain.semantic.sql_validator import (
    QuerySpec,
    QuerySpecCompiler,
    SemanticSQLValidator,
    SemanticValidationResult,
    normalize_sql_signature,
)

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
    "QuestionTerms",
    "SchemaRetrievalResult",
    "SchemaRetriever",
    "TableRetrievalSelection",
    "Subject",
    "SubjectError",
    "SubjectSelection",
    "SubjectTree",
    "SubjectTreeLoader",
    "SemanticSQLValidator",
    "SemanticValidationResult",
    "QuerySpec",
    "QuerySpecCompiler",
    "normalize_sql_signature",
]
