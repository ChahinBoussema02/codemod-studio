"""Analyzer abstraction.

Phase 1 ships a single native backend (CPython's ``ast``). Phase 3 adds
Tree-sitter grammars for TypeScript, C# and Go. Both satisfy
:class:`SourceAnalyzer`, so the graph's parse node never learns which backend
it is talking to.
"""

from __future__ import annotations

import abc

from app.schemas.agent_state import Language, ModuleProfile

__all__ = ["AnalyzerError", "SourceAnalyzer"]


class AnalyzerError(RuntimeError):
    """Raised when a backend fails for a reason other than invalid syntax.

    Invalid syntax is *not* an error: it is reported via
    :attr:`ModuleProfile.syntax_error` so the pipeline can degrade to
    text-only heuristics rather than aborting.
    """


class SourceAnalyzer(abc.ABC):
    """Produces a :class:`ModuleProfile` from source text."""

    #: Languages this backend claims. Checked by the registry at import time.
    languages: tuple[Language, ...] = ()

    @abc.abstractmethod
    def analyze(self, source: str, path: str) -> ModuleProfile:
        """Parse ``source`` and return its structural profile.

        Implementations must never raise on malformed input; they set
        :attr:`ModuleProfile.syntax_error` instead.
        """

    @abc.abstractmethod
    def is_valid_syntax(self, source: str) -> tuple[bool, str | None]:
        """Cheap syntax check used to validate LLM-generated replacements."""
