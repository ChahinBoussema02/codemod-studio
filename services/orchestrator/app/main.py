"""FastAPI application factory.

The app owns three long-lived objects, all created in the lifespan and torn
down deterministically:

* an :class:`~app.llm.ollama.OllamaClient` with a pooled HTTP connection,
* a :class:`~app.telemetry.client.TelemetryClient` for the .NET sidecar,
* the compiled LangGraph pipeline.

Building the graph once at startup matters: compilation walks and validates
the whole topology, and doing that per-request would add measurable latency
to the fast ``/parse`` path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents.graph import build_graph
from app.api.routes import analysis, health
from app.core.config import Settings, get_settings
from app.core.errors import StudioError
from app.core.logging import configure_logging, get_logger
from app.llm.ollama import OllamaClient
from app.telemetry.client import TelemetryClient

__all__ = ["app", "create_app"]

logger = get_logger(__name__)

DESCRIPTION = """\
Local-first, multi-agent code analysis.

Specialised agents -- an AST profiler, a security auditor and a refactoring
engineer -- are orchestrated as a LangGraph state machine. All inference runs
against a local Ollama host; source code never leaves the machine.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create and dispose the service's shared resources."""
    settings: Settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.json_logs)
    logger.info("Starting %s v%s (%s)", settings.app_name, settings.version, settings.environment)

    llm = OllamaClient(
        settings.ollama_base_url,
        timeout_s=settings.ollama_timeout_s,
        max_retries=settings.ollama_max_retries,
        num_ctx=settings.llm_num_ctx,
        offline_only=settings.offline_only,
    )

    telemetry: TelemetryClient | None = None
    if settings.telemetry_enabled and settings.telemetry_base_url:
        telemetry = TelemetryClient(
            settings.telemetry_base_url,
            timeout_s=settings.telemetry_timeout_s,
            enabled=True,
        )

    app.state.settings = settings
    app.state.llm = llm
    app.state.telemetry = telemetry
    app.state.graph = build_graph(llm, telemetry=telemetry)

    # A startup probe is informational only: the service must still boot so
    # the IDE can show "Ollama not running" instead of failing to connect.
    if not await llm.ping():
        logger.warning(
            "Ollama at %s is not responding; analysis will degrade to "
            "deterministic rules until it is available.",
            settings.ollama_base_url,
        )

    try:
        yield
    finally:
        await llm.aclose()
        if telemetry is not None:
            await telemetry.aclose()
        logger.info("Shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the application.

    Args:
        settings: Injected in tests; defaults to the cached environment-driven
            singleton.
    """
    settings = settings or get_settings()

    application = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(vscode-webview://.*|http://localhost:\d+)$",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @application.exception_handler(StudioError)
    async def _studio_error_handler(_request: Request, exc: StudioError) -> JSONResponse:
        """Map domain errors onto their declared status codes."""
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    application.include_router(health.router)
    application.include_router(analysis.router)
    return application


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        reload=_settings.environment == "local",
    )
