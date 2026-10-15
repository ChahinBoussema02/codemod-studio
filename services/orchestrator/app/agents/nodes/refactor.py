"""Node 3 -- semantic refactoring.

Targets are chosen deterministically, not by asking the model what it feels
like improving: a function qualifies if it carries an actionable finding or if
its measured complexity marks it a hotspot. Findings-carrying functions are
ranked first, because a refactor that also closes a vulnerability is worth
more than one that only lowers a number.

Every proposal is re-parsed by the AST backend before it is surfaced. A
suggestion that does not compile is dropped, never shown. This is the single
most important guard in the pipeline -- a 7B model producing plausible but
syntactically broken code is the normal failure mode, and the user must never
be asked to spot it.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from app.agents.nodes._support import GraphNode, TraceRecorder
from app.analysis.base import SourceAnalyzer
from app.analysis.registry import get_analyzer
from app.core.errors import LlmResponseError, LlmUnavailableError
from app.core.logging import get_logger
from app.llm.ollama import LlmClient
from app.llm.prompts import (
    REFACTOR_SYSTEM_PROMPT,
    LlmRefactorProposal,
    build_refactor_prompt,
)
from app.schemas.agent_state import (
    AnalysisPhase,
    AnalysisState,
    FunctionProfile,
    RefactorProposal,
    SecurityFinding,
    Severity,
)

__all__ = ["NODE_NAME", "make_refactor_node", "select_targets"]

logger = get_logger(__name__)

NODE_NAME: Final[Literal["refactor"]] = "refactor"


def select_targets(state: AnalysisState) -> list[tuple[FunctionProfile, list[SecurityFinding]]]:
    """Pick which functions to spend inference budget on.

    Ordering: functions with the most severe findings first, then pure
    complexity hotspots worst-first. Capped by ``config.max_hotspots`` so a
    1,000-line legacy module cannot queue an hour of CPU inference.
    """
    module = state.module
    if module is None or module.syntax_error:
        return []

    actionable = state.actionable_findings(state.config.min_severity_to_refactor)
    by_symbol: dict[str, list[SecurityFinding]] = {}
    for finding in actionable:
        if finding.symbol:
            by_symbol.setdefault(finding.symbol, []).append(finding)

    scored: list[tuple[int, int, FunctionProfile, list[SecurityFinding]]] = []
    for function in module.functions:
        name = function.signature.qualified_name
        related = by_symbol.get(name, [])
        if not related and not function.metrics.is_hotspot:
            continue
        severity_score = max((f.severity.weight for f in related), default=0)
        scored.append((severity_score, function.metrics.cognitive, function, related))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    capped = scored[: state.config.max_hotspots]
    return [(function, related) for _severity, _cognitive, function, related in capped]


def make_refactor_node(llm: LlmClient) -> GraphNode:
    """Build the refactor node bound to an inference client."""

    async def refactor_node(state: AnalysisState) -> dict[str, Any]:
        recorder = TraceRecorder(node=NODE_NAME)
        warnings: list[str] = []

        if not state.config.enable_refactoring:
            recorder.skip("refactoring disabled for this run")
            return {"phase": AnalysisPhase.REPORTING, "traces": [recorder.build()]}

        targets = select_targets(state)
        if not targets:
            recorder.skip("no functions met the complexity or severity threshold")
            return {"phase": AnalysisPhase.REPORTING, "traces": [recorder.build()]}

        analyzer = get_analyzer(state.language)
        module = state.module
        assert module is not None  # guaranteed by select_targets

        proposals: list[RefactorProposal] = []
        for function, related in targets:
            name = function.signature.qualified_name
            try:
                result, usage = await llm.structured(
                    model=state.config.refactor_model,
                    system=REFACTOR_SYSTEM_PROMPT,
                    prompt=build_refactor_prompt(function, module, related),
                    schema=LlmRefactorProposal,
                    temperature=state.config.temperature,
                )
                recorder.account(usage.prompt_tokens, usage.completion_tokens, usage.model)
            except (LlmUnavailableError, LlmResponseError) as exc:
                recorder.degrade(exc.message)
                warnings.append(f"Refactor of {name} skipped: {exc.message}")
                logger.warning("Refactor degraded for %s: %s", name, exc.message)
                break  # The runtime is down; further calls will fail too.
            except Exception as exc:  # pragma: no cover - last-resort guard
                recorder.fail(exc)
                warnings.append(f"Refactor of {name} failed unexpectedly: {exc}")
                logger.exception("Unhandled error refactoring %s", name)
                break

            proposal = _validate(result, function, state, analyzer, related)
            if proposal is None:
                warnings.append(f"Discarded refactor of {name}: the proposed code did not parse.")
                continue
            proposals.append(proposal)

        recorder.detail = f"{len(proposals)}/{len(targets)} proposals survived validation"
        logger.info("Refactor pass for %s: %s", state.file_path, recorder.detail)

        return {
            "phase": AnalysisPhase.REPORTING,
            "proposals": proposals,
            "warnings": warnings,
            "traces": [recorder.build()],
        }

    return refactor_node


def _validate(
    result: LlmRefactorProposal,
    function: FunctionProfile,
    state: AnalysisState,
    analyzer: SourceAnalyzer | None,
    related: list[SecurityFinding],
) -> RefactorProposal | None:
    """Turn a model answer into a domain proposal, or reject it.

    Rejection criteria, in order:

    * empty or unchanged code -- nothing to show the user;
    * the replacement does not parse in the target language;
    * the model flagged ``behaviour_preserved=False`` *and* the proposal
      claims low risk, which is self-contradictory and not worth surfacing.
    """
    code = result.proposed_code.strip()
    if not code or code == function.source.strip():
        return None

    syntax_ok = True
    if analyzer is not None:
        # Dedent to module level before parsing: a method body is indented.
        candidate = _dedent_to_module_level(code)
        syntax_ok, error = analyzer.is_valid_syntax(candidate)
        if not syntax_ok:
            logger.info(
                "Rejected refactor of %s: %s",
                function.signature.qualified_name,
                error,
            )
            return None

    risk = Severity(result.estimated_risk)
    if not result.behaviour_preserved and risk.weight < Severity.MEDIUM.weight:
        risk = Severity.MEDIUM

    return RefactorProposal(
        file_path=state.file_path,
        target_symbol=function.signature.qualified_name,
        strategy=result.strategy,
        rationale=result.rationale.strip(),
        proposed_code=code,
        line_start=function.signature.line_start,
        line_end=function.signature.line_end,
        estimated_risk=risk,
        confidence=result.confidence,
        syntax_validated=syntax_ok,
        # Only the findings actually shown to the model. Matching on symbol
        # instead would claim the proposal addresses every finding inside the
        # function, including ones it never saw and did not touch.
        addresses_finding_ids=tuple(f.id for f in related),
    )


def _dedent_to_module_level(code: str) -> str:
    """Strip the common leading indentation so a method body parses standalone."""
    lines = [line for line in code.splitlines() if line.strip()]
    if not lines:
        return code
    indent = min(len(line) - len(line.lstrip()) for line in lines)
    if indent == 0:
        return code
    return "\n".join(line[indent:] if len(line) >= indent else line for line in code.splitlines())
