"""Client for the .NET telemetry microservice.

Deliberately fire-and-forget in spirit: telemetry is an observability nicety,
never a dependency of the analysis itself. Every failure path here degrades
silently (with a log line) rather than propagating.

What crosses the wire is strictly metrics: a hashed file identifier, counts,
durations and model names. Never source, never finding text.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from app.core.logging import get_logger
from app.schemas.agent_state import AgentTrace

__all__ = ["TelemetryClient"]

logger = get_logger(__name__)


class TelemetryClient:
    """Posts run metrics to the .NET Web API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        enabled: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.enabled = enabled
        self._client = client
        self._owns_client = client is None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=httpx.Timeout(self.timeout_s)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        if not self.enabled:
            return False
        try:
            response = await self._ensure_client().get("/health", timeout=3.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def record_run(
        self,
        *,
        run_id: str,
        file_hash: str,
        language: str,
        risk_score: float,
        grade: str,
        total_findings: int,
        total_proposals: int,
        duration_ms: float,
        traces: Sequence[AgentTrace],
    ) -> bool:
        """Persist one analysis run. Returns False on any failure.

        The caller is expected to ignore the return value in production; it
        exists so the integration test can assert the contract.
        """
        if not self.enabled:
            return False

        payload = {
            "runId": run_id,
            "fileHash": file_hash,
            "language": language,
            "riskScore": risk_score,
            "grade": grade,
            "totalFindings": total_findings,
            "totalProposals": total_proposals,
            "durationMs": round(duration_ms, 2),
            "nodes": [
                {
                    "node": trace.node,
                    "status": trace.status.value,
                    "durationMs": trace.duration_ms,
                    "model": trace.model,
                    "promptTokens": trace.prompt_tokens,
                    "completionTokens": trace.completion_tokens,
                }
                for trace in traces
            ],
        }

        try:
            response = await self._ensure_client().post("/api/v1/runs", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Telemetry POST failed: %s", exc)
            return False
        return True
