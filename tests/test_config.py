import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.core.config import list_model_definitions, load_config


class ProviderConfigTest(unittest.TestCase):
    def load(self, values: dict[str, str]):
        with patch("queryforge.core.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            return load_config()

    def test_qwen_uses_dashscope_key_fallback(self) -> None:
        config = self.load(
            {"LLM_PROVIDER": "qwen", "DASHSCOPE_API_KEY": "qwen-key"}
        )
        self.assertEqual(config.llm_provider, "qwen")
        self.assertEqual(config.llm_api_key, "qwen-key")
        self.assertEqual(config.llm_model, "qwen-plus")
        self.assertIn("compatible-mode/v1", config.llm_base_url or "")

    def test_deepseek_defaults(self) -> None:
        config = self.load(
            {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "ds-key"}
        )
        self.assertEqual(config.llm_model, "deepseek-v4-flash")
        self.assertEqual(config.llm_base_url, "https://api.deepseek.com")

    def test_glm_alias_and_zai_key(self) -> None:
        config = self.load({"LLM_PROVIDER": "zai", "ZAI_API_KEY": "glm-key"})
        self.assertEqual(config.llm_provider, "glm")
        self.assertEqual(config.llm_api_key, "glm-key")
        self.assertEqual(config.llm_model, "glm-5.1")

    def test_claude_and_gemini_use_dedicated_adapter_types(self) -> None:
        claude = self.load(
            {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "claude-key"}
        )
        gemini = self.load(
            {"LLM_PROVIDER": "google", "GEMINI_API_KEY": "gemini-key"}
        )
        self.assertEqual((claude.llm_provider, claude.llm_type), ("claude", "claude"))
        self.assertEqual((gemini.llm_provider, gemini.llm_type), ("gemini", "gemini"))

    def test_cli_style_overrides_take_precedence(self) -> None:
        with patch("queryforge.core.config.load_dotenv"), patch.dict(
            os.environ,
            {"LLM_PROVIDER": "openai", "QWEN_API_KEY": "qwen-key"},
            clear=True,
        ):
            config = load_config(
                provider_override="qwen", model_override="qwen-turbo"
            )
        self.assertEqual(config.llm_provider, "qwen")
        self.assertEqual(config.llm_model, "qwen-turbo")

    def test_model_list_reads_lightweight_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.yml"
            path.write_text(
                """active_provider: local
providers:
  local:
    type: openai_compatible
    model: local-model
    api_key_env: LOCAL_API_KEY
""",
                encoding="utf-8",
            )
            definitions = list_model_definitions(path)
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0].model, "local-model")

    def test_unknown_provider_is_clear(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported model provider"):
            self.load({"LLM_PROVIDER": "unknown"})

    def test_missing_provider_key_is_clear(self) -> None:
        config = self.load({"LLM_PROVIDER": "deepseek"})
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            config.require_api_key()

    def test_optional_semantic_model_path_comes_from_environment(self) -> None:
        config = self.load(
            {
                "LLM_PROVIDER": "openai",
                "SEMANTIC_MODEL_PATH": "sample_data/example.semantic.yml",
            }
        )
        self.assertEqual(
            config.semantic_model_path, "sample_data/example.semantic.yml"
        )

    def test_loaded_config_requires_semantic_model_by_default_and_can_opt_out(self) -> None:
        self.assertTrue(self.load({"LLM_PROVIDER": "openai"}).require_semantic_model)
        self.assertFalse(
            self.load(
                {
                    "LLM_PROVIDER": "openai",
                    "REQUIRE_SEMANTIC_MODEL": "false",
                }
            ).require_semantic_model
        )

    def test_optional_sql_security_policy_path_comes_from_environment(self) -> None:
        config = self.load(
            {
                "LLM_PROVIDER": "openai",
                "SQL_SECURITY_POLICY_PATH": "policies/analyst.yml",
            }
        )
        self.assertEqual(config.sql_policy_path, "policies/analyst.yml")


if __name__ == "__main__":
    unittest.main()
