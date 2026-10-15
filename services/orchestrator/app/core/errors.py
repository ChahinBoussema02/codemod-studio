"""Domain exceptions and their HTTP mapping."""

from __future__ import annotations

__all__ = [
    "LlmResponseError",
    "LlmUnavailableError",
    "StudioError",
    "UnsupportedLanguageError",
]


class StudioError(Exception):
    """Base class for every error this service raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_payload(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class LlmUnavailableError(StudioError):
    """Ollama could not be reached, or the requested model is not pulled.

    Mapped to 503 because it is a dependency problem, not a client mistake --
    the IDE extension uses that to show "start Ollama" rather than an error.
    """

    status_code = 503
    code = "llm_unavailable"


class LlmResponseError(StudioError):
    """The model answered, but not in a shape we can use."""

    status_code = 502
    code = "llm_bad_response"


class UnsupportedLanguageError(StudioError):
    """No analyzer backend is registered for the requested language."""

    status_code = 422
    code = "unsupported_language"
