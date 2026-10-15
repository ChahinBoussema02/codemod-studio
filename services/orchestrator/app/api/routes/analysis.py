"""Analysis endpoints.

Three entry points, in increasing cost:

``POST /parse``
    AST only. Milliseconds. The VS Code extension calls this on save to keep
    the complexity gutter live.
``POST /run``
    The full agent pipeline, awaited to completion. Used by CI.
``POST /stream``
    The same pipeline as server-sent events. Used by the IDE, because a
    14B refactor pass on CPU takes long enough that silent waiting reads as a
    hang.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.analysis.registry import get_analyzer
from app.api.deps import GraphDep, SettingsDep
from app.core.errors import StudioError
from app.core.logging import bind_run_id, get_logger
from app.schemas.agent_state import AnalysisPhase, AnalysisState, PipelineConfig
from app.schemas.api import (
    AnalyzeRequest,
    AnalyzeResponse,
    ParseRequest,
    ParseResponse,
    StreamEvent,
)

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])
logger = get_logger(__name__)


def _initial_state(request: AnalyzeRequest, settings: SettingsDep) -> AnalysisState:
    """Build the seed state, letting the request override server defaults."""
    config = request.config or PipelineConfig(
        audit_model=settings.audit_model,
        refactor_model=settings.refactor_model,
        reasoning_model=settings.reasoning_model,
        temperature=settings.llm_temperature,
        max_hotspots=settings.max_hotspots_per_run,
        offline_only=settings.offline_only,
    )
    return AnalysisState(
        file_path=request.file_path,
        source_code=request.source_code,
        language=request.language,
        config=config,
    )


def _to_response(final: dict[str, Any], *, duration_ms: float) -> AnalyzeResponse:
    """Lift LangGraph's dict output back into the typed response model."""
    state = AnalysisState.model_validate(final)
    return AnalyzeResponse(
        run_id=state.run_id,
        phase=state.phase,
        file_path=state.file_path,
        language=state.language,
        module=state.module,
        findings=state.findings,
        proposals=state.proposals,
        summary=state.summary,
        traces=state.traces,
        warnings=state.warnings,
        fatal_error=state.fatal_error,
        duration_ms=round(duration_ms, 2),
    )


@router.post(
    "/parse",
    response_model=ParseResponse,
    summary="Structural analysis only (no inference)",
)
async def parse(request: ParseRequest) -> ParseResponse:
    """Return signatures and complexity metrics without touching the LLM."""
    analyzer = get_analyzer(request.language)
    if analyzer is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No parser backend registered for '{request.language.value}'",
        )
    started = time.perf_counter()
    module = analyzer.analyze(request.source_code, request.file_path)
    return ParseResponse(
        module=module, duration_ms=round((time.perf_counter() - started) * 1000, 2)
    )


@router.post("/run", response_model=AnalyzeResponse, summary="Run the full agent pipeline")
async def run(request: AnalyzeRequest, graph: GraphDep, settings: SettingsDep) -> AnalyzeResponse:
    """Execute parse -> audit -> refactor -> report and return the result."""
    state = _initial_state(request, settings)
    bind_run_id(state.run_id)
    started = time.perf_counter()

    try:
        final = await graph.ainvoke(state, config={"configurable": {"thread_id": state.run_id}})
    except StudioError as exc:
        logger.error("Run %s failed: %s", state.run_id, exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.to_payload()) from exc
    finally:
        bind_run_id(None)

    return _to_response(final, duration_ms=(time.perf_counter() - started) * 1000)


@router.post("/stream", summary="Run the pipeline, streaming progress as SSE")
async def stream(
    request: AnalyzeRequest, graph: GraphDep, settings: SettingsDep
) -> StreamingResponse:
    """Stream node-by-node progress to the IDE.

    Each completed node emits a ``phase`` event plus one event per new
    finding or proposal, so the webview can render results incrementally
    instead of waiting for the whole run.
    """
    state = _initial_state(request, settings)

    async def events() -> AsyncIterator[str]:
        bind_run_id(state.run_id)
        seen_findings = 0
        seen_proposals = 0
        try:
            async for update in graph.astream(
                state,
                config={"configurable": {"thread_id": state.run_id}},
                stream_mode="values",
            ):
                snapshot = AnalysisState.model_validate(update)

                yield StreamEvent(
                    event="phase",
                    run_id=state.run_id,
                    data={"phase": snapshot.phase.value},
                ).to_sse()

                for finding in snapshot.findings[seen_findings:]:
                    yield StreamEvent(
                        event="finding",
                        run_id=state.run_id,
                        data=finding.model_dump(mode="json"),
                    ).to_sse()
                seen_findings = len(snapshot.findings)

                for proposal in snapshot.proposals[seen_proposals:]:
                    yield StreamEvent(
                        event="proposal",
                        run_id=state.run_id,
                        data=proposal.model_dump(mode="json"),
                    ).to_sse()
                seen_proposals = len(snapshot.proposals)

                if snapshot.phase is AnalysisPhase.COMPLETED and snapshot.summary:
                    yield StreamEvent(
                        event="summary",
                        run_id=state.run_id,
                        data=snapshot.summary.model_dump(mode="json"),
                    ).to_sse()

        except StudioError as exc:
            yield StreamEvent(event="error", run_id=state.run_id, data=exc.to_payload()).to_sse()
        except Exception as exc:  # pragma: no cover - transport-level guard
            logger.exception("Stream for run %s crashed", state.run_id)
            yield StreamEvent(
                event="error",
                run_id=state.run_id,
                data={"code": "internal_error", "message": str(exc)},
            ).to_sse()
        finally:
            yield StreamEvent(event="done", run_id=state.run_id).to_sse()
            bind_run_id(None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Required so the webview sees events immediately behind a proxy.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{run_id}", response_model=AnalyzeResponse, summary="Fetch a checkpointed run")
async def get_run(run_id: str, graph: GraphDep) -> AnalyzeResponse:
    """Replay a run from the checkpointer.

    Lets the IDE recover results after a window reload without re-running
    minutes of local inference.
    """
    snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    return _to_response(dict(snapshot.values), duration_ms=0.0)


__all__ = ["router"]
