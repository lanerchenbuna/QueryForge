import unittest
from types import SimpleNamespace
from unittest.mock import patch

from queryforge import __version__
from queryforge.core.config import Config
from queryforge.infrastructure.models.base import BaseModelProvider, ModelError
from queryforge.infrastructure.models.factory import ModelFactory
from queryforge.infrastructure.models.llm import LLM
from queryforge.infrastructure.models.providers import (
    ClaudeProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
)


class LLMParsingTest(unittest.TestCase):
    def test_extracts_json_from_markdown_fence(self) -> None:
        raw = 'Here is the result:\n```json\n{"sql": "SELECT 1"}\n```'
        self.assertEqual(LLM._extract_json_object(raw), '{"sql": "SELECT 1"}')

    def test_extracts_json_with_surrounding_text(self) -> None:
        raw = 'Result: {"sql": "SELECT 1"} done.'
        self.assertEqual(LLM._extract_json_object(raw), '{"sql": "SELECT 1"}')

    def test_generate_json_accepts_single_quoted_dictionary(self) -> None:
        class StubProvider(BaseModelProvider):
            def generate_with_messages(self, messages, json_mode=False):
                return "prefix {'sql': 'SELECT 1'} suffix"

        llm = StubProvider()
        self.assertEqual(llm.generate_json("test"), {"sql": "SELECT 1"})

    def test_provider_configures_openai_compatible_client(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: response)
            )
        )
        config = Config(
            llm_provider="qwen",
            llm_api_key="qwen-key",
            llm_model="qwen-plus",
            llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            database_path="sample.sqlite",
            api_key_env_names=("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        )
        with patch(
            "queryforge.infrastructure.models.providers.openai_compatible.OpenAI",
            return_value=client,
        ) as factory:
            llm = LLM(config)
            self.assertEqual(llm.generate_json("Return JSON."), {"ok": True})
        factory.assert_called_once_with(
            api_key="qwen-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    def test_factory_selects_provider_adapter(self) -> None:
        cases = (
            ("openai_compatible", OpenAICompatibleProvider),
            ("claude", ClaudeProvider),
            ("gemini", GeminiProvider),
        )
        for provider_type, expected_type in cases:
            with self.subTest(provider_type=provider_type), patch(
                "queryforge.infrastructure.models.providers.openai_compatible.OpenAI"
            ):
                config = Config(
                    llm_provider=provider_type,
                    llm_type=provider_type,
                    llm_api_key="key",
                    llm_model="test-model",
                    llm_base_url="https://example.invalid/v1",
                    database_path="sample.sqlite",
                )
                model = ModelFactory.create(config, use_cache=False)
            self.assertIsInstance(model, expected_type)

    def test_missing_key_error_names_provider_environment_variable(self) -> None:
        config = Config(
            llm_provider="claude",
            llm_type="claude",
            llm_api_key=None,
            llm_model="claude-test",
            llm_base_url="https://api.anthropic.com/v1/",
            database_path="sample.sqlite",
            api_key_env_names=("ANTHROPIC_API_KEY",),
        )
        with self.assertRaisesRegex(ModelError, "ANTHROPIC_API_KEY"):
            ModelFactory.create(config, use_cache=False)

    def test_claude_omits_unsupported_response_format(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
        )
        create = unittest.mock.Mock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        config = Config(
            llm_provider="claude",
            llm_type="claude",
            llm_api_key="key",
            llm_model="claude-test",
            llm_base_url="https://api.anthropic.com/v1/",
            database_path="sample.sqlite",
        )
        with patch(
            "queryforge.infrastructure.models.providers.openai_compatible.OpenAI",
            return_value=client,
        ):
            ClaudeProvider(config).generate_json("Return JSON")
        self.assertNotIn("response_format", create.call_args.kwargs)

    def test_gemini_adds_client_identification_header(self) -> None:
        config = Config(
            llm_provider="gemini",
            llm_type="gemini",
            llm_api_key="key",
            llm_model="gemini-test",
            llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            database_path="sample.sqlite",
        )
        with patch(
            "queryforge.infrastructure.models.providers.openai_compatible.OpenAI"
        ) as factory:
            GeminiProvider(config)
        self.assertEqual(
            factory.call_args.kwargs["default_headers"]["x-goog-api-client"],
            f"queryforge-oai/{__version__}",
        )


if __name__ == "__main__":
    unittest.main()
