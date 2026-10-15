"""Node 4 -- aggregation and telemetry.

Turns the accumulated findings, proposals and traces into one verdict the IDE
can render in a single line, then forwards an anonymised summary to the .NET
telemetry service.

Only *metrics* are forwarded. No source code, no finding text, no file
contents -- the file path is hashed before it leaves this process. That keeps
the zero-leakage guarantee intact even with telemetry switched on.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Final, Literal

from app.agents.nodes._support import GraphNode, TraceRecorder
from app.core.logging import get_logger
from app.schemas.agent_state import (
    AnalysisPhase,
    AnalysisState,
    RiskSummary,
    Severity,
)
from app.telemetry.client import TelemetryClient

__all__ = ["NODE_NAME", "build_summary", "make_report_node"]

logger = get_logger(__name__)

NODE_NAME: Final[Literal["report"]] = "report"

#: Must match ``RiskSummary.grade``.
Grade = Literal["A", "B", "C", "D", "F"]

#: Score at or above which each grade is awarded, worst first.
_GRADE_BANDS: list[tuple[float, Grade]] = [
    (60.0, "F"),
    (40.0, "D"),
    (25.0, "C"),
    (10.0, "B"),
    (0.0, "A"),
]


def _grade(score: float) -> Grade:
    for threshold, letter in _GRADE_BANDS:
        if score >= threshold:
            return letter
    return "A"


def build_summary(state: AnalysisState) -> RiskSummary:
    """Collapse the run into a single risk verdict.

    The score is a saturating function of weighted severity rather than a
    plain sum: ten medium findings should not outrank one critical, and the
    scale must stay inside 0-100 for an arbitrary number of findings.
    """
    counts: dict[Severity, int] = {}
    weighted = 0.0
    for finding in state.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        weighted += finding.severity.weight * max(finding.confidence, 0.3)

    # 1 - e^(-x/25) saturates smoothly: one critical lands around 55, a
    # handful of lows stays in the teens.
    score = round(100.0 * (1.0 - math.exp(-weighted / 25.0)), 1)

    module = state.module
    hotspots = tuple(f.signature.qualified_name for f in module.hotspots()[:10]) if module else ()

    if not state.findings:
        headline = (
            f"No security findings. {len(hotspots)} complexity hotspot(s) flagged."
            if hotspots
            else "No security findings and no complexity hotspots."
        )
    else:
        worst = max(state.findings, key=lambda f: f.severity.weight)
        headline = (
            f"{len(state.findings)} finding(s); most severe: "
            f"{worst.title} ({worst.severity.value}) at line {worst.line_start}."
        )

    return RiskSummary(
        risk_score=score,
        grade=_grade(score),
        findings_by_severity=counts,
        total_findings=len(state.findings),
        total_proposals=len(state.proposals),
        hotspot_functions=hotspots,
        headline=headline,
    )


def _hash_path(path: str) -> str:
    """Stable, non-reversible identifier so telemetry can count repeat files."""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def make_report_node(telemetry: TelemetryClient | None = None) -> GraphNode:
    """Build the terminal node, optionally wired to the telemetry sidecar."""

    async def report_node(state: AnalysisState) -> dict[str, Any]:
        recorder = TraceRecorder(node=NODE_NAME)
        summary = build_summary(state)

        if telemetry is not None:
            try:
                await telemetry.record_run(
                    run_id=state.run_id,
                    file_hash=_hash_path(state.file_path),
                    language=state.language.value,
                    risk_score=summary.risk_score,
                    grade=summary.grade,
                    total_findings=summary.total_findings,
                    total_proposals=summary.total_proposals,
                    duration_ms=state.total_duration_ms(),
                    traces=state.traces,
                )
            except Exception as exc:  # telemetry must never fail a run
                recorder.degrade(f"telemetry push failed: {exc}")
                logger.warning("Telemetry push failed for run %s: %s", state.run_id, exc)

        recorder.detail = f"grade {summary.grade}, risk {summary.risk_score}"
        logger.info("Run %s complete: %s", state.run_id, summary.headline)

        return {
            "phase": AnalysisPhase.COMPLETED,
            "summary": summary,
            "traces": [recorder.build()],
        }

    return report_node
