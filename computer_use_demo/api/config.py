"""Configuration for the session-management backend."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    worker_image: str
    anthropic_api_key: str
    api_provider: str
    default_model: str
    default_tool_version: str
    default_max_tokens: int
    default_recent_images: int
    default_width: int
    default_height: int


def _get_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return int(raw_value)


@lru_cache(maxsize=1)
def _load_dotenv_file() -> None:
    """Load key=value pairs from .env if present.

    Existing environment variables take precedence and are not overwritten.
    """

    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]

    env_path = next((path for path in candidates if path.is_file()), None)
    if env_path is None:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        cleaned = value.strip().strip('"').strip("'")
        os.environ[key] = cleaned


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv_file()
    return Settings(
        database_url=os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./data/sessions.db"
        ),
        worker_image=os.getenv("SESSION_WORKER_IMAGE", "computer-use-worker:local"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        api_provider=os.getenv("API_PROVIDER", "anthropic"),
        default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-4-5-20250929"),
        default_tool_version=os.getenv(
            "DEFAULT_TOOL_VERSION", "computer_use_20250124"
        ),
        default_max_tokens=_get_int("DEFAULT_MAX_TOKENS", 16_384),
        default_recent_images=_get_int("DEFAULT_RECENT_IMAGES", 3),
        default_width=_get_int("DEFAULT_WIDTH", 1024),
        default_height=_get_int("DEFAULT_HEIGHT", 768),
    )
