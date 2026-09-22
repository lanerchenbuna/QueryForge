"""Transport-level authentication and path allowlisting for network entrypoints.

The CLI is a local operator surface and is intentionally exempt. REST, SSE,
Gateway, and MCP entrypoints must not let remote callers read arbitrary files,
write reports into arbitrary directories, or burn model quota anonymously.
Caller-supplied file paths are therefore confined to an explicit allowlist when
one is configured, or to the project root plus the directory of the configured
default database otherwise.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from queryforge.core.config import PROJECT_ROOT, Config
from queryforge.application.options import AgentOptions

# Entrypoints that reach QueryForge over a network transport.
NETWORK_ENTRYPOINTS = frozenset({"api", "api_stream", "gateway", "mcp"})


class TransportAuthError(ValueError):
    """Raised when a transport request fails the API-key gate."""


def request_api_key_matches(
    config: Config,
    authorization: str | None,
    x_api_key: str | None,
) -> bool:
    """Constant-time comparison of the request credential with the configured key.

    Disabled (returns True) when no API key is configured, keeping local-first
    deployments unchanged.
    """
    expected = (config.api_key or "").strip()
    if not expected:
        return True
    bearer = ""
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and separator:
            bearer = token.strip()
    supplied = (bearer or (x_api_key or "")).strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _resolve_entry(entry: str) -> Path:
    path = Path(entry).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _entry_root(entry: str) -> Path:
    """Map an allowlist entry to a directory root.

    Existing files confine the root to their parent directory; everything else
    (including not-yet-created paths) is treated as a directory entry so a
    misconfigured path never silently widens the allowlist.
    """
    resolved = _resolve_entry(entry)
    return resolved.parent if resolved.is_file() else resolved


def database_roots(config: Config) -> tuple[Path, ...]:
    """Directory roots that may contain caller-supplied database/semantic files."""
    if config.allowed_database_paths:
        return tuple(_entry_root(entry) for entry in config.allowed_database_paths)
    fallback = [PROJECT_ROOT.resolve()]
    try:
        fallback.append(Path(config.database_path).expanduser().resolve().parent)
    except (OSError, ValueError):
        pass
    return tuple(fallback)


def report_roots(config: Config) -> tuple[Path, ...]:
    """Directories that may receive generated HTML reports."""
    if config.allowed_report_roots:
        return tuple(_entry_root(entry) for entry in config.allowed_report_roots)
    return (_resolve_entry(config.report_output_dir),)


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def validate_file_path(
    config: Config,
    label: str,
    value: str | None,
    roots: tuple[Path, ...],
) -> None:
    """Reject a caller-supplied path outside the allowed roots."""
    if value is None:
        return
    resolved = Path(value).expanduser().resolve()
    if not _is_within(resolved, roots):
        raise ValueError(
            f"{label} {value!r} is outside the allowed transport paths; "
            "configure DATABASE_ALLOWLIST or REPORT_ROOT_ALLOWLIST to extend "
            "the permitted locations"
        )


def validate_transport_options(config: Config, options: AgentOptions) -> None:
    """Confine every caller-supplied file path on a network entrypoint."""
    roots = database_roots(config)
    validate_file_path(config, "database", options.database, roots)
    validate_file_path(config, "semantic_model_path", options.semantic_model_path, roots)
    validate_file_path(config, "sql_policy_path", options.sql_policy_path, roots)
    validate_file_path(config, "subject_tree_path", options.subject_tree_path, roots)
    validate_file_path(
        config, "report_output_dir", options.report_output_dir, report_roots(config)
    )


def validate_database_path(config: Config, database: str | None) -> None:
    """Confine an explicitly supplied database path (resource endpoints)."""
    validate_file_path(config, "database", database, database_roots(config))


def validate_report_root(config: Config, report_output_dir: str | None) -> None:
    """Confine an explicitly supplied report directory."""
    validate_file_path(
        config, "report_output_dir", report_output_dir, report_roots(config)
    )
