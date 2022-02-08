"""Pydantic schemas for HTTP API payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    title: str | None = None
    provider: str | None = None
    model: str | None = None
    tool_version: str | None = None
    system_prompt_suffix: str = ""
    only_n_most_recent_images: int | None = None
    max_tokens: int | None = None
    thinking_budget: int | None = None
    token_efficient_tools_beta: bool = False


class SessionResponse(BaseModel):
    id: str
    title: str
    status: str
    provider: str
    model: str
    tool_version: str
    novnc_url: str
    vnc_port: int
    created_at: datetime
    updated_at: datetime | None


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


class MessageCreateRequest(BaseModel):
    text: str = Field(min_length=1)


class MessageResponse(BaseModel):
    id: str
    session_id: str
    run_id: str | None
    role: str
    content: Any
    created_at: datetime


class MessageListResponse(BaseModel):
    messages: list[MessageResponse]


class RunResponse(BaseModel):
    id: str
    session_id: str
    status: str
    error_text: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SubmitMessageResponse(BaseModel):
    run: RunResponse


class HealthResponse(BaseModel):
    status: str
