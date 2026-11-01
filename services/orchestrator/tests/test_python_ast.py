"""Tests for the AST backend and the deterministic security rules."""

from __future__ import annotations

import pytest

from app.analysis.python_ast import PythonAstAnalyzer
from app.analysis.security_rules import scan_python
from app.schemas.agent_state import Language

ANALYZER = PythonAstAnalyzer()


# --------------------------------------------------------------------------- #
# Signatures
# --------------------------------------------------------------------------- #


def test_extracts_full_signature() -> None:
    source = (
        "async def fetch(a, /, b: int = 3, *args, key: str = 'k', **kw) -> dict[str, int]:\n"
        "    return {}\n"
    )
    module = ANALYZER.analyze(source, "m.py")
    (function,) = module.functions
    signature = function.signature

    assert signature.is_async is True
    assert signature.return_annotation == "dict[str, int]"
    kinds = [p.kind for p in signature.parameters]
    assert kinds == [
        "positional_only",
        "positional_or_keyword",
        "var_positional",
        "keyword_only",
        "var_keyword",
    ]
    assert signature.parameters[1].annotation == "int"
    assert signature.parameters[1].default == "3"
    assert "async def fetch(a, b: int = 3, *args, key: str = 'k', **kw)" in signature.render()


def test_qualified_names_for_methods_and_closures() -> None:
    source = (
        "class A:\n"
        "    def outer(self):\n"
        "        def inner():\n"
        "            return 1\n"
        "        return inner\n"
    )
    module = ANALYZER.analyze(source, "m.py")
    names = {f.signature.qualified_name for f in module.functions}

    assert names == {"A.outer", "A.outer.inner"}
    assert next(f for f in module.functions if f.signature.name == "outer").signature.is_method
    assert module.classes[0].method_names == ("outer",)


def test_decorators_are_captured() -> None:
    source = "import functools\n\n@functools.cache\n@staticmethod\ndef f():\n    return 1\n"
    module = ANALYZER.analyze(source, "m.py")
    assert module.functions[0].signature.decorators == ("functools.cache", "staticmethod")
    assert module.imports == ("functools",)


# --------------------------------------------------------------------------- #
# Complexity
# --------------------------------------------------------------------------- #


def test_cyclomatic_counts_each_decision_point() -> None:
    source = (
        "def f(a, b):\n"
        "    if a and b:\n"  # +1 if, +1 boolop
        "        return 1\n"
        "    for _ in range(3):\n"  # +1
        "        pass\n"
        "    while a:\n"  # +1
        "        break\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"  # +1
        "        pass\n"
        "    return 0\n"
    )
    metrics = ANALYZER.analyze(source, "m.py").functions[0].metrics
    assert metrics.cyclomatic == 6
    assert metrics.return_points == 2


def test_cognitive_complexity_penalises_nesting() -> None:
    flat = "def f(a):\n    if a:\n        pass\n    if a:\n        pass\n    if a:\n        pass\n"
    nested = "def g(a):\n    if a:\n        if a:\n            if a:\n                pass\n"

    flat_metrics = ANALYZER.analyze(flat, "m.py").functions[0].metrics
    nested_metrics = ANALYZER.analyze(nested, "m.py").functions[0].metrics

    # Same number of branches, same McCabe score...
    assert flat_metrics.cyclomatic == nested_metrics.cyclomatic == 4
    # ...but nesting is what actually hurts a reader.
    assert flat_metrics.cognitive == 3
    assert nested_metrics.cognitive == 6
    assert nested_metrics.max_nesting == 3


def test_elif_chain_is_not_treated_as_nesting() -> None:
    source = (
        "def f(x):\n"
        "    if x == 1:\n"
        "        return 'a'\n"
        "    elif x == 2:\n"
        "        return 'b'\n"
        "    elif x == 3:\n"
        "        return 'c'\n"
        "    return 'd'\n"
    )
    metrics = ANALYZER.analyze(source, "m.py").functions[0].metrics
    assert metrics.max_nesting == 1, "elif must not accumulate depth"
    assert metrics.cognitive == 3


def test_nested_function_complexity_is_not_charged_to_parent() -> None:
    source = (
        "def parent(xs):\n"
        "    def child(y):\n"
        "        if y:\n"
        "            for _ in range(3):\n"
        "                pass\n"
        "        return y\n"
        "    return [child(x) for x in xs]\n"
    )
    module = ANALYZER.analyze(source, "m.py")
    parent = next(f for f in module.functions if f.signature.name == "parent")
    child = next(f for f in module.functions if f.signature.name == "child")

    assert child.metrics.cyclomatic == 3
    # parent: base 1 + the comprehension generator only.
    assert parent.metrics.cyclomatic == 2


def test_maintainability_index_is_bounded_and_ordered() -> None:
    trivial = "def f():\n    return 1\n"
    gnarly = (
        "def g(a, b, c):\n"
        + "".join(f"    if a == {i}:\n        b = c + {i}\n" for i in range(25))
        + "    return b\n"
    )

    trivial_mi = ANALYZER.analyze(trivial, "m.py").functions[0].metrics.maintainability_index
    gnarly_mi = ANALYZER.analyze(gnarly, "m.py").functions[0].metrics.maintainability_index

    assert 0.0 <= gnarly_mi < trivial_mi <= 100.0


def test_hotspot_detection_and_ranking() -> None:
    source = (
        "def simple():\n    return 1\n\n"
        "def complicated(a, b, c):\n"
        + "".join(f"    if a == {i} and b:\n        c += 1\n" for i in range(10))
        + "    return c\n"
    )
    module = ANALYZER.analyze(source, "m.py")
    hotspots = module.hotspots()

    assert [f.signature.name for f in hotspots] == ["complicated"]
    assert module.average_cyclomatic > 1


@pytest.mark.parametrize(
    "source,expected_cc",
    [
        ("def f(x):\n    return 1 if x else 0\n", 2),
        ("def f(xs):\n    return [x for x in xs if x > 0]\n", 3),
        ("def f(x):\n    assert x\n    return x\n", 2),
        (
            "def f(x):\n"
            "    match x:\n"
            "        case 1:\n"
            "            return 'a'\n"
            "        case _:\n"
            "            return 'b'\n",
            3,
        ),
    ],
)
def test_expression_level_decisions(source: str, expected_cc: int) -> None:
    assert ANALYZER.analyze(source, "m.py").functions[0].metrics.cyclomatic == expected_cc


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_syntax_error_degrades_instead_of_raising() -> None:
    module = ANALYZER.analyze("def broken(\n", "m.py")

    assert module.syntax_error is not None
    assert module.functions == ()
    assert module.language is Language.PYTHON
    assert module.total_lines == 1


def test_empty_file_is_valid() -> None:
    module = ANALYZER.analyze("", "empty.py")
    assert module.loc == 0
    assert module.functions == ()
    assert module.syntax_error is None


def test_is_valid_syntax_round_trips() -> None:
    ok, error = ANALYZER.is_valid_syntax("def f():\n    return 1\n")
    assert ok and error is None

    ok, error = ANALYZER.is_valid_syntax("def f(:\n")
    assert not ok and error


def test_loc_ignores_blanks_and_comments() -> None:
    source = "# a comment\n\n\nx = 1\n   \n# another\ny = 2\n"
    assert ANALYZER.analyze(source, "m.py").loc == 2


# --------------------------------------------------------------------------- #
# Security rules
# --------------------------------------------------------------------------- #


def test_detects_the_expected_rule_set(vulnerable_source: str) -> None:
    findings = scan_python(vulnerable_source, "svc.py")
    rules = {f.rule_id for f in findings}

    assert {
        "PY-HARDCODED-SECRET",
        "PY-SQL-CONCAT",
        "PY-SUBPROCESS-SHELL",
        "PY-WEAK-HASH",
        "PY-SILENT-EXCEPT",
    } <= rules


def test_placeholder_secrets_are_not_reported() -> None:
    source = (
        'PASSWORD = "changeme"\n'
        'API_KEY = "${VAULT_KEY}"\n'
        'TOKEN = "<your-token-here>"\n'
        'SECRET = "test"\n'
    )
    assert scan_python(source, "c.py") == []


def test_real_looking_secret_is_reported() -> None:
    (finding,) = scan_python('API_KEY = "sk-live-7f3a9c21b4e8"\n', "c.py")
    assert finding.rule_id == "PY-HARDCODED-SECRET"
    assert finding.cwe_id == "CWE-798"
    assert finding.line_start == 1


def test_subprocess_without_shell_is_clean() -> None:
    source = 'import subprocess\nsubprocess.run(["ls", "-l"], check=True)\n'
    assert [f for f in scan_python(source, "c.py") if f.rule_id == "PY-SUBPROCESS-SHELL"] == []


def _weak_hash_findings(source: str) -> list[str]:
    return [f.rule_id or "" for f in scan_python(source, "c.py") if f.rule_id == "PY-WEAK-HASH"]


def test_weak_hash_is_reported_by_default() -> None:
    assert _weak_hash_findings("import hashlib\nhashlib.md5(data).hexdigest()\n")
    assert _weak_hash_findings("import hashlib\nhashlib.sha1(data).hexdigest()\n")


@pytest.mark.parametrize(
    "source",
    [
        "import hashlib\nhashlib.md5(data, usedforsecurity=False)\n",
        "import hashlib\nhashlib.sha1(data, usedforsecurity=False).hexdigest()\n",
        # Keyword order must not matter.
        "import hashlib\nhashlib.md5(usedforsecurity=False, string=data)\n",
    ],
)
def test_weak_hash_honours_its_own_remediation(source: str) -> None:
    """Regression: the rule flagged code that had followed its advice.

    PY-WEAK-HASH's remediation says to pass ``usedforsecurity=False`` for a
    non-security hash. Reporting that call anyway makes the finding
    impossible to resolve, which trains users to ignore the rule.
    """
    assert _weak_hash_findings(source) == []


def test_weak_hash_still_reported_when_flag_is_true() -> None:
    """``usedforsecurity=True`` is an explicit security use -- still a finding."""
    assert _weak_hash_findings("import hashlib\nhashlib.md5(data, usedforsecurity=True)\n")


def test_strong_hash_is_never_reported() -> None:
    assert _weak_hash_findings("import hashlib\nhashlib.sha256(data).hexdigest()\n") == []


def test_findings_are_attributed_to_their_enclosing_function(vulnerable_source: str) -> None:
    module = ANALYZER.analyze(vulnerable_source, "svc.py")
    spans = {
        f.signature.qualified_name: (f.signature.line_start, f.signature.line_end)
        for f in module.functions
    }

    def resolve(line: int) -> str | None:
        for name, (start, end) in spans.items():
            if start <= line <= end:
                return name
        return None

    findings = scan_python(vulnerable_source, "svc.py", resolve)
    sql = next(f for f in findings if f.rule_id == "PY-SQL-CONCAT")
    assert sql.symbol == "run_report"


def test_text_rules_apply_to_unparseable_files() -> None:
    source = "this is not python {{{\nAKIAIOSFODNN7EXAMPLE\n"
    findings = scan_python(source, "broken.tf")
    assert [f.rule_id for f in findings] == ["GEN-AWS-KEY"]


def test_clean_source_produces_no_findings(clean_source: str) -> None:
    assert scan_python(clean_source, "clean.py") == []
