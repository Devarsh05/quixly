"""OpenAI Structured-Outputs extractor client.

Modeled against the live OpenAI **Structured Outputs** guide
(https://developers.openai.com/api/docs/guides/structured-outputs), confirmed 2026-07:

* Endpoint: ``POST https://api.openai.com/v1/chat/completions``, ``Authorization: Bearer <key>``.
* Structured Outputs is requested via ``response_format={"type": "json_schema", "json_schema":
  {"name": ..., "strict": true, "schema": {...}}}``. ``strict: true`` is required, every object
  needs ``additionalProperties: false``, and every property must appear in ``required`` (nullable
  fields use a ``["string", "null"]`` type union rather than being omitted).
* Answer JSON is a string at ``choices[0].message.content``; ``choices[0].message.refusal`` is set
  instead when the model refuses.

Default model ``gpt-5-nano`` (settings knob) is a **reasoning** model, so:

* No ``temperature`` is sent — reasoning models reject a non-default temperature. Extraction
  determinism comes from the strict schema plus a precise system prompt, not a pinned temperature.
* ``reasoning_effort`` is pinned (settings, default ``"minimal"``): strict-schema extraction needs
  no reasoning, and unpinned effort wastes output-billed reasoning tokens and latency per row.

Unlike the Perplexity engine call (deliberately bare — a measurement), this call carries a precise
system prompt instructing strict extraction. The API key comes from settings as a ``SecretStr`` and
is never logged or interpolated into an error. Rate-limit/5xx responses are retried with exponential
backoff up to a cap; anything left over raises ``ExtractorError`` for the caller to record per-row.
"""

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel

from app.settings import get_settings

logger = logging.getLogger(__name__)

OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# Statuses worth retrying: rate limiting and transient server errors. Everything else
# (auth failures, malformed requests) is terminal and raised immediately.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

_SYSTEM_PROMPT = (
    "You extract EVERY brand and product explicitly named in a shopping-assistant answer.\n"
    "This is TWO separate steps — do not conflate them:\n"
    "1. INCLUDE every brand or product the answer explicitly names, no matter how it is framed — "
    "top pick, budget option, cheaper alternative, runner-up, honorable mention, 'also consider', "
    "or a brand named only in passing. Being secondary, cheaper, or a fallback is NEVER a reason "
    "to leave a brand out. If the answer names four brands, return four.\n"
    "2. RANK the brands you included by recommendation prominence: the top / most strongly "
    "recommended is rank 1, the next is rank 2, and so on (consecutive, starting at 1). A "
    "low-prominence brand gets a LOWER rank — it is ranked last, never dropped.\n"
    "Never infer, guess, or add a brand that is not literally present; extract only brands "
    "actually written in the text. If the text names no brands, return an empty list.\n"
    "For each brand set 'product' to the specific product named for it, or null if only the brand "
    "is named. Set 'verbatim' to the exact snippet of the answer text the brand was taken from."
)

# Hand-written JSON schema (not derived from the Pydantic model) so strict compliance is
# guaranteed: additionalProperties:false everywhere, every property in `required`, and the optional
# `product` expressed as a nullable type union rather than being omitted.
_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "extracted_brands",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["brands"],
            "properties": {
                "brands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["rank", "brand", "product", "verbatim"],
                        "properties": {
                            "rank": {"type": "integer"},
                            "brand": {"type": "string"},
                            "product": {"type": ["string", "null"]},
                            "verbatim": {"type": "string"},
                        },
                    },
                }
            },
        },
    },
}


class ExtractorError(RuntimeError):
    """A brand extraction could not be completed (retry cap hit, terminal HTTP error, or refusal).

    The response body is never attached — nothing that could carry the key ever lands in the
    message. Callers record this per-row; it does not abort the batch.
    """


class ExtractedBrand(BaseModel):
    """One brand/product pulled from an answer, with the snippet it came from (for grounding)."""

    rank: int
    brand: str
    product: str | None = None
    verbatim: str


class ExtractedBrands(BaseModel):
    """The structured-output payload: brands ordered by recommendation prominence."""

    brands: list[ExtractedBrand] = []


@runtime_checkable
class ExtractorClient(Protocol):
    """The one method the Extractor node needs from any extraction backend."""

    async def extract(self, answer_text: str) -> ExtractedBrands: ...


class OpenAIExtractorClient:
    """Async OpenAI Structured-Outputs client for extracting brands from one answer."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.openai_api_key
        self._model = settings.openai_extractor_model
        self._reasoning_effort = settings.extractor_reasoning_effort
        self._max_retries = settings.extractor_max_retries
        self._client = client or httpx.AsyncClient(timeout=settings.openai_timeout_seconds)

    async def extract(self, answer_text: str) -> ExtractedBrands:
        """Extract brands from one answer, retrying transient failures up to the cap."""
        payload = {
            "model": self._model,
            # NO temperature — gpt-5-nano is a reasoning model and rejects a non-default value.
            "reasoning_effort": self._reasoning_effort,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": answer_text},
            ],
            "response_format": _RESPONSE_FORMAT,
        }

        for attempt in range(self._max_retries + 1):
            response = await self._client.post(
                OPENAI_ENDPOINT,
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
                raise ExtractorError(
                    f"OpenAI returned {response.status_code} after {attempt + 1} attempt(s)."
                )

            return self._parse(response.json())

        raise ExtractorError(f"OpenAI still failing after {self._max_retries + 1} attempts.")

    @staticmethod
    def _parse(body: dict[str, Any]) -> ExtractedBrands:
        choices = body.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}

        if message.get("refusal"):
            raise ExtractorError("OpenAI refused the extraction request.")
        if first.get("finish_reason") == "length":
            # Output hit the token cap: the JSON is truncated and cannot be trusted.
            raise ExtractorError("OpenAI response was truncated (finish_reason=length).")

        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise ExtractorError("OpenAI response carried no content to parse.")

        return ExtractedBrands.model_validate_json(content)

    @staticmethod
    async def _backoff(attempt: int) -> None:
        delay = 2**attempt
        logger.info("OpenAI transient failure; backing off %ds", delay)
        await asyncio.sleep(delay)
