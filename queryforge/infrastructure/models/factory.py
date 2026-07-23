"""Create provider adapters from resolved QueryForge configuration."""

from __future__ import annotations

from typing import ClassVar

from queryforge.core.config import Config
from queryforge.infrastructure.models.base import BaseModelProvider, ModelError
from queryforge.infrastructure.models.providers import (
    ClaudeProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
)


class ModelFactory:
    ADAPTERS: ClassVar[dict[str, type[BaseModelProvider]]] = {
        "openai_compatible": OpenAICompatibleProvider,
        "claude": ClaudeProvider,
        "gemini": GeminiProvider,
    }
    _cached_key: ClassVar[tuple[str, str, str, str | None, str] | None] = None
    _cached_model: ClassVar[BaseModelProvider | None] = None

    @classmethod
    def create(cls, config: Config, use_cache: bool = True) -> BaseModelProvider:
        adapter = cls.ADAPTERS.get(config.llm_type)
        if adapter is None:
            supported = ", ".join(cls.ADAPTERS)
            raise ModelError(
                f"Unsupported model adapter type {config.llm_type!r}. "
                f"Supported: {supported}"
            )
        cache_key = (
            config.llm_provider,
            config.llm_type,
            config.llm_model,
            config.llm_base_url,
            config.llm_api_key or "",
        )
        if use_cache and cls._cached_key == cache_key and cls._cached_model:
            return cls._cached_model
        model = adapter(config)
        if use_cache:
            cls._cached_key = cache_key
            cls._cached_model = model
        return model

    @classmethod
    def clear_cache(cls) -> None:
        cls._cached_key = None
        cls._cached_model = None
