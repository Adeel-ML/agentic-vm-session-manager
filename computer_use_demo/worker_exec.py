"""Worker process entrypoint run inside each session container."""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from dataclasses import asdict
from typing import Any

import httpx
from anthropic.types.beta import BetaContentBlockParam
from anyio import Path as AnyioPath

from computer_use_demo.loop import APIProvider, sampling_loop
from computer_use_demo.tools import ToolResult


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=True), flush=True)  # noqa: T201


def _output_callback(content: BetaContentBlockParam) -> None:
    _emit({"type": "assistant.block", "block": content})


def _tool_callback(result: ToolResult, tool_use_id: str) -> None:
    _emit(
        {
            "type": "tool.result",
            "tool_use_id": tool_use_id,
            "result": asdict(result),
        }
    )


def _api_callback(
    request: httpx.Request,
    response: httpx.Response | object | None,
    error: Exception | None,
) -> None:
    status_code = response.status_code if isinstance(response, httpx.Response) else None
    _emit(
        {
            "type": "api.exchange",
            "request": {"method": request.method, "url": str(request.url)},
            "status_code": status_code,
            "error": str(error) if error else None,
        }
    )


async def _run(request_path: AnyioPath) -> int:
    payload = json.loads(await request_path.read_text())

    provider = APIProvider(payload["provider"])
    messages = payload["messages"]

    updated_messages = await sampling_loop(
        model=payload["model"],
        provider=provider,
        system_prompt_suffix=payload.get("system_prompt_suffix", ""),
        messages=messages,
        output_callback=_output_callback,
        tool_output_callback=_tool_callback,
        api_response_callback=_api_callback,
        api_key=payload.get("api_key", ""),
        only_n_most_recent_images=payload.get("only_n_most_recent_images"),
        max_tokens=payload.get("max_tokens", 16_384),
        tool_version=payload["tool_version"],
        thinking_budget=payload.get("thinking_budget"),
        token_efficient_tools_beta=payload.get("token_efficient_tools_beta", False),
    )

    _emit({"type": "completed", "messages": updated_messages})
    return 0


async def _amain(argv: list[str]) -> int:
    if len(argv) != 2:
        _emit(
            {
                "type": "fatal",
                "error": "Usage: python -m computer_use_demo.worker_exec /tmp/request.json",
            }
        )
        return 2

    path = AnyioPath(argv[1])
    if not await path.exists():
        _emit({"type": "fatal", "error": f"Payload file not found: {path}"})
        return 2

    try:
        return await _run(path)
    except Exception as exc:
        _emit(
            {
                "type": "fatal",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv)))


if __name__ == "__main__":
    main()
