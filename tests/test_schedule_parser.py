"""Schedule parsers (fake and OpenAI-compatible over a mocked API) and admin command parsing."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
import respx

from tests.conftest import make_settings
from ya_voy.domain.commands import AdminCommand, ParsedAdminCommand, parse_admin_command
from ya_voy.domain.enums import ScheduleOverrideKind as K
from ya_voy.llm.extractor import LLMError, ScheduleContext, ScheduleIntentParser
from ya_voy.llm.factory import LLMConfigError, build_schedule_parser
from ya_voy.llm.fake_schedule import FakeScheduleParser
from ya_voy.llm.openai_compatible import OpenAICompatibleScheduleParser
from ya_voy.llm.schemas import ScheduleOverrideIntent
from ya_voy.llm.unavailable import UnavailableScheduleParser

TODAY = dt.date(2026, 10, 9)
CONTEXT = ScheduleContext(
    now_local=dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=-3))),
    business_date=TODAY,
    hours_start=dt.time(9),
    hours_end=dt.time(23),
)
BASE = "http://llm.test/v1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # docs/05 table
        ("hoy no tomamos pedidos por lluvia", {"kind": K.CLOSED_TODAY, "reason": "lluvia"}),
        ("hoy no tomamos pedidos hasta nuevo aviso", {"kind": K.CLOSED_UNTIL_FURTHER_NOTICE}),
        (
            "de 15 a 18 no tomaremos pedidos hoy",
            {"kind": K.CLOSED_WINDOW, "date": TODAY, "start_time": dt.time(15),
             "end_time": dt.time(18)},
        ),
        (
            "hoy hasta las 00",
            {"kind": K.OPEN_WINDOW, "date": TODAY, "start_time": dt.time(23),
             "end_time": dt.time(0)},
        ),
        (
            "hoy tomamos pedidos hasta la medianoche",
            {"kind": K.OPEN_WINDOW, "date": TODAY, "start_time": dt.time(23),
             "end_time": dt.time(0)},
        ),
        (
            "mañana abrimos a las 8",
            {"kind": K.OPEN_WINDOW, "date": TODAY + dt.timedelta(1), "start_time": dt.time(8),
             "end_time": dt.time(9)},
        ),
        ("ya tomamos pedidos de nuevo", {"kind": K.REOPEN}),
        ("¿cuántos pedidos hubo hoy?", {}),
        # ambiguous hours are read within the base hours (docs/05)
        (
            "de 3 a 6 no tomamos pedidos",
            {"kind": K.CLOSED_WINDOW, "date": TODAY, "start_time": dt.time(15),
             "end_time": dt.time(18)},
        ),
    ],
)  # fmt: skip
async def test_fake_parser_doc_05_examples(text: str, expected: dict) -> None:
    parser = FakeScheduleParser()
    assert isinstance(parser, ScheduleIntentParser)
    assert await parser.parse(text, CONTEXT) == ScheduleOverrideIntent(**expected)


@respx.mock
async def test_openai_parser_request_and_answer() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "kind": "closed_window",
                                    "date": "2026-10-09",
                                    "start_time": "15:00",
                                    "end_time": "18:00",
                                    "reason": None,
                                }
                            )
                        }
                    }
                ]
            },
        )
    )
    parser = OpenAICompatibleScheduleParser(
        api_key="sk-test", model="m", base_url=BASE, backoff_base_seconds=0
    )
    try:
        intent = await parser.parse("de 15 a 18 no tomaremos pedidos hoy", CONTEXT)
    finally:
        await parser.aclose()
    assert intent == ScheduleOverrideIntent(
        kind=K.CLOSED_WINDOW, date=TODAY, start_time=dt.time(15), end_time=dt.time(18)
    )
    body = json.loads(route.calls.last.request.content)
    assert body["response_format"]["json_schema"]["name"] == "ScheduleOverrideIntent"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert set(body["response_format"]["json_schema"]["schema"]["required"]) == set(
        ScheduleOverrideIntent.model_fields
    )
    user = body["messages"][1]["content"]
    assert '"business_day": "2026-10-09"' in user
    assert '"base_hours": "09:00-23:00"' in user
    assert user.endswith("de 15 a 18 no tomaremos pedidos hoy")


@respx.mock
async def test_openai_parser_retries_then_fails() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(503))
    parser = OpenAICompatibleScheduleParser(
        api_key="k", model="m", base_url=BASE, max_retries=2, backoff_base_seconds=0
    )
    try:
        with pytest.raises(LLMError, match="3 attempts"):
            await parser.parse("x", CONTEXT)
    finally:
        await parser.aclose()
    assert route.call_count == 3


@respx.mock
async def test_openai_parser_rejects_unusable_kind() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": '{"kind": "party"}'}}]}
        )
    )
    parser = OpenAICompatibleScheduleParser(
        api_key="k", model="m", base_url=BASE, max_retries=0, backoff_base_seconds=0
    )
    try:
        with pytest.raises(LLMError, match="unusable"):
            await parser.parse("x", CONTEXT)
    finally:
        await parser.aclose()


def test_factory() -> None:
    real = build_schedule_parser(make_settings(llm_api_key="k", llm_model="m"))
    assert isinstance(real, OpenAICompatibleScheduleParser)
    fake = build_schedule_parser(make_settings(llm_provider="fake"))
    assert isinstance(fake, FakeScheduleParser)
    with pytest.raises(LLMConfigError):
        build_schedule_parser(make_settings())


async def test_unavailable_parser() -> None:
    with pytest.raises(LLMError, match="not configured"):
        await UnavailableScheduleParser("no key").parse("x", CONTEXT)


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("sí", ParsedAdminCommand(AdminCommand.CONFIRM)),
        ("Si", ParsedAdminCommand(AdminCommand.CONFIRM)),
        ("dale", ParsedAdminCommand(AdminCommand.CONFIRM)),
        ("OK", ParsedAdminCommand(AdminCommand.CONFIRM)),
        ("confirmo", ParsedAdminCommand(AdminCommand.CONFIRM)),
        ("👍", None),  # store only (doc 07)
        ("no", ParsedAdminCommand(AdminCommand.REJECT)),
        ("estado", ParsedAdminCommand(AdminCommand.STATUS)),
        ("Horario", ParsedAdminCommand(AdminCommand.STATUS)),
        ("ayuda", ParsedAdminCommand(AdminCommand.HELP)),
        ("cancelar yv-0042", ParsedAdminCommand(AdminCommand.CANCEL, "YV-0042")),
        ("liberar YV-0042", ParsedAdminCommand(AdminCommand.RELEASE, "YV-0042")),
        ("cancelar", None),
        ("hoy no tomamos pedidos por lluvia", None),
        ("no tomamos pedidos de 15 a 18", None),
        ("", None),
    ],
)
def test_parse_admin_command(text: str, parsed: ParsedAdminCommand | None) -> None:
    assert parse_admin_command(text) == parsed
