"""Provider-independent model interface and JSON response parsing."""

from __future__ import annotations

import ast
import json
import re
from abc import ABC, abstractmethod
from typing import Any


Message = dict[str, str]


class ModelError(RuntimeError):
    """Raised when model setup or invocation fails."""


class ModelResponseError(ModelError):
    def __init__(self, message: str, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class BaseModelProvider(ABC):
    """Small interface shared by every QueryForge provider adapter."""

    provider: str
    model: str

    def generate_text(self, prompt: str) -> str:
        return self.generate_with_messages(
            [
                {"role": "system", "content": "You are a careful assistant."},
                {"role": "user", "content": prompt},
            ]
        )

    def generate_json(self, prompt: str) -> dict[str, Any]:
        raw_output = self.generate_with_messages(
            [
                {
                    "role": "system",
                    "content": "Return one valid JSON object and no other text.",
                },
                {"role": "user", "content": prompt},
            ],
            json_mode=True,
        )
        candidate = self._extract_json_object(raw_output)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            try:
                value = ast.literal_eval(candidate)
            except (SyntaxError, ValueError) as literal_exc:
                raise ModelResponseError(
                    f"Model response is not valid JSON: {exc}", raw_output
                ) from literal_exc
        if not isinstance(value, dict):
            raise ModelResponseError(
                "Model JSON response must be an object", raw_output
            )
        return value

    @abstractmethod
    def generate_with_messages(
        self, messages: list[Message], json_mode: bool = False
    ) -> str:
        """Generate text from normalized role/content messages."""

    @staticmethod
    def _extract_json_object(raw_output: str) -> str:
        text = raw_output.strip()
        fenced = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.IGNORECASE | re.DOTALL
        )
        if fenced:
            return fenced.group(1)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start : end + 1]
        return text
