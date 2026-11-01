"""End-to-end tests for the LangGraph pipeline.

These run the real compiled graph -- real routing, real state reducers, real
validation -- against :class:`StubLlm`. No Ollama, no network, no sleeping.
"""

from __future__ import annotations

import pytest

from app.agents.graph import build_graph, route_after_audit, route_after_parse
from app.agents.nodes.refactor import select_targets
from app.agents.nodes.report import build_summary
from app.agents.nodes.security import merge_findings
from app.core.errors import LlmUnavailableError
from app.llm.prompts import LlmAuditReport, LlmFinding, LlmRefactorProposal
from app.schemas.agent_state import (
    AnalysisPhase,
    AnalysisState,
    PipelineConfig,
    SecurityFinding,
    Severity,
)
from tests.conftest import StubLlm


async def run_graph(llm: StubLlm, state: AnalysisState) -> AnalysisState:
    """Invoke the compiled graph and return the validated final state."""
    graph = build_graph(llm)
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": state.run_id}})
    return AnalysisState.model_validate(result)


def make_state(source: str, **config_overrides) -> AnalysisState:
    return AnalysisState(
        file_path="svc.py",
        source_code=source,
        config=PipelineConfig(**config_overrides),
    )


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


async def test_full_pipeline_reaches_completion(opinionated_llm, vulnerable_source) -> None:
    final = await run_graph(opinionated_llm, make_state(vulnerable_source))

    assert final.phase is AnalysisPhase.COMPLETED
    assert final.module is not None and final.module.syntax_error is None
    assert final.summary is not None
    assert [t.node for t in final.traces] == [
        "parse_ast",
        "security_audit",
        "refactor",
        "report",
    ]


async def test_both_detectors_contribute_findings(opinionated_llm, vulnerable_source) -> None:
    final = await run_graph(opinionated_llm, make_state(vulnerable_source))
    detectors = {f.detector for f in final.findings}

    assert "heuristic" in detectors, "deterministic rules must always run"
    assert "llm" in detectors, "the agent's findings must survive into the report"
    assert any(f.rule_id == "PY-SQL-CONCAT" for f in final.findings)
    assert any(f.cwe_id == "CWE-862" for f in final.findings)


async def test_findings_are_sorted_by_severity(opinionated_llm, vulnerable_source) -> None:
    final = await run_graph(opinionated_llm, make_state(vulnerable_source))
    weights = [f.severity.weight for f in final.findings]
    assert weights == sorted(weights, reverse=True)


async def test_refactor_proposal_is_syntax_validated(opinionated_llm, vulnerable_source) -> None:
    final = await run_graph(opinionated_llm, make_state(vulnerable_source))

    assert len(final.proposals) == 1
    proposal = final.proposals[0]
    assert proposal.syntax_validated is True
    assert proposal.target_symbol == "run_report"
    assert proposal.strategy == "guard_clause"


async def test_each_agent_uses_its_configured_model(opinionated_llm, vulnerable_source) -> None:
    state = make_state(
        vulnerable_source, audit_model="qwen2.5-coder:7b", refactor_model="qwen2.5-coder:14b"
    )
    await run_graph(opinionated_llm, state)

    by_schema = {call["schema"]: call["model"] for call in opinionated_llm.calls}
    assert by_schema["LlmAuditReport"] == "qwen2.5-coder:7b"
    assert by_schema["LlmRefactorProposal"] == "qwen2.5-coder:14b"


async def test_known_issues_are_passed_to_the_audit_prompt(
    opinionated_llm, vulnerable_source
) -> None:
    await run_graph(opinionated_llm, make_state(vulnerable_source))
    audit_prompt = next(
        c["prompt"] for c in opinionated_llm.calls if c["schema"] == "LlmAuditReport"
    )
    assert "do NOT repeat these" in audit_prompt
    assert "PY-SQL-CONCAT" in audit_prompt


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


async def test_run_survives_an_unreachable_runtime(vulnerable_source) -> None:
    broken = StubLlm(raises=LlmUnavailableError("ollama is down"))
    final = await run_graph(broken, make_state(vulnerable_source))

    assert final.phase is AnalysisPhase.COMPLETED
    assert final.findings, "deterministic findings must still be reported"
    assert all(f.detector == "heuristic" for f in final.findings)
    assert any("unavailable" in w for w in final.warnings)
    assert final.traces[1].status.value == "degraded"


async def test_unparseable_file_skips_refactoring(opinionated_llm) -> None:
    final = await run_graph(opinionated_llm, make_state("def broken(\n    x = 1\n"))

    assert final.phase is AnalysisPhase.COMPLETED
    assert final.proposals == []
    assert final.module is not None and final.module.syntax_error is not None
    assert "refactor" not in [t.node for t in final.traces]


async def test_llm_findings_with_impossible_line_numbers_are_dropped() -> None:
    hallucinating = StubLlm(
        audit=LlmAuditReport(
            findings=[
                LlmFinding(
                    title="Phantom issue",
                    description="Reported on a line that does not exist.",
                    severity="critical",
                    cwe_id="CWE-0",
                    line_start=9999,
                    line_end=9999,
                    remediation="n/a",
                    confidence=0.9,
                )
            ]
        )
    )
    final = await run_graph(hallucinating, make_state("x = 1\n"))

    assert final.findings == []
    assert any("out of range" in w for w in final.warnings)


async def test_invalid_refactor_code_is_discarded(vulnerable_source) -> None:
    sloppy = StubLlm(
        audit=LlmAuditReport(findings=[]),
        refactor=LlmRefactorProposal(
            strategy="other",
            rationale="Looks tidier.",
            proposed_code="def run_report(user_input, cursor:\n    return None\n",
            estimated_risk="low",
            confidence=0.9,
            behaviour_preserved=True,
        ),
    )
    final = await run_graph(sloppy, make_state(vulnerable_source))

    assert final.proposals == []
    assert any("did not parse" in w for w in final.warnings)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


async def test_trivial_file_short_circuits_to_report(opinionated_llm) -> None:
    final = await run_graph(opinionated_llm, make_state("\n\n# just a comment\n"))

    assert final.phase is AnalysisPhase.COMPLETED
    assert [t.node for t in final.traces] == ["parse_ast", "report"]
    assert opinionated_llm.calls == [], "no inference should be spent on an empty file"


async def test_clean_file_skips_the_refactor_agent(silent_llm, clean_source) -> None:
    final = await run_graph(silent_llm, make_state(clean_source))

    assert [t.node for t in final.traces] == ["parse_ast", "security_audit", "report"]
    assert final.summary is not None and final.summary.grade == "A"


async def test_refactoring_can_be_disabled(opinionated_llm, vulnerable_source) -> None:
    final = await run_graph(
        opinionated_llm, make_state(vulnerable_source, enable_refactoring=False)
    )

    assert final.proposals == []
    assert "refactor" not in [t.node for t in final.traces]


async def test_llm_audit_can_be_disabled(opinionated_llm, vulnerable_source) -> None:
    final = await run_graph(
        opinionated_llm,
        make_state(vulnerable_source, enable_llm_audit=False, enable_refactoring=False),
    )

    assert opinionated_llm.calls == []
    assert final.findings, "heuristics still run"
    assert final.traces[1].status.value == "skipped"


def test_route_after_parse_handles_a_missing_module() -> None:
    state = AnalysisState(file_path="x.py", source_code="x = 1\n")
    assert route_after_parse(state) == "report"


def test_route_after_audit_respects_syntax_errors(vulnerable_source) -> None:
    from app.analysis.python_ast import PythonAstAnalyzer

    state = AnalysisState(
        file_path="x.py",
        source_code="def f(\n",
        module=PythonAstAnalyzer().analyze("def f(\n", "x.py"),
    )
    assert route_after_audit(state) == "report"


# --------------------------------------------------------------------------- #
# Unit-level behaviour
# --------------------------------------------------------------------------- #


def _finding(**overrides) -> SecurityFinding:
    base = {
        "title": "SQL injection",
        "description": "d",
        "severity": Severity.HIGH,
        "cwe_id": "CWE-89",
        "file_path": "a.py",
        "line_start": 10,
        "line_end": 10,
        "remediation": "r",
        "confidence": 0.6,
    }
    return SecurityFinding(**{**base, **overrides})


def test_merge_promotes_corroborated_findings_to_hybrid() -> None:
    heuristic = _finding(detector="heuristic", confidence=0.6, severity=Severity.MEDIUM)
    agent = _finding(detector="llm", confidence=0.8, severity=Severity.CRITICAL)

    (merged,) = merge_findings([heuristic], [agent])

    assert merged.detector == "hybrid"
    assert merged.severity is Severity.CRITICAL, "the higher severity wins"
    assert merged.confidence == pytest.approx(0.9), "agreement raises confidence"


def test_merge_keeps_distinct_findings_apart() -> None:
    merged = merge_findings([_finding()], [_finding(line_start=99, line_end=99)])
    assert len(merged) == 2


def test_merge_collapses_same_line_despite_disagreeing_cwe() -> None:
    """Regression: an MD5 call reported twice with different CWEs.

    A live 7B run produced CWE-327 (correct, heuristic) and CWE-789
    (meaningless for Python, agent) for the same line. Keying the merge on
    the CWE left both rows in the report.
    """
    heuristic = _finding(
        title="Broken hash algorithm `md5`", cwe_id="CWE-327", severity=Severity.MEDIUM
    )
    agent = _finding(
        title="Information Disclosure in Errors",
        cwe_id="CWE-789",
        severity=Severity.MEDIUM,
        detector="llm",
        confidence=1.0,
    )

    (merged,) = merge_findings([heuristic], [agent])

    assert merged.cwe_id == "CWE-327", "the curated CWE must survive the merge"
    assert merged.detector == "hybrid"


def test_risk_score_saturates_and_orders_grades() -> None:
    low = AnalysisState(
        file_path="a.py", source_code="x=1\n", findings=[_finding(severity=Severity.LOW)]
    )
    critical = AnalysisState(
        file_path="a.py",
        source_code="x=1\n",
        findings=[_finding(severity=Severity.CRITICAL, confidence=1.0)],
    )
    many = AnalysisState(
        file_path="a.py",
        source_code="x=1\n",
        findings=[_finding(severity=Severity.CRITICAL, line_start=i) for i in range(1, 40)],
    )

    assert build_summary(low).grade == "A"
    assert build_summary(critical).risk_score > build_summary(low).risk_score
    assert build_summary(many).risk_score <= 100.0
    assert build_summary(many).grade == "F"


def test_summary_headline_names_the_worst_finding() -> None:
    state = AnalysisState(
        file_path="a.py",
        source_code="x=1\n",
        findings=[
            _finding(severity=Severity.LOW, title="Minor"),
            _finding(severity=Severity.CRITICAL, title="Remote code execution", line_start=4),
        ],
    )
    assert "Remote code execution" in build_summary(state).headline


def test_select_targets_prioritises_findings_over_raw_complexity(vulnerable_source) -> None:
    from app.analysis.python_ast import PythonAstAnalyzer

    module = PythonAstAnalyzer().analyze(vulnerable_source, "svc.py")
    state = AnalysisState(
        file_path="svc.py",
        source_code=vulnerable_source,
        module=module,
        findings=[
            _finding(
                file_path="svc.py",
                severity=Severity.CRITICAL,
                symbol="run_report",
                line_start=15,
                line_end=15,
            )
        ],
    )
    targets = select_targets(state)

    assert targets, "a function carrying a critical finding must be a target"
    function, related = targets[0]
    assert function.signature.qualified_name == "run_report"
    assert related and related[0].severity is Severity.CRITICAL


def test_select_targets_is_capped_by_config() -> None:
    source = "".join(
        f"def f{i}(a, b):\n"
        + "".join(f"    if a == {j} and b:\n        a += 1\n" for j in range(10))
        + "    return a\n\n"
        for i in range(10)
    )
    from app.analysis.python_ast import PythonAstAnalyzer

    state = AnalysisState(
        file_path="m.py",
        source_code=source,
        module=PythonAstAnalyzer().analyze(source, "m.py"),
        config=PipelineConfig(max_hotspots=3),
    )
    assert len(select_targets(state)) == 3


async def test_proposal_only_claims_findings_it_was_shown(
    opinionated_llm, vulnerable_source
) -> None:
    """Regression: `addresses_finding_ids` listed every finding in the function.

    Matching on symbol swept in findings below the refactor threshold that
    were never sent to the model, so the proposal claimed to fix issues it
    had not seen.
    """
    final = await run_graph(opinionated_llm, make_state(vulnerable_source))
    (proposal,) = final.proposals

    claimed = set(proposal.addresses_finding_ids)
    by_id = {f.id: f for f in final.findings}
    threshold = final.config.min_severity_to_refactor.weight

    assert claimed, "a refactor driven by findings should cite them"
    assert all(by_id[finding_id].severity.weight >= threshold for finding_id in claimed), (
        "must not claim findings below the severity threshold"
    )
    assert all(by_id[finding_id].symbol == proposal.target_symbol for finding_id in claimed)
