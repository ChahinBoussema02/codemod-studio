"""Node 1 -- structural parsing.

Deterministic, LLM-free and fast. Everything downstream depends on the
:class:`ModuleProfile` this node produces: the audit node uses it to pick what
to show the model, the refactor node uses it to pick targets, and the report
node uses it for the maintainability verdict.

A syntax error does **not** stop the run. The profile carries the error and
the pipeline continues with text-level heuristics only -- analysing a file
mid-edit is the common case in an IDE.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from app.agents.nodes._support import TraceRecorder
from app.analysis.registry import get_analyzer
from app.core.logging import get_logger
from app.schemas.agent_state import AnalysisPhase, AnalysisState, ModuleProfile

__all__ = ["NODE_NAME", "parse_node"]

logger = get_logger(__name__)

NODE_NAME: Final[Literal["parse_ast"]] = "parse_ast"


async def parse_node(state: AnalysisState) -> dict[str, Any]:
    """Build the module profile for the file under analysis.

    Args:
        state: Current graph state; reads ``source_code``, ``file_path`` and
            ``language``.

    Returns:
        A partial state update carrying ``module``, ``phase`` and one trace.
    """
    recorder = TraceRecorder(node=NODE_NAME)
    analyzer = get_analyzer(state.language)

    if analyzer is None:
        recorder.skip(f"no analyzer registered for {state.language.value}")
        logger.info("Skipping AST pass: unsupported language %s", state.language.value)
        return {
            "phase": AnalysisPhase.AUDITING,
            "module": ModuleProfile(
                path=state.file_path,
                language=state.language,
                loc=len([ln for ln in state.source_code.splitlines() if ln.strip()]),
                total_lines=len(state.source_code.splitlines()),
                syntax_error=f"No parser backend for {state.language.value}",
            ),
            "warnings": [
                f"{state.language.value} has no AST backend yet; "
                "only text-level security rules were applied."
            ],
            "traces": [recorder.build()],
        }

    try:
        module = analyzer.analyze(state.source_code, state.file_path)
    except Exception as exc:  # defensive: a backend bug must not kill the run
        recorder.fail(exc)
        logger.exception("AST backend crashed on %s", state.file_path)
        return {
            "phase": AnalysisPhase.AUDITING,
            "warnings": [f"Structural analysis failed: {exc}"],
            "traces": [recorder.build()],
        }

    warnings: list[str] = []
    if module.syntax_error:
        recorder.degrade(module.syntax_error)
        warnings.append(
            f"{state.file_path} does not parse ({module.syntax_error}); "
            "structural metrics and refactoring are unavailable."
        )

    recorder.detail = (
        f"{len(module.functions)} functions, {len(module.classes)} classes, "
        f"{len(module.hotspots())} hotspots"
    )
    logger.info("Parsed %s: %s", state.file_path, recorder.detail)

    return {
        "phase": AnalysisPhase.AUDITING,
        "module": module,
        "warnings": warnings,
        "traces": [recorder.build()],
    }
