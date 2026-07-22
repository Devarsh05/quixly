"""OpenAI Structured-Outputs client for the grounded Optimizer.

Mirrors ``services.extractor_llm.OpenAIExtractorClient`` exactly in shape — the same OpenAI
Structured-Outputs contract (strict ``json_schema``, ``additionalProperties:false``, every property
in ``required``, nullable via a ``["string","null"]`` union), the same reasoning model
(``gpt-5-nano``, no ``temperature``, pinned ``reasoning_effort``), the same retry/refusal handling,
and the same injected-``Protocol`` design so tests drive a scripted client with no network.

This client does **one** job: given a product's own source fields and a list of requested
attributes, return, per attribute, the value **literally stated** in a source field plus the exact
``source_field`` + verbatim ``snippet`` it came from — or ``value:null`` when absent, or
``ambiguous:true`` (with ``value:null``) when source fields disagree. It never infers, never uses
outside knowledge, and never proposes a barcode/GTIN. **The model's output is only a candidate** —
the node's grounding guard (``services.matching.is_grounded``) is what actually enforces literal
presence; a hallucinated value is dropped there, not trusted here.

Config is shared with the Extractor (same structured-extraction model): ``openai_api_key``,
``openai_extractor_model``, ``extractor_reasoning_effort``, ``extractor_max_retries``,
``openai_timeout_seconds``.
"""

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel

from app.settings import get_settings

logger = logging.getLogger(__name__)

OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

_SYSTEM_PROMPT = (
    "You EXTRACT product attributes that are LITERALLY STATED in the provided source fields. You "
    "do NOT generate, infer, guess, or use any outside knowledge.\n"
    "You are given SOURCE FIELDS (the product's own data) and a list of REQUESTED ATTRIBUTES. For "
    "each requested attribute, return exactly one entry:\n"
    "- If the attribute is explicitly stated in a source field, set 'value' to it and set "
    "'source_field' to the field name and 'snippet' to the EXACT verbatim substring of that field "
    "the value came from.\n"
    "- If the attribute is NOT stated in any source field, set value, source_field and snippet to "
    "null. Do NOT fill it.\n"
    "- If different source fields state DIFFERENT values for the attribute, set 'ambiguous' to "
    "true and value/source_field/snippet to null. NEVER pick one.\n"
    "Never derive an attribute from the product name/title unless the title literally contains the "
    "value. Never output a barcode or GTIN. Return one entry per requested attribute, no more."
)

# Fixed schema — one entry per requested attribute. Hand-written (not derived) so strict compliance
# holds: additionalProperties:false everywhere, every property required, nullables as unions.
_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "extracted_attributes",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["attributes"],
            "properties": {
                "attributes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["attribute", "value", "source_field", "snippet", "ambiguous"],
                        "properties": {
                            "attribute": {"type": "string"},
                            "value": {"type": ["string", "null"]},
                            "source_field": {"type": ["string", "null"]},
                            "snippet": {"type": ["string", "null"]},
                            "ambiguous": {"type": "boolean"},
                        },
                    },
                }
            },
        },
    },
}


class OptimizerError(RuntimeError):
    """An attribute extraction could not be completed (retry cap, terminal HTTP error, refusal).

    The response body is never attached — nothing that could carry the key lands in the message.
    """


class AttributeCandidate(BaseModel):
    """One requested attribute's extraction candidate. Only a CANDIDATE — the node grounds it."""

    attribute: str
    value: str | None = None
    source_field: str | None = None
    snippet: str | None = None
    ambiguous: bool = False


class ExtractedAttributes(BaseModel):
    """The structured-output payload: one candidate per requested attribute."""

    attributes: list[AttributeCandidate] = []


@runtime_checkable
class OptimizerClient(Protocol):
    """The one method the Optimizer node needs from any extraction backend."""

    async def extract(
        self, source_fields: dict[str, str], target_attributes: list[str]
    ) -> ExtractedAttributes: ...


def _user_message(source_fields: dict[str, str], target_attributes: list[str]) -> str:
    fields = "\n".join(f"{name}: {text}" for name, text in source_fields.items())
    return (
        f"SOURCE FIELDS:\n{fields}\n\n"
        f"REQUESTED ATTRIBUTES: {', '.join(target_attributes)}"
    )


class OpenAIOptimizerClient:
    """Async OpenAI Structured-Outputs client: extract product attributes from source fields."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.openai_api_key
        self._model = settings.openai_extractor_model
        self._reasoning_effort = settings.extractor_reasoning_effort
        self._max_retries = settings.extractor_max_retries
        self._client = client or httpx.AsyncClient(timeout=settings.openai_timeout_seconds)

    async def extract(
        self, source_fields: dict[str, str], target_attributes: list[str]
    ) -> ExtractedAttributes:
        """Extract candidates for ``target_attributes`` from ``source_fields``; retries 5xx/429."""
        if not target_attributes:
            return ExtractedAttributes(attributes=[])

        payload = {
            "model": self._model,
            # NO temperature — gpt-5-nano is a reasoning model and rejects a non-default value.
            "reasoning_effort": self._reasoning_effort,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _user_message(source_fields, target_attributes)},
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
                raise OptimizerError(
                    f"OpenAI returned {response.status_code} after {attempt + 1} attempt(s)."
                )

            return self._parse(response.json())

        raise OptimizerError(f"OpenAI still failing after {self._max_retries + 1} attempts.")

    @staticmethod
    def _parse(body: dict[str, Any]) -> ExtractedAttributes:
        choices = body.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}

        if message.get("refusal"):
            raise OptimizerError("OpenAI refused the extraction request.")
        if first.get("finish_reason") == "length":
            raise OptimizerError("OpenAI response was truncated (finish_reason=length).")

        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise OptimizerError("OpenAI response carried no content to parse.")

        return ExtractedAttributes.model_validate_json(content)

    @staticmethod
    async def _backoff(attempt: int) -> None:
        delay = 2**attempt
        logger.info("OpenAI transient failure; backing off %ds", delay)
        await asyncio.sleep(delay)
