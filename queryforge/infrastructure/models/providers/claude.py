"""Claude adapter using Anthropic's OpenAI SDK compatibility endpoint."""

from queryforge.infrastructure.models.providers.openai_compatible import OpenAICompatibleProvider


class ClaudeProvider(OpenAICompatibleProvider):
    # Anthropic's compatibility layer currently ignores response_format.
    # BaseModelProvider still asks for JSON explicitly and parses it locally.
    supports_response_format = False
