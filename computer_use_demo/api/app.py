"""FastAPI app exposing session management and SSE streaming APIs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .database import Database
from .docker_runtime import DockerSessionRuntime
from .events import RunEventBus
from .schemas import (
    HealthResponse,
    MessageCreateRequest,
    MessageListResponse,
    RunResponse,
    SessionCreateRequest,
    SessionListResponse,
    SessionResponse,
    SubmitMessageResponse,
)
from .service import SessionService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    database = Database(settings)
    await database.init_models()

    event_bus = RunEventBus()
    runtime = DockerSessionRuntime(settings)

    service = SessionService(
        session_factory=database.session_factory,
        runtime=runtime,
        event_bus=event_bus,
        settings=settings,
    )

    app.state.database = database
    app.state.service = service
    yield

    await service.shutdown()
    await database.close()


app = FastAPI(
    title="Computer Use Session API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_service(request: Request) -> SessionService:
    return request.app.state.service


ServiceDep = Annotated[SessionService, Depends(get_service)]


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/api/sessions", response_model=SessionResponse)
async def create_session(
    payload: SessionCreateRequest,
    request: Request,
    service: ServiceDep,
) -> SessionResponse:
    return await service.create_session(payload, request)


@app.get("/api/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    service: ServiceDep,
) -> SessionListResponse:
    return await service.list_sessions(request)


@app.get("/api/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    request: Request,
    service: ServiceDep,
) -> SessionResponse:
    return await service.get_session(session_id, request)


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str,
    service: ServiceDep,
) -> dict[str, str]:
    await service.delete_session(session_id)
    return {"status": "deleted"}


@app.get("/api/sessions/{session_id}/messages", response_model=MessageListResponse)
async def list_messages(
    session_id: str,
    service: ServiceDep,
) -> MessageListResponse:
    return await service.list_messages(session_id)


@app.post(
    "/api/sessions/{session_id}/messages",
    response_model=SubmitMessageResponse,
)
async def submit_message(
    session_id: str,
    payload: MessageCreateRequest,
    service: ServiceDep,
) -> SubmitMessageResponse:
    return await service.submit_message(session_id, payload)


@app.get("/api/sessions/{session_id}/runs/{run_id}", response_model=RunResponse)
async def get_run(
    session_id: str,
    run_id: str,
    service: ServiceDep,
) -> RunResponse:
    return await service.get_run(session_id, run_id)


@app.get("/api/sessions/{session_id}/runs/{run_id}/events")
async def stream_events(
    session_id: str,
    run_id: str,
    service: ServiceDep,
) -> StreamingResponse:
    queue, backlog, was_closed = await service.subscribe_events(session_id, run_id)

    async def event_generator() -> AsyncIterator[str]:
        try:
            for event in backlog:
                yield _format_sse(event)

            if was_closed:
                yield _format_sse({"type": "stream.closed", "run_id": run_id})
                return

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue

                if item is None:
                    break
                yield _format_sse(item)

            yield _format_sse({"type": "stream.closed", "run_id": run_id})
        finally:
            await service.unsubscribe_events(run_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _format_sse(data: dict[str, object]) -> str:
    return f"data: {json.dumps(data)}\n\n"
