"""Gemini adapter using Google's OpenAI-compatible endpoint."""

from typing import Any

from queryforge import __version__
from queryforge.infrastructure.models.providers.openai_compatible import OpenAICompatibleProvider


class GeminiProvider(OpenAICompatibleProvider):
    def client_options(self) -> dict[str, Any]:
        return {
            "default_headers": {
                "x-goog-api-client": f"queryforge-oai/{__version__}",
            }
        }
