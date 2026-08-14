"""FastAPI routes that delegate every operation to AgentService."""

from __future__ import annotations

from typing import Callable

from queryforge import __version__
from queryforge.interfaces.api.schemas import AskRequest, GatewayWebhookRequest
from queryforge.interfaces.gateway import GatewayAdapter
from queryforge.interfaces.transport_security import request_api_key_matches
from queryforge.application import AgentService

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


def create_app(service: AgentService | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    except ImportError as exc:
        raise APIUnavailableError(
            "FastAPI server is unavailable. Install optional dependencies with: "
            "pip install -r requirements-server.txt"
        ) from exc

    agent_service = service or AgentService()
    gateway = GatewayAdapter(agent_service)
    try:
        config = agent_service.config_loader()
    except Exception:
        # The application must still boot without a resolvable model config;
        # transport hardening simply stays disabled in that case.
        config = None
    api_key_configured = bool(config and config.api_key)

    app = FastAPI(
        title="QueryForge API",
        version=__version__,
        description="Thin REST/Gateway transports over the shared AgentService.",
    )

    @app.middleware("http")
    async def transport_auth(request: Request, call_next):
        if (
            api_key_configured
            and request.url.path not in _PUBLIC_PATHS
            and not request_api_key_matches(
                config,
                request.headers.get("authorization"),
                request.headers.get("x-api-key"),
            )
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

    @app.post("/ask/stream")
    def ask_stream(request: AskRequest, http: Request):
        try:
            event_stream = agent_service.stream(
                request.question,
                request.to_options(entrypoint="api_stream"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def sse_events():
            try:
                for event in event_stream:
                    if await_disconnect(http):
                        break
                    yield f"data: {event.model_dump_json()}\n\n"
            finally:
                event_stream.cancel()

        return StreamingResponse(
            sse_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/plan")
    def plan(request: AskRequest):
        return call(lambda: agent_service.plan(request.question, request.to_options()))

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

    return app


def await_disconnect(http) -> bool:
    """Detect a dropped SSE client when the ASGI layer supports it."""
    detector = getattr(http, "is_disconnected", None)
    if detector is None:
        return False
    try:
        return bool(detector())
    except RuntimeError:
        return False
