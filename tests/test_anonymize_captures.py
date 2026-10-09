from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.anonymize_captures import REDACTED, Pseudonymizer, anonymize, anonymize_file, main
from tests.evolution_fixtures import load_fixture

REAL_PHONE = "5491187654321"
REAL_LID = "123456789012345"
REAL_GROUP = "120363111122223333"


def _capture() -> dict:
    payload = load_fixture("group_reaction")
    key = payload["data"]["key"]
    key.update(
        remoteJid=f"{REAL_GROUP}@g.us",
        participant=f"{REAL_LID}@lid",
        participantAlt=f"{REAL_PHONE}@s.whatsapp.net",
    )
    payload["data"]["pushName"] = "Joel Pérez"
    payload["data"]["message"]["reactionMessage"]["key"]["remoteJid"] = f"{REAL_GROUP}@g.us"
    payload["sender"] = "5491199990000@s.whatsapp.net"
    payload["server_url"] = "https://evo.example.com"
    return payload


def _text_capture() -> dict:
    payload = load_fixture("private_quoted_cancel")
    payload["data"]["key"]["remoteJid"] = f"{REAL_PHONE}@s.whatsapp.net"
    payload["data"]["message"]["conversation"] = f"cancelar, llamame al {REAL_PHONE}"
    payload["data"]["message"]["imageMessage"] = {"jpegThumbnail": "AAAA", "url": "https://mmg"}
    payload["data"]["pushName"] = "Joel Pérez"
    return payload


def test_personal_data_is_replaced_and_structure_kept() -> None:
    p = Pseudonymizer()
    out = anonymize(_capture(), p)
    dumped = json.dumps(out, ensure_ascii=False)
    for real in (REAL_PHONE, REAL_LID, REAL_GROUP, "Joel Pérez", "5491199990000", "evo.example"):
        assert real not in dumped
    key = out["data"]["key"]
    assert key["participant"].endswith("@lid")
    assert key["participantAlt"].endswith("@s.whatsapp.net")
    assert key["remoteJid"].endswith("@g.us")
    assert key["id"] == "3EB0A1B2C3D4E5F60007"  # message ids are kept
    reaction = out["data"]["message"]["reactionMessage"]
    assert reaction["text"] == "👍"  # emoji kept
    assert reaction["key"]["id"] == "BAE5F00000000003"
    assert reaction["key"]["remoteJid"] == key["remoteJid"]  # same group, same pseudonym
    assert out["data"]["messageTimestamp"] == 1791561600
    assert reaction["senderTimestampMs"] == "1791561600123"


def test_pseudonyms_are_stable_across_files() -> None:
    p = Pseudonymizer()
    first = anonymize(_capture(), p)
    second = anonymize(_text_capture(), p)
    # The courier's phone (participantAlt) and the private chat are the same person.
    assert first["data"]["key"]["participantAlt"] == second["data"]["key"]["remoteJid"]
    assert first["data"]["pushName"] == second["data"]["pushName"] == "Nombre 1"


def test_texts_and_media_are_replaced(tmp_path: Path) -> None:
    source = tmp_path / "in.json"
    source.write_text(json.dumps(_text_capture()))
    leftovers = anonymize_file(
        source, tmp_path / "out" / "in.json", Pseudonymizer(), keep_text=False
    )
    out = json.loads((tmp_path / "out" / "in.json").read_text())
    message = out["data"]["message"]
    assert message["conversation"].startswith("Texto anonimizado")
    assert message["imageMessage"] == {"jpegThumbnail": REDACTED, "url": REDACTED}
    assert REAL_PHONE not in json.dumps(out)
    assert leftovers == []


def test_keep_text_still_replaces_known_numbers_inside_texts(tmp_path: Path) -> None:
    source = tmp_path / "in.json"
    source.write_text(json.dumps(_text_capture()))
    anonymize_file(source, tmp_path / "o.json", Pseudonymizer(), keep_text=True)
    out = json.loads((tmp_path / "o.json").read_text())
    assert out["data"]["message"]["conversation"].startswith("cancelar, llamame al 549000")
    assert REAL_PHONE not in json.dumps(out)


def test_unknown_long_numbers_are_reported(tmp_path: Path) -> None:
    payload = load_fixture("private_text")
    payload["data"]["message"]["conversation"] = "x"
    payload["data"]["extra"] = "ref 99887766554433"
    source = tmp_path / "in.json"
    source.write_text(json.dumps(payload))
    leftovers = anonymize_file(source, tmp_path / "o.json", Pseudonymizer(), keep_text=True)
    assert leftovers == ["extra=99887766554433"]


def test_cli_folder_and_mapping_must_stay_in_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    captures = tmp_path / "data" / "captures"
    captures.mkdir(parents=True)
    (captures / "a.json").write_text(json.dumps(_capture()))
    (captures / "b.json").write_text(json.dumps(_text_capture()))
    mapping = Path("data/captures/pseudonyms.json")
    assert main([str(captures), "fixtures", "--mapping", str(mapping)]) == 0
    assert sorted(p.name for p in (tmp_path / "fixtures").iterdir()) == ["a.json", "b.json"]
    assert REAL_PHONE in mapping.read_text()  # real values only live in data/
    with pytest.raises(SystemExit):
        main([str(captures), "fixtures", "--mapping", "fixtures/map.json"])
