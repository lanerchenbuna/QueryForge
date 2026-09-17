"""FastAPI routes that delegate every operation to AgentService."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Callable

from queryforge import __version__
from queryforge.core.config import Config
from queryforge.interfaces.api.schemas import (
    AnalyzeRequest,
    AskRequest,
    GatewayWebhookRequest,
    SessionExpireRequest,
    SessionPreferenceRequest,
    SessionVersionInvalidationRequest,
)
from queryforge.interfaces.gateway import GatewayAdapter
from queryforge.interfaces.transport_security import request_api_key_matches
from queryforge.application import AgentService
from queryforge.application.analysis_planner import AnalysisPlannerService
from queryforge.workflow.event_emitter import PROTOCOL_VERSION

LOGGER = logging.getLogger("queryforge.api")

#: How long the SSE generator blocks on the event stream before re-checking
#: whether the client is still connected. Short enough to notice a disconnect
#: promptly, long enough not to spin.
SSE_POLL_SECONDS = 0.25

# Security headers applied to served HTML reports (see report_generator.py,
# which escapes embedded script JSON). ``script-src 'unsafe-inline'`` is
# required by the inline vegaEmbed bootstrap; the ``</script>`` breakout itself
# is closed by escaping, and nosniff prevents content-type sniffing attacks.
REPORT_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "font-src 'self' data:"
    ),
    "Cache-Control": "no-store",
}

_PUBLIC_PATHS = frozenset({"/health", "/openapi.json", "/docs", "/redoc"})


class APIUnavailableError(RuntimeError):
    pass


def load_transport_config(service: object) -> tuple[Config | None, str | None]:
    """Load the configuration the transport authorization gate runs against.

    Returns ``(config, None)`` when the configuration loaded, and
    ``(None, reason)`` when a *known* config loader failed. A service that exposes
    no config loader at all is a transport-level double rather than a deployment:
    it reports ``(None, None)``, because there is no configuration to authorize
    against, and every deployment service (``AgentService``) always has one.
    """

    loader = getattr(service, "config_loader", None)
    if not callable(loader):
        return None, None
    try:
        return loader(), None
    except Exception as exc:
        return None, str(exc) or exc.__class__.__name__


def create_app(service: AgentService | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    except ImportError as exc:
        raise APIUnavailableError(
            "FastAPI server is unavailable. Install optional dependencies with: "
            "pip install -r requirements-server.txt"
        ) from exc

    # FastAPI resolves postponed endpoint annotations against module globals.
    globals()["Request"] = Request
    agent_service = service or AgentService()
    gateway = GatewayAdapter(agent_service)
    config, config_error = load_transport_config(agent_service)
    api_key_configured = bool(config and config.api_key)

    app = FastAPI(
        title="QueryForge API",
        version=__version__,
        description="Thin REST/Gateway transports over the shared AgentService.",
    )

    @app.middleware("http")
    async def transport_auth(request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        if config_error is not None:
            # Fail closed (M4): the deployment's configuration could not be read,
            # so whether an API key is required is unknown. Serving the route
            # anyway is exactly how a secured deployment became anonymous; the
            # only safe answer is to refuse until the configuration loads again.
            LOGGER.error(
                "transport_config_unavailable path=%s error=%s",
                request.url.path,
                config_error,
            )
            return JSONResponse(
                {
                    "detail": (
                        "QueryForge configuration could not be loaded, so this "
                        "route cannot be authorized. Fix the configuration "
                        f"(for example models.yml) and retry: {config_error}"
                    )
                },
                status_code=503,
            )
        if api_key_configured and not request_api_key_matches(
            config,
            request.headers.get("authorization"),
            request.headers.get("x-api-key"),
        ):
            return JSONResponse(
                {"detail": "Authentication required."}, status_code=401
            )
        return await call_next(request)

    def call(operation: Callable[[], dict | list]):
        try:
            return operation()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/health")
    def health():
        return agent_service.health()

    @app.get("/models")
    def models():
        return call(agent_service.list_models)

    @app.get("/skills")
    def skills():
        return call(agent_service.list_skills)

    @app.post("/ask")
    def ask(request: AskRequest):
        return call(lambda: agent_service.ask(request.question, request.to_options()))

    @app.post("/analyze")
    def analyze(request: AnalyzeRequest):
        """Planned multi-step analysis with typed evidence and budgets.

        A structured clarification/blocked outcome is reported as HTTP 400 with
        the full result body (there is no partial success to hide), an invalid
        request is a 400 via ValueError, and anything unexpected is a 422. The
        route names its entrypoint (``api``) so the planner applies the same
        transport path allowlist as ``/ask``.
        """

        planner = AnalysisPlannerService(config_loader=agent_service.config_loader)
        from queryforge.application.options import AgentOptions
        from queryforge.interfaces.transport_security import validate_transport_options
        call(lambda: validate_transport_options(agent_service.config_loader(), AgentOptions(
            database=request.database, semantic_model_path=request.semantic_model_path,
            sql_policy_path=request.sql_policy_path, entrypoint="api")))
        result = call(lambda: planner.analyze(request.question, **request.to_kwargs(entrypoint="api")))
        if result.get("status") in {"needs_clarification", "blocked"}:
            raise HTTPException(status_code=400, detail=result)
        return result

    @app.get("/analyze/runs/{run_id}")
    def analyze_run_status(run_id: str):
        """Status of one durable analysis run: steps, budget, terminal outcome."""

        planner = AnalysisPlannerService(config_loader=agent_service.config_loader)
        return call(lambda: planner.run_status(run_id).model_dump(mode="json"))

    @app.post("/analyze/runs/{run_id}/cancel")
    def cancel_analyze_run(run_id: str, reason: str = "cancelled by client"):
        """Persist cancellation for one durable analysis run.

        ``cancelled`` reports whether this call won the race: ``false`` means the
        run had already ended and its single terminal outcome was left untouched.
        The run's status afterwards is returned so the caller never has to guess
        what was recorded.
        """

        planner = AnalysisPlannerService(config_loader=agent_service.config_loader)

        def cancel() -> dict:
            cancelled = planner.cancel_run(run_id, reason=reason)
            return {
                "run_id": run_id,
                "cancelled": cancelled,
                "reason": reason,
                "status": planner.run_status(run_id).model_dump(mode="json"),
            }

        return call(cancel)

    @app.post("/ask/stream")
    async def ask_stream(request: AskRequest, http: Request):
        """Stream one run over SSE using the versioned event protocol.

        Request contract (M6): this route refuses exactly what ``/ask`` refuses and
        with the same HTTP 4xx — question, options, transport allowlist, database
        existence, domain and semantic-gate checks all run *before* the stream
        exists, so a 4xx here carries the same meaning as a 4xx there and an SSE
        client is never handed ``final_result status=failed`` for a request the
        synchronous route rejects. Once the response has started, the single
        terminal ``final_result`` event carries the run's outcome
        (``success``/``failed``/``cancelled``/``blocked``).

        The generator is async so the real ``Request.is_disconnected`` coroutine
        can be awaited (calling it from a sync generator returned a coroutine
        object, which is always truthy and leaked an un-awaited coroutine
        warning). Blocking on the workflow queue happens in a worker thread with
        a bounded timeout, so a disconnect is noticed while the run continues.
        """

        try:
            event_stream = agent_service.stream(
                request.question,
                request.to_options(entrypoint="api_stream"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return StreamingResponse(
            sse_event_generator(event_stream, http),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-QueryForge-Event-Protocol": PROTOCOL_VERSION,
            },
        )

    @app.post("/plan")
    def plan(request: AskRequest):
        return call(lambda: agent_service.plan(request.question, request.to_options()))

    @app.post("/domains/{domain_id}/publish")
    async def publish_domain(domain_id: str, http: Request):
        """Build and publish one governed data-domain version from uploads."""
        try:
            from starlette.datastructures import UploadFile
        except ImportError as exc:  # pragma: no cover - fastapi is present here
            raise APIUnavailableError(
                "FastAPI server is unavailable. Install optional dependencies with: "
                "pip install -r requirements-server.txt"
            ) from exc
        try:
            from queryforge.application.publish_service import (
                PublishError,
                PublishService,
            )

            form = await http.form()
            raw_contract = form.get("contract")
            contract = (
                json.loads(raw_contract)
                if isinstance(raw_contract, str)
                else raw_contract
            )
            uploads: list[tuple[str, bytes]] = []
            for item in form.getlist("files"):
                if isinstance(item, UploadFile):
                    uploads.append(
                        (item.filename or "upload", await item.read())
                    )
            result = PublishService(agent_service.config_loader).publish(
                domain_id=domain_id,
                files=uploads,
                contract=contract,
            )
            return {"status": "published", "domain": result.to_dict()}
        except (PublishError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/report/{run_id}")
    def report(run_id: str):
        try:
            return FileResponse(
                agent_service.report_path(run_id),
                media_type="text/html",
                filename=f"{run_id}.html",
                headers=REPORT_SECURITY_HEADERS,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/gateway/webhook")
    def gateway_webhook(request: GatewayWebhookRequest):
        return call(
            lambda: gateway.handle(
                user_id=request.user_id,
                channel=request.channel,
                text=request.text,
            )
        )

    # ------------------------------------------ conversation memory governance
    # Stage 13's session lifecycle (retention, deletion, export, preference scope,
    # definition-version invalidation) existed in ``SessionStore`` with no caller
    # and no route, so none of it was reachable from a deployment (M7). Every route
    # below goes through the same transport middleware as the rest of the API.

    @app.get("/sessions/{session_id}")
    def session_status(session_id: str):
        """Retention, preference and invalidation status of one session."""

        status = call(lambda: agent_service.session_status(session_id))
        if not status.get("found"):
            raise HTTPException(status_code=404, detail=f"session {session_id!r} not found")
        return status

    @app.get("/sessions/{session_id}/export")
    def session_export(session_id: str):
        """Export one session (result rows are never stored, so never exported)."""

        exported = call(lambda: agent_service.export_session(session_id))
        if not exported.get("found"):
            raise HTTPException(status_code=404, detail=f"session {session_id!r} not found")
        return exported

    @app.delete("/sessions/{session_id}")
    def session_delete(
        session_id: str, turn_start: int | None = None, turn_end: int | None = None
    ):
        """Delete a whole session, or only the inclusive ``turn_start..turn_end``."""

        if (turn_start is None) != (turn_end is None):
            raise HTTPException(
                status_code=400,
                detail="turn_start and turn_end must be provided together",
            )
        turn_range = None if turn_start is None else (int(turn_start), int(turn_end))
        deleted = call(
            lambda: agent_service.delete_session(session_id, turn_range=turn_range)
        )
        if deleted.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=f"session {session_id!r} not found")
        return deleted

    @app.post("/sessions/expire")
    def sessions_expire(request: SessionExpireRequest):
        """Drop turns outside the retention window (all sessions when unscoped)."""

        return call(
            lambda: agent_service.expire_sessions(
                session_id=request.session_id, before=request.before
            )
        )

    @app.get("/sessions/{session_id}/preferences")
    def session_preferences(
        session_id: str, user_id: str | None = None, domain_id: str | None = None
    ):
        """List a session's user-scoped preferences."""

        return call(
            lambda: agent_service.session_preferences(
                session_id, user_id=user_id, domain_id=domain_id
            )
        )

    @app.post("/sessions/{session_id}/preferences")
    def session_set_preference(session_id: str, request: SessionPreferenceRequest):
        """Store one user-scoped preference on a session."""

        return call(
            lambda: agent_service.set_session_preference(
                session_id,
                user_id=request.user_id,
                name=request.name,
                value=request.value,
                domain_id=request.domain_id,
            )
        )

    @app.delete("/sessions/{session_id}/preferences/{name}")
    def session_revoke_preference(
        session_id: str, name: str, user_id: str, domain_id: str | None = None
    ):
        """Revoke one preference; only its owner can (``user_id`` is required)."""

        return call(
            lambda: agent_service.revoke_session_preference(
                session_id, name, user_id=user_id, domain_id=domain_id
            )
        )

    @app.post("/sessions/invalidate-version")
    def sessions_invalidate_version(request: SessionVersionInvalidationRequest):
        """Mark the turns that recorded a superseded definition version."""

        return call(
            lambda: agent_service.invalidate_session_knowledge_version(
                request.version_ref,
                session_id=request.session_id,
                reason=request.reason,
            )
        )

    return app


async def sse_event_generator(
    event_stream, http, *, poll_seconds: float = SSE_POLL_SECONDS
):
    """Yield `text/event-stream` frames for one run until it terminates.

    Kept as a module-level async generator (instead of a closure inside the
    route) so the disconnect/terminal contract can be exercised without an ASGI
    server. Guarantees:

    * the client is asked about disconnects with a real ``await``;
    * exactly one terminal frame is written and iteration stops after it;
    * a disconnect cancels the run, which propagates to the SQL boundary.
    """

    terminal_sent = False
    try:
        while True:
            if await is_disconnected(http):
                LOGGER.info(
                    "sse_client_disconnected run_id=%s", event_stream.run_id
                )
                event_stream.cancel()
                break
            event = await asyncio.to_thread(
                event_stream.next_event, poll_seconds
            )
            if event is None:
                if event_stream.finished:
                    break
                continue
            yield f"data: {event.model_dump_json()}\n\n"
            if event.event_type == "final_result":
                # The protocol allows exactly one terminal event and nothing
                # after it: stop iterating here.
                terminal_sent = True
                break
        if not terminal_sent:
            LOGGER.warning(
                "sse_stream_without_terminal run_id=%s protocol_violation=%s",
                event_stream.run_id,
                event_stream.protocol_violation,
            )
    finally:
        # Cancellation reaches the workflow worker (and, through the cancel flag,
        # the SQL boundary) as soon as the client is gone.
        event_stream.cancel()


async def is_disconnected(http) -> bool:
    """Await the ASGI layer's disconnect detector when it supports one.

    ``starlette``'s ``Request.is_disconnected`` is a coroutine; awaiting it is
    what makes detection real. A transport without the detector (or with a sync
    one) is treated as connected, which keeps compatibility without leaking an
    un-awaited coroutine.
    """

    detector = getattr(http, "is_disconnected", None)
    if detector is None:
        return False
    try:
        result = detector()
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)
    except RuntimeError:
        # No running event loop (or a closed ASGI scope): assume connected.
        return False


def await_disconnect(http) -> bool:
    """Synchronous best-effort wrapper kept for non-async callers.

    An async detector cannot be awaited here; the coroutine is closed explicitly
    so a sync caller never leaves an un-awaited coroutine warning behind. Use
    :func:`is_disconnected` from async code.
    """

    detector = getattr(http, "is_disconnected", None)
    if detector is None:
        return False
    try:
        result = detector()
    except RuntimeError:
        return False
    if inspect.iscoroutine(result):
        result.close()
        return False
    if inspect.isawaitable(result):
        return False
    return bool(result)
