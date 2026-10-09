"""OpenAI-compatible extractor against a mocked HTTP API (never the real provider)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import make_settings
from tests.llm_stubs import CANONICAL_EXTRACTION, CANONICAL_ORDER_TEXT
from ya_voy.llm.extractor import LLMError, OrderExtractor
from ya_voy.llm.factory import LLMConfigError, build_order_extractor
from ya_voy.llm.fake import FakeOrderExtractor
from ya_voy.llm.openai_compatible import OpenAICompatibleExtractor
from ya_voy.llm.schemas import OrderExtraction
from ya_voy.llm.unavailable import UnavailableOrderExtractor

BASE = "http://llm.test/v1"
URL = f"{BASE}/chat/completions"


def _answer(extraction: OrderExtraction | dict | str) -> httpx.Response:
    if isinstance(extraction, OrderExtraction):
        content = extraction.model_dump_json()
    elif isinstance(extraction, dict):
        content = json.dumps(extraction)
    else:
        content = extraction
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
async def client():
    extractor = OpenAICompatibleExtractor(
        api_key="sk-test", model="test-model", base_url=BASE, backoff_base_seconds=0
    )
    yield extractor
    await extractor.aclose()


@respx.mock
async def test_extracts_the_canonical_order(client: OpenAICompatibleExtractor) -> None:
    route = respx.post(URL).mock(return_value=_answer(CANONICAL_EXTRACTION))
    assert isinstance(client, OrderExtractor)
    assert await client.extract(CANONICAL_ORDER_TEXT) == CANONICAL_EXTRACTION

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["temperature"] == 0
    assert body["messages"][1] == {"role": "user", "content": CANONICAL_ORDER_TEXT}
    schema = body["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert set(schema["schema"]["required"]) == set(OrderExtraction.model_fields)
    assert schema["schema"]["additionalProperties"] is False


@respx.mock
async def test_sends_the_current_draft_as_context(client: OpenAICompatibleExtractor) -> None:
    route = respx.post(URL).mock(return_value=_answer({"payment_method_raw": "efectivo"}))
    current = OrderExtraction(product="almacen")
    await client.extract("efectivo", current=current)
    user = json.loads(route.calls.last.request.content)["messages"][1]["content"]
    assert user == 'Current draft: {"product": "almacen"}\n\nNew message:\nefectivo'


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503, 408, 429])
async def test_retries_transient_status(client: OpenAICompatibleExtractor, status: int) -> None:
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(status), _answer(CANONICAL_EXTRACTION)]
    )
    assert await client.extract(CANONICAL_ORDER_TEXT) == CANONICAL_EXTRACTION
    assert route.call_count == 2


@respx.mock
async def test_retries_network_errors_then_gives_up(client: OpenAICompatibleExtractor) -> None:
    route = respx.post(URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(LLMError, match="4 attempts: ConnectTimeout"):
        await client.extract("x")
    assert route.call_count == 4  # 1 + LLM_MAX_RETRIES (3)


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_not_retried(
    client: OpenAICompatibleExtractor, status: int
) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(status, text="nope"))
    with pytest.raises(LLMError, match=f"HTTP {status}"):
        await client.extract("x")
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        '{"pickup_address": 5}',
        '{"payment_method": "bitcoin"}',
        '{"product": ["a"]}',
    ],
)
async def test_unusable_answers_are_retried_then_fail(
    client: OpenAICompatibleExtractor, answer: str
) -> None:
    route = respx.post(URL).mock(return_value=_answer(answer))
    with pytest.raises(LLMError, match="unusable answer"):
        await client.extract("x")
    assert route.call_count == 4


@respx.mock
async def test_malformed_envelope_is_unusable(client: OpenAICompatibleExtractor) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMError, match="unusable answer"):
        await client.extract("x")


async def test_api_key_is_not_in_errors() -> None:
    extractor = OpenAICompatibleExtractor(
        api_key="sk-secret-value", model="m", base_url=BASE, max_retries=0
    )
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(401, text="invalid key"))
        with pytest.raises(LLMError) as info:
            await extractor.extract("x")
    await extractor.aclose()
    assert "sk-secret-value" not in str(info.value)


def test_factory_builds_openai_compatible_from_settings() -> None:
    settings = make_settings(
        llm_provider="openai",
        llm_api_key="sk-x",
        llm_model="m",
        llm_base_url=BASE,
        llm_max_retries=1,
    )
    extractor = build_order_extractor(settings)
    assert isinstance(extractor, OpenAICompatibleExtractor)
    assert (extractor.model, extractor.max_retries) == ("m", 1)
    assert str(extractor._http.base_url) == f"{BASE}/"


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "openai"},
        {"llm_provider": "openai", "llm_api_key": "sk-x"},
        {"llm_provider": "openai", "llm_model": "m"},
        {"llm_provider": "anthropic", "llm_api_key": "k", "llm_model": "m"},
        {"llm_provider": "fake", "app_env": "production", "webhook_secret": "s",
         "evolution_api_key": "k"},
    ],
)  # fmt: skip
def test_factory_rejects_incomplete_config(overrides: dict) -> None:
    with pytest.raises(LLMConfigError):
        build_order_extractor(make_settings(**overrides))


def test_factory_fake_provider_in_development() -> None:
    settings = make_settings(llm_provider="fake", app_env="development")
    assert isinstance(build_order_extractor(settings), FakeOrderExtractor)


async def test_unavailable_extractor_fails_like_an_outage() -> None:
    with pytest.raises(LLMError, match="not configured"):
        await UnavailableOrderExtractor("no key").extract("x")
