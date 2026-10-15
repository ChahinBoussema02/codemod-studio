"""The LangGraph state machine.

Topology
--------

::

    START
      |
      v
    parse_ast ----(file does not parse / no symbols)----.
      |                                                  |
      v                                                  |
    security_audit                                       |
      |                                                  |
      +--(nothing worth refactoring / disabled)--.       |
      |                                          |       |
      v                                          v       v
    refactor ------------------------------->  report  <-'
                                                  |
                                                  v
                                                 END

Why this shape rather than a fan-out?

The refactor agent needs the audit's findings to prioritise targets and to
mention the vulnerability it is closing, so the two LLM stages are genuinely
sequential. Running them in parallel would double peak VRAM for no latency
win on a single-GPU developer machine -- Ollama serialises requests anyway.

State is a Pydantic model (:class:`AnalysisState`), so LangGraph validates
every partial update a node returns. A checkpointer is attached by default,
which gives the API layer free run resumption: the IDE can reconnect to an
in-flight analysis after a reload by replaying the same ``thread_id``.
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.nodes.parse import NODE_NAME as PARSE_NODE
from app.agents.nodes.parse import parse_node
from app.agents.nodes.refactor import NODE_NAME as REFACTOR_NODE
from app.agents.nodes.refactor import make_refactor_node, select_targets
from app.agents.nodes.report import NODE_NAME as REPORT_NODE
from app.agents.nodes.report import make_report_node
from app.agents.nodes.security import NODE_NAME as SECURITY_NODE
from app.agents.nodes.security import make_security_node
from app.core.logging import get_logger
from app.llm.ollama import LlmClient
from app.schemas.agent_state import (
    AgentTrace,
    AnalysisPhase,
    AnalysisState,
    ClassProfile,
    ComplexityMetrics,
    FunctionProfile,
    FunctionSignature,
    Language,
    ModuleProfile,
    NodeStatus,
    Parameter,
    PipelineConfig,
    RefactorProposal,
    RiskSummary,
    SecurityFinding,
    Severity,
)
from app.telemetry.client import TelemetryClient

#: The pipeline's concrete graph type. ``ContextT``/``InputT``/``OutputT``
#: are left open: this graph takes and returns its own state.
AnalysisGraph: TypeAlias = CompiledStateGraph[AnalysisState, Any, Any, Any]

__all__ = [
    "AnalysisGraph",
    "build_graph",
    "default_checkpointer",
    "route_after_audit",
    "route_after_parse",
]

logger = get_logger(__name__)

#: Types the checkpointer is permitted to reconstruct. LangGraph refuses to
#: deserialise anything not on this list -- a deliberate guard against
#: arbitrary object construction from checkpoint data. Listing the classes
#: themselves (rather than string paths) keeps the allow-list correct through
#: refactors: move a model and this follows it.
_CHECKPOINT_TYPES: tuple[type, ...] = (
    AgentTrace,
    AnalysisPhase,
    AnalysisState,
    ClassProfile,
    ComplexityMetrics,
    FunctionProfile,
    FunctionSignature,
    Language,
    ModuleProfile,
    NodeStatus,
    Parameter,
    PipelineConfig,
    RefactorProposal,
    RiskSummary,
    SecurityFinding,
    Severity,
)


def default_checkpointer() -> BaseCheckpointSaver[Any]:
    """In-memory checkpointer configured for this service's state types.

    Swap for ``AsyncPostgresSaver`` (same serde argument) when runs need to
    survive a restart; nothing else in the graph changes.
    """
    return MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_TYPES))


# --------------------------------------------------------------------------- #
# Conditional edges
# --------------------------------------------------------------------------- #


def route_after_parse(state: AnalysisState) -> Literal["security_audit", "report"]:
    """Skip the agents entirely for files with nothing to analyse.

    An empty file, or one whose only content is imports, is common in an IDE
    (new module, scaffolded ``__init__.py``). Spending 30 seconds of local
    inference on it is pure latency.
    """
    module = state.module
    if module is None:
        return REPORT_NODE
    if module.loc == 0:
        logger.debug("Routing %s straight to report: no code", state.file_path)
        return REPORT_NODE
    return SECURITY_NODE


def route_after_audit(state: AnalysisState) -> Literal["refactor", "report"]:
    """Enter the refactor agent only when it has something concrete to do."""
    if not state.config.enable_refactoring:
        return REPORT_NODE
    if state.module is None or state.module.syntax_error:
        # Splicing generated code into a file that does not parse is unsafe.
        return REPORT_NODE
    if not select_targets(state):
        return REPORT_NODE
    return REFACTOR_NODE


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def build_graph(
    llm: LlmClient,
    *,
    telemetry: TelemetryClient | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> AnalysisGraph:
    """Compile the analysis pipeline.

    Args:
        llm: Inference client shared by the audit and refactor agents.
        telemetry: Optional sidecar client; when ``None`` the report node
            skips the metrics push.
        checkpointer: Override for tests or for a durable (Postgres-backed)
            checkpointer in production. Defaults to in-memory.

    Returns:
        A compiled graph. Invoke it with ``AnalysisState`` (or a dict that
        validates into one) and a config carrying a ``thread_id``.

    Example:
        >>> graph = build_graph(client)
        >>> final = await graph.ainvoke(
        ...     AnalysisState(file_path="a.py", source_code="x = 1\\n"),
        ...     config={"configurable": {"thread_id": "run_1"}},
        ... )
        >>> final["summary"].grade
        'A'
    """
    builder = StateGraph(AnalysisState)

    # The three factory-built nodes need an ignore: LangGraph's `add_node`
    # overloads declare their node parameter as `_Node[Never]`, which no
    # concrete callable type can satisfy. The nodes are correct -- they are
    # type-checked against `GraphNode` at their definition sites -- so this
    # suppresses a library typing limitation, not a real mismatch.
    builder.add_node(PARSE_NODE, parse_node)
    builder.add_node(SECURITY_NODE, make_security_node(llm))  # type: ignore[arg-type]
    builder.add_node(REFACTOR_NODE, make_refactor_node(llm))  # type: ignore[arg-type]
    builder.add_node(REPORT_NODE, make_report_node(telemetry))  # type: ignore[arg-type]

    builder.add_edge(START, PARSE_NODE)
    builder.add_conditional_edges(
        PARSE_NODE,
        route_after_parse,
        {SECURITY_NODE: SECURITY_NODE, REPORT_NODE: REPORT_NODE},
    )
    builder.add_conditional_edges(
        SECURITY_NODE,
        route_after_audit,
        {REFACTOR_NODE: REFACTOR_NODE, REPORT_NODE: REPORT_NODE},
    )
    builder.add_edge(REFACTOR_NODE, REPORT_NODE)
    builder.add_edge(REPORT_NODE, END)

    compiled = builder.compile(checkpointer=checkpointer or default_checkpointer())
    logger.debug("Compiled analysis graph with 4 nodes")
    return compiled
