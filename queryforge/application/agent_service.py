"""Application service that maps every transport to the Agent Team workflow."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any, Callable

from queryforge.workflow.event_emitter import (
    TERMINAL_EVENT_TYPE,
    EventEmitter,
    emit_event,
)
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.workflow.workflow import WorkflowCancelled
from queryforge.interfaces.transport_security import (
    NETWORK_ENTRYPOINTS,
    validate_transport_options,
)
from queryforge.orchestration.agents.entry_router import EntryRouterAgent
from queryforge.orchestration.agents.product_analyst import ProductAnalystAgent
from queryforge.orchestration.agents.sql_review import SQLReviewAgent
from queryforge.orchestration.orchestrator.orchestrator import OrchestratorAgent
from queryforge.orchestration.runtime.session_store import SessionStore
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.schemas.knowledge_versions import (
    knowledge_version_refs,
    merge_version_refs,
)
from queryforge.orchestration.schemas.session import (
    SessionMemory,
    UserPreference,
)
from queryforge.core.config import Config, load_config
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.infrastructure.models.factory import ModelFactory
from queryforge.core.observability import (
    SpanRecorder,
    get_span_recorder,
    new_run_id,
    start_span_recorder,
)
from queryforge.core.schemas.models import SqlTask
from queryforge.application.event_stream import WorkflowEventStream
from queryforge.domain.analysis import AnalysisRequest, apply_patch
from queryforge.domain.skills import SkillRegistry
from queryforge.application.direct_tasks import DirectTaskExecutor
from queryforge.application.options import AgentOptions
from queryforge.application.resources import ResourceService
from queryforge.domain.domains import DomainContext, DomainResolver
from queryforge.domain.semantic import discover_semantic_model


LOGGER = logging.getLogger("queryforge.agent_service")

#: Persisted terminal statuses, first-writer-wins. A cancel may only *replace* a
#: non-terminal record: once a run persisted any terminal outcome — ``completed``,
#: ``blocked`` (a governance stop), ``failed``, or an earlier ``cancelled`` — a
#: late cancel must not rewrite what happened. ``blocked`` was missing here, so a
#: governance-stopped run was relabelled ``cancelled`` and its error text was
#: replaced by the disconnect reason (H7).
_PERSISTED_TERMINAL_STATUSES = frozenset(
    {"completed", "blocked", "failed", "cancelled", "needs_clarification"}
)


@dataclass(frozen=True)
class _PreparedRun:
    """One request after the shared pre-flight, before any run is scheduled.

    Carrying the resolved values (config, governed paths, domain context) means the
    streaming entry point can validate a request synchronously and then hand the
    *same* resolution to the worker, instead of validating twice with two
    different outcomes (M6).
    """

    question: str
    options: AgentOptions
    config: Config
    database_path: Path
    domain_context: "DomainContext | None"


class IdentifiedStateStore(AgentTeamStateStore):
    """State store that publishes stable run/task identity into observability.

    The orchestrator owns the persisted ``task_id``. Binding it here means every
    later event and span carries the same ``run_id``/``task_id`` pair as the run
    record, instead of only the run id.
    """

    def __init__(
        self,
        root,
        event_emitter: EventEmitter | None = None,
        span_recorder: SpanRecorder | None = None,
    ) -> None:
        super().__init__(root, event_emitter)
        self.span_recorder = span_recorder

    def initialize(self, state):
        run_dir = super().initialize(state)
        if self.event_emitter is not None:
            self.event_emitter.bind(run_id=state.run_id, task_id=state.task_id)
        if self.span_recorder is not None:
            self.span_recorder.set_task_id(state.task_id)
        return run_dir


def _close_span_recorder(recorder: Any) -> None:
    """Close a run's span recorder, tolerating an already-closed one.

    A recorder is created per run; the workflow path closes its own, the
    direct-run path does not. Closing twice must stay harmless because both paths
    can run for the same run id in tests and in re-entrant calls.
    """
    try:
        if recorder is not None and not recorder.closed:
            recorder.close()
    except Exception:  # pragma: no cover - closing must never mask a run result
        LOGGER.debug("span recorder close failed", exc_info=True)


def late_cancel_preserved_the_outcome(persisted: dict[str, Any] | None) -> bool:
    """Translate the cancel write into the flag published on the terminal event.

    ``persist_cancelled_outcome`` returns the document it wrote exactly when it
    **rewrote** the run, and ``None`` when an already-terminal outcome was left
    alone. ``StreamEvent.cancelled_after_completion`` documents the opposite
    direction ("the persisted outcome was preserved"), so the flag is the ``None``
    case; publishing ``persisted is not None`` reported the inverse of what the
    field claims.
    """
    return persisted is None


def persist_cancelled_outcome(
    *,
    state_root: str | Path,
    run_id: str,
    task_id: str | None = None,
    reason: str,
) -> dict[str, Any] | None:
    """Persist the ``cancelled`` terminal outcome of a run, if it may win.

    Cancellation is recorded on the run's ``state.json`` so a disconnect is
    never persisted as ``failed`` (or as a success). The write is first-writer
    wins over the whole terminal vocabulary the run protocol defines —
    ``completed``, ``blocked``, ``failed`` and ``cancelled``: an already terminal
    record is left byte-for-byte untouched, so ``last_error``, ``blocked_reason``
    and ``current_phase`` keep the outcome the run really reached (a governance
    block must stay a governance block, H7). Returns the persisted document, or
    ``None`` when nothing was written because a terminal outcome was already
    recorded.
    """

    path = Path(state_root).expanduser() / run_id / "state.json"
    document: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
        except (OSError, ValueError):
            document = {}
    if document.get("status") in _PERSISTED_TERMINAL_STATUSES:
        LOGGER.info(
            "cancel_outcome_preserved run_id=%s status=%s",
            run_id,
            document.get("status"),
        )
        return None
    document["run_id"] = document.get("run_id") or run_id
    if task_id:
        document["task_id"] = document.get("task_id") or task_id
    document["status"] = "cancelled"
    document["outcome"] = "cancelled"
    document["cancelled"] = True
    document["current_phase"] = "cancelled"
    document["last_error"] = reason
    document["finished_at"] = document.get("finished_at") or datetime.now(
        timezone.utc
    ).isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        LOGGER.warning("cancel_outcome_not_persisted run_id=%s error=%s", run_id, exc)
        return None
    _mark_execution_journal_cancelled(path.parent, run_id, reason)
    return document


def _mark_execution_journal_cancelled(
    run_dir: Path, run_id: str, reason: str
) -> None:
    """Record the cancellation on an existing durable execution journal.

    Step 15's ``ExecutionJournal`` owns the same single-terminal-outcome
    contract (``TERMINAL_OUTCOMES``), so an existing journal must agree with the
    state file. Best effort and only for runs that already have a journal: this
    never creates one for a run that does not use durable execution, and a
    journal failure never affects the cancelled outcome.
    """

    if not (run_dir / "execution.json").is_file():
        return
    try:
        from queryforge.orchestration.runtime.execution_journal import (
            ExecutionJournal,
        )

        ExecutionJournal(run_dir, run_id=run_id).mark_cancelled(reason)
    except Exception as exc:  # pragma: no cover - depends on the step 15 module
        LOGGER.warning(
            "cancel_journal_not_updated run_id=%s error=%s", run_id, exc
        )


def _same_path(left: str | None, right: str | None) -> bool:
    """Return True only when both values name the same existing-or-planned path."""
    if left is None or right is None:
        return False
    try:
        return (
            Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
        )
    except (OSError, ValueError):
        return Path(left).as_posix() == Path(right).as_posix()


def _last_analysis_request(memory: SessionMemory) -> AnalysisRequest | None:
    """Return the typed analysis request of the most recent turn that has one."""

    for turn in reversed(memory.history):
        if isinstance(turn.analysis_request, dict):
            return AnalysisRequest.model_validate_artifact(turn.analysis_request)
    return None


def _load_analysis_payload(artifacts_dir: Path) -> dict[str, Any] | None:
    """Read the last ``analysis_request`` artifact payload written for a run."""

    if not artifacts_dir.is_dir():
        return None
    for path in sorted(artifacts_dir.glob("*_analysis_request.json"), reverse=True):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        payload = document.get("payload") if isinstance(document, dict) else None
        if isinstance(payload, dict):
            return payload
    return None


def _high_severity(clarifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in clarifications
        if isinstance(item, dict) and item.get("severity") == "high"
    ]


def _retrieval_scope(domain_context: "DomainContext | None") -> dict[str, Any]:
    """The governed retrieval scope of a run bound to a published domain.

    Returns an empty dict for a run with no domain, which keeps the previous
    unfiltered behaviour for single-database deployments instead of filtering on
    an empty domain id.
    """

    if domain_context is None:
        return {}
    scope: dict[str, Any] = {
        "domain_id": domain_context.domain_id,
        "data_version": domain_context.data_version,
    }
    if domain_context.semantic_version:
        scope["version"] = domain_context.semantic_version
    return {key: value for key, value in scope.items() if value}


def run_observability_summary(
    emitter: EventEmitter, run_id: str
) -> dict[str, Any] | None:
    """Payload-free usage/latency summary for a run's terminal event.

    Carries token counts, latency breakdowns and span counts only: no prompts, no
    SQL text and no result rows, so it is safe to put on the wire.
    """

    recorder = get_span_recorder(run_id)
    if recorder is None:
        return None
    return recorder.summary()


class AgentService(ResourceService):
    """Transport-neutral facade; it contains no SQL generation logic itself."""

    def __init__(
        self,
        *,
        config_loader: Callable[..., Config] = load_config,
        runner_factory: Callable[..., WorkflowRunner] = WorkflowRunner,
        llm_factory: Callable[[Config], BaseModelProvider] = ModelFactory.create,
        skill_registry: SkillRegistry | None = None,
        entry_router: EntryRouterAgent | None = None,
    ) -> None:
        self.config_loader = config_loader
        self.runner_factory = runner_factory
        self.llm_factory = llm_factory
        self.skill_registry = skill_registry or SkillRegistry()
        self.entry_router = entry_router or EntryRouterAgent()
        self.direct_tasks = DirectTaskExecutor()
        super().__init__(config_loader, self.skill_registry)

    def ask(
        self, question: str, options: AgentOptions | None = None
    ) -> dict:
        return self._run(question, options or AgentOptions(), plan_only=False)

    def plan(
        self, question: str, options: AgentOptions | None = None
    ) -> dict:
        return self._run(question, options or AgentOptions(), plan_only=True)

    def stream(
        self,
        question: str,
        options: AgentOptions | None = None,
    ) -> "WorkflowEventStream":
        """Run one ask request in a worker and expose progress-only events.

        The stream follows the versioned event protocol: one ``run_started``
        event, progress events, and exactly one terminal ``final_result`` event
        carrying the run's ``outcome``. A client disconnect reaches the SQL/tool
        boundary through ``stream.cancel`` and is persisted as ``cancelled``,
        never as ``failed``.

        Contract for a *rejected* request (M6): every validation the synchronous
        ``ask`` entry point performs happens here, before the worker exists, and
        raises ``ValueError`` — the transport answers 4xx like ``/ask`` does.
        A stream is therefore only ever created for a request that has already
        passed the question, options, transport-allowlist, database and semantic
        gate checks, so an SSE client can never be told "failed" for a request the
        same API refuses with a 400.
        """
        resolved_options = options or AgentOptions()
        config = self.config_loader(
            provider_override=resolved_options.model_provider,
            model_override=resolved_options.model,
        )
        if not config.streaming_enabled:
            raise ValueError("Streaming is disabled by configuration")
        # The shared pre-flight is the same call ``_run`` makes: one
        # implementation, so /ask and /ask/stream can never drift apart.
        prepared = self._prepare(question, resolved_options, config=config)
        run_id = prepared.options.run_id or new_run_id()
        prepared = replace(prepared, options=replace(prepared.options, run_id=run_id))
        resolved_options = prepared.options
        emitter = EventEmitter(config.streaming_event_buffer_size)
        # The run identity is stable from the first event onwards; the task
        # identity is bound by IdentifiedStateStore once the orchestrator has
        # created the persisted task state.
        emitter.bind(run_id=run_id)
        stream = WorkflowEventStream(
            emitter, queue_maxsize=config.streaming_event_buffer_size
        )
        emitter.on_event(stream._publish)
        state_root = (
            resolved_options.orchestration_state_root
            or config.orchestration_state_root
        )
        terminal_sent = False

        def emit_terminal(**fields: Any) -> None:
            """Emit the run's one terminal event (never a second one)."""

            nonlocal terminal_sent
            if terminal_sent:
                LOGGER.warning("stream_terminal_skipped run_id=%s duplicate", run_id)
                return
            terminal_sent = True
            emit_event(emitter, TERMINAL_EVENT_TYPE, run_id, **fields)

        def worker() -> None:
            emit_event(
                emitter,
                "run_started",
                run_id,
                status="running",
                message="Started QueryForge workflow.",
            )
            try:
                stream.result = self._run(
                    prepared.question,
                    resolved_options,
                    plan_only=False,
                    event_emitter=emitter,
                    cancel_check=stream.is_cancelled,
                    prepared=prepared,
                )
            except WorkflowCancelled as exc:
                # Bounded, payload-free reason: which step stopped, never the
                # surrounding workflow context dump.
                node_name = str(getattr(exc, "node_name", "") or "workflow")
                reason = (
                    "Client disconnected before the workflow completed "
                    f"(stopped at {node_name})."
                )
                stream.result = {
                    "status": "cancelled",
                    "outcome": "cancelled",
                    "run_id": run_id,
                    "question": question,
                    "reason": reason,
                }
                persist_cancelled_outcome(
                    state_root=state_root,
                    run_id=run_id,
                    task_id=(emitter.run_metadata or {}).get("task_id"),
                    reason=reason,
                )
                emit_terminal(
                    status="cancelled",
                    outcome="cancelled",
                    message="QueryForge workflow was cancelled.",
                    result=stream.result,
                )
            except Exception as exc:
                stream.error = exc
                emit_terminal(
                    status="failed",
                    outcome="failed",
                    message="QueryForge workflow failed.",
                    error=str(exc),
                )
            else:
                if stream.is_cancelled():
                    # The workflow finished, but cancellation arrived before the
                    # client could be told. The persisted outcome wins: if the run
                    # already recorded ``completed`` it is not rewritten, and a
                    # cancel that lost the race is reported as such.
                    persisted = persist_cancelled_outcome(
                        state_root=state_root,
                        run_id=run_id,
                        task_id=(emitter.run_metadata or {}).get("task_id"),
                        reason=(
                            "Cancellation arrived after the workflow returned; "
                            "an already completed run is not rewritten."
                        ),
                    )
                    stream.cancelled_after_completion = (
                        late_cancel_preserved_the_outcome(persisted)
                    )
                    LOGGER.warning(
                        "cancel_after_completion run_id=%s rewritten=%s preserved=%s",
                        run_id,
                        persisted is not None,
                        stream.cancelled_after_completion,
                    )
                emit_terminal(
                    status=str(stream.result.get("status") or "success"),
                    message="QueryForge workflow completed.",
                    result=stream.result,
                    data={
                        "cancelled_after_completion": bool(
                            stream.cancelled_after_completion
                        ),
                        "observability": run_observability_summary(emitter, run_id),
                    },
                )
            finally:
                stream._close()

        Thread(target=worker, name=f"queryforge-{run_id}", daemon=True).start()
        return stream

    def new_session(
        self,
        *,
        session_id: str | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        config = self.config_loader()
        options = AgentOptions(orchestration_state_root=orchestration_state_root)
        store = self._session_store(options, config)
        memory = store.create(session_id)
        path = store.save(memory)
        return {
            "session_id": memory.session_id,
            "path": str(path),
            "turn_count": memory.turn_count,
        }

    def reset_session(
        self,
        session_id: str,
        *,
        orchestration_state_root: str | None = None,
    ) -> dict:
        config = self.config_loader()
        options = AgentOptions(orchestration_state_root=orchestration_state_root)
        memory = self._session_store(options, config).reset(session_id)
        return {
            "session_id": memory.session_id,
            "turn_count": memory.turn_count,
            "status": "reset",
        }

    # ------------------------------------------------ session memory governance

    def _governance_session_store(
        self, orchestration_state_root: str | None = None
    ) -> SessionStore:
        """The session store used by the administrative session operations.

        It resolves to the *same* directory ``_session_store`` uses for runs
        (``<orchestration_state_root>/../sessions``), so an operator reads and
        deletes exactly the memory the runs write.
        """

        config = self.config_loader()
        runs_root = Path(
            orchestration_state_root or config.orchestration_state_root
        ).expanduser()
        return SessionStore(runs_root.parent / "sessions")

    def list_sessions(self, *, orchestration_state_root: str | None = None) -> dict:
        """Every stored session id (the operator's index of retained memory)."""

        store = self._governance_session_store(orchestration_state_root)
        session_ids = store.session_ids()
        return {"sessions": session_ids, "count": len(session_ids)}

    def session_status(
        self,
        session_id: str,
        *,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Retention/preference/invalidation status of one stored session.

        This is the read side of stage 13's memory governance: which turns are
        still retained, which were invalidated by a superseded definition version,
        and which user-scoped preferences the session carries.
        """

        memory = self._governance_session_store(orchestration_state_root).load(
            session_id
        )
        if memory is None:
            return {"session_id": session_id, "found": False}
        return {
            "session_id": memory.session_id,
            "found": True,
            "user_id": memory.user_id,
            "domain_id": memory.domain_id,
            "created_at": memory.created_at,
            "updated_at": memory.updated_at,
            "retention_days": memory.retention_days,
            "expires_at": memory.expires_at,
            "turn_count": memory.turn_count,
            "retained_turns": len(memory.history),
            "invalidated_turns": sum(
                1 for turn in memory.history if turn.invalidated
            ),
            "pending_clarifications": len(memory.pending_clarifications),
            "preferences": [
                preference.model_dump(mode="json")
                for preference in memory.preferences
            ],
            "turns": [
                {
                    "turn_number": turn.turn_number,
                    "question": turn.question,
                    "status": turn.status,
                    "created_at": turn.created_at,
                    "invalidated": turn.invalidated,
                    "invalidated_reason": turn.invalidated_reason,
                    "knowledge_versions": [
                        reference.reference() for reference in turn.knowledge_versions
                    ],
                }
                for turn in memory.history
            ],
        }

    def export_session(
        self,
        session_id: str,
        *,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Export one session as JSON-safe data (result rows are never stored)."""

        return self._governance_session_store(orchestration_state_root).export(
            session_id
        )

    def delete_session(
        self,
        session_id: str,
        *,
        turn_range: tuple[int, int] | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Delete one session, or only an inclusive turn range inside it."""

        return self._governance_session_store(orchestration_state_root).delete(
            session_id, turn_range=turn_range
        )

    def expire_sessions(
        self,
        *,
        session_id: str | None = None,
        before: str | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Drop turns outside the retention window.

        With a ``session_id`` only that session is expired; otherwise every stored
        session is, each against its own retention window (or ``before``).
        """

        store = self._governance_session_store(orchestration_state_root)
        if session_id:
            return store.expire(session_id, before=before)
        summary = store.expire_all(before=before)
        return {
            "sessions": summary["sessions"],
            "expired_turns": summary["expired_turns"],
            "details": summary["details"],
        }

    def set_session_preference(
        self,
        session_id: str,
        *,
        user_id: str,
        name: str,
        value: Any = None,
        domain_id: str | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Store one user-scoped preference on a session."""

        stored = self._governance_session_store(
            orchestration_state_root
        ).set_preference(
            session_id,
            UserPreference(
                user_id=user_id, name=name, value=value, domain_id=domain_id
            ),
        )
        return {
            "session_id": session_id,
            "preference": stored.model_dump(mode="json"),
        }

    def session_preferences(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        domain_id: str | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Read a session's preferences, optionally narrowed to one scope."""

        preferences = self._governance_session_store(
            orchestration_state_root
        ).preferences(session_id, user_id=user_id, domain_id=domain_id)
        return {
            "session_id": session_id,
            "count": len(preferences),
            "preferences": [
                preference.model_dump(mode="json") for preference in preferences
            ],
        }

    def revoke_session_preference(
        self,
        session_id: str,
        name: str,
        *,
        user_id: str,
        domain_id: str | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Revoke one preference for its owner only."""

        revoked = self._governance_session_store(
            orchestration_state_root
        ).revoke_preference(
            session_id, name, user_id=user_id, domain_id=domain_id
        )
        return {
            "session_id": session_id,
            "name": name,
            "user_id": user_id,
            "revoked": revoked,
        }

    def invalidate_session_knowledge_version(
        self,
        version_ref: str,
        *,
        session_id: str | None = None,
        reason: str | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        """Mark the turns that used a superseded definition version.

        Without a ``session_id`` every stored session is scanned; the turns that
        actually recorded the version are flagged (nothing is deleted), so a later
        follow-up does not reuse a stale metric formula.
        """

        return self._governance_session_store(
            orchestration_state_root
        ).invalidate_version(version_ref, session_id=session_id, reason=reason)

    def _prepare(
        self,
        question: str,
        options: AgentOptions,
        *,
        config: Config | None = None,
    ) -> "_PreparedRun":
        """Validate one request and resolve its governed paths.

        This is the *shared* pre-flight of every entry point: the synchronous
        ``ask``/``plan`` path and the streaming path both run it, so a request the
        API refuses with 4xx is refused identically by ``/ask/stream`` instead of
        being accepted with HTTP 200 and reported later as a failed terminal
        event (M6). It performs the question check, the option checks, the
        transport allowlist, the database existence check and the semantic-layer
        gate — everything that must happen before any file or model is touched.
        """

        text = (question or "").strip()
        if not text:
            raise ValueError("question must be non-empty")
        options.validate()
        config = config or self.config_loader(
            provider_override=options.model_provider,
            model_override=options.model,
        )
        options.validate_for_config(config)
        domain_context: DomainContext | None = None
        if options.domain_id is not None:
            domain_context = DomainResolver.from_config(config).resolve(
                options.domain_id.strip()
            )
            options = self._apply_domain_paths(options, domain_context)
        if options.entrypoint in NETWORK_ENTRYPOINTS:
            validate_transport_options(config, options)
        database = options.database or config.database_path
        path = Path(database).expanduser()
        if not path.is_file():
            raise ValueError(f"SQLite database does not exist: {database}")
        semantic_model_path = discover_semantic_model(
            path,
            options.semantic_model_path or config.semantic_model_path,
        )
        if (
            config.require_semantic_model
            and not options.allow_schema_only
            and semantic_model_path is None
        ):
            suggested_output = path.resolve().with_suffix(".semantic.yml")
            raise ValueError(
                "A validated semantic layer is required before this database can "
                "be queried. Build one with: "
                f"python scripts/build_semantic_model.py --database {path.resolve()} "
                f"--output {suggested_output}; review the generated draft/report, "
                "then retry. Use allow_schema_only only for explicit diagnostics."
            )
        options = replace(options, semantic_model_path=semantic_model_path)
        return _PreparedRun(
            question=text,
            options=options,
            config=config,
            database_path=path,
            domain_context=domain_context,
        )

    def _run(
        self,
        question: str,
        options: AgentOptions,
        *,
        plan_only: bool,
        event_emitter: EventEmitter | None = None,
        cancel_check: Callable[[], bool] | None = None,
        prepared: "_PreparedRun | None" = None,
    ) -> dict:
        prepared = prepared or self._prepare(question, options)
        question = prepared.question
        options = prepared.options
        config = prepared.config
        path = prepared.database_path
        domain_context = prepared.domain_context

        original_question = question
        session_store = None
        session_memory = None
        followup_reason = None
        rewritten_question = None
        previous_request: AnalysisRequest | None = None
        if options.session_id or options.new_session:
            session_store = self._session_store(options, config)
            if options.new_session:
                session_memory = session_store.create()
            elif options.reset_session:
                session_memory = session_store.reset(options.session_id or "")
            else:
                session_memory = session_store.load_or_create(options.session_id or "")
            previous_request = _last_analysis_request(session_memory)
            rewrite = ProductAnalystAgent.rewrite_followup(question, session_memory)
            if bool(rewrite["is_followup"]):
                question = str(rewrite["question"])
                rewritten_question = question
                followup_reason = str(rewrite["reason"])

        decision = self.entry_router.route(question, options.entrypoint)
        run_id = options.run_id or new_run_id()
        effective_complex = (
            options.complexity_mode == "complex"
            or (
                options.complexity_mode == "auto"
                and decision.complexity_profile == "complex"
            )
        )
        runner_kwargs = {
            "llm_factory": self.llm_factory,
            "selected_skills": options.skills,
            "date_llm_fallback": options.date_llm_fallback,
            "plan_mode": options.plan_mode,
            "auto_approve_plan": options.auto_approve_plan,
            "plan_approver": options.plan_approver,
            "plan_presenter": options.plan_presenter,
            "max_retries": options.max_retries,
            "history_top_k": options.history_top_k,
            "enable_vector_kb": options.enable_vector_kb,
            "vector_top_k": options.vector_top_k,
            "visualize": options.visualize,
            "chart_output_dir": options.chart_output_dir,
            "debug_prompts": options.debug_prompts,
            "show_run_summary": options.show_run_summary,
            "plan_only": plan_only,
            "semantic_model_path": options.semantic_model_path,
            "subject_tree_enabled": options.subject_tree_enabled,
            "subject_tree_path": options.subject_tree_path,
            "subject": options.subject,
            "default_subject": options.default_subject,
            "sql_policy_path": options.sql_policy_path,
            "history_domain_id": (
                domain_context.domain_id if domain_context else None
            ),
            "history_data_version": (
                domain_context.data_version if domain_context else None
            ),
            # Governed retrieval scope (step 13): the run filters knowledge
            # retrieval by the domain it is bound to, so another domain's
            # definitions and examples can never enter this run's context.
            "retrieval_scope": _retrieval_scope(domain_context),
            "tool_loop_enabled": options.tool_loop_enabled or effective_complex,
            "tool_loop_max_rounds": options.tool_loop_max_rounds,
            "tool_loop_timeout_seconds": options.tool_loop_timeout_seconds,
            "tool_loop_preview_limit": options.tool_loop_preview_limit,
            # Complexity routing deliberately does NOT raise the candidate count.
            #
            # It used to: a "complex" request set parallel_candidates to 2, while
            # also enabling the tool loop. A controlled ablation over identical
            # inputs (--parallel-candidates 1 vs 2 in scripts/evaluate_sql.py) found
            # 20 paired cases with ZERO accuracy difference, ~20% higher p50
            # latency, and a few percent more tokens. The candidate winner did
            # execute faster in the engine (0.57ms vs 1.44ms), but the engine is
            # ~0.03% of an end-to-end run dominated by model latency, so that
            # gain is ~1ms against ~1550ms of extra work.
            #
            # The implicit boost was also frequently wasted outright: the tool
            # loop runs first, and when it produces SQL the candidate node is
            # skipped entirely (workflow.py), so ~0.9 avg tool-call rounds per
            # complex run usually bypassed candidates that had already been paid
            # for by the complexity decision.
            #
            # Candidates remain available as an explicit, opt-in choice for a
            # caller that has measured a benefit (CLI --parallel-candidates,
            # AnalyzeRequest, MCP, or AgentOptions). Details:
            # docs/evaluation_baselines.md, "Multi-candidate ablation".
            "parallel_candidates": options.parallel_candidates,
            "parallel_max_preview": options.parallel_max_preview,
            "parallel_preview_limit": options.parallel_preview_limit,
            "parallel_preview_timeout_seconds": options.parallel_preview_timeout_seconds,
            "selector_weights": options.selector_weights,
            "event_emitter": event_emitter,
            "report_requested": config.report_enabled and (
                options.report or decision.task_type == "build_report"
            ),
            "report_output_dir": options.report_output_dir,
            "report_max_rows": options.report_max_rows,
            "report_max_charts": options.report_max_charts,
            "cancel_check": cancel_check,
        }
        runner_kwargs["run_id_factory"] = lambda: run_id
        task = SqlTask(question=question, database_path=str(path))
        if decision.task_type == "troubleshoot_sql":
            runner_kwargs["initial_sql"] = (
                options.provided_sql or SQLReviewAgent.extract_sql(question) or None
            )
        span_recorder = get_span_recorder(run_id) or start_span_recorder(run_id)
        orchestrator = OrchestratorAgent(
            IdentifiedStateStore(
                options.orchestration_state_root or config.orchestration_state_root,
                event_emitter,
                span_recorder=span_recorder,
            )
        )

        def run_workflow(analysis_hook, candidate_hook, completion_hook):
            runner = self.runner_factory(
                config,
                **runner_kwargs,
                analysis_hook=analysis_hook,
                candidate_hook=candidate_hook,
                completion_hook=completion_hook,
            )
            return runner.run(task)

        direct_run = None
        if decision.task_type == "metadata_query":
            direct_run = lambda state, orch: self.direct_tasks.metadata(
                state, orch, task, config, options, run_id
            )
        elif decision.task_type == "sql_review":
            direct_run = lambda state, orch: self.direct_tasks.sql_review(
                state, orch, task, config, options, run_id
            )

        try:
            output = orchestrator.run(
                run_id=run_id,
                decision=decision,
                workflow=run_workflow,
                direct_run=direct_run,
                plan_only=plan_only,
                session_memory=session_memory,
                session_store=session_store,
                original_question=original_question if session_memory else None,
                rewritten_question=rewritten_question,
                followup_reason=followup_reason,
            )
        finally:
            # `WorkflowRunner.run` closes the recorder it uses, but the direct-run
            # paths (`metadata_query`, `sql_review`) never enter the runner, so
            # their recorder stayed open for the life of the process. Once the
            # registry stops evicting live recorders (M2) that leak is permanent:
            # close it here, on every path, after the summary has been built.
            _close_span_recorder(span_recorder)
        if session_memory is not None and session_store is not None:
            self._record_structured_intent(
                output=output,
                memory=session_memory,
                store=session_store,
                run_id=run_id,
                state_root=Path(
                    options.orchestration_state_root or config.orchestration_state_root
                ).expanduser(),
                original_question=original_question,
                previous_request=previous_request,
                is_followup=bool(rewritten_question),
                # The governed definitions this turn ran against. Recorded here
                # because the orchestrator records the turn without them, which
                # left `SessionStore.invalidate_version` with nothing to match
                # (M7).
                semantic_model_path=options.semantic_model_path,
            )
        if domain_context is not None:
            output["domain"] = {
                **domain_context.to_public_dict(),
                "resolved_by": "registry",
            }
        return output

    def _record_structured_intent(
        self,
        *,
        output: dict[str, Any],
        memory: SessionMemory,
        store: SessionStore,
        run_id: str,
        state_root: Path,
        original_question: str,
        previous_request: AnalysisRequest | None,
        is_followup: bool,
        semantic_model_path: str | None = None,
    ) -> None:
        """Store the typed analysis intent and clarifications on the session turn.

        The orchestrator records the turn before the run result is returned, so
        this method annotates that turn: the typed request (from the run's
        ``analysis_request`` artifact, or from the rule-based follow-up patch
        when no artifact is reachable), the clarifications the analysis stage
        raised, the pending clarification state a later turn resumes from, and the
        definition versions the turn relied on (what ``invalidate_version``
        matches against). Best effort by design: a session annotation must never
        fail a run.
        """

        try:
            if not memory.history:
                return
            artifacts_dir = Path(
                str(
                    (output.get("agent_team") or {}).get("artifacts_dir")
                    or (state_root / run_id / "artifacts")
                )
            )
            payload = _load_analysis_payload(artifacts_dir)
            artifact_request = (
                AnalysisRequest.model_validate_artifact(payload) if payload else None
            )
            request = artifact_request or AnalysisRequest()
            # A resumed clarification is the same intent: the previous typed
            # request is the patch base even when the question text is not a
            # recognised follow-up (a blocked turn does not set last_question).
            resuming = bool(memory.pending_clarifications) and previous_request is not None
            if (is_followup or resuming) and previous_request is not None:
                patched, patch_reason = apply_patch(original_question, previous_request)
                if patch_reason or resuming:
                    request = patched
                    if artifact_request is not None:
                        request.clarifications = list(artifact_request.clarifications)
                        request.status = artifact_request.status
                        request.unresolved_questions = (
                            list(artifact_request.unresolved_questions)
                            or request.unresolved_questions
                        )
                        request.assumptions = (
                            list(artifact_request.assumptions) or request.assumptions
                        )
            turn = memory.history[-1]
            turn.analysis_request = request.model_dump(mode="json")
            turn.needs_clarification = [dict(item) for item in request.clarifications]
            # The definitions this turn relied on, so a later
            # `invalidate_version` can mark exactly the affected turns (M7). The
            # orchestrator now records its own references when it writes the turn
            # (including the governed knowledge it retrieved), so this is a
            # compensation: the union keeps whatever the writer recorded and adds
            # only what it could not see, never a duplicate of the same version.
            turn.knowledge_versions = merge_version_refs(
                turn.knowledge_versions,
                knowledge_version_refs(semantic_model_path, list(request.metric_ids)),
            )
            memory.pending_clarifications = (
                _high_severity(request.clarifications)
                if turn.status == "blocked" or request.is_blocked
                else []
            )
            store.save(memory)
            session_output = output.get("session")
            if isinstance(session_output, dict):
                session_output["analysis_request"] = turn.analysis_request
                session_output["needs_clarification"] = list(turn.needs_clarification)
                session_output["pending_clarifications"] = list(
                    memory.pending_clarifications
                )
                session_output["knowledge_versions"] = [
                    reference.reference() for reference in turn.knowledge_versions
                ]
        except Exception as exc:
            # Session annotation is advisory; the run result stays authoritative.
            LOGGER.warning("structured intent was not recorded: %s", exc)
            return

    @staticmethod
    def _apply_domain_paths(
        options: AgentOptions, context: DomainContext
    ) -> AgentOptions:
        """Bind request paths to the resolved data domain.

        A published domain owns its database, semantic model, and SQL policy, so
        no caller may select a looser policy by shipping an explicit path.
        Network entrypoints therefore reject explicit paths that disagree with
        the domain; the local CLI keeps its controlled-path compatibility, where
        the explicit option wins for that path only (the run still records and
        carries the domain identity it was resolved against).
        """
        resolved: dict[str, str | None] = {}
        conflicts: list[str] = []
        for label, attribute, domain_value in (
            ("database", "database", context.database_path),
            (
                "semantic_model_path",
                "semantic_model_path",
                context.semantic_model_path,
            ),
            ("sql_policy_path", "sql_policy_path", context.sql_policy_path),
        ):
            explicit = getattr(options, attribute)
            if explicit is None or _same_path(explicit, domain_value):
                resolved[attribute] = domain_value
                continue
            conflicts.append(label)
            resolved[attribute] = explicit
        if conflicts and options.entrypoint in NETWORK_ENTRYPOINTS:
            # The message names only the offending option, never the server-side
            # domain path, so a network caller cannot probe registry contents.
            raise ValueError(
                f"domain_id {context.domain_id!r} conflicts with explicit path "
                f"options: {', '.join(conflicts)}; omit them and let the domain "
                "registry supply the controlled paths"
            )
        return replace(options, **resolved)

    @staticmethod
    def _session_store(options: AgentOptions, config: Config) -> SessionStore:
        runs_root = Path(
            options.orchestration_state_root or config.orchestration_state_root
        ).expanduser()
        return SessionStore(runs_root.parent / "sessions")
