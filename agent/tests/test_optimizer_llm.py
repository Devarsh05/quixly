"""No-mock smoke tests for the real ``optimizer_llm`` module.

Per CLAUDE.md ("Mocks hide missing properties"): import the REAL module and assert the structured-
output contract it actually sends is well-formed, and that ``_parse`` reads the real OpenAI
response shape. The one HTTP round-trip is faked with ``respx`` so the real client code (headers,
payload, parse) runs — nothing of ours is mocked.
"""

import httpx
import pytest
import respx

from app.services.optimizer_llm import (
    _RESPONSE_FORMAT,
    AttributeCandidate,
    ExtractedAttributes,
    OpenAIOptimizerClient,
    OptimizerError,
)

ENDPOINT = "https://api.openai.com/v1/chat/completions"


def _walk_schema_objects(schema: dict):
    """Yield every JSON-schema object node (``type: object``) in the tree."""
    if schema.get("type") == "object":
        yield schema
    for key in ("items", "schema"):
        if isinstance(schema.get(key), dict):
            yield from _walk_schema_objects(schema[key])
    for prop in (schema.get("properties") or {}).values():
        if isinstance(prop, dict):
            yield from _walk_schema_objects(prop)


def test_response_format_is_strict_and_complete():
    js = _RESPONSE_FORMAT["json_schema"]
    assert js["strict"] is True
    # OpenAI strict mode requires additionalProperties:false and every property in `required`.
    for obj in _walk_schema_objects(js["schema"]):
        assert obj.get("additionalProperties") is False
        assert set(obj.get("required", [])) == set(obj.get("properties", {}).keys())


def test_nullable_attribute_fields_use_a_type_union():
    item = _RESPONSE_FORMAT["json_schema"]["schema"]["properties"]["attributes"]["items"]
    props = item["properties"]
    assert props["value"]["type"] == ["string", "null"]
    assert props["source_field"]["type"] == ["string", "null"]
    assert props["snippet"]["type"] == ["string", "null"]
    assert props["ambiguous"]["type"] == "boolean"


def test_parse_reads_the_real_openai_response_shape():
    payload = ExtractedAttributes(
        attributes=[
            AttributeCandidate(attribute="roast_level", value="light",
                               source_field="body_html", snippet="Roast level: light",
                               ambiguous=False)
        ]
    ).model_dump_json()
    body = {"choices": [{"message": {"content": payload}, "finish_reason": "stop"}]}
    parsed = OpenAIOptimizerClient._parse(body)
    assert parsed.attributes[0].value == "light"


def test_parse_raises_on_refusal():
    body = {"choices": [{"message": {"refusal": "no"}}]}
    with pytest.raises(OptimizerError):
        OpenAIOptimizerClient._parse(body)


@respx.mock
async def test_extract_posts_and_parses_through_the_real_client(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from app.settings import get_settings

    get_settings.cache_clear()
    content = ExtractedAttributes(
        attributes=[
            AttributeCandidate(attribute="origin", value="Ethiopia", source_field="title",
                               snippet="Ethiopia", ambiguous=False)
        ]
    ).model_dump_json()
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )

    client = OpenAIOptimizerClient()
    result = await client.extract({"title": "Ethiopia Yirgacheffe"}, ["origin"])

    assert route.called
    assert result.attributes[0].value == "Ethiopia"
    get_settings.cache_clear()


async def test_extract_short_circuits_with_no_targets():
    # No requested attributes → no HTTP call, empty result.
    client = OpenAIOptimizerClient()
    result = await client.extract({"title": "x"}, [])
    assert result.attributes == []
