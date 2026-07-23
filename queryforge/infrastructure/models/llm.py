"""Backward-compatible imports for the original QueryForge LLM module."""

from queryforge.infrastructure.models.base import ModelError, ModelResponseError
from queryforge.infrastructure.models.providers.openai_compatible import OpenAICompatibleProvider

LLM = OpenAICompatibleProvider
LLMError = ModelError
LLMResponseError = ModelResponseError

__all__ = ["LLM", "LLMError", "LLMResponseError"]
