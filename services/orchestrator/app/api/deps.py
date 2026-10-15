"""FastAPI dependencies.

Long-lived objects (the HTTP client pool, the compiled graph) are built once
during the lifespan and read from ``app.state`` here. Nothing is a module
global, which is what lets the test suite spin up an app with a stubbed LLM
client and no network at all.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.agents.graph import AnalysisGraph
from app.core.config import Settings, get_settings
from app.llm.ollama import LlmClient
from app.telemetry.client import TelemetryClient

__all__ = [
    "GraphDep",
    "LlmDep",
    "SettingsDep",
    "TelemetryDep",
    "get_graph",
    "get_llm",
    "get_telemetry",
]


def get_graph(request: Request) -> AnalysisGraph:
    """The compiled LangGraph pipeline built at startup."""
    graph: AnalysisGraph = request.app.state.graph
    return graph


def get_llm(request: Request) -> LlmClient:
    llm: LlmClient = request.app.state.llm
    return llm


def get_telemetry(request: Request) -> TelemetryClient | None:
    return getattr(request.app.state, "telemetry", None)


GraphDep = Annotated[AnalysisGraph, Depends(get_graph)]
LlmDep = Annotated[LlmClient, Depends(get_llm)]
TelemetryDep = Annotated["TelemetryClient | None", Depends(get_telemetry)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
