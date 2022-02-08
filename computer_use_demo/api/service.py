"""Session orchestration: persistence, execution, and event streaming."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .docker_runtime import DockerSessionRuntime
from .events import RunEventBus
from .models import MessageRecord, RunRecord, SessionRecord
from .schemas import (
    MessageCreateRequest,
    MessageListResponse,
    MessageResponse,
    RunResponse,
    SessionCreateRequest,
    SessionListResponse,
    SessionResponse,
    SubmitMessageResponse,
)


class SessionService:
    """Business logic for session lifecycle and concurrent run execution."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runtime: DockerSessionRuntime,
        event_bus: RunEventBus,
        settings: Settings,
    ):
        self._session_factory = session_factory
        self._runtime = runtime
        self._event_bus = event_bus
        self._settings = settings

        self._locks: dict[str, asyncio.Lock] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}

    async def shutdown(self) -> None:
        for task in list(self._run_tasks.values()):
            task.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks.values(), return_exceptions=True)

    async def create_session(
        self, data: SessionCreateRequest, request: Request
    ) -> SessionResponse:
        session_id = str(uuid4())
        runtime_handle = await self._runtime.create_runtime(session_id)

        record = SessionRecord(
            id=session_id,
            title=data.title or "New Agent Task",
            status="active",
            provider=data.provider or self._settings.api_provider,
            model=data.model or self._settings.default_model,
            tool_version=data.tool_version or self._settings.default_tool_version,
            system_prompt_suffix=data.system_prompt_suffix,
            only_n_most_recent_images=(
                data.only_n_most_recent_images
                if data.only_n_most_recent_images is not None
                else self._settings.default_recent_images
            ),
            max_tokens=(
                data.max_tokens
                if data.max_tokens is not None
                else self._settings.default_max_tokens
            ),
            thinking_budget=data.thinking_budget,
            token_efficient_tools_beta=data.token_efficient_tools_beta,
            container_id=runtime_handle.container_id,
            novnc_host_port=runtime_handle.novnc_host_port,
            vnc_host_port=runtime_handle.vnc_host_port,
        )

        try:
            async with self._session_factory() as db:
                db.add(record)
                await db.commit()
                await db.refresh(record)
        except Exception:
            await self._runtime.remove_runtime(runtime_handle.container_id)
            raise

        return self._to_session_response(record, request)

    async def list_sessions(self, request: Request) -> SessionListResponse:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(SessionRecord).order_by(SessionRecord.created_at.desc())
            )
            sessions = [self._to_session_response(item, request) for item in rows]
        return SessionListResponse(sessions=sessions)

    async def get_session(self, session_id: str, request: Request) -> SessionResponse:
        async with self._session_factory() as db:
            record = await db.get(SessionRecord, session_id)
            if not record:
                raise HTTPException(status_code=404, detail="Session not found")
        return self._to_session_response(record, request)

    async def delete_session(self, session_id: str) -> None:
        async with self._session_factory() as db:
            record = await db.get(SessionRecord, session_id)
            if not record:
                raise HTTPException(status_code=404, detail="Session not found")

            record.status = "deleted"
            container_id = record.container_id
            await db.delete(record)
            await db.commit()

        await self._runtime.remove_runtime(container_id)

    async def list_messages(self, session_id: str) -> MessageListResponse:
        async with self._session_factory() as db:
            await self._require_session(db, session_id)
            rows = await db.scalars(
                select(MessageRecord)
                .where(MessageRecord.session_id == session_id)
                .order_by(MessageRecord.created_at.asc())
            )
            messages = [self._to_message_response(item) for item in rows]
        return MessageListResponse(messages=messages)

    async def submit_message(
        self, session_id: str, payload: MessageCreateRequest
    ) -> SubmitMessageResponse:
        async with self._session_factory() as db:
            session_record = await self._require_session(db, session_id)

            if (
                session_record.provider == "anthropic"
                and not self._settings.anthropic_api_key
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "ANTHROPIC_API_KEY is not configured. Export the key and "
                        "restart backend before submitting Anthropic runs."
                    ),
                )

            run = RunRecord(session_id=session_id, status="queued")
            db.add(run)
            await db.flush()

            user_message = MessageRecord(
                session_id=session_id,
                run_id=run.id,
                role="user",
                content=[{"type": "text", "text": payload.text}],
            )
            db.add(user_message)

            await db.commit()
            await db.refresh(run)

        await self._event_bus.create_stream(run.id)

        task = asyncio.create_task(self._execute_run(session_id=session_id, run_id=run.id))
        self._run_tasks[run.id] = task

        def _cleanup(_: asyncio.Task[None]) -> None:
            self._run_tasks.pop(run.id, None)

        task.add_done_callback(_cleanup)

        return SubmitMessageResponse(run=self._to_run_response(run))

    async def get_run(self, session_id: str, run_id: str) -> RunResponse:
        async with self._session_factory() as db:
            await self._require_session(db, session_id)
            run = await db.get(RunRecord, run_id)
            if not run or run.session_id != session_id:
                raise HTTPException(status_code=404, detail="Run not found")
            return self._to_run_response(run)

    async def subscribe_events(
        self, session_id: str, run_id: str
    ) -> tuple[asyncio.Queue[dict[str, Any] | None], list[dict[str, Any]], bool]:
        async with self._session_factory() as db:
            await self._require_session(db, session_id)
            run = await db.get(RunRecord, run_id)
            if not run or run.session_id != session_id:
                raise HTTPException(status_code=404, detail="Run not found")

        queue, backlog, was_closed = await self._event_bus.subscribe(run_id)

        # Event streams are in-memory; after backend restarts, finished runs may have
        # no retained stream state. In that case emit a terminal event and close.
        if not backlog and not was_closed and run.status in {"completed", "failed"}:
            terminal_type = "run.completed" if run.status == "completed" else "run.failed"
            terminal_event: dict[str, Any] = {
                "type": terminal_type,
                "run_id": run_id,
                "session_id": session_id,
                "at": (
                    run.finished_at.isoformat()
                    if run.finished_at is not None
                    else datetime.now(UTC).isoformat()
                ),
            }
            if run.status == "failed" and run.error_text:
                terminal_event["error"] = run.error_text
            backlog = [terminal_event]
            was_closed = True

        return queue, backlog, was_closed

    async def unsubscribe_events(
        self, run_id: str, queue: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        await self._event_bus.unsubscribe(run_id, queue)

    async def _execute_run(self, *, session_id: str, run_id: str) -> None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        final_messages: list[dict[str, Any]] | None = None
        fatal_error: str | None = None

        async with lock:
            await self._set_run_status(run_id, "running", started=True)
            await self._event_bus.publish(
                run_id,
                {
                    "type": "run.started",
                    "run_id": run_id,
                    "session_id": session_id,
                    "at": datetime.now(UTC).isoformat(),
                },
            )

            try:
                async with self._session_factory() as db:
                    session_record = await self._require_session(db, session_id)
                    history = await self._load_session_messages(db, session_id)

                payload = {
                    "model": session_record.model,
                    "provider": session_record.provider,
                    "system_prompt_suffix": session_record.system_prompt_suffix,
                    "messages": history,
                    "api_key": self._settings.anthropic_api_key,
                    "only_n_most_recent_images": session_record.only_n_most_recent_images,
                    "max_tokens": session_record.max_tokens,
                    "tool_version": session_record.tool_version,
                    "thinking_budget": session_record.thinking_budget,
                    "token_efficient_tools_beta": session_record.token_efficient_tools_beta,
                }

                async def _on_event(event: dict[str, Any]) -> None:
                    nonlocal final_messages, fatal_error
                    if event.get("type") == "completed":
                        messages = event.get("messages", [])
                        if isinstance(messages, list):
                            final_messages = messages
                        await self._event_bus.publish(
                            run_id,
                            {
                                "type": "run.agent_completed",
                                "run_id": run_id,
                                "message_count": len(messages)
                                if isinstance(messages, list)
                                else 0,
                            },
                        )
                        return
                    if event.get("type") == "fatal":
                        error_value = event.get("error")
                        if isinstance(error_value, str) and error_value:
                            fatal_error = error_value
                    await self._event_bus.publish(run_id, event)

                exit_code = await self._runtime.run_worker_exec(
                    container_id=session_record.container_id,
                    payload=payload,
                    on_event=_on_event,
                )

                if exit_code != 0:
                    if fatal_error:
                        raise RuntimeError(f"Worker failed: {fatal_error}")
                    raise RuntimeError(f"Worker exited with non-zero code: {exit_code}")

                if final_messages is None:
                    raise RuntimeError("Worker finished without a completed payload")

                await self._persist_messages_delta(
                    session_id=session_id,
                    run_id=run_id,
                    original_length=len(history),
                    final_messages=final_messages,
                )

                await self._set_run_status(run_id, "completed", finished=True)
                await self._event_bus.publish(
                    run_id,
                    {
                        "type": "run.completed",
                        "run_id": run_id,
                        "session_id": session_id,
                        "at": datetime.now(UTC).isoformat(),
                    },
                )
            except Exception as exc:
                await self._set_run_status(
                    run_id,
                    "failed",
                    finished=True,
                    error_text=str(exc),
                )
                await self._event_bus.publish(
                    run_id,
                    {
                        "type": "run.failed",
                        "run_id": run_id,
                        "session_id": session_id,
                        "error": str(exc),
                        "at": datetime.now(UTC).isoformat(),
                    },
                )
            finally:
                await self._event_bus.close_stream(run_id)

    async def _set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        started: bool = False,
        finished: bool = False,
        error_text: str | None = None,
    ) -> None:
        async with self._session_factory() as db:
            run = await db.get(RunRecord, run_id)
            if not run:
                return
            run.status = status
            if started:
                run.started_at = datetime.now(UTC)
            if finished:
                run.finished_at = datetime.now(UTC)
            run.error_text = error_text
            await db.commit()

    async def _persist_messages_delta(
        self,
        *,
        session_id: str,
        run_id: str,
        original_length: int,
        final_messages: list[dict[str, Any]],
    ) -> None:
        if len(final_messages) < original_length:
            raise RuntimeError("Model returned fewer messages than current history")

        delta = final_messages[original_length:]
        if not delta:
            return

        async with self._session_factory() as db:
            for item in delta:
                role = str(item.get("role", "assistant"))
                content = item.get("content", "")
                db.add(
                    MessageRecord(
                        session_id=session_id,
                        run_id=run_id,
                        role=role,
                        content=content,
                    )
                )
            await db.commit()

    async def _load_session_messages(
        self, db: AsyncSession, session_id: str
    ) -> list[dict[str, Any]]:
        rows = await db.scalars(
            select(MessageRecord)
            .where(MessageRecord.session_id == session_id)
            .order_by(MessageRecord.created_at.asc())
        )
        return [{"role": item.role, "content": item.content} for item in rows]

    async def _require_session(self, db: AsyncSession, session_id: str) -> SessionRecord:
        session_record = await db.get(SessionRecord, session_id)
        if not session_record:
            raise HTTPException(status_code=404, detail="Session not found")
        return session_record

    def _build_novnc_url(self, request: Request, port: int) -> str:
        return (
            f"{request.url.scheme}://{request.url.hostname}:{port}/"
            "vnc.html?autoconnect=1&resize=scale&view_only=1"
        )

    def _to_session_response(
        self, record: SessionRecord, request: Request
    ) -> SessionResponse:
        return SessionResponse(
            id=record.id,
            title=record.title,
            status=record.status,
            provider=record.provider,
            model=record.model,
            tool_version=record.tool_version,
            novnc_url=self._build_novnc_url(request, record.novnc_host_port),
            vnc_port=record.vnc_host_port,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _to_message_response(record: MessageRecord) -> MessageResponse:
        return MessageResponse(
            id=record.id,
            session_id=record.session_id,
            run_id=record.run_id,
            role=record.role,
            content=record.content,
            created_at=record.created_at,
        )

    @staticmethod
    def _to_run_response(record: RunRecord) -> RunResponse:
        return RunResponse(
            id=record.id,
            session_id=record.session_id,
            status=record.status,
            error_text=record.error_text,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )
