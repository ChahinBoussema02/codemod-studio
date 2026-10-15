"""Test fixtures.

The key fixture is :class:`StubLlm`: a deterministic implementation of the
:class:`~app.llm.ollama.LlmClient` protocol. Because the nodes depend on that
protocol rather than on a concrete client, the entire graph -- routing,
merging, validation, reporting -- is testable with no Ollama, no network and
no flakiness.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.ollama import LlmResult
from app.llm.prompts import LlmAuditReport, LlmFinding, LlmRefactorProposal

T = TypeVar("T", bound=BaseModel)

VULNERABLE_SOURCE = '''\
import hashlib
import subprocess

DB_PASSWORD = "sup3rs3cret-prod-value"


def run_report(user_input, cursor, mode="fast"):
    """Build and run a report query."""
    if user_input:
        if mode == "fast":
            for row in range(10):
                if row % 2 == 0:
                    try:
                        cursor.execute("SELECT * FROM t WHERE n = '" + user_input + "'")
                    except Exception:
                        pass
        elif mode == "slow":
            subprocess.run("report " + user_input, shell=True)
        else:
            return None
    return hashlib.md5(user_input.encode()).hexdigest()
'''

CLEAN_SOURCE = '''\
def add(left: int, right: int) -> int:
    """Return the sum of two integers."""
    return left + right
'''


class StubLlm:
    """Deterministic stand-in for the Ollama client.

    Records every call so tests can assert on prompts and model selection.
    """

    def __init__(
        self,
        *,
        audit: LlmAuditReport | None = None,
        refactor: LlmRefactorProposal | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.audit = audit if audit is not None else LlmAuditReport(findings=[])
        self.refactor = refactor
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        temperature: float = 0.1,
    ) -> tuple[T, LlmResult]:
        self.calls.append({"model": model, "schema": schema.__name__, "prompt": prompt})
        if self.raises is not None:
            raise self.raises

        if schema is LlmAuditReport:
            payload: Any = self.audit
        elif schema is LlmRefactorProposal:
            if self.refactor is None:
                raise AssertionError("StubLlm was asked for a refactor but none was configured")
            payload = self.refactor
        else:  # pragma: no cover - guards against new schemas slipping through
            raise AssertionError(f"StubLlm has no canned response for {schema.__name__}")

        usage = LlmResult(
            content=payload.model_dump_json(),
            model=model,
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=12.0,
        )
        return payload, usage  # type: ignore[return-value]

    async def available_models(self) -> list[str]:
        return ["qwen2.5-coder:7b", "qwen2.5-coder:14b"]

    async def ping(self) -> bool:
        return True


@pytest.fixture
def vulnerable_source() -> str:
    return VULNERABLE_SOURCE


@pytest.fixture
def clean_source() -> str:
    return CLEAN_SOURCE


@pytest.fixture
def silent_llm() -> StubLlm:
    """An LLM that finds nothing -- isolates the deterministic detectors."""
    return StubLlm()


@pytest.fixture
def opinionated_llm() -> StubLlm:
    """An LLM that reports a finding and proposes a valid refactor."""
    return StubLlm(
        audit=LlmAuditReport(
            findings=[
                LlmFinding(
                    title="Missing authorisation check",
                    description="run_report executes queries without verifying the caller.",
                    severity="high",
                    cwe_id="CWE-862",
                    line_start=7,
                    line_end=7,
                    remediation="Require an authenticated principal and check it before querying.",
                    confidence=0.7,
                )
            ],
            reviewed_reasoning="Checked trust boundaries and query construction.",
        ),
        refactor=LlmRefactorProposal(
            strategy="guard_clause",
            rationale="Flattens nesting and parameterises the query.",
            proposed_code=(
                'def run_report(user_input, cursor, mode="fast"):\n'
                '    """Build and run a report query."""\n'
                "    if not user_input:\n"
                "        return None\n"
                '    cursor.execute("SELECT * FROM t WHERE n = %s", (user_input,))\n'
                "    return cursor.fetchall()\n"
            ),
            estimated_risk="low",
            confidence=0.8,
            behaviour_preserved=True,
        ),
    )
