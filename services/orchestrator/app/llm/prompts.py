"""Prompts and the LLM-facing schemas they are constrained to.

These schemas are intentionally *flatter and smaller* than the domain models
in :mod:`app.schemas.agent_state`. Schema-constrained decoding on a 7B model
degrades quickly with nesting depth and optional unions, so the model is asked
for the minimum it actually knows, and the node layer enriches the result with
everything deterministic (file path, symbol, detector provenance).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.agent_state import FunctionProfile, ModuleProfile, SecurityFinding

__all__ = [
    "AUDIT_SYSTEM_PROMPT",
    "REFACTOR_SYSTEM_PROMPT",
    "LlmAuditReport",
    "LlmFinding",
    "LlmRefactorProposal",
    "build_audit_prompt",
    "build_refactor_prompt",
]

# --------------------------------------------------------------------------- #
# Constrained output schemas
# --------------------------------------------------------------------------- #


class LlmFinding(BaseModel):
    """One vulnerability as reported by the auditing agent."""

    title: str = Field(description="Under 12 words, naming the specific weakness.")
    description: str = Field(description="Why this specific code is exploitable.")
    severity: Literal["info", "low", "medium", "high", "critical"]
    cwe_id: str = Field(description="CWE identifier, e.g. CWE-89. Use CWE-0 if unsure.")
    line_start: int = Field(ge=1, description="First line of the offending code.")
    line_end: int = Field(ge=1)
    remediation: str = Field(description="The concrete change to make, not generic advice.")
    confidence: float = Field(ge=0.0, le=1.0)


class LlmAuditReport(BaseModel):
    """Top-level audit response. An empty list is a valid, expected answer."""

    findings: list[LlmFinding] = Field(default_factory=list, max_length=12)
    reviewed_reasoning: str = Field(
        default="",
        description="One sentence on what was checked. Keeps the model honest about empties.",
    )


class LlmRefactorProposal(BaseModel):
    """A rewritten function body plus the reasoning behind it."""

    strategy: Literal[
        "extract_function",
        "simplify_conditional",
        "remove_duplication",
        "introduce_type_hints",
        "guard_clause",
        "harden_input",
        "other",
    ]
    rationale: str = Field(description="What improves, in terms a reviewer can verify.")
    proposed_code: str = Field(
        description="The complete replacement definition, correctly indented, no fences."
    )
    estimated_risk: Literal["info", "low", "medium", "high", "critical"]
    confidence: float = Field(ge=0.0, le=1.0)
    behaviour_preserved: bool = Field(
        description="False if the rewrite intentionally changes observable behaviour."
    )


# --------------------------------------------------------------------------- #
# System prompts
# --------------------------------------------------------------------------- #

AUDIT_SYSTEM_PROMPT = """\
You are a senior application-security engineer performing a code review.

Rules:
- Report only weaknesses you can point to in the code shown. Do not speculate \
about code you cannot see.
- A missing control is only a finding if this code is the right place for it.
- Do not repeat findings that are already listed as "known issues" in the \
prompt; those were found by a deterministic scanner.
- Prefer precision over volume. Zero findings is a correct answer for safe code.
- Line numbers must refer to the numbered listing given to you.
- Severity reflects exploitability and blast radius, not how unusual the code is.
"""

REFACTOR_SYSTEM_PROMPT = """\
You are a senior engineer performing a behaviour-preserving refactor.

Rules:
- Return the complete replacement for the function shown, including its \
`def` line and decorators. No markdown fences, no commentary outside the \
JSON fields.
- Preserve the public signature unless fixing it is the stated goal; callers \
you cannot see depend on it.
- Preserve observable behaviour, including raised exception types and return \
values for every branch. Set behaviour_preserved=false if you cannot.
- Keep the module's existing imports; do not invent dependencies.
- Match the surrounding indentation exactly -- the output is spliced back into \
the file verbatim.
- Improve clarity and reduce nesting. Do not golf the code.
"""


# --------------------------------------------------------------------------- #
# Prompt builders
# --------------------------------------------------------------------------- #


def _numbered(source: str, start_line: int = 1) -> str:
    """Render source with absolute line numbers so the model can cite them."""
    lines = source.splitlines()
    width = len(str(start_line + len(lines)))
    return "\n".join(
        f"{start_line + offset:>{width}} | {line}" for offset, line in enumerate(lines)
    )


def _format_known_issues(findings: list[SecurityFinding]) -> str:
    if not findings:
        return "(none)"
    return "\n".join(
        f"- line {f.line_start}: [{f.severity.value}] {f.title} ({f.rule_id})" for f in findings
    )


def build_audit_prompt(
    module: ModuleProfile,
    source: str,
    known_issues: list[SecurityFinding],
) -> str:
    """Assemble the audit request for a whole module.

    The deterministic findings are included so the model spends its budget on
    what the scanner *cannot* see, rather than re-deriving `eval()` is unsafe.
    """
    hotspots = ", ".join(
        f"{f.signature.qualified_name} (cyclomatic {f.metrics.cyclomatic}, "
        f"cognitive {f.metrics.cognitive})"
        for f in module.hotspots()[:5]
    )
    return f"""\
File: {module.path}
Language: {module.language.value}
Imports: {", ".join(module.imports) or "(none)"}
Complexity hotspots: {hotspots or "(none)"}

Known issues already reported by the deterministic scanner -- do NOT repeat these:
{_format_known_issues(known_issues)}

Review the following source for security weaknesses the scanner would miss:
missing authorisation checks, trust-boundary mistakes, unsafe defaults, race
conditions, information disclosure in errors, and flawed validation logic.

```
{_numbered(source)}
```
"""


def build_refactor_prompt(
    function: FunctionProfile,
    module: ModuleProfile,
    related_findings: list[SecurityFinding],
) -> str:
    """Assemble the refactor request for one function."""
    metrics = function.metrics
    issues = (
        "\n".join(f"- {f.title}: {f.remediation}" for f in related_findings)
        if related_findings
        else "(none -- this is a pure maintainability refactor)"
    )
    return f"""\
File: {module.path}
Function: {function.signature.render()}
Lines {function.signature.line_start}-{function.signature.line_end}

Measured problems:
- cyclomatic complexity {metrics.cyclomatic}
- cognitive complexity {metrics.cognitive}
- maximum nesting depth {metrics.max_nesting}
- {metrics.return_points} return points, {metrics.parameter_count} parameters
- docstring: {"present" if metrics.has_docstring else "missing"}

Security issues this refactor should also resolve:
{issues}

Available module imports: {", ".join(module.imports) or "(none)"}

Source to replace:
```
{function.source}
```
"""
