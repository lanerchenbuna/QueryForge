"""Lightweight YAML and environment configuration for model providers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS_CONFIG = PROJECT_ROOT / "models.yml"
PACKAGED_MODELS_CONFIG = Path(__file__).with_name("default_models.yml")
DEFAULT_DATABASE_PATH = "sample_data/anime_streaming/anime_streaming.sqlite"
DEFAULT_HISTORY_DB_PATH = ".queryforge/history.db"
DEFAULT_VECTOR_KB_PATH = ".queryforge/lancedb"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_ORCHESTRATION_STATE_ROOT = ".queryforge/runs"

PROVIDER_ALIASES = {
    "anthropic": "claude",
    "dashscope": "qwen",
    "google": "gemini",
    "zhipu": "glm",
    "zhipuai": "glm",
    "zai": "glm",
}


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    name: str
    type: str
    model: str
    api_key_env_names: tuple[str, ...]
    base_url: str | None = None
    model_env: str | None = None
    base_url_env: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    llm_provider: str
    llm_api_key: str | None
    llm_model: str
    llm_base_url: str | None
    database_path: str
    api_key_env_names: tuple[str, ...] = ()
    llm_type: str = "openai_compatible"
    history_db_path: str = DEFAULT_HISTORY_DB_PATH
    vector_kb_path: str = DEFAULT_VECTOR_KB_PATH
    embedding_api_key: str | None = None
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_base_url: str | None = None
    semantic_model_path: str | None = None
    # Directly constructed Config objects remain backward-compatible for embedded
    # callers; load_config() enables the production semantic gate by default.
    require_semantic_model: bool = False
    subject_tree_path: str | None = None
    subject_tree_enabled: bool = False
    default_subject: str | None = None
    streaming_enabled: bool = True
    streaming_event_buffer_size: int = 100
    report_enabled: bool = True
    report_max_rows: int = 50
    report_max_charts: int = 3
    report_output_dir: str = ".queryforge/reports"
    mcp_resources_enabled: bool = True
    mcp_prompts_enabled: bool = True
    mcp_session_enabled: bool = True
    mcp_history_limit: int = 20
    sql_policy_path: str | None = None
    orchestration_state_root: str = DEFAULT_ORCHESTRATION_STATE_ROOT
    # Transport hardening for network deployments (REST/Gateway/MCP).
    api_key: str | None = None
    allowed_database_paths: tuple[str, ...] = ()
    allowed_report_roots: tuple[str, ...] = ()

    def require_api_key(self) -> str:
        if not self.llm_api_key:
            names = " or ".join(self.api_key_env_names) or "the provider API key"
            raise ValueError(
                f"No API key is configured for provider {self.llm_provider!r}. "
                f"Set {names} in .env before using model generation."
            )
        return self.llm_api_key


def load_provider_definitions(
    config_path: str | Path | None = None,
) -> tuple[str, dict[str, ProviderDefinition]]:
    environment_path = os.getenv("MODELS_CONFIG")
    path = Path(config_path or environment_path or DEFAULT_MODELS_CONFIG).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file() and config_path is None and environment_path is None:
        path = PACKAGED_MODELS_CONFIG
    if not path.is_file():
        raise ValueError(f"Models configuration does not exist: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read models configuration {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), dict):
        raise ValueError(f"Models configuration {path} must contain a providers mapping")

    definitions: dict[str, ProviderDefinition] = {}
    for raw_name, raw_definition in payload["providers"].items():
        if not isinstance(raw_name, str) or not isinstance(raw_definition, dict):
            raise ValueError(f"Invalid provider definition in {path}: {raw_name!r}")
        name = raw_name.strip().lower()
        provider_type = str(raw_definition.get("type") or "").strip()
        model = str(raw_definition.get("model") or "").strip()
        raw_key_env = raw_definition.get("api_key_env")
        if isinstance(raw_key_env, str):
            key_envs = (raw_key_env,)
        elif isinstance(raw_key_env, list) and all(
            isinstance(item, str) for item in raw_key_env
        ):
            key_envs = tuple(raw_key_env)
        else:
            key_envs = ()
        if not provider_type or not model or not key_envs:
            raise ValueError(
                f"Provider {name!r} must define type, model, and api_key_env"
            )
        definitions[name] = ProviderDefinition(
            name=name,
            type=provider_type,
            model=model,
            api_key_env_names=key_envs,
            base_url=_optional_string(raw_definition.get("base_url")),
            model_env=_optional_string(raw_definition.get("model_env")),
            base_url_env=_optional_string(raw_definition.get("base_url_env")),
        )

    active = str(payload.get("active_provider") or "").strip().lower()
    active = PROVIDER_ALIASES.get(active, active)
    if active not in definitions:
        raise ValueError(f"active_provider {active!r} is not defined in {path}")
    return active, definitions


def list_model_definitions(
    config_path: str | Path | None = None,
) -> list[ProviderDefinition]:
    _, definitions = load_provider_definitions(config_path)
    return list(definitions.values())


def load_config(
    provider_override: str | None = None,
    model_override: str | None = None,
    config_path: str | Path | None = None,
) -> Config:
    load_dotenv()
    yaml_active, definitions = load_provider_definitions(config_path)
    requested_provider = (
        provider_override or os.getenv("LLM_PROVIDER") or yaml_active
    ).strip().lower()
    provider = PROVIDER_ALIASES.get(requested_provider, requested_provider)
    if provider not in definitions:
        supported = ", ".join(definitions)
        raise ValueError(
            f"Unsupported model provider {requested_provider!r}. Supported: {supported}"
        )

    definition = definitions[provider]
    environment_model = (
        os.getenv(definition.model_env) if definition.model_env else None
    )
    environment_base_url = (
        os.getenv(definition.base_url_env) if definition.base_url_env else None
    )
    return Config(
        llm_provider=provider,
        llm_type=definition.type,
        llm_api_key=_first_environment_value(definition.api_key_env_names),
        llm_model=model_override or environment_model or definition.model,
        llm_base_url=environment_base_url or definition.base_url,
        database_path=os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        api_key_env_names=definition.api_key_env_names,
        history_db_path=os.getenv("HISTORY_DB_PATH", DEFAULT_HISTORY_DB_PATH),
        vector_kb_path=os.getenv("VECTOR_KB_PATH", DEFAULT_VECTOR_KB_PATH),
        embedding_api_key=(
            os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
        ),
        embedding_model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        embedding_base_url=(
            os.getenv("EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
        ),
        semantic_model_path=_optional_string(os.getenv("SEMANTIC_MODEL_PATH")),
        require_semantic_model=_environment_bool(
            os.getenv("REQUIRE_SEMANTIC_MODEL"), default=True
        ),
        subject_tree_path=_optional_string(os.getenv("SUBJECT_TREE_PATH")),
        subject_tree_enabled=_environment_bool(
            os.getenv("SUBJECT_TREE_ENABLED"), default=False
        ),
        default_subject=_optional_string(os.getenv("DEFAULT_SUBJECT")),
        streaming_enabled=_environment_bool(
            os.getenv("STREAMING_ENABLED"), default=True
        ),
        streaming_event_buffer_size=_environment_int(
            os.getenv("STREAMING_EVENT_BUFFER_SIZE"), default=100, minimum=1
        ),
        report_enabled=_environment_bool(os.getenv("REPORT_ENABLED"), default=True),
        report_max_rows=_environment_int(
            os.getenv("REPORT_MAX_ROWS"), default=50, minimum=1
        ),
        report_max_charts=_environment_int(
            os.getenv("REPORT_MAX_CHARTS"), default=3, minimum=1
        ),
        report_output_dir=os.getenv("REPORT_OUTPUT_DIR", ".queryforge/reports"),
        mcp_resources_enabled=_environment_bool(
            os.getenv("MCP_RESOURCES_ENABLED"), default=True
        ),
        mcp_prompts_enabled=_environment_bool(
            os.getenv("MCP_PROMPTS_ENABLED"), default=True
        ),
        mcp_session_enabled=_environment_bool(
            os.getenv("MCP_SESSION_ENABLED"), default=True
        ),
        mcp_history_limit=_environment_int(
            os.getenv("MCP_HISTORY_LIMIT"), default=20, minimum=1
        ),
        sql_policy_path=_optional_string(os.getenv("SQL_SECURITY_POLICY_PATH")),
        orchestration_state_root=os.getenv(
            "ORCHESTRATION_STATE_ROOT", DEFAULT_ORCHESTRATION_STATE_ROOT
        ),
        api_key=_optional_string(os.getenv("QUERYFORGE_API_KEY")),
        allowed_database_paths=_environment_paths(
            os.getenv("DATABASE_ALLOWLIST")
        ),
        allowed_report_roots=_environment_paths(os.getenv("REPORT_ROOT_ALLOWLIST")),
    )


def _first_environment_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _environment_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"environment value {value!r} must be a boolean")


def _environment_int(value: str | None, *, default: int, minimum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"environment value {value!r} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"environment integer value must be at least {minimum}")
    return parsed


def _environment_paths(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated path allowlist into clean, non-empty entries."""
    if value is None:
        return ()
    return tuple(
        entry.strip()
        for entry in value.split(",")
        if entry.strip()
    )
