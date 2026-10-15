#!/usr/bin/env python3
"""Standalone AST profiler.

Runs the same analysis the graph's first node runs, with no FastAPI, no
LangGraph and no Ollama -- useful as a pre-commit hook, in CI, or to sanity
check the metrics on a file you already know well.

Usage:
    python scripts/analyze_file.py app/agents/graph.py
    python scripts/analyze_file.py src/**/*.py --json --min-complexity 10
    python scripts/analyze_file.py src/ --fail-over 15     # exits 1 when breached

Exit codes:
    0  clean
    1  a function exceeded --fail-over, or a scanned file did not parse
    2  bad invocation
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

# Allow running from the repo without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis.python_ast import PythonAstAnalyzer
from app.analysis.security_rules import scan_python
from app.schemas.agent_state import ModuleProfile

_RESET = "\033[0m"
_COLOURS = {"green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "dim": "\033[2m"}


def _paint(text: str, colour: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_COLOURS[colour]}{text}{_RESET}"


def _severity_colour(cyclomatic: int) -> str:
    if cyclomatic >= 15:
        return "red"
    if cyclomatic >= 8:
        return "yellow"
    return "green"


def iter_python_files(paths: Sequence[str]) -> Iterable[Path]:
    """Expand files and directories into a de-duplicated list of .py paths."""
    seen: set[Path] = set()
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_dir():
            found = sorted(candidate.rglob("*.py"))
        elif candidate.is_file():
            found = [candidate]
        else:
            print(f"warning: {raw} does not exist", file=sys.stderr)
            continue
        for path in found:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def render_module(
    module: ModuleProfile,
    *,
    min_complexity: int,
    colour: bool,
    show_security: bool,
    source: str,
) -> None:
    """Print a human-readable report for one module."""
    header = f"{module.path}  ({module.loc} LOC, {len(module.functions)} functions)"
    print(_paint(header, "dim", enabled=colour))

    if module.syntax_error:
        print(_paint(f"  SYNTAX ERROR: {module.syntax_error}", "red", enabled=colour))
        return

    shown = [f for f in module.functions if f.metrics.cyclomatic >= min_complexity]
    if not shown:
        message = f"  (no function at or above complexity {min_complexity})"
        print(_paint(message, "dim", enabled=colour))
    for function in sorted(shown, key=lambda f: f.metrics.cyclomatic, reverse=True):
        metrics = function.metrics
        colour_name = _severity_colour(metrics.cyclomatic)
        marker = _paint(f"CC {metrics.cyclomatic:>3}", colour_name, enabled=colour)
        print(
            f"  {marker}  cog {metrics.cognitive:>3}  nest {metrics.max_nesting}  "
            f"MI {metrics.maintainability_index:>5.1f}  "
            f"L{function.signature.line_start}-{function.signature.line_end}  "
            f"{function.signature.qualified_name}"
        )
        if not metrics.has_docstring:
            print(_paint("        no docstring", "dim", enabled=colour))

    if show_security:
        findings = scan_python(source, module.path)
        for finding in findings:
            print(
                _paint(
                    f"  [{finding.severity.value.upper():8s}] L{finding.line_start:<4d} "
                    f"{finding.title}  ({finding.rule_id})",
                    "red" if finding.severity.weight >= 9 else "yellow",
                    enabled=colour,
                )
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze_file",
        description="Extract function signatures and complexity metrics from Python source.",
    )
    parser.add_argument("paths", nargs="+", help="Files or directories to analyse.")
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of a table."
    )
    parser.add_argument(
        "--min-complexity",
        type=int,
        default=1,
        help="Hide functions below this cyclomatic complexity (default: 1).",
    )
    parser.add_argument(
        "--fail-over",
        type=int,
        default=None,
        metavar="N",
        help="Exit 1 if any function exceeds cyclomatic complexity N.",
    )
    parser.add_argument(
        "--security", action="store_true", help="Also run the deterministic security rules."
    )
    parser.add_argument("--no-colour", action="store_true", help="Disable ANSI colour.")
    args = parser.parse_args(argv)

    files = list(iter_python_files(args.paths))
    if not files:
        print("error: no Python files found", file=sys.stderr)
        return 2

    analyzer = PythonAstAnalyzer()
    colour = not args.no_colour and sys.stdout.isatty()
    modules: list[ModuleProfile] = []
    breached = False
    unparseable = False

    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
            continue

        module = analyzer.analyze(source, str(path))
        modules.append(module)
        unparseable = unparseable or module.syntax_error is not None

        if args.fail_over is not None:
            breached = breached or any(
                f.metrics.cyclomatic > args.fail_over for f in module.functions
            )

        if not args.json:
            render_module(
                module,
                min_complexity=args.min_complexity,
                colour=colour,
                show_security=args.security,
                source=source,
            )

    if args.json:
        # Function bodies are excluded: the JSON output is for dashboards and
        # CI, and shipping source into a build log defeats the point of the
        # whole project.
        without_source = {"functions": {"__all__": {"source"}}}
        payload = [m.model_dump(mode="json", exclude=without_source) for m in modules]
        print(json.dumps(payload, indent=2))

    if breached:
        print(
            f"\nFAIL: at least one function exceeds cyclomatic complexity {args.fail_over}",
            file=sys.stderr,
        )
        return 1
    return 1 if unparseable and args.fail_over is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
