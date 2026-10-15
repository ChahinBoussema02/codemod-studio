"""Transport-layer DTOs.

Kept separate from :mod:`app.schemas.agent_state` so the HTTP contract can
evolve without forcing a change in the graph's internal state, and vice versa.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.agent_state import (
    AgentTrace,
    AnalysisPhase,
    Language,
    ModuleProfile,
    PipelineConfig,
    RefactorProposal,
    RiskSummary,
    SecurityFinding,
)

__all__ = [
    "AnalyzeRequest",
    "AnalyzeResponse",
    "HealthResponse",
    "ParseRequest",
    "ParseResponse",
    "StreamEvent",
]

# 1 MiB. Anything larger is almost certainly a vendored bundle or generated
# file; sending it to a 7B local model wastes minutes of wall-clock time.
MAX_SOURCE_BYTES = 1_048_576


class AnalyzeRequest(BaseModel):
    """Request body for ``POST /api/v1/analysis/run``."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "file_path": "src/payments/service.py",
                "source_code": "def charge(amount):\n    ...\n",
                "language": "python",
            }
        }
    )

    file_path: str = Field(min_length=1, max_length=4096)
    source_code: str = Field(min_length=1)
    language: Language = Language.PYTHON
    config: PipelineConfig | None = Field(
        default=None, description="Overrides the server defaults for this run only."
    )

    @field_validator("source_code")
    @classmethod
    def _reject_oversized(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError(
                f"source_code exceeds {MAX_SOURCE_BYTES} bytes; split the file or "
                "analyse it with the batch endpoint"
            )
        return v


class AnalyzeResponse(BaseModel):
    """Terminal result of a pipeline run."""

    run_id: str
    phase: AnalysisPhase
    file_path: str
    language: Language
    module: ModuleProfile | None
    findings: list[SecurityFinding]
    proposals: list[RefactorProposal]
    summary: RiskSummary | None
    traces: list[AgentTrace]
    warnings: list[str] = Field(default_factory=list)
    fatal_error: str | None = None
    duration_ms: float = 0.0


class ParseRequest(BaseModel):
    """Request body for the cheap, LLM-free structural endpoint."""

    file_path: str = Field(min_length=1, max_length=4096)
    source_code: str = Field(min_length=1)
    language: Language = Language.PYTHON


class ParseResponse(BaseModel):
    module: ModuleProfile
    duration_ms: float


class StreamEvent(BaseModel):
    """One server-sent event emitted while the graph executes.

    The VS Code webview renders these as a live timeline, which is the whole
    reason the analysis endpoint exposes a streaming variant: a 14B refactor
    pass on CPU can take 60+ seconds and silent progress feels broken.
    """

    event: Literal["phase", "finding", "proposal", "trace", "summary", "error", "done"]
    run_id: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)

    def to_sse(self) -> str:
        """Serialise to the SSE wire format."""
        return f"event: {self.event}\ndata: {self.model_dump_json()}\n\n"


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    ollama_reachable: bool
    available_models: list[str] = Field(default_factory=list)
    missing_models: list[str] = Field(default_factory=list)
    telemetry_reachable: bool | None = None
