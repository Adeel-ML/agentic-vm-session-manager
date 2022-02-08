"""Docker-backed isolated runtime management for each agent session."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import docker
from docker.errors import NotFound
from docker.models.containers import Container

from .config import Settings


@dataclass(frozen=True)
class RuntimeHandle:
    container_id: str
    novnc_host_port: int
    vnc_host_port: int


class DockerSessionRuntime:
    """Creates and manages one containerized VM runtime per session."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = docker.from_env()

    async def create_runtime(self, session_id: str) -> RuntimeHandle:
        return await asyncio.to_thread(self._create_runtime_sync, session_id)

    def _create_runtime_sync(self, session_id: str) -> RuntimeHandle:
        container = self._client.containers.run(
            self._settings.worker_image,
            detach=True,
            environment={
                "MODE": "runtime",
                "HIDE_WARNING": "1",
                "WIDTH": str(self._settings.default_width),
                "HEIGHT": str(self._settings.default_height),
                "ANTHROPIC_API_KEY": self._settings.anthropic_api_key,
                "API_PROVIDER": self._settings.api_provider,
            },
            labels={
                "managed-by": "computer-use-session-api",
                "session-id": session_id,
            },
            ports={"6080/tcp": None, "5900/tcp": None},
            shm_size="1gb",
        )

        novnc_host_port: int | None = None
        vnc_host_port: int | None = None

        for _ in range(40):
            container.reload()
            ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            novnc_host_port = self._extract_host_port(ports, "6080/tcp")
            vnc_host_port = self._extract_host_port(ports, "5900/tcp")
            if novnc_host_port and vnc_host_port:
                break
            time.sleep(0.25)

        if not novnc_host_port or not vnc_host_port:
            try:
                container.remove(force=True)
            except Exception:
                pass
            raise RuntimeError("Runtime container started but host ports were not assigned")

        if not self._wait_for_novnc_ready(container):
            try:
                container.remove(force=True)
            except Exception:
                pass
            raise RuntimeError("Runtime container started but noVNC was not ready in time")

        container_id = container.id
        if not container_id:
            try:
                container.remove(force=True)
            except Exception:
                pass
            raise RuntimeError("Runtime container started but container ID was unavailable")

        return RuntimeHandle(
            container_id=container_id,
            novnc_host_port=novnc_host_port,
            vnc_host_port=vnc_host_port,
        )

    def _wait_for_novnc_ready(self, container: Container, timeout_seconds: float = 25.0) -> bool:
        """Wait until the worker container serves noVNC HTML on localhost:6080."""

        deadline = time.monotonic() + timeout_seconds
        check_cmd = [
            "python",
            "-c",
            (
                "import socket, sys, urllib.request; "
                "resp = urllib.request.urlopen('http://127.0.0.1:6080/vnc.html', timeout=1.0); "
                "sock = socket.create_connection(('127.0.0.1', 5900), timeout=1.0); "
                "sock.close(); "
                "sys.exit(0 if resp.status == 200 else 1)"
            ),
        ]

        while time.monotonic() < deadline:
            try:
                container.reload()
                if container.status != "running":
                    return False

                result = container.exec_run(check_cmd)
                if result.exit_code == 0:
                    return True
            except Exception:
                # Keep polling while the runtime is booting.
                pass

            time.sleep(0.5)

        return False

    @staticmethod
    def _extract_host_port(ports: dict[str, Any], key: str) -> int | None:
        binding = ports.get(key)
        if not binding:
            return None
        host_port = binding[0].get("HostPort")
        if not host_port:
            return None
        return int(host_port)

    async def remove_runtime(self, container_id: str) -> None:
        await asyncio.to_thread(self._remove_runtime_sync, container_id)

    def _remove_runtime_sync(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
        except NotFound:
            return
        container.remove(force=True)

    async def run_worker_exec(
        self,
        *,
        container_id: str,
        payload: dict[str, Any],
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> int:
        """Run the sampling loop inside the session container and stream JSON events."""

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        event_loop = asyncio.get_running_loop()

        def _worker() -> None:
            try:
                container = self._client.containers.get(container_id)
                request_name = f"agent_request_{uuid4().hex}.json"
                request_path = f"/tmp/{request_name}"
                self._copy_json(container, request_name, payload)

                exec_id = self._client.api.exec_create(
                    container.id,
                    cmd=["python", "-m", "computer_use_demo.worker_exec", request_path],
                )["Id"]

                stream = self._client.api.exec_start(
                    exec_id,
                    stream=True,
                    demux=False,
                )

                buffer = ""
                for chunk in stream:
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", maxsplit=1)
                        line = line.strip()
                        if line:
                            event_loop.call_soon_threadsafe(
                                queue.put_nowait,
                                ("line", line),
                            )

                trailing = buffer.strip()
                if trailing:
                    event_loop.call_soon_threadsafe(queue.put_nowait, ("line", trailing))

                exit_code = int(self._client.api.exec_inspect(exec_id).get("ExitCode") or 0)
                event_loop.call_soon_threadsafe(queue.put_nowait, ("exit", exit_code))
            except Exception as exc:
                event_loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        while True:
            kind, value = await queue.get()
            if kind == "line":
                try:
                    event = json.loads(value)
                except json.JSONDecodeError:
                    event = {"type": "worker.stdout", "raw": value}
                await on_event(event)
                continue
            if kind == "error":
                raise RuntimeError(str(value))
            if kind == "exit":
                return int(value)

    def _copy_json(self, container: Container, filename: str, payload: dict[str, Any]) -> None:
        raw_bytes = json.dumps(payload).encode("utf-8")
        tar_stream = io.BytesIO()

        with tarfile.open(fileobj=tar_stream, mode="w") as archive:
            info = tarfile.TarInfo(name=filename)
            info.size = len(raw_bytes)
            archive.addfile(info, io.BytesIO(raw_bytes))

        tar_stream.seek(0)
        if not container.put_archive("/tmp", tar_stream.getvalue()):
            raise RuntimeError("Failed to copy execution payload into worker container")
