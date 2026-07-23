"""FastAPI routes that delegate every operation to AgentService."""

from __future__ import annotations

import json
from typing import Callable

from queryforge import __version__
from queryforge.interfaces.api.schemas import AskRequest, GatewayWebhookRequest
from queryforge.interfaces.gateway import GatewayAdapter
from queryforge.application import AgentService


class APIUnavailableError(RuntimeError):
    pass


def create_app(service: AgentService | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, StreamingResponse
    except ImportError as exc:
        raise APIUnavailableError(
            "FastAPI server is unavailable. Install optional dependencies with: "
            "pip install -r requirements-server.txt"
        ) from exc

    agent_service = service or AgentService()
    gateway = GatewayAdapter(agent_service)
    app = FastAPI(
        title="QueryForge API",
        version=__version__,
        description="Thin REST/Gateway transports over the shared AgentService.",
    )

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
    def ask_stream(request: AskRequest):
        try:
            event_stream = agent_service.stream(
                request.question,
                request.to_options(entrypoint="api_stream"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def sse_events():
            for event in event_stream:
                yield f"data: {event.model_dump_json()}\n\n"

        return StreamingResponse(sse_events(), media_type="text/event-stream")

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
