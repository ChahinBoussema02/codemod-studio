"""Tests for the readiness probe.

The model-availability check is worth testing on its own: a false "ready"
here is worse than no probe at all, because the IDE stops warning the user
and the failure resurfaces as a 404 mid-run.
"""

from __future__ import annotations

import pytest

from app.api.routes.health import _is_pulled

PULLED = {
    "qwen2.5-coder:7b",
    "qwen2.5:14b",
    "llama3:latest",
    "deepseek-coder-v2:latest",
}


@pytest.mark.parametrize(
    "model",
    [
        "qwen2.5-coder:7b",  # exact match
        "llama3",  # untagged config value matches the ":latest" entry
        "deepseek-coder-v2",
    ],
)
def test_pulled_models_are_recognised(model: str) -> None:
    assert _is_pulled(model, PULLED)


@pytest.mark.parametrize(
    "model",
    [
        # The regression this test exists for: a different tag of a family
        # that IS pulled must not count as present.
        "qwen2.5-coder:14b",
        "qwen2.5-coder:32b",
        # A different family that merely shares a prefix.
        "qwen2.5-coder",
        # Nothing like it at all.
        "mistral:7b",
    ],
)
def test_absent_models_are_reported_missing(model: str) -> None:
    assert not _is_pulled(model, PULLED)


def test_empty_host_reports_everything_missing() -> None:
    assert not _is_pulled("qwen2.5-coder:7b", set())
