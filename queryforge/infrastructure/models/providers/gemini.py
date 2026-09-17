"""Gemini adapter using Google's OpenAI-compatible endpoint."""

from typing import Any

from queryforge import __version__
from queryforge.infrastructure.models.providers.openai_compatible import OpenAICompatibleProvider


class GeminiProvider(OpenAICompatibleProvider):
    # Step 14: Google's OpenAI-compatible endpoint reports usage in the OpenAI
    # shape (``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``), and
    # ``usageMetadata``-style camelCase keys are mapped by ``normalize_usage``
    # too. Missing usage becomes ``estimated=True`` upstream, never a fake zero.
    def client_options(self) -> dict[str, Any]:
        return {
            "default_headers": {
                "x-goog-api-client": f"queryforge-oai/{__version__}",
            }
        }
