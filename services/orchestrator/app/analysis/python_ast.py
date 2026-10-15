"""Native Python AST backend.

Uses CPython's own ``ast`` module -- no third-party parser, no network, no
subprocess. That matters for two reasons:

1. **Zero data leakage.** The source never leaves the process.
2. **Speed.** Parsing a 2k-line module takes single-digit milliseconds, so the
   graph can run the structural pass on every keystroke-triggered save and
   reserve the expensive local-LLM passes for flagged hotspots.

Metrics implemented
-------------------
``cyclomatic``
    McCabe: one plus the number of independent decision points.
``cognitive``
    Sonar-style: like McCabe but nesting-weighted, so a flat chain of ``if``
    statements scores far lower than the same count nested four deep. This is
    the metric that correlates with "a human will misread this".
``halstead_volume``
    ``N * log2(n)`` over distinct/total operators and operands.
``maintainability_index``
    The classic Coleman-Oman formula, normalised to 0-100.

Nested functions are profiled independently: a closure's complexity is
attributed to the closure, not smeared onto its parent.
"""

from __future__ import annotations

import ast
import math
from typing import Final, Literal

from app.analysis.base import SourceAnalyzer
from app.schemas.agent_state import (
    ClassProfile,
    ComplexityMetrics,
    FunctionProfile,
    FunctionSignature,
    Language,
    ModuleProfile,
    Parameter,
)

__all__ = ["PythonAstAnalyzer", "compute_metrics"]

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

#: Mirrors ``Parameter.kind``; keeps the literal union in one place.
ParameterKind = Literal[
    "positional_only",
    "positional_or_keyword",
    "var_positional",
    "keyword_only",
    "var_keyword",
]

#: Statements that both branch control flow and increase nesting depth.
_NESTING_NODES: Final = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.Match,
)


def _unparse(node: ast.AST | None) -> str | None:
    """Best-effort source rendering of an AST fragment."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is total in 3.11+
        return None


def _is_elif(node: ast.If) -> bool:
    """True when ``node``'s ``orelse`` is itself an ``elif`` rather than ``else``."""
    return len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)


# --------------------------------------------------------------------------- #
# Complexity visitors
# --------------------------------------------------------------------------- #


class _ComplexityVisitor(ast.NodeVisitor):
    """Walks one function body, stopping at nested function/class boundaries.

    ``generic_visit`` is deliberately *not* called for nested definitions: each
    gets its own :class:`FunctionProfile` with its own numbers.
    """

    def __init__(self) -> None:
        self.cyclomatic = 1
        self.cognitive = 0
        self.max_nesting = 0
        self.return_points = 0
        self._depth = 0
        self._root_consumed = False

    # -- helpers ----------------------------------------------------------- #

    def _enter(self) -> None:
        self._depth += 1
        self.max_nesting = max(self.max_nesting, self._depth)

    def _exit(self) -> None:
        self._depth -= 1

    def _nested_visit(self, body: list[ast.stmt]) -> None:
        self._enter()
        for stmt in body:
            self.visit(stmt)
        self._exit()

    # -- boundaries --------------------------------------------------------- #

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition(node)

    def _visit_definition(self, node: FunctionNode) -> None:
        if not self._root_consumed:
            # This is the function we were asked to measure: descend into it.
            self._root_consumed = True
            for stmt in node.body:
                self.visit(stmt)
            return
        # A nested def. Its decision points belong to its own profile; we only
        # note that defining it is itself a (trivial) structural element.
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return  # Nested classes are profiled separately.

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # A lambda body is an expression; its conditionals still cost the reader.
        self.visit(node.body)

    # -- branching constructs ------------------------------------------------ #

    def visit_If(self, node: ast.If) -> None:
        self.cyclomatic += 1
        self.cognitive += 1 + self._depth
        self.visit(node.test)
        self._nested_visit(node.body)
        if node.orelse:
            if _is_elif(node):
                # `elif` chains read linearly: no extra nesting penalty.
                self.visit(node.orelse[0])
            else:
                self.cognitive += 1
                self._nested_visit(node.orelse)

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._visit_loop(node)

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        self.cyclomatic += 1
        self.cognitive += 1 + self._depth
        self.visit(node.test if isinstance(node, ast.While) else node.iter)
        self._nested_visit(node.body)
        if node.orelse:
            self.cognitive += 1
            self._nested_visit(node.orelse)

    def visit_Try(self, node: ast.Try) -> None:
        self._nested_visit(node.body)
        for handler in node.handlers:
            self.visit(handler)
        if node.orelse:
            self._nested_visit(node.orelse)
        if node.finalbody:
            self._nested_visit(node.finalbody)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        """``except*`` groups (PEP 654) score exactly like ``try``."""
        self.visit_Try(node)  # type: ignore[arg-type]

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.cyclomatic += 1
        self.cognitive += 1 + self._depth
        self._nested_visit(node.body)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        self._enter()
        for case in node.cases:
            self.cyclomatic += 1
            self.cognitive += 1 + (self._depth - 1)
            for stmt in case.body:
                self.visit(stmt)
        self._exit()

    # -- expression-level decisions ------------------------------------------ #

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # `a and b and c` is one sequence: +1 cognitive, +n-1 cyclomatic paths.
        self.cyclomatic += len(node.values) - 1
        self.cognitive += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.cyclomatic += 1
        self.cognitive += 1 + self._depth
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.cyclomatic += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.cyclomatic += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self.return_points += 1
        self.generic_visit(node)


class _HalsteadVisitor(ast.NodeVisitor):
    """Counts distinct/total operators and operands for Halstead volume."""

    _OPERATOR_NODES: Final = (
        ast.operator,
        ast.unaryop,
        ast.boolop,
        ast.cmpop,
    )
    _KEYWORD_NODES: Final = (
        ast.If,
        ast.For,
        ast.While,
        ast.Return,
        ast.Try,
        ast.With,
        ast.Raise,
        ast.Assert,
        ast.Assign,
        ast.AugAssign,
        ast.Call,
        ast.Subscript,
        ast.Attribute,
        ast.Lambda,
        ast.Yield,
        ast.Await,
    )

    def __init__(self) -> None:
        self.operators: list[str] = []
        self.operands: list[str] = []

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, (*self._OPERATOR_NODES, *self._KEYWORD_NODES)):
            self.operators.append(type(node).__name__)
        elif isinstance(node, ast.Name):
            self.operands.append(node.id)
        elif isinstance(node, ast.Constant):
            self.operands.append(repr(node.value)[:64])
        elif isinstance(node, ast.arg):
            self.operands.append(node.arg)
        super().generic_visit(node)

    def volume(self) -> float:
        """``N * log2(n)``; zero when the body holds no symbols at all."""
        n1, n2 = len(set(self.operators)), len(set(self.operands))
        total = len(self.operators) + len(self.operands)
        vocabulary = n1 + n2
        if vocabulary == 0 or total == 0:
            return 0.0
        return total * math.log2(vocabulary)


# --------------------------------------------------------------------------- #
# Metric assembly
# --------------------------------------------------------------------------- #


def _maintainability_index(volume: float, cyclomatic: int, sloc: int) -> float:
    """Coleman-Oman MI, clamped and normalised to 0-100.

    The ``max(1, ...)`` guards keep the logarithms defined for one-line bodies
    and for functions whose body is a bare ``pass``.
    """
    raw = (
        171.0 - 5.2 * math.log(max(volume, 1.0)) - 0.23 * cyclomatic - 16.2 * math.log(max(sloc, 1))
    )
    return max(0.0, min(100.0, raw * 100.0 / 171.0))


def _effective_lines(lines: list[str]) -> int:
    """Count lines that carry code (ignoring blanks and whole-line comments)."""
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def compute_metrics(node: FunctionNode, source_lines: list[str]) -> ComplexityMetrics:
    """Measure a single function node.

    Args:
        node: The ``def``/``async def`` node to measure.
        source_lines: The full file split into lines, used for physical LOC.

    Returns:
        A fully populated, immutable :class:`ComplexityMetrics`.
    """
    complexity = _ComplexityVisitor()
    complexity.visit(node)

    halstead = _HalsteadVisitor()
    for stmt in node.body:
        halstead.visit(stmt)
    volume = halstead.volume()

    start = node.lineno - 1
    end = node.end_lineno or node.lineno
    body_lines = source_lines[start:end]
    sloc = _effective_lines(body_lines)

    args = node.args
    parameter_count = (
        len(args.posonlyargs)
        + len(args.args)
        + len(args.kwonlyargs)
        + (1 if args.vararg else 0)
        + (1 if args.kwarg else 0)
    )

    return ComplexityMetrics(
        cyclomatic=complexity.cyclomatic,
        cognitive=complexity.cognitive,
        max_nesting=complexity.max_nesting,
        length=max(0, end - node.lineno + 1),
        parameter_count=parameter_count,
        return_points=complexity.return_points,
        halstead_volume=round(volume, 2),
        maintainability_index=round(_maintainability_index(volume, complexity.cyclomatic, sloc), 2),
        has_docstring=ast.get_docstring(node) is not None,
    )


def _build_parameters(args: ast.arguments) -> tuple[Parameter, ...]:
    """Flatten ``ast.arguments`` into ordered :class:`Parameter` records."""
    params: list[Parameter] = []

    # Defaults align to the *tail* of posonly + args.
    positional = list(args.posonlyargs) + list(args.args)
    pad = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        default_node = args.defaults[index - pad] if index >= pad else None
        kind: ParameterKind = (
            "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword"
        )
        params.append(
            Parameter(
                name=arg.arg,
                annotation=_unparse(arg.annotation),
                default=_unparse(default_node),
                kind=kind,
            )
        )

    if args.vararg:
        params.append(
            Parameter(
                name=args.vararg.arg,
                annotation=_unparse(args.vararg.annotation),
                kind="var_positional",
            )
        )

    # The AST guarantees these two lists are the same length (``kw_defaults``
    # holds ``None`` for parameters without a default), so strict zip is safe
    # and documents the invariant.
    for arg, default_node in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        params.append(
            Parameter(
                name=arg.arg,
                annotation=_unparse(arg.annotation),
                default=_unparse(default_node),
                kind="keyword_only",
            )
        )

    if args.kwarg:
        params.append(
            Parameter(
                name=args.kwarg.arg,
                annotation=_unparse(args.kwarg.annotation),
                kind="var_keyword",
            )
        )

    return tuple(params)


class _CallCollector(ast.NodeVisitor):
    """Collects callee names, stopping at nested definitions."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self._root_consumed = False

    def visit_Call(self, node: ast.Call) -> None:
        name = _unparse(node.func)
        if name:
            self.names.append(name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._skip_or_descend(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._skip_or_descend(node)

    def _skip_or_descend(self, node: FunctionNode) -> None:
        if not self._root_consumed:
            self._root_consumed = True
            self.generic_visit(node)


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #


class PythonAstAnalyzer(SourceAnalyzer):
    """Structural analyzer for Python 3 source."""

    languages = (Language.PYTHON,)

    def analyze(self, source: str, path: str) -> ModuleProfile:
        """Parse ``source`` into a :class:`ModuleProfile`.

        A :class:`SyntaxError` is captured rather than raised: the pipeline
        still runs its text-level security heuristics over unparseable files.
        """
        lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            return ModuleProfile(
                path=path,
                language=Language.PYTHON,
                loc=_effective_lines(lines),
                total_lines=len(lines),
                syntax_error=f"{exc.msg} (line {exc.lineno}, col {exc.offset})",
            )

        functions: list[FunctionProfile] = []
        classes: list[ClassProfile] = []
        self._walk(tree, source, lines, prefix="", functions=functions, classes=classes)

        return ModuleProfile(
            path=path,
            language=Language.PYTHON,
            loc=_effective_lines(lines),
            total_lines=len(lines),
            imports=self._collect_imports(tree),
            functions=tuple(functions),
            classes=tuple(classes),
        )

    def is_valid_syntax(self, source: str) -> tuple[bool, str | None]:
        """Re-parse LLM output before it is ever offered to the user."""
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return False, f"{exc.msg} (line {exc.lineno})"
        return True, None

    # -- internals ----------------------------------------------------------- #

    def _walk(
        self,
        node: ast.AST,
        source: str,
        lines: list[str],
        prefix: str,
        functions: list[FunctionProfile],
        classes: list[ClassProfile],
        *,
        in_class: bool = False,
    ) -> None:
        """Recursively profile every definition, preserving qualified names."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(
                    self._profile_function(child, source, lines, prefix, is_method=in_class)
                )
                self._walk(
                    child,
                    source,
                    lines,
                    prefix=f"{prefix}{child.name}.",
                    functions=functions,
                    classes=classes,
                    in_class=False,
                )
            elif isinstance(child, ast.ClassDef):
                classes.append(self._profile_class(child, prefix))
                self._walk(
                    child,
                    source,
                    lines,
                    prefix=f"{prefix}{child.name}.",
                    functions=functions,
                    classes=classes,
                    in_class=True,
                )
            else:
                self._walk(
                    child,
                    source,
                    lines,
                    prefix=prefix,
                    functions=functions,
                    classes=classes,
                    in_class=in_class,
                )

    def _profile_function(
        self,
        node: FunctionNode,
        source: str,
        lines: list[str],
        prefix: str,
        *,
        is_method: bool,
    ) -> FunctionProfile:
        signature = FunctionSignature(
            name=node.name,
            qualified_name=f"{prefix}{node.name}",
            parameters=_build_parameters(node.args),
            return_annotation=_unparse(node.returns),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            is_method=is_method,
            decorators=tuple(d for d in (_unparse(x) for x in node.decorator_list) if d),
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
        )
        collector = _CallCollector()
        collector.visit(node)
        return FunctionProfile(
            signature=signature,
            metrics=compute_metrics(node, lines),
            source=ast.get_source_segment(source, node) or "",
            calls=tuple(dict.fromkeys(collector.names)),
        )

    @staticmethod
    def _profile_class(node: ast.ClassDef, prefix: str) -> ClassProfile:
        return ClassProfile(
            name=f"{prefix}{node.name}",
            bases=tuple(b for b in (_unparse(x) for x in node.bases) if b),
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            method_names=tuple(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            has_docstring=ast.get_docstring(node) is not None,
        )

    @staticmethod
    def _collect_imports(tree: ast.Module) -> tuple[str, ...]:
        """Top-level module names, de-duplicated and order-preserving."""
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        return tuple(dict.fromkeys(names))
