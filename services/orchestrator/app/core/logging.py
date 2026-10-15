"""Logging setup.

Two formats: readable text locally, single-line JSON everywhere else so the
records land cleanly in a log shipper. A ``run_id`` context variable is bound
into every record, which is what makes a concurrent multi-agent run traceable
after the fact.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

__all__ = ["bind_run_id", "configure_logging", "get_logger"]

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)


def bind_run_id(run_id: str | None) -> None:
    """Attach ``run_id`` to every log record emitted in this task."""
    _run_id.set(run_id)


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get() or "-"
        return True


class _JsonFormatter(logging.Formatter):
    """Minimal JSON formatter -- no dependency on structlog."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Install handlers on the root logger. Idempotent."""
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RunIdFilter())
    if json_output:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-7s  [%(run_id)s]  %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn duplicates access logs through its own handlers; let ours win.
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
