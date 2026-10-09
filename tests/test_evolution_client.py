from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from tests.conftest import make_settings
from ya_voy.whatsapp.client import EvolutionClient, EvolutionError, is_retryable_status

BASE = "http://evolution:8080"
API_KEY = "evo-key-123"


@pytest.fixture
async def client() -> AsyncIterator[EvolutionClient]:
    c = EvolutionClient(base_url=BASE + "/", api_key=API_KEY, instance="yavoy")
    yield c
    await c.aclose()


def _body(route: respx.Route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_send_text(client: EvolutionClient) -> None:
    route = respx.post(f"{BASE}/message/sendText/yavoy").respond(
        201, json={"key": {"id": "BAE5AAA", "remoteJid": "549@s.whatsapp.net", "fromMe": True}}
    )
    sent = await client.send_text("549@s.whatsapp.net", "hola")
    assert sent.wa_message_id == "BAE5AAA"
    assert route.calls.last.request.headers["apikey"] == API_KEY
    assert _body(route) == {"number": "549@s.whatsapp.net", "text": "hola"}


@respx.mock
async def test_send_text_quoting(client: EvolutionClient) -> None:
    route = respx.post(f"{BASE}/message/sendText/yavoy").respond(201, json={"key": {"id": "X"}})
    await client.send_text("1@g.us", "✅", quoted_wa_message_id="BAE5POST")
    assert _body(route)["quoted"] == {"key": {"id": "BAE5POST"}}


@respx.mock
async def test_send_document(client: EvolutionClient) -> None:
    route = respx.post(f"{BASE}/message/sendMedia/yavoy").respond(201, json={"key": {"id": "D"}})
    sent = await client.send_document(
        "549@s.whatsapp.net",
        media_base64="UEsDBA==",
        file_name="reporte.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        caption="Reporte",
    )
    assert sent.wa_message_id == "D"
    body = _body(route)
    assert body["mediatype"] == "document"
    assert body["fileName"] == "reporte.xlsx"
    assert body["media"] == "UEsDBA=="
    assert body["caption"] == "Reporte"


@respx.mock
async def test_delete_for_everyone(client: EvolutionClient) -> None:
    route = respx.delete(f"{BASE}/chat/deleteMessageForEveryone/yavoy").respond(
        201, json={"key": {"id": "P"}}
    )
    await client.delete_for_everyone("1@g.us", "BAE5POST")
    assert _body(route) == {"id": "BAE5POST", "remoteJid": "1@g.us", "fromMe": True}


@respx.mock
async def test_update_message(client: EvolutionClient) -> None:
    route = respx.post(f"{BASE}/chat/updateMessage/yavoy").respond(201, json={"key": {"id": "E"}})
    await client.update_message("1@g.us", "BAE5POST", "Pedido cancelado")
    assert _body(route) == {
        "number": "1@g.us",
        "key": {"id": "BAE5POST", "remoteJid": "1@g.us", "fromMe": True},
        "text": "Pedido cancelado",
    }


@respx.mock
async def test_http_error_raises_without_the_api_key(client: EvolutionClient) -> None:
    respx.post(f"{BASE}/message/sendText/yavoy").respond(400, json={"message": "bad number"})
    with pytest.raises(EvolutionError) as info:
        await client.send_text("x", "hola")
    assert info.value.status_code == 400
    assert "bad number" in str(info.value)
    assert API_KEY not in str(info.value)


@respx.mock
async def test_transport_error_raises_evolution_error(client: EvolutionClient) -> None:
    respx.post(f"{BASE}/message/sendText/yavoy").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(EvolutionError) as info:
        await client.send_text("x", "hola")
    assert info.value.status_code is None


@respx.mock
async def test_non_json_response_raises(client: EvolutionClient) -> None:
    respx.post(f"{BASE}/message/sendText/yavoy").respond(200, text="<html>")
    with pytest.raises(EvolutionError):
        await client.send_text("x", "hola")


async def test_from_settings() -> None:
    settings = make_settings(
        evolution_base_url="http://evo:8080", evolution_api_key="k", evolution_instance="i"
    )
    client = EvolutionClient.from_settings(settings)
    assert client.instance == "i"
    await client.aclose()


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (None, True),  # network error / timeout
        (408, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (422, False),
        (200, False),  # unusable 2xx body: may have been sent, do not resend
    ],
)
def test_retry_classification(status_code: int | None, retryable: bool) -> None:
    assert is_retryable_status(status_code) is retryable
    assert EvolutionError("op", "x", status_code).retryable is retryable


def test_explicit_retryable_overrides_status() -> None:
    assert EvolutionError("op", "x", retryable=False).retryable is False


@respx.mock
async def test_timeout_is_retryable(client: EvolutionClient) -> None:
    respx.post(f"{BASE}/message/sendText/yavoy").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(EvolutionError) as info:
        await client.send_text("x", "hola")
    assert info.value.retryable
