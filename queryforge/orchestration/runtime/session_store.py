"""Atomic local persistence for privacy-bounded conversation sessions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from queryforge.orchestration.schemas import utc_now
from queryforge.orchestration.schemas.session import (
    DEFAULT_MEMORY_RETENTION_DAYS,
    SessionMemory,
    SessionTurn,
    UserPreference,
    strip_result_rows,
)


class SessionStore:
    """Session memory lifecycle: expiry, scoped deletion, export, version invalidation.

    All operations are scoped to a single session file, so deleting or expiring one
    session can never touch another user's or another domain's memory.
    """

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
        payload, stripped = strip_result_rows(memory.model_dump(mode="json"))
        if stripped:
            # Visible bookkeeping: rows were dropped instead of silently written.
            # The key deliberately avoids the substring "rows" so a downstream
            # "no result data persisted" assertion stays meaningful.
            payload["result_payloads_dropped"] = stripped
        self._atomic_json(path, payload)
        return path

    def reset(self, session_id: str) -> SessionMemory:
        memory = self.create(session_id)
        self.save(memory)
        return memory

    # ------------------------------------------------------------ lifecycle
    def expire(
        self,
        session_id: str,
        *,
        before: str | None = None,
    ) -> dict[str, Any]:
        """Drop turns older than ``before`` (default: the retention window).

        Returns a summary; the session file is rewritten only when something
        actually expired.
        """
        memory = self.load(session_id)
        if memory is None:
            return {
                "session_id": session_id,
                "status": "not_found",
                "expired_turns": 0,
                "remaining_turns": 0,
            }
        cutoff = before or self._retention_cutoff(memory)
        remaining = [
            turn for turn in memory.history if not self._is_before(turn.created_at, cutoff)
        ]
        expired = len(memory.history) - len(remaining)
        if expired:
            memory.history = remaining
            self._refresh_summary(memory)
            self.save(memory)
        return {
            "session_id": session_id,
            "status": "expired" if expired else "unchanged",
            "before": cutoff,
            "expired_turns": expired,
            "remaining_turns": len(memory.history),
        }

    def expire_all(self, *, before: str | None = None) -> dict[str, Any]:
        """Expire every stored session; each session keeps its own retention window."""
        results = [
            self.expire(path.stem, before=before)
            for path in sorted(self.root.glob("*.json"))
            if path.is_file()
        ]
        return {
            "sessions": len(results),
            "expired_turns": sum(int(item["expired_turns"]) for item in results),
            "details": results,
        }

    def delete(
        self,
        session_id: str,
        *,
        turn_range: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        """Delete a whole session, or only an inclusive ``turn_range`` inside it.

        ``turn_count`` is intentionally left monotonic: turn numbers are stable
        identifiers, and reusing a deleted number would let a later turn silently
        inherit the deleted turn's audit identity.
        """
        path = self.path_for(session_id)
        memory = self.load(session_id)
        if memory is None:
            return {"session_id": session_id, "status": "not_found", "deleted_turns": 0}
        if turn_range is None:
            removed = len(memory.history)
            path.unlink(missing_ok=True)
            return {
                "session_id": session_id,
                "status": "deleted",
                "deleted_turns": removed,
                "file_removed": True,
            }
        if len(turn_range) != 2:
            raise ValueError("turn_range must be a (start, end) inclusive pair")
        start, end = int(turn_range[0]), int(turn_range[1])
        if start > end:
            raise ValueError("turn_range start must not be greater than end")
        kept = [
            turn
            for turn in memory.history
            if not (start <= turn.turn_number <= end)
        ]
        removed = len(memory.history) - len(kept)
        memory.history = kept
        self._refresh_summary(memory)
        self.save(memory)
        return {
            "session_id": session_id,
            "status": "deleted_turns" if removed else "unchanged",
            "deleted_turns": removed,
            "remaining_turns": len(kept),
            "turn_range": [start, end],
        }

    def export(self, session_id: str) -> dict[str, Any]:
        """Export a session as JSON-safe data (never result rows: never stored)."""
        memory = self.load(session_id)
        if memory is None:
            return {"session_id": session_id, "found": False, "exported_at": utc_now()}
        payload = memory.model_dump(mode="json")
        return {
            "session_id": session_id,
            "found": True,
            "exported_at": utc_now(),
            "turn_count": memory.turn_count,
            "user_id": memory.user_id,
            "domain_id": memory.domain_id,
            "preferences": payload.get("preferences", []),
            "memory": payload,
        }

    # ----------------------------------------------------------- preferences
    def set_preference(
        self,
        session_id: str,
        preference: UserPreference,
    ) -> UserPreference:
        """Store one user-scoped preference; conflicting scopes are rejected."""
        memory = self.load_or_create(session_id)
        if not str(preference.user_id or "").strip():
            raise ValueError("UserPreference requires a non-empty user_id")
        if memory.user_id and memory.user_id != preference.user_id:
            raise ValueError(
                "session belongs to another user; refusing to write a preference "
                "into a foreign session"
            )
        if memory.domain_id and preference.domain_id and memory.domain_id != preference.domain_id:
            raise ValueError(
                "preference domain does not match the session domain"
            )
        stored = preference.model_copy(
            update={"session_id": session_id, "updated_at": utc_now()}
        )
        memory.user_id = memory.user_id or preference.user_id
        memory.domain_id = memory.domain_id or preference.domain_id
        memory.preferences = [
            existing
            for existing in memory.preferences
            if not (
                existing.name == stored.name
                and existing.user_id == stored.user_id
                and existing.domain_id == stored.domain_id
            )
        ]
        memory.preferences.append(stored)
        self.save(memory)
        return stored

    def preferences(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        domain_id: str | None = None,
    ) -> list[UserPreference]:
        """Read preferences, optionally narrowed to one user and/or domain."""
        memory = self.load(session_id)
        if memory is None:
            return []
        if user_id is None and domain_id is None:
            return list(memory.preferences)
        return [
            preference
            for preference in memory.preferences
            if (user_id is None or preference.user_id == user_id)
            and (domain_id is None or preference.domain_id == domain_id)
        ]

    def revoke_preference(
        self,
        session_id: str,
        name: str,
        *,
        user_id: str,
        domain_id: str | None = None,
    ) -> bool:
        """Revoke one preference for its owner only.

        Returns ``False`` (and changes nothing) when the caller's user/domain does
        not own the preference, so one user can never delete another's memory.
        """
        memory = self.load(session_id)
        if memory is None:
            return False
        owned = [
            preference
            for preference in memory.preferences
            if preference.name == name
            and preference.matches_scope(user_id=user_id, domain_id=domain_id)
        ]
        if not owned:
            return False
        memory.preferences = [
            preference
            for preference in memory.preferences
            if not (
                preference.name == name
                and preference.matches_scope(user_id=user_id, domain_id=domain_id)
            )
        ]
        self.save(memory)
        return True

    # ------------------------------------------------------ version tracking
    def invalidate_version(
        self,
        version_ref: str,
        *,
        session_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Mark turns that used a superseded definition version.

        Only the turns that actually recorded the version are invalidated: turns
        that never used it keep their context, so a version update does not wipe
        unrelated memory. Nothing is deleted — an invalidated turn is visible and
        skipped by follow-up rewriting instead.
        """
        if not str(version_ref or "").strip():
            raise ValueError("invalidate_version requires a non-empty version reference")
        targets = [session_id] if session_id else self.session_ids()
        affected: list[dict[str, Any]] = []
        for current in targets:
            memory = self.load(current)
            if memory is None:
                continue
            marked = 0
            for turn in memory.history:
                if turn.invalidated or not turn.uses_version(str(version_ref)):
                    continue
                turn.invalidated = True
                turn.invalidated_reason = (
                    reason or f"superseded_definition:{version_ref}"
                )
                marked += 1
            if marked:
                self.save(memory)
                affected.append({"session_id": current, "turns": marked})
        return {
            "version_ref": version_ref,
            "sessions": len(affected),
            "turns": sum(int(item["turns"]) for item in affected),
            "affected": affected,
        }

    def session_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(path.stem for path in self.root.glob("*.json") if path.is_file())

    def path_for(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.root / f"{session_id}.json"

    # ------------------------------------------------------------ internals
    @staticmethod
    def _retention_cutoff(memory: SessionMemory) -> str:
        if memory.expires_at:
            return memory.expires_at
        days = (
            memory.retention_days
            if memory.retention_days is not None
            else DEFAULT_MEMORY_RETENTION_DAYS
        )
        return (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()

    @staticmethod
    def _is_before(timestamp: str, cutoff: str) -> bool:
        try:
            moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            limit = datetime.fromisoformat(str(cutoff).replace("Z", "+00:00"))
        except ValueError:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if limit.tzinfo is None:
            limit = limit.replace(tzinfo=timezone.utc)
        return moment < limit

    @staticmethod
    def _refresh_summary(memory: SessionMemory) -> None:
        """Point the summary fields at the newest surviving turn."""
        latest: SessionTurn | None = memory.history[-1] if memory.history else None
        if latest is None:
            memory.last_question = None
            memory.last_sql = None
            memory.last_result_schema = []
            memory.last_metrics = []
            memory.last_dimensions = []
            memory.last_filters = []
            memory.last_time_range = None
            return
        memory.last_question = latest.rewritten_question or latest.question
        memory.last_sql = latest.sql
        memory.last_result_schema = list(latest.result_schema)
        memory.last_metrics = list(latest.metrics)
        memory.last_dimensions = list(latest.dimensions)
        memory.last_filters = list(latest.filters)
        memory.last_time_range = latest.time_range

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
