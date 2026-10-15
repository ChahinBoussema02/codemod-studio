"""Local-inference layer: the only module in the service that opens a socket
for model calls."""

from app.llm.ollama import LlmClient, LlmResult, OllamaClient, strip_reasoning
from app.llm.prompts import (
    AUDIT_SYSTEM_PROMPT,
    REFACTOR_SYSTEM_PROMPT,
    LlmAuditReport,
    LlmFinding,
    LlmRefactorProposal,
    build_audit_prompt,
    build_refactor_prompt,
)

__all__ = [
    "AUDIT_SYSTEM_PROMPT",
    "REFACTOR_SYSTEM_PROMPT",
    "LlmAuditReport",
    "LlmClient",
    "LlmFinding",
    "LlmRefactorProposal",
    "LlmResult",
    "OllamaClient",
    "build_audit_prompt",
    "build_refactor_prompt",
    "strip_reasoning",
]
