"""Live contract smoke test for Perplexity Sonar — the no-mock external-surface test.

Opt-in: marked ``live`` and skipped unless PERPLEXITY_API_KEY is set, so it never runs in default
CI. It makes ONE real ``sonar`` call and asserts the response parses into ``EngineAnswer`` with
non-empty sources — proving our model matches what the live API actually returns, which a mock
can never establish.
"""

import os

import pytest

from app.services.perplexity import EngineAnswer, PerplexitySonarClient

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    not os.getenv("PERPLEXITY_API_KEY"),
    reason="requires a real PERPLEXITY_API_KEY in the environment",
)
async def test_sonar_call_parses_and_returns_sources(settings):
    from app.settings import get_settings

    get_settings.cache_clear()  # pick up the real key from the environment

    client = PerplexitySonarClient()
    answer = await client.run_query("best light roast coffee beans")

    assert isinstance(answer, EngineAnswer)
    assert answer.answer_text
    # Sonar is a web-search model: at least one of the source channels must be populated.
    assert answer.citations or answer.search_results
