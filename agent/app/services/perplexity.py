"""Perplexity Sonar engine client.

Modeled against the live Perplexity **Create Chat Completion** reference
(https://docs.perplexity.ai/api-reference/chat-completions-post), confirmed 2026-07:

* Endpoint: ``POST https://api.perplexity.ai/v1/sonar``, ``Authorization: Bearer <key>``.
* Request: ``{"model": "sonar", "messages": [{"role": "user", "content": <query>}], ...}``.
* Response ``CompletionResponse``: top-level ``id, model, created, usage, choices, citations,
  search_results, images, related_questions``. Answer text at ``choices[0].message.content``;
  ``citations`` is an array of URL strings; ``search_results`` is an array of
  ``{title, url, snippet, date, last_updated}`` objects.

The API key comes from settings as a ``SecretStr`` and is never logged or interpolated into an
error. Rate-limit/5xx responses are retried with exponential backoff up to a cap; anything left
over raises ``EngineError`` for the caller to record per-query.
"""

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict

from app.settings import get_settings

logger = logging.getLogger(__name__)

PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/v1/sonar"

# Statuses worth retrying: rate limiting and transient server errors. Everything else
# (auth failures, malformed requests) is terminal and raised immediately.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class EngineError(RuntimeError):
    """A query could not be answered by the engine (retry cap hit, or a terminal HTTP error).

    The engine's raw error body is not attached — nothing that could carry the key ever lands
    in the message. Callers record this per-query; it does not abort the batch.
    """


class SearchResult(BaseModel):
    """One ``search_results`` entry, exactly as the API returns it (extra keys preserved)."""

    model_config = ConfigDict(extra="allow")

    title: str | None = None
    url: str | None = None
    snippet: str | None = None
    date: str | None = None
    last_updated: str | None = None


class EngineUsage(BaseModel):
    """Token usage, if the response carries it (extra keys preserved)."""

    model_config = ConfigDict(extra="allow")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class EngineAnswer(BaseModel):
    """A parsed engine answer plus the raw payload as a lossless backstop."""

    answer_text: str
    citations: list[str] = []
    search_results: list[SearchResult] = []
    usage: EngineUsage | None = None
    raw: dict[str, Any]


@runtime_checkable
class EngineClient(Protocol):
    """The one method EngineRunner needs from any engine. ``engine`` names the source."""

    engine: str

    async def run_query(self, query: str) -> EngineAnswer: ...


class PerplexitySonarClient:
    """Async Perplexity Sonar client for a single engine query."""

    engine = "perplexity"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.perplexity_api_key
        self._model = settings.perplexity_model
        self._temperature = settings.perplexity_temperature
        self._max_retries = settings.engine_max_retries
        self._client = client or httpx.AsyncClient(timeout=settings.perplexity_timeout_seconds)

    async def run_query(self, query: str) -> EngineAnswer:
        """Run one buyer-intent query, retrying transient failures up to the cap."""
        payload = {
            "model": self._model,
            # NO system prompt, by design — this is a measurement-integrity requirement.
            # A system prompt would bias what the engine recommends and corrupt share-of-model.
            # The panel query is sent bare as the user turn. Do not "fix" this by adding one.
            "messages": [{"role": "user", "content": query}],
            # Temperature is pinned (settings.perplexity_temperature) rather than left to the
            # engine default: share-of-model is compared across periods, so the sampling
            # temperature must be stable for historical engine_runs to be comparable.
            "temperature": self._temperature,
        }

        for attempt in range(self._max_retries + 1):
            response = await self._client.post(
                PERPLEXITY_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code in _RETRYABLE_STATUSES and attempt < self._max_retries:
                await self._backoff(attempt)
                continue

            if response.status_code != 200:
                # Terminal: raise with the status only — never the body (it may echo the key).
                raise EngineError(
                    f"Perplexity returned {response.status_code} after {attempt + 1} attempt(s)."
                )

            return self._parse(response.json())

        raise EngineError(f"Perplexity still failing after {self._max_retries + 1} attempts.")

    @staticmethod
    def _parse(body: dict[str, Any]) -> EngineAnswer:
        choices = body.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        content = message.get("content")
        answer_text = content if isinstance(content, str) else ""

        usage = EngineUsage.model_validate(body["usage"]) if body.get("usage") else None
        search_results = [
            SearchResult.model_validate(item) for item in body.get("search_results") or []
        ]

        return EngineAnswer(
            answer_text=answer_text,
            citations=list(body.get("citations") or []),
            search_results=search_results,
            usage=usage,
            raw=body,
        )

    @staticmethod
    async def _backoff(attempt: int) -> None:
        delay = 2**attempt
        logger.info("Perplexity transient failure; backing off %ds", delay)
        await asyncio.sleep(delay)
