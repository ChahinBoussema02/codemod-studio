"""Shared plumbing for graph nodes.

Every node is timed and produces exactly one :class:`AgentTrace`, which is what
makes the pipeline observable end to end: the IDE timeline, the telemetry
rows in PostgreSQL and the SSE stream all read from the same records.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from app.schemas.agent_state import AgentTrace, AnalysisState, NodeStatus

__all__ = ["GraphNode", "SymbolResolver", "TraceRecorder"]

#: The shape every graph node satisfies: state in, partial update out.
#: LangGraph merges the returned mapping using each field's reducer.
#: ``Coroutine`` rather than ``Awaitable``: LangGraph's ``add_node``
#: overloads only accept the former.
GraphNode = Callable[[AnalysisState], Coroutine[Any, Any, dict[str, Any]]]

#: Maps a 1-based line number to its enclosing symbol, when one is known.
SymbolResolver = Callable[[int], str | None]


@dataclass
class TraceRecorder:
    """Mutable handle a node fills in while it works."""

    node: str
    status: NodeStatus = NodeStatus.OK
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    detail: str | None = None
    error: str | None = None
    _started: float = field(default_factory=time.perf_counter, repr=False)

    def account(self, prompt_tokens: int, completion_tokens: int, model: str) -> None:
        """Accumulate token usage across several calls within one node."""
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.model = model

    def degrade(self, reason: str) -> None:
        """Mark partial success: the node produced something, but not everything."""
        self.status = NodeStatus.DEGRADED
        self.error = reason

    def skip(self, reason: str) -> None:
        self.status = NodeStatus.SKIPPED
        self.detail = reason

    def fail(self, exc: BaseException) -> None:
        """Record an unrecoverable node error without propagating it.

        Nodes swallow their own exceptions on purpose: one agent failing
        should downgrade the report, not destroy the findings the earlier
        agents already produced.
        """
        self.status = NodeStatus.FAILED
        self.error = f"{type(exc).__name__}: {exc}"

    def build(self) -> AgentTrace:
        return AgentTrace(
            node=self.node,
            status=self.status,
            duration_ms=round((time.perf_counter() - self._started) * 1000, 2),
            model=self.model,
            prompt_tokens=self.prompt_tokens or None,
            completion_tokens=self.completion_tokens or None,
            detail=self.detail,
            error=self.error,
        )
