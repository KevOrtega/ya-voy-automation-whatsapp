from __future__ import annotations

import pytest

from tests.evolution_fixtures import (
    COURIERS_GROUP_JID,
    INSTANCE,
    fixture_names,
    load_fixture,
)
from ya_voy.whatsapp import jids
from ya_voy.whatsapp.dedup import compute_inbound_dedup_key, payload_hash
from ya_voy.whatsapp.filters import DiscardReason, discard_reason
from ya_voy.whatsapp.payloads import (
    ConnectionUpdate,
    InboundMessage,
    OtherEvent,
    PayloadError,
    parse_webhook,
    sanitize_payload,
)


def _reason(name: str) -> DiscardReason | None:
    event = parse_webhook(load_fixture(name))
    return discard_reason(event, instance=INSTANCE, couriers_group_jid=COURIERS_GROUP_JID)


@pytest.mark.parametrize("name", fixture_names())
def test_every_fixture_parses(name: str) -> None:
    assert parse_webhook(load_fixture(name)) is not None


def test_private_text() -> None:
    event = parse_webhook(load_fixture("private_text"))
    assert isinstance(event, InboundMessage)
    assert event.event_type == "messages.upsert"
    assert event.wa_message_id == "3EB0A1B2C3D4E5F60001"
    assert event.remote_jid == "5491155550001@s.whatsapp.net"
    assert not event.from_me
    assert event.push_name == "Pizzeria Test"
    assert event.text is not None and event.text.startswith("📍 Dirección de retiro")
    assert not event.is_reaction
    assert event.quoted_wa_message_id is None


def test_quoted_message_exposes_stanza_id() -> None:
    event = parse_webhook(load_fixture("private_quoted_cancel"))
    assert isinstance(event, InboundMessage)
    assert event.text == "cancelar"
    assert event.quoted_wa_message_id == "BAE5F00000000001"


def test_group_reaction() -> None:
    event = parse_webhook(load_fixture("group_reaction"))
    assert isinstance(event, InboundMessage)
    assert event.is_reaction and not event.is_reaction_removal
    assert event.reaction_target_id == "BAE5F00000000003"
    assert event.reaction_text == "👍"
    assert event.participant == "200000000000001@lid"
    assert event.participant_alt == "5491166660001@s.whatsapp.net"
    assert event.text is None


def test_reaction_removal_is_empty_text() -> None:
    event = parse_webhook(load_fixture("group_reaction_removal"))
    assert isinstance(event, InboundMessage)
    assert event.is_reaction_removal
    assert event.reaction_target_id == "BAE5F00000000003"


def test_extended_text_message_is_read_too() -> None:
    payload = load_fixture("private_text")
    payload["data"]["message"] = {"extendedTextMessage": {"text": "hola"}}
    event = parse_webhook(payload)
    assert isinstance(event, InboundMessage)
    assert event.text == "hola"


def test_non_text_message_has_no_text() -> None:
    event = parse_webhook(load_fixture("private_image"))
    assert isinstance(event, InboundMessage)
    assert event.text is None
    assert event.message_type == "imageMessage"


def test_connection_update() -> None:
    event = parse_webhook(load_fixture("connection_update_close"))
    assert isinstance(event, ConnectionUpdate)
    assert event.state == "close"
    assert event.status_reason == 401


def test_unsupported_event() -> None:
    event = parse_webhook(load_fixture("unsupported_event"))
    assert isinstance(event, OtherEvent)
    assert event.event_type == "presence.update"


def test_event_name_is_case_insensitive() -> None:
    # Evolution accepts MESSAGES_UPSERT-style names in its config; be tolerant.
    payload = load_fixture("private_text")
    payload["event"] = "MESSAGES.UPSERT"
    assert isinstance(parse_webhook(payload), InboundMessage)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        {},
        {"event": None},
        {"event": "messages.upsert", "data": None},
        {"event": "messages.upsert", "data": {"message": {}}},
        {"event": "messages.upsert", "data": {"key": "not-an-object"}},
    ],
)
def test_malformed_payloads_raise_payload_error(payload: object) -> None:
    with pytest.raises(PayloadError):
        parse_webhook(payload)


def test_sanitize_payload_redacts_secret_envelope_fields() -> None:
    payload = load_fixture("private_text")
    payload["destination"] = "http://web:8000/webhooks/evolution/super-secret"
    payload["apikey"] = "instance-key"
    clean = sanitize_payload(payload)
    assert clean["destination"] == "***"
    assert clean["apikey"] == "***"
    assert payload["destination"].endswith("super-secret")  # input untouched
    assert clean["data"] == payload["data"]


# --- filters (web layer) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("private_text", None),
        ("private_text_lid_with_alt", None),
        ("private_text_lid_only", None),
        ("private_quoted_cancel", None),
        ("private_image", None),  # the worker answers non-text messages (later stages)
        ("group_reaction", None),
        ("group_reaction_lid_only", None),
        ("group_reaction_removal", None),
        ("connection_update_close", None),
        ("connection_update_open", None),
        ("private_from_me", DiscardReason.FROM_ME),
        ("status_broadcast", DiscardReason.STATUS_BROADCAST),
        ("other_group_reaction", DiscardReason.OTHER_GROUP),
        ("group_text", DiscardReason.GROUP_NOT_REACTION),
        ("unsupported_event", DiscardReason.UNSUPPORTED_EVENT),
    ],
)
def test_discard_reason(name: str, expected: DiscardReason | None) -> None:
    assert _reason(name) == expected


def test_other_instance_is_discarded() -> None:
    payload = load_fixture("private_text")
    payload["instance"] = "someone-else"
    event = parse_webhook(payload)
    reason = discard_reason(event, instance=INSTANCE, couriers_group_jid=COURIERS_GROUP_JID)
    assert reason == DiscardReason.OTHER_INSTANCE


def test_group_messages_dropped_when_couriers_group_not_configured() -> None:
    event = parse_webhook(load_fixture("group_reaction"))
    assert discard_reason(event, instance=INSTANCE, couriers_group_jid="") == (
        DiscardReason.OTHER_GROUP
    )


def test_message_without_chat_is_discarded() -> None:
    payload = load_fixture("private_text", remoteJid=None)
    event = parse_webhook(payload)
    reason = discard_reason(event, instance=INSTANCE, couriers_group_jid=COURIERS_GROUP_JID)
    assert reason == DiscardReason.NO_CHAT


# --- dedup key -----------------------------------------------------------------------------


def test_dedup_key_is_the_message_key_id() -> None:
    payload = load_fixture("private_text")
    assert compute_inbound_dedup_key(parse_webhook(payload), payload) == "3EB0A1B2C3D4E5F60001"


def test_dedup_key_for_a_reaction_is_its_own_key_id_not_the_target() -> None:
    payload = load_fixture("group_reaction")
    key = compute_inbound_dedup_key(parse_webhook(payload), payload)
    assert key == "3EB0A1B2C3D4E5F60007"


def test_dedup_key_without_message_id_is_a_stable_payload_hash() -> None:
    payload = load_fixture("connection_update_close")
    reordered = dict(reversed(list(payload.items())))
    key = compute_inbound_dedup_key(parse_webhook(payload), payload)
    assert key.startswith("sha256:")
    assert key == compute_inbound_dedup_key(parse_webhook(reordered), reordered)
    other = load_fixture("connection_update_open")
    assert key != compute_inbound_dedup_key(parse_webhook(other), other)


def test_dedup_key_message_without_id_falls_back_to_hash() -> None:
    payload = load_fixture("private_text", id=None)
    assert compute_inbound_dedup_key(parse_webhook(payload), payload) == payload_hash(payload)


# --- jids ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("jid", "phone"),
    [
        ("5491155550001@s.whatsapp.net", "5491155550001"),
        ("5491155550001:12@s.whatsapp.net", "5491155550001"),
        ("100000000000001@lid", None),
        ("120363000000000001@g.us", None),
        (None, None),
        ("", None),
    ],
)
def test_phone_from_jid(jid: str | None, phone: str | None) -> None:
    assert jids.phone_from_jid(jid) == phone


def test_jid_kinds() -> None:
    assert jids.is_group("1@g.us") and not jids.is_group("1@lid")
    assert jids.is_lid("1@lid") and not jids.is_lid("1@s.whatsapp.net")
    assert jids.is_status_broadcast("status@broadcast")
    assert jids.jid_for_phone("549") == "549@s.whatsapp.net"
