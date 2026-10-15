"""Node 2 -- security auditing.

Two detectors, merged:

1. **Heuristics** (``app.analysis.security_rules``) -- instant, reproducible,
   and the only detector that runs when Ollama is unavailable.
2. **Local LLM** -- catches the reasoning-dependent classes the pattern
   matcher structurally cannot: missing authorisation, trust-boundary
   confusion, unsafe defaults, information disclosure.

The LLM is told what the scanner already found, so it spends its context on
the complement rather than re-deriving that ``eval`` is dangerous. Overlapping
reports are merged into a single ``hybrid`` finding with boosted confidence --
two independent detectors agreeing is the strongest signal available here.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from app.agents.nodes._support import GraphNode, SymbolResolver, TraceRecorder
from app.analysis.security_rules import scan_python, scan_text
from app.core.errors import LlmResponseError, LlmUnavailableError
from app.core.logging import get_logger
from app.llm.ollama import LlmClient
from app.llm.prompts import AUDIT_SYSTEM_PROMPT, LlmAuditReport, build_audit_prompt
from app.schemas.agent_state import (
    AnalysisPhase,
    AnalysisState,
    Language,
    ModuleProfile,
    SecurityFinding,
    Severity,
)

__all__ = ["NODE_NAME", "make_security_node", "merge_findings"]

logger = get_logger(__name__)

NODE_NAME: Final[Literal["security_audit"]] = "security_audit"


def _symbol_resolver(module: ModuleProfile | None) -> SymbolResolver:
    """Return a callable mapping a line number to its enclosing symbol."""

    if module is None:
        return lambda _line: None

    spans = [
        (f.signature.line_start, f.signature.line_end, f.signature.qualified_name)
        for f in module.functions
    ]
    # Innermost match wins: nested closures are more specific than their parent.
    spans.sort(key=lambda s: s[1] - s[0])

    def resolve(line: int) -> str | None:
        for start, end, name in spans:
            if start <= line <= end:
                return name
        return None

    return resolve


def merge_findings(
    heuristic: list[SecurityFinding],
    llm: list[SecurityFinding],
) -> list[SecurityFinding]:
    """Combine both detectors, collapsing duplicates into ``hybrid`` findings.

    Two findings are considered the same issue when they share a file, a start
    line and a CWE (or, lacking a CWE, a title). The heuristic record wins on
    wording -- it is curated -- while the agreement raises confidence and the
    higher of the two severities is kept.
    """
    merged: dict[tuple[str, int, str], SecurityFinding] = {}

    for finding in heuristic:
        merged[finding.dedupe_key()] = finding

    for finding in llm:
        key = finding.dedupe_key()
        existing = merged.get(key)
        if existing is None:
            merged[key] = finding
            continue
        corroborated = existing.model_copy(
            update={
                "detector": "hybrid",
                "confidence": min(1.0, max(existing.confidence, finding.confidence) + 0.1),
                "severity": max(existing.severity, finding.severity, key=lambda s: s.weight),
                "description": f"{existing.description}\n\nAgent agreement: {finding.description}",
            }
        )
        merged[key] = corroborated

    return sorted(
        merged.values(),
        key=lambda f: (f.severity.weight, f.confidence),
        reverse=True,
    )


def _to_domain(
    report: LlmAuditReport,
    *,
    file_path: str,
    total_lines: int,
    resolve_symbol: SymbolResolver,
) -> tuple[list[SecurityFinding], list[str]]:
    """Validate and lift LLM findings into the domain model.

    Small models hallucinate line numbers past the end of the file; those
    records are dropped rather than shown, because a finding the user cannot
    navigate to erodes trust in every other finding.
    """
    findings: list[SecurityFinding] = []
    rejected: list[str] = []

    for item in report.findings:
        if item.line_start > max(total_lines, 1):
            rejected.append(f"dropped '{item.title}': line {item.line_start} is out of range")
            continue
        cwe = item.cwe_id.strip().upper()
        findings.append(
            SecurityFinding(
                title=item.title.strip(),
                description=item.description.strip(),
                severity=Severity(item.severity),
                confidence=item.confidence,
                cwe_id=None if cwe in {"", "CWE-0", "N/A", "NONE"} else cwe,
                file_path=file_path,
                line_start=item.line_start,
                line_end=max(item.line_end, item.line_start),
                symbol=resolve_symbol(item.line_start),
                remediation=item.remediation.strip(),
                detector="llm",
            )
        )
    return findings, rejected


def make_security_node(llm: LlmClient) -> GraphNode:
    """Build the audit node bound to an inference client.

    The client is injected rather than imported so tests can supply a
    deterministic stub and the FastAPI layer can share one pooled connection.
    """

    async def security_node(state: AnalysisState) -> dict[str, Any]:
        recorder = TraceRecorder(node=NODE_NAME)
        resolve = _symbol_resolver(state.module)
        warnings: list[str] = []

        # --- pass 1: deterministic ------------------------------------------ #
        if state.language is Language.PYTHON:
            heuristic = scan_python(state.source_code, state.file_path, resolve)
        else:
            heuristic = scan_text(state.source_code, state.file_path)

        # --- pass 2: local LLM ---------------------------------------------- #
        llm_findings: list[SecurityFinding] = []
        if not state.config.enable_llm_audit:
            recorder.skip("LLM audit disabled for this run")
        else:
            module = state.module or ModuleProfile(
                path=state.file_path,
                language=state.language,
                loc=0,
                total_lines=len(state.source_code.splitlines()),
            )
            prompt = build_audit_prompt(module, state.source_code, heuristic)
            try:
                report, usage = await llm.structured(
                    model=state.config.audit_model,
                    system=AUDIT_SYSTEM_PROMPT,
                    prompt=prompt,
                    schema=LlmAuditReport,
                    temperature=state.config.temperature,
                )
                recorder.account(usage.prompt_tokens, usage.completion_tokens, usage.model)
                llm_findings, rejected = _to_domain(
                    report,
                    file_path=state.file_path,
                    total_lines=module.total_lines,
                    resolve_symbol=resolve,
                )
                warnings.extend(rejected)
            except (LlmUnavailableError, LlmResponseError) as exc:
                recorder.degrade(exc.message)
                warnings.append(
                    f"LLM audit unavailable ({exc.message}); reporting deterministic findings only."
                )
                logger.warning("LLM audit degraded: %s", exc.message)
            except Exception as exc:  # pragma: no cover - last-resort guard
                recorder.fail(exc)
                warnings.append(f"LLM audit failed unexpectedly: {exc}")
                logger.exception("Unhandled error during LLM audit")

        findings = merge_findings(heuristic, llm_findings)
        recorder.detail = (
            f"{len(heuristic)} heuristic + {len(llm_findings)} agent -> {len(findings)} after merge"
        )
        logger.info("Audit complete for %s: %s", state.file_path, recorder.detail)

        return {
            "phase": AnalysisPhase.REFACTORING,
            "findings": findings,
            "warnings": warnings,
            "traces": [recorder.build()],
        }

    return security_node
