"""Live contract smoke test for the OpenAI Structured-Outputs extractor — the no-mock test.

Opt-in: marked ``live`` and skipped unless OPENAI_API_KEY is set, so it never runs in default CI.
It makes ONE real extraction call on a realistic coffee-recommendation answer and asserts the
response parses into ``ExtractedBrands`` AND that every returned brand passes the grounding check
against the source text — proving our schema and the model's structured output actually agree,
which a mock can never establish.
"""

import os

import pytest

from app.graph.extractor import _is_grounded
from app.services.extractor_llm import ExtractedBrands, OpenAIExtractorClient

pytestmark = pytest.mark.live

_ANSWER = (
    "For a smooth medium roast, Blue Bottle's Bella Donovan is a crowd-pleaser. If you want "
    "something brighter, Stumptown Hair Bender is a classic, and Counter Culture's Hologram is "
    "worth a try for fruit-forward notes."
)


@pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="requires a real OPENAI_API_KEY in the environment",
)
async def test_extract_parses_and_every_brand_is_grounded():
    from app.settings import get_settings

    get_settings.cache_clear()  # pick up the real key from the environment

    client = OpenAIExtractorClient()
    extracted = await client.extract(_ANSWER)

    assert isinstance(extracted, ExtractedBrands)
    assert extracted.brands, "expected at least one brand from a text naming several"
    # The structured-output surface must never emit a brand absent from the source.
    for brand in extracted.brands:
        assert _is_grounded(brand.brand, _ANSWER), f"ungrounded live brand: {brand.brand!r}"
