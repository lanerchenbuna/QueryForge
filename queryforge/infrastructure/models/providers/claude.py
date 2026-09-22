"""Claude adapter using Anthropic's OpenAI SDK compatibility endpoint."""

from queryforge.infrastructure.models.providers.openai_compatible import OpenAICompatibleProvider


class ClaudeProvider(OpenAICompatibleProvider):
    # Anthropic's compatibility layer currently ignores response_format.
    # BaseModelProvider still asks for JSON explicitly and parses it locally.
    supports_response_format = False
    # Step 14: Anthropic's compatibility layer reports usage as
    # ``input_tokens`` / ``output_tokens``; ``normalize_usage`` maps those names,
    # so no extra adapter code is needed. A response that omits usage is
    # reported as ``estimated=True`` upstream rather than as zero tokens.
