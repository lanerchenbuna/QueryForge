"""Atomic local persistence for privacy-bounded conversation sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from queryforge.orchestration.schemas import utc_now
from queryforge.orchestration.schemas.session import SessionMemory


class SessionStore:
    def __init__(self, root: str | Path = ".queryforge/sessions") -> None:
        self.root = Path(root).expanduser()

    def create(self, session_id: str | None = None) -> SessionMemory:
        resolved_id = session_id or f"session_{uuid4().hex}"
        self._validate_session_id(resolved_id)
        return SessionMemory(session_id=resolved_id)

    def load(self, session_id: str) -> SessionMemory | None:
        path = self.path_for(session_id)
        if not path.is_file():
            return None
        try:
            return SessionMemory.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not load session {session_id!r}: {exc}") from exc

    def load_or_create(self, session_id: str) -> SessionMemory:
        return self.load(session_id) or self.create(session_id)

    def save(self, memory: SessionMemory) -> Path:
        path = self.path_for(memory.session_id)
        memory.updated_at = utc_now()
        self._atomic_json(path, memory.model_dump(mode="json"))
        return path

    def reset(self, session_id: str) -> SessionMemory:
        memory = self.create(session_id)
        self.save(memory)
        return memory

    def path_for(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.root / f"{session_id}.json"

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not session_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in session_id
        ):
            raise ValueError(
                "session_id must contain only letters, numbers, underscores, or hyphens"
            )

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
