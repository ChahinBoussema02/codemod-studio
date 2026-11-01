"""Liveness and readiness endpoints.

``/health`` answers as long as the process is up -- it is what the container
orchestrator polls. ``/health/ready`` additionally checks Ollama and reports
which configured models are missing, which is the single most common setup
problem (the service runs fine, but nobody ran ``ollama pull``).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import LlmDep, SettingsDep, TelemetryDep
from app.schemas.api import HealthResponse

router = APIRouter(tags=["health"])


def _is_pulled(model: str, pulled: set[str]) -> bool:
    """Is ``model`` actually available on the Ollama host?

    Tags must match exactly. Treating ``qwen2.5-coder:7b`` as satisfying a
    request for ``qwen2.5-coder:14b`` would make this endpoint report ready
    while the refactor agent 404s on its first call -- precisely the failure
    the readiness probe exists to catch.

    The one allowance: Ollama lists an untagged pull as ``name:latest``, so a
    config value with no tag at all matches that entry.
    """
    if model in pulled:
        return True
    return ":" not in model and f"{model}:latest" in pulled


@router.get("/health", summary="Liveness probe")
async def health(settings: SettingsDep) -> dict[str, str]:
    """Cheap, dependency-free liveness signal."""
    return {"status": "ok", "version": settings.version, "environment": settings.environment}


@router.get("/health/ready", response_model=HealthResponse, summary="Readiness probe")
async def ready(
    settings: SettingsDep,
    llm: LlmDep,
    telemetry: TelemetryDep,
) -> HealthResponse:
    """Report whether the inference runtime can actually serve a request."""
    reachable = await llm.ping()
    available = await llm.available_models() if reachable else []

    configured = {settings.audit_model, settings.refactor_model}
    pulled = set(available)
    missing = sorted(model for model in configured if not _is_pulled(model, pulled))

    telemetry_ok: bool | None = None
    if telemetry is not None and settings.telemetry_enabled:
        telemetry_ok = await telemetry.ping()

    return HealthResponse(
        status="ok" if reachable and not missing else "degraded",
        version=settings.version,
        ollama_reachable=reachable,
        available_models=available,
        missing_models=missing,
        telemetry_reachable=telemetry_ok,
    )
