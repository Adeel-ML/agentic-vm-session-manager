"""In-memory SSE event bus for run streams."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _RunStream:
    backlog: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)
    closed: bool = False


class RunEventBus:
    """Publishes run events to multiple SSE subscribers."""

    def __init__(self, backlog_limit: int = 2_000):
        self._backlog_limit = backlog_limit
        self._streams: dict[str, _RunStream] = {}
        self._lock = asyncio.Lock()

    async def create_stream(self, run_id: str) -> None:
        async with self._lock:
            self._streams.setdefault(run_id, _RunStream())

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            stream = self._streams.setdefault(run_id, _RunStream())
            stream.backlog.append(event)
            if len(stream.backlog) > self._backlog_limit:
                stream.backlog = stream.backlog[-self._backlog_limit :]
            subscribers = list(stream.subscribers)

        for queue in subscribers:
            queue.put_nowait(event)

    async def close_stream(self, run_id: str) -> None:
        async with self._lock:
            stream = self._streams.setdefault(run_id, _RunStream())
            stream.closed = True
            subscribers = list(stream.subscribers)

        for queue in subscribers:
            queue.put_nowait(None)

    async def subscribe(
        self, run_id: str
    ) -> tuple[asyncio.Queue[dict[str, Any] | None], list[dict[str, Any]], bool]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        async with self._lock:
            stream = self._streams.setdefault(run_id, _RunStream())
            backlog = list(stream.backlog)
            was_closed = stream.closed
            if not was_closed:
                stream.subscribers.add(queue)
        return queue, backlog, was_closed

    async def unsubscribe(
        self, run_id: str, queue: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        async with self._lock:
            stream = self._streams.get(run_id)
            if not stream:
                return
            stream.subscribers.discard(queue)
