"""Persistence adapters for QueryForge."""

from queryforge.infrastructure.storage.sql_history_store import (
    CURATED_SOURCES,
    DEFAULT_HISTORY_DB_PATH,
    DEFAULT_SEARCH_WINDOW,
    HistoryEntry,
    HistorySearchResult,
    ImportSummary,
    SQLHistoryError,
    SQLHistoryStore,
)
from queryforge.infrastructure.storage.knowledge_base import (
    DEFAULT_HOLDOUT_TASKS_PATH,
    KnowledgeBaseBuilder,
    SQL_SOURCE_TYPES,
    load_holdout_registry,
)
from queryforge.infrastructure.storage.vector_store import (
    DEFAULT_VECTOR_KB_PATH,
    document_content_hash,
    document_matches_filters,
    effective_filters,
    LanceDBVectorStore,
    OpenAIEmbeddingProvider,
    VectorDocument,
    VectorSearchResult,
    VectorStore,
    VectorStoreError,
)

__all__ = [
    "CURATED_SOURCES",
    "DEFAULT_HISTORY_DB_PATH",
    "DEFAULT_HOLDOUT_TASKS_PATH",
    "DEFAULT_SEARCH_WINDOW",
    "HistorySearchResult",
    "document_content_hash",
    "document_matches_filters",
    "effective_filters",
    "load_holdout_registry",
    "HistoryEntry",
    "ImportSummary",
    "SQLHistoryError",
    "SQLHistoryStore",
    "DEFAULT_VECTOR_KB_PATH",
    "KnowledgeBaseBuilder",
    "LanceDBVectorStore",
    "OpenAIEmbeddingProvider",
    "SQL_SOURCE_TYPES",
    "VectorDocument",
    "VectorSearchResult",
    "VectorStore",
    "VectorStoreError",
]
