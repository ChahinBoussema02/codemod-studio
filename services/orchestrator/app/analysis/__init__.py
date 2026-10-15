"""Static analysis backends: parsing, metrics and deterministic security rules."""

from app.analysis.base import AnalyzerError, SourceAnalyzer
from app.analysis.python_ast import PythonAstAnalyzer, compute_metrics
from app.analysis.registry import get_analyzer, infer_language, register, supported_languages
from app.analysis.security_rules import scan_python, scan_text

__all__ = [
    "AnalyzerError",
    "PythonAstAnalyzer",
    "SourceAnalyzer",
    "compute_metrics",
    "get_analyzer",
    "infer_language",
    "register",
    "scan_python",
    "scan_text",
    "supported_languages",
]
