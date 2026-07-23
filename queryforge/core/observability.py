"""Standard-library logging and lightweight run/model observability."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = PROJECT_ROOT / ".queryforge/logs/queryforge.log"
DEFAULT_TRACE_DIR = PROJECT_ROOT / ".queryforge/traces"
_RUN_ID = ContextVar("queryforge_run_id", default="-")
_NODE_NAME = ContextVar("queryforge_node_name", default="-")


def new_run_id() -> str:
    return f"qf_{uuid.uuid4().hex}"


def current_run_id() -> str:
    return _RUN_ID.get()


def current_node_name() -> str:
    return _NODE_NAME.get()


@contextmanager
def run_logging_context(run_id: str):
    token = _RUN_ID.set(run_id)
    try:
        yield
    finally:
        _RUN_ID.reset(token)


@contextmanager
def node_logging_context(node_name: str):
    token = _NODE_NAME.set(node_name)
    try:
        yield
    finally:
        _NODE_NAME.reset(token)


class SafeContextFilter(logging.Filter):
    """Attach context and redact common credential shapes before formatting."""

    _PATTERNS = (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
        re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = getattr(record, "run_id", None) or current_run_id()
        record.node_name = getattr(record, "node_name", None) or current_node_name()
        message = record.getMessage()
        for pattern in self._PATTERNS:
            if pattern.groups:
                message = pattern.sub(r"\1[REDACTED]", message)
            else:
                message = pattern.sub("[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(
    level: str | None = None,
    *,
    log_path: str | Path | None = None,
    console: bool = True,
) -> Path:
    """Configure QueryForge loggers without touching third-party root logging."""

    load_dotenv()
    level_name = (level or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    numeric_level = logging.getLevelNamesMapping().get(level_name)
    if not isinstance(numeric_level, int):
        supported = "DEBUG, INFO, WARNING, ERROR, CRITICAL"
        raise ValueError(f"Invalid LOG_LEVEL {level_name!r}. Supported: {supported}")

    path = Path(log_path or os.getenv("LOG_FILE") or DEFAULT_LOG_PATH).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()

    logger = logging.getLogger("queryforge")
    logger.setLevel(numeric_level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_queryforge_handler", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s run_id=%(run_id)s node=%(node_name)s "
        "logger=%(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    safe_filter = SafeContextFilter()

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(safe_filter)
        console_handler._queryforge_handler = True  # type: ignore[attr-defined]
        logger.addHandler(console_handler)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(safe_filter)
        file_handler._queryforge_handler = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("file_logging_unavailable path=%s error=%s", path, exc)
    return path


def ensure_logging_configured() -> Path:
    logger = logging.getLogger("queryforge")
    if not any(getattr(handler, "_queryforge_handler", False) for handler in logger.handlers):
        return configure_logging()
    configured = next(
        (
            Path(handler.baseFilename)
            for handler in logger.handlers
            if getattr(handler, "_queryforge_handler", False)
            and isinstance(handler, logging.FileHandler)
        ),
        DEFAULT_LOG_PATH,
    )
    return configured


class ObservedModelProvider:
    """Duck-typed provider decorator that records summaries, never prompts by default."""

    def __init__(
        self,
        provider: Any,
        *,
        provider_name: str,
        model_name: str,
        debug_prompts: bool = False,
        trace_dir: str | Path | None = None,
    ) -> None:
        self._provider = provider
        self.provider = provider_name
        self.model = model_name
        self.debug_prompts = debug_prompts
        path = Path(trace_dir or DEFAULT_TRACE_DIR).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.trace_dir = path.resolve()
        self._counter = 0
        self._lock = threading.Lock()
        self._logger = logging.getLogger("queryforge.model")

    def generate_json(self, prompt: str) -> dict[str, Any]:
        return self._observe(
            "generate_json", prompt, lambda: self._provider.generate_json(prompt)
        )

    def generate_text(self, prompt: str) -> str:
        return self._observe(
            "generate_text", prompt, lambda: self._provider.generate_text(prompt)
        )

    def generate_with_messages(
        self, messages: list[dict[str, str]], json_mode: bool = False
    ) -> str:
        prompt = json.dumps(messages, ensure_ascii=False)
        return self._observe(
            "generate_with_messages",
            prompt,
            lambda: self._provider.generate_with_messages(messages, json_mode=json_mode),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def _observe(
        self, method: str, prompt: str, operation: Callable[[], Any]
    ) -> Any:
        started = time.perf_counter()
        response: Any = None
        error: Exception | None = None
        try:
            response = operation()
            return response
        except Exception as exc:
            error = exc
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            response_text = self._response_text(response)
            fields = (
                f"model_call method={method} provider={self.provider} model={self.model} "
                f"prompt_chars={len(prompt)} response_chars={len(response_text)} "
                f"duration_ms={duration_ms} success={error is None}"
            )
            if error is None:
                self._logger.info(fields)
            else:
                self._logger.error("%s error=%s", fields, error)
            if self.debug_prompts:
                self._write_trace(method, prompt, response_text, duration_ms, error)

    def _write_trace(
        self,
        method: str,
        prompt: str,
        response: str,
        duration_ms: float,
        error: Exception | None,
    ) -> None:
        try:
            with self._lock:
                self._counter += 1
                counter = self._counter
            run_id = current_run_id()
            node_name = self._safe_name(current_node_name())
            run_dir = self.trace_dir / self._safe_name(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = run_dir / f"{counter:03d}_{node_name}_{timestamp}.json"
            payload = {
                "run_id": run_id,
                "node": current_node_name(),
                "method": method,
                "provider": self.provider,
                "model": self.model,
                "prompt_chars": len(prompt),
                "response_chars": len(response),
                "duration_ms": duration_ms,
                "prompt": prompt,
                "response": response,
                "error": str(error) if error else None,
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._logger.info("prompt_trace_written path=%s", path)
        except Exception as exc:
            self._logger.warning("prompt_trace_write_failed error=%s", exc)

    @staticmethod
    def _response_text(response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        try:
            return json.dumps(response, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(response)

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return safe or "unknown"
