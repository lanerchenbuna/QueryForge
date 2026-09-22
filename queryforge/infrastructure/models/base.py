"""Provider-independent model interface and JSON response parsing."""

from __future__ import annotations

import ast
import json
import re
from abc import ABC, abstractmethod
from typing import Any

from queryforge.core.observability import ModelUsage, normalize_usage


Message = dict[str, str]


class ModelError(RuntimeError):
    """Raised when model setup or invocation fails."""


class ModelResponseError(ModelError):
    def __init__(self, message: str, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def _timeout_kwarg(timeout: float | None) -> dict[str, float]:
    """``{"timeout": ...}`` only when a deadline was actually declared.

    Subclasses and test doubles commonly override ``generate_with_messages`` with
    the historical two-argument signature. Passing ``timeout=None``
    unconditionally would break every one of them, so the keyword is added only
    when there is a real deadline to communicate.
    """

    return {} if timeout is None else {"timeout": float(timeout)}


class BaseModelProvider(ABC):
    """Small interface shared by every QueryForge provider adapter."""

    provider: str
    model: str
    #: Normalized usage of the most recent call (``None`` when the provider
    #: reported nothing). Adapters set this so observability can report measured
    #: tokens instead of estimating; an unset value must never become a fake 0.
    last_usage: ModelUsage | None = None

    def record_usage(self, raw_usage: Any) -> ModelUsage | None:
        """Normalize and store the usage payload of the latest provider response.

        Adapters call this with the raw provider payload (OpenAI ``usage``,
        Anthropic ``usage``, Gemini ``usage_metadata``, ...). A payload without
        token counts clears the previous value, so observation marks the call
        estimated instead of reusing a stale measured number.
        """

        usage = normalize_usage(raw_usage)
        self.last_usage = usage
        return usage

    def generate_text(
        self, prompt: str, timeout: float | None = None
    ) -> str:
        return self.generate_with_messages(
            [
                {"role": "system", "content": "You are a careful assistant."},
                {"role": "user", "content": prompt},
            ],
            **_timeout_kwarg(timeout),
        )

    def generate_json(
        self, prompt: str, timeout: float | None = None
    ) -> dict[str, Any]:
        raw_output = self.generate_with_messages(
            [
                {
                    "role": "system",
                    "content": "Return one valid JSON object and no other text.",
                },
                {"role": "user", "content": prompt},
            ],
            json_mode=True,
            **_timeout_kwarg(timeout),
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
        self,
        messages: list[Message],
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Generate text from normalized role/content messages.

        ``timeout`` is the remaining wall-clock budget for this call in seconds, or
        ``None`` when the run declared no deadline. Adapters that can bound a
        request should honour it; the parameter exists so the run's remaining
        deadline can actually reach the client instead of being recorded and then
        ignored (``BudgetLimits.model_deadline_ms`` used to have no consumer).
        """

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
