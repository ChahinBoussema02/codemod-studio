"""Application settings.

All configuration arrives through the environment (12-factor), which keeps the
container image identical across dev, CI and self-hosted installs. Values are
validated once at import time by :func:`get_settings`, so a typo in a
``.env`` file fails the health check rather than the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    """Environment-driven configuration for the orchestrator service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CMS_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- service identity ---------------------------------------------------- #
    app_name: str = "Code Modernization & Security Studio -- Orchestrator"
    version: str = "0.2.0"
    environment: Literal["local", "ci", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_logs: bool = Field(
        default=False, description="Emit structured JSON logs instead of console text."
    )

    # -- HTTP ----------------------------------------------------------------- #
    host: str = "0.0.0.0"  # noqa: S104 - bound inside the container network only.
    port: int = 8000
    cors_origins: list[str] = Field(
        default_factory=lambda: ["vscode-webview://*", "http://localhost:5173"],
        description="The VS Code webview origin plus the local dev dashboard.",
    )

    # -- Ollama --------------------------------------------------------------- #
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout_s: float = Field(
        default=180.0,
        ge=5.0,
        description="A 14B model on CPU can legitimately take minutes for a refactor.",
    )
    ollama_max_retries: int = Field(default=2, ge=0, le=5)
    audit_model: str = "qwen2.5-coder:7b"
    refactor_model: str = "qwen2.5-coder:14b"
    reasoning_model: str = "deepseek-r1:8b"
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_num_ctx: int = Field(default=8192, ge=2048)

    # -- telemetry sidecar (.NET) --------------------------------------------- #
    telemetry_base_url: str | None = "http://telemetry-api:8080"
    telemetry_enabled: bool = True
    telemetry_timeout_s: float = 5.0

    # -- safety --------------------------------------------------------------- #
    offline_only: bool = Field(
        default=True,
        description=(
            "When true the service refuses any LLM base URL that is not loopback or a "
            "private address. This is the technical backing for the zero-leakage claim."
        ),
    )
    max_hotspots_per_run: int = Field(default=5, ge=1, le=50)

    @field_validator("ollama_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton, cached so env parsing happens exactly once."""
    return Settings()
