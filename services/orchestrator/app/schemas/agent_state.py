"""Canonical domain model for the multi-agent analysis pipeline.

Everything that crosses a node boundary in the LangGraph state machine is
defined here. :class:`AnalysisState` is used directly as the graph's state
schema, which means LangGraph validates every partial update a node returns
against these models -- a malformed node output fails loudly at the boundary
instead of silently corrupting downstream state.

Design rules enforced in this module:

* **Accumulators use reducers.** Fields that several nodes contribute to are
  annotated with :func:`operator.add` so concurrent (fan-out) nodes merge
  cleanly instead of overwriting each other.
* **Scalars are last-write-wins.** Phase, risk score and error fields are plain
  fields; only one node owns each at a time.
* **Nothing here imports LangGraph.** The domain model stays framework-free so
  it can be reused by the CLI, the tests and the FastAPI layer.
"""

from __future__ import annotations

import operator
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

__all__ = [
    "AgentTrace",
    "AnalysisPhase",
    "AnalysisState",
    "ComplexityMetrics",
    "FunctionProfile",
    "FunctionSignature",
    "Language",
    "ModuleProfile",
    "Parameter",
    "PipelineConfig",
    "RefactorProposal",
    "RiskSummary",
    "SecurityFinding",
    "Severity",
]


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (``datetime.utcnow`` is deprecated)."""
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Language(str, Enum):
    """Source languages the studio can ingest.

    Only ``PYTHON`` has a native analyzer in Phase 1/2; the rest are routed to
    the Tree-sitter backend as it comes online (see ``app.analysis.registry``).
    """

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    CSHARP = "csharp"
    JAVA = "java"
    GO = "go"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    """Finding severity, ordered from least to most urgent."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def weight(self) -> int:
        """Numeric weight used by the risk-scoring aggregation."""
        return _SEVERITY_WEIGHTS[self]


_SEVERITY_WEIGHTS: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 4,
    Severity.HIGH: 9,
    Severity.CRITICAL: 20,
}


class AnalysisPhase(str, Enum):
    """Lifecycle of a single analysis run, mirrored in the IDE status bar."""

    PENDING = "pending"
    PARSING = "parsing"
    AUDITING = "auditing"
    REFACTORING = "refactoring"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"


class NodeStatus(str, Enum):
    OK = "ok"
    SKIPPED = "skipped"
    DEGRADED = "degraded"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# Static-analysis artefacts
# --------------------------------------------------------------------------- #


class Parameter(BaseModel):
    """A single parameter in a function signature."""

    model_config = ConfigDict(frozen=True)

    name: str
    annotation: str | None = Field(
        default=None, description="Source text of the type annotation, if present."
    )
    default: str | None = Field(
        default=None, description="Source text of the default value, if present."
    )
    kind: Literal[
        "positional_only", "positional_or_keyword", "var_positional", "keyword_only", "var_keyword"
    ] = "positional_or_keyword"

    def render(self) -> str:
        """Render the parameter back to (approximately) its source form."""
        prefix = {"var_positional": "*", "var_keyword": "**"}.get(self.kind, "")
        text = f"{prefix}{self.name}"
        if self.annotation:
            text += f": {self.annotation}"
        if self.default is not None:
            text += f" = {self.default}" if self.annotation else f"={self.default}"
        return text


class FunctionSignature(BaseModel):
    """Language-agnostic description of a callable's interface."""

    model_config = ConfigDict(frozen=True)

    name: str
    qualified_name: str = Field(
        description="Dotted path within the module, e.g. ``PaymentService.charge``."
    )
    parameters: tuple[Parameter, ...] = ()
    return_annotation: str | None = None
    is_async: bool = False
    is_method: bool = False
    decorators: tuple[str, ...] = ()
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    def render(self) -> str:
        """Human-readable one-line signature, used in prompts and the webview."""
        params = ", ".join(p.render() for p in self.parameters)
        prefix = "async def" if self.is_async else "def"
        suffix = f" -> {self.return_annotation}" if self.return_annotation else ""
        return f"{prefix} {self.qualified_name}({params}){suffix}"


class ComplexityMetrics(BaseModel):
    """Per-function complexity measurements produced by the AST backend."""

    model_config = ConfigDict(frozen=True)

    cyclomatic: int = Field(ge=1, description="McCabe complexity (decision points + 1).")
    cognitive: int = Field(ge=0, description="Sonar-style cognitive complexity.")
    max_nesting: int = Field(ge=0, description="Deepest nesting level inside the body.")
    length: int = Field(ge=0, description="Physical lines of code in the body.")
    parameter_count: int = Field(ge=0)
    return_points: int = Field(ge=0)
    halstead_volume: float = Field(ge=0.0)
    maintainability_index: float = Field(
        ge=0.0, le=100.0, description="Normalised 0-100; below 65 is considered hard to maintain."
    )
    has_docstring: bool = False

    @property
    def is_hotspot(self) -> bool:
        """Heuristic used to decide which functions deserve LLM attention."""
        return self.cyclomatic >= 8 or self.cognitive >= 12 or self.maintainability_index < 65


class FunctionProfile(BaseModel):
    """A function plus everything static analysis learned about it."""

    signature: FunctionSignature
    metrics: ComplexityMetrics
    source: str = Field(
        default="", description="Verbatim source of the function; never leaves the machine."
    )
    calls: tuple[str, ...] = Field(
        default=(), description="Names invoked inside the body (best-effort, unresolved)."
    )


class ClassProfile(BaseModel):
    name: str
    bases: tuple[str, ...] = ()
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    method_names: tuple[str, ...] = ()
    has_docstring: bool = False


class ModuleProfile(BaseModel):
    """Structural summary of one source file."""

    path: str
    language: Language
    loc: int = Field(ge=0, description="Non-blank, non-comment lines.")
    total_lines: int = Field(ge=0)
    imports: tuple[str, ...] = ()
    functions: tuple[FunctionProfile, ...] = ()
    classes: tuple[ClassProfile, ...] = ()
    syntax_error: str | None = Field(
        default=None, description="Set when the file could not be parsed; analysis degrades."
    )

    @property
    def average_cyclomatic(self) -> float:
        if not self.functions:
            return 0.0
        return sum(f.metrics.cyclomatic for f in self.functions) / len(self.functions)

    def hotspots(self) -> tuple[FunctionProfile, ...]:
        """Functions worth spending local-LLM tokens on, worst first."""
        ranked = sorted(
            (f for f in self.functions if f.metrics.is_hotspot),
            key=lambda f: (f.metrics.cognitive, f.metrics.cyclomatic),
            reverse=True,
        )
        return tuple(ranked)


# --------------------------------------------------------------------------- #
# Agent outputs
# --------------------------------------------------------------------------- #


class SecurityFinding(BaseModel):
    """A single vulnerability or weakness attributed to a code location."""

    id: str = Field(default_factory=lambda: _new_id("find"))
    title: str = Field(max_length=160)
    description: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    cwe_id: str | None = Field(default=None, examples=["CWE-89"])
    owasp_category: str | None = None
    file_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol: str | None = Field(default=None, description="Enclosing function/class, if known.")
    remediation: str = Field(description="Concrete fix, not generic advice.")
    detector: Literal["heuristic", "llm", "hybrid"] = "heuristic"
    rule_id: str | None = None

    @field_validator("line_end")
    @classmethod
    def _end_after_start(cls, v: int, info: ValidationInfo) -> int:
        """Clamp an inverted range rather than rejecting the finding.

        Small models occasionally emit ``line_end < line_start``. Losing a
        real finding to an off-by-one is worse than a one-line range.
        """
        start = info.data.get("line_start")
        if isinstance(start, int) and v < start:
            return start
        return v

    def dedupe_key(self) -> tuple[str, int]:
        """Two findings on the same line are treated as one issue.

        The CWE deliberately plays no part. Local models assign CWE
        identifiers incorrectly often enough that including one splits a
        single real issue into two rows -- observed in practice with an MD5
        call reported once as CWE-327 (correct) and once as CWE-789
        (meaningless here). Occasionally merging two genuinely distinct
        issues that share a line is the cheaper error.
        """
        return (self.file_path, self.line_start)


class RefactorProposal(BaseModel):
    """A semantic refactoring suggestion with a verifiable replacement body."""

    id: str = Field(default_factory=lambda: _new_id("ref"))
    file_path: str
    target_symbol: str
    strategy: Literal[
        "extract_function",
        "simplify_conditional",
        "remove_duplication",
        "introduce_type_hints",
        "guard_clause",
        "harden_input",
        "other",
    ] = "other"
    rationale: str
    proposed_code: str = Field(description="Full replacement source for the target symbol.")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    estimated_risk: Severity = Severity.LOW
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    syntax_validated: bool = Field(
        default=False,
        description="True once the proposal was re-parsed successfully by the AST backend.",
    )
    addresses_finding_ids: tuple[str, ...] = ()


class AgentTrace(BaseModel):
    """Execution record for one graph node; the backbone of telemetry."""

    node: str
    status: NodeStatus = NodeStatus.OK
    started_at: datetime = Field(default_factory=_utcnow)
    duration_ms: float = Field(ge=0.0, default=0.0)
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    detail: str | None = None
    error: str | None = None


class RiskSummary(BaseModel):
    """Aggregate verdict presented in the IDE and shipped to telemetry."""

    risk_score: float = Field(ge=0.0, le=100.0, default=0.0)
    grade: Literal["A", "B", "C", "D", "F"] = "A"
    findings_by_severity: dict[Severity, int] = Field(default_factory=dict)
    total_findings: int = 0
    total_proposals: int = 0
    hotspot_functions: tuple[str, ...] = ()
    headline: str = "No issues detected."


# --------------------------------------------------------------------------- #
# Pipeline configuration & state
# --------------------------------------------------------------------------- #


class PipelineConfig(BaseModel):
    """Per-run knobs. Immutable for the lifetime of a graph invocation."""

    model_config = ConfigDict(frozen=True)

    audit_model: str = "qwen2.5-coder:7b"
    refactor_model: str = "qwen2.5-coder:14b"
    reasoning_model: str = "deepseek-r1:8b"
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    enable_llm_audit: bool = True
    enable_refactoring: bool = True
    max_hotspots: int = Field(default=5, ge=1, le=50)
    min_severity_to_refactor: Severity = Severity.MEDIUM
    offline_only: bool = Field(
        default=True,
        description="Hard guarantee that no source code leaves the host. Always true in prod.",
    )


class AnalysisState(BaseModel):
    """The LangGraph state object.

    Nodes return *partial* dictionaries; LangGraph merges them using the
    reducer attached to each annotated field. Plain fields are replaced.
    """

    model_config = ConfigDict(validate_assignment=True)

    # --- inputs (written once by the API layer) ---------------------------- #
    run_id: str = Field(default_factory=lambda: _new_id("run"))
    file_path: str
    source_code: str
    language: Language = Language.PYTHON
    config: PipelineConfig = Field(default_factory=PipelineConfig)

    # --- scalar progress (last write wins) --------------------------------- #
    phase: AnalysisPhase = AnalysisPhase.PENDING
    module: ModuleProfile | None = None
    summary: RiskSummary | None = None
    fatal_error: str | None = None

    # --- accumulators (merged across nodes) -------------------------------- #
    findings: Annotated[list[SecurityFinding], operator.add] = Field(default_factory=list)
    proposals: Annotated[list[RefactorProposal], operator.add] = Field(default_factory=list)
    traces: Annotated[list[AgentTrace], operator.add] = Field(default_factory=list)
    warnings: Annotated[list[str], operator.add] = Field(default_factory=list)

    # --- derived helpers ---------------------------------------------------- #

    def actionable_findings(self, minimum: Severity) -> list[SecurityFinding]:
        """Findings at or above ``minimum`` severity, worst first."""
        threshold = minimum.weight
        return sorted(
            (f for f in self.findings if f.severity.weight >= threshold),
            key=lambda f: (f.severity.weight, f.confidence),
            reverse=True,
        )

    def total_duration_ms(self) -> float:
        return sum(t.duration_ms for t in self.traces)
