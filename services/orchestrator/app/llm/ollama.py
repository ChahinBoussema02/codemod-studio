"""Async Ollama client with schema-constrained decoding.

Why not LangChain's ``ChatOllama``? Three reasons specific to this project:

* We want Ollama's native ``format`` parameter, which constrains generation to
  a JSON Schema at the sampler level. A 7B model asked politely for JSON will
  drift; a 7B model that *cannot emit* invalid JSON will not.
* We need the token counts from the raw response for telemetry.
* Keeping the transport thin makes the offline guarantee auditable: there is
  exactly one place in this codebase that opens a socket for inference.

Reasoning models (DeepSeek-R1) prefix their answer with a ``<think>`` block.
:func:`strip_reasoning` removes it before parsing.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from app.core.errors import LlmResponseError, LlmUnavailableError
from app.core.logging import get_logger

__all__ = [
    "LlmClient",
    "LlmResult",
    "OllamaClient",
    "strip_reasoning",
]

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Remove ``<think>...</think>`` spans and markdown fences from a reply."""
    cleaned = _THINK_BLOCK.sub("", text).strip()
    fenced = _JSON_FENCE.search(cleaned)
    if fenced:
        return fenced.group(1).strip()
    return cleaned


@dataclass(frozen=True, slots=True)
class LlmResult:
    """One completion plus the accounting the telemetry service wants."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float


class LlmClient(Protocol):
    """The surface the graph nodes depend on.

    Narrow by design: tests inject a deterministic stub implementing exactly
    these three methods, so the whole pipeline is testable without Ollama.
    """

    async def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        temperature: float = 0.1,
    ) -> tuple[T, LlmResult]:
        """Generate a response validated against ``schema``."""
        ...

    async def available_models(self) -> list[str]:
        """Model tags currently pulled on the host."""
        ...

    async def ping(self) -> bool:
        """True when the runtime answers."""
        ...


def _assert_local(base_url: str) -> None:
    """Refuse non-local inference endpoints when offline mode is on.

    This is the enforcement point behind the product's zero-leakage promise:
    a misconfigured ``CMS_OLLAMA_BASE_URL`` pointing at a hosted endpoint is a
    startup failure, not a silent data exfiltration path.
    """
    host = urlparse(base_url).hostname or ""
    if host in {"localhost", "host.docker.internal", "ollama"}:
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise LlmUnavailableError(
            f"offline_only is enabled but '{host}' is not a recognised local host",
            detail="Set CMS_OFFLINE_ONLY=false only if you accept sending source off-box.",
        ) from exc
    if not (address.is_loopback or address.is_private):
        raise LlmUnavailableError(
            f"offline_only is enabled but '{host}' is a public address",
            detail="Point CMS_OLLAMA_BASE_URL at a loopback or private-network Ollama.",
        )


class OllamaClient:
    """Thin async wrapper over the Ollama HTTP API."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        timeout_s: float = 180.0,
        max_retries: int = 2,
        num_ctx: int = 8192,
        offline_only: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if offline_only:
            _assert_local(self.base_url)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.num_ctx = num_ctx
        self._client = client
        self._owns_client = client is None

    # -- lifecycle ------------------------------------------------------------ #

    async def __aenter__(self) -> OllamaClient:
        self._ensure_client()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_s, connect=10.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- public API ------------------------------------------------------------ #

    async def ping(self) -> bool:
        try:
            response = await self._ensure_client().get("/api/version", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def available_models(self) -> list[str]:
        """Return pulled model tags, or an empty list if the runtime is down."""
        try:
            response = await self._ensure_client().get("/api/tags", timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Could not list Ollama models: %s", exc)
            return []
        payload = response.json()
        return [entry["name"] for entry in payload.get("models", []) if "name" in entry]

    async def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        temperature: float = 0.1,
    ) -> tuple[T, LlmResult]:
        """Generate JSON constrained to ``schema`` and parse it.

        Args:
            model: Ollama tag, e.g. ``qwen2.5-coder:7b``.
            system: System prompt establishing the agent's role.
            prompt: The task, including the code under analysis.
            schema: Pydantic model used both to constrain and to validate.
            temperature: Kept low; this is extraction, not prose.

        Raises:
            LlmUnavailableError: The runtime is unreachable or lacks the model.
            LlmResponseError: Output could not be validated after all retries.
        """
        json_schema = schema.model_json_schema()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "format": json_schema,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
            "messages": messages,
        }

        last_error: str = "unknown"
        for attempt in range(self.max_retries + 1):
            result = await self._chat(body, model=model)
            raw = strip_reasoning(result.content)
            try:
                return schema.model_validate_json(raw), result
            except ValidationError as exc:
                last_error = exc.errors(include_url=False)[0].get("msg", str(exc))
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON: {exc}"

            logger.warning(
                "Model %s returned unusable output (attempt %d/%d): %s",
                model,
                attempt + 1,
                self.max_retries + 1,
                last_error,
            )
            # Nudge the model with the validator's own complaint.
            messages = [
                *messages[:2],
                {"role": "assistant", "content": raw[:2000]},
                {
                    "role": "user",
                    "content": (
                        f"That response failed schema validation: {last_error}. "
                        "Return only valid JSON matching the schema."
                    ),
                },
            ]
            body["messages"] = messages

        raise LlmResponseError(
            f"{model} did not produce schema-valid output after {self.max_retries + 1} attempts",
            detail=last_error,
        )

    # -- transport -------------------------------------------------------------- #

    async def _chat(self, body: dict[str, Any], *, model: str) -> LlmResult:
        """POST ``/api/chat`` with bounded exponential backoff on transport errors."""
        client = self._ensure_client()
        started = time.perf_counter()
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await client.post("/api/chat", json=body)
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if response.status_code == 404:
                raise LlmUnavailableError(
                    f"Model '{model}' is not available on the Ollama host",
                    detail=f"Run: ollama pull {model}",
                )
            if response.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    "server error", request=response.request, response=response
                )
                await asyncio.sleep(min(2**attempt, 8))
                continue

            response.raise_for_status()
            payload = response.json()
            return LlmResult(
                content=payload.get("message", {}).get("content", ""),
                model=payload.get("model", model),
                prompt_tokens=int(payload.get("prompt_eval_count", 0)),
                completion_tokens=int(payload.get("eval_count", 0)),
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        raise LlmUnavailableError(
            f"Ollama at {self.base_url} is unreachable",
            detail=str(last_exc) if last_exc else None,
        )


@asynccontextmanager
async def lifespan_client(**kwargs: Any) -> AsyncIterator[OllamaClient]:
    """Helper for scripts and tests that want a client without FastAPI."""
    client = OllamaClient(**kwargs)
    try:
        yield client
    finally:
        await client.aclose()
