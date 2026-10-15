"""Language -> analyzer resolution.

A tiny registry rather than an ``if/elif`` chain so that adding the
Tree-sitter backend in Phase 3 is a single ``register()`` call and requires no
change to the graph nodes.
"""

from __future__ import annotations

from app.analysis.base import SourceAnalyzer
from app.analysis.python_ast import PythonAstAnalyzer
from app.schemas.agent_state import Language

__all__ = ["get_analyzer", "register", "supported_languages"]

_REGISTRY: dict[Language, SourceAnalyzer] = {}

#: Mapping used to infer a language when the client does not declare one.
_EXTENSION_MAP: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".cs": Language.CSHARP,
    ".java": Language.JAVA,
    ".go": Language.GO,
}


def register(analyzer: SourceAnalyzer) -> None:
    """Bind an analyzer to every language it declares support for."""
    if not analyzer.languages:
        raise ValueError(f"{type(analyzer).__name__} declares no languages")
    for language in analyzer.languages:
        _REGISTRY[language] = analyzer


def get_analyzer(language: Language) -> SourceAnalyzer | None:
    """Return the backend for ``language``, or ``None`` if unsupported.

    Callers must handle ``None`` by degrading to text-level heuristics; the
    pipeline is explicitly designed to produce partial results for languages
    whose grammar is not wired up yet.
    """
    return _REGISTRY.get(language)


def supported_languages() -> tuple[Language, ...]:
    return tuple(_REGISTRY)


def infer_language(path: str) -> Language:
    """Guess the language from a file extension."""
    lowered = path.lower()
    for extension, language in _EXTENSION_MAP.items():
        if lowered.endswith(extension):
            return language
    return Language.UNKNOWN


register(PythonAstAnalyzer())
