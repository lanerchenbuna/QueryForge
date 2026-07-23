"""OpenAI Chat Completions adapter used by compatible providers."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from queryforge.core.config import Config
from queryforge.infrastructure.models.base import BaseModelProvider, Message, ModelError, ModelResponseError


class OpenAICompatibleProvider(BaseModelProvider):
    supports_response_format = True

    def __init__(self, config: Config) -> None:
        try:
            api_key = config.require_api_key()
        except ValueError as exc:
            raise ModelError(str(exc)) from exc
        kwargs: dict[str, Any] = {"api_key": api_key}
        if config.llm_base_url:
            kwargs["base_url"] = config.llm_base_url
        kwargs.update(self.client_options())
        self._client = OpenAI(**kwargs)
        self.provider = config.llm_provider
        self.model = config.llm_model

    def client_options(self) -> dict[str, Any]:
        return {}

    def generate_with_messages(
        self, messages: list[Message], json_mode: bool = False
    ) -> str:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
        }
        if json_mode and self.supports_response_format:
            request["response_format"] = {"type": "json_object"}
        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            raise ModelError(
                f"Model request failed for provider={self.provider}, "
                f"model={self.model}: {exc}"
            ) from exc
        content = response.choices[0].message.content
        if not content:
            raise ModelResponseError("Model returned an empty response", "")
        return content
