"""Turn captured Evolution webhook payloads into anonymized test fixtures.

    python -m scripts.anonymize_captures data/captures tests/fixtures/evolution/captured \
        [--mapping data/captures/pseudonyms.json] [--keep-text]

Captures (``CAPTURE_INBOUND_PAYLOADS=1``) hold real phone numbers, ``@lid`` ids, names and
message texts (RNF-09). This script replaces them with **stable** pseudonyms: the same real value
gets the same fake one in every file of a run (and across runs with ``--mapping``), so the links
between events survive (a reaction's participant is the same courier as in the next event, the
``@lid`` and the phone of one person stay paired). Structure, field names, message ids
(``key.id``, ``stanzaId``, ``reactionMessage.key.id``), timestamps and reaction emojis are kept.

The mapping file contains the real values: keep it under ``data/`` (gitignored), never commit it.
Review the output before committing; the script prints any long digit run it could not explain.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PHONE_JID = re.compile(r"(?<!\d)(\d{8,15})(?::\d+)?@s\.whatsapp\.net")
LID_JID = re.compile(r"(?<!\d)(\d{6,20})(?::\d+)?@lid")
GROUP_JID = re.compile(r"(?<!\d)(\d{6,20}(?:-\d+)?)@g\.us")
BARE_PHONE = re.compile(r"^\+?\d{10,15}$")
LONG_DIGITS = re.compile(r"\d{8,}")

# Free text written by people.
TEXT_KEYS = {
    "conversation",
    "text",
    "caption",
    "body",
    "title",
    "description",
    "selectedDisplayText",
}
# Display names.
NAME_KEYS = {"pushName", "profileName", "verifiedBizName", "notify", "name", "subject", "fileName"}
# Media bytes, URLs and keys: dropped entirely.
REDACT_KEYS = {
    "base64",
    "jpegThumbnail",
    "thumbnail",
    "url",
    "directPath",
    "mediaKey",
    "fileSha256",
    "fileEncSha256",
    "profilePictureUrl",
    "profilePicUrl",
    "thumbnailDirectPath",
    "thumbnailSha256",
    "thumbnailEncSha256",
    "streamingSidecar",
    "waveform",
}
# Keys that may hold a bare phone number ("5491155551234") instead of a JID.
PHONE_KEYS = {"number", "phone", "phoneNumber", "owner", "wuid", "sender", "senderPn"}
# Long digit runs that are not personal data.
NON_PERSONAL_KEYS = {"id", "stanzaId", "instanceId", "date_time", "messageTimestamp"}
REDACTED = "<redacted>"
SERVER_URL = "http://localhost:8080"


class Pseudonymizer:
    """Stable real -> fake mapping per kind (phone, lid, group, name, text)."""

    def __init__(self, mapping: dict[str, dict[str, str]] | None = None) -> None:
        self.mapping: dict[str, dict[str, str]] = {
            kind: dict((mapping or {}).get(kind, {}))
            for kind in ("phone", "lid", "group", "name", "text")
        }

    def _get(self, kind: str, real: str, make: Any) -> str:
        table = self.mapping[kind]
        if real not in table:
            table[real] = make(len(table) + 1)
        return table[real]

    def phone(self, real: str) -> str:
        return self._get("phone", real, lambda n: f"54900000{n:05d}")

    def lid(self, real: str) -> str:
        return self._get("lid", real, lambda n: f"9{n:014d}")

    def group(self, real: str) -> str:
        return self._get("group", real, lambda n: f"120363{n:012d}")

    def name(self, real: str) -> str:
        return self._get("name", real, lambda n: f"Nombre {n}")

    def text(self, real: str) -> str:
        return self._get("text", real, lambda n: f"Texto anonimizado {n}")

    def jids_in(self, value: str) -> str:
        value = PHONE_JID.sub(lambda m: f"{self.phone(m.group(1))}@s.whatsapp.net", value)
        value = LID_JID.sub(lambda m: f"{self.lid(m.group(1))}@lid", value)
        value = GROUP_JID.sub(lambda m: f"{self.group(m.group(1))}@g.us", value)
        return value

    def known_numbers_in(self, value: str) -> str:
        """Real numbers already seen (e.g. a phone quoted inside a text) -> their pseudonyms."""
        for kind in ("phone", "lid", "group"):
            for real, fake in self.mapping[kind].items():
                if real in value:
                    value = value.replace(real, fake)
        return value


def _is_reaction_text(path: tuple[str, ...]) -> bool:
    return len(path) >= 2 and path[-2] == "reactionMessage" and path[-1] == "text"


def anonymize(
    node: Any, p: Pseudonymizer, *, keep_text: bool = False, path: tuple[str, ...] = ()
) -> Any:
    if isinstance(node, dict):
        return {
            key: anonymize(value, p, keep_text=keep_text, path=(*path, key))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [anonymize(value, p, keep_text=keep_text, path=path) for value in node]
    if not isinstance(node, str):
        return node

    key = path[-1] if path else ""
    if key in REDACT_KEYS:
        return REDACTED
    if key == "server_url":
        return SERVER_URL
    if key in NAME_KEYS and node:
        return p.name(node)
    if key in TEXT_KEYS and node and not _is_reaction_text(path):
        return node if keep_text else p.text(node)
    if key in PHONE_KEYS and BARE_PHONE.match(node):
        return p.phone(node.lstrip("+"))
    return p.jids_in(node)


def leftovers(node: Any, p: Pseudonymizer) -> list[str]:
    """Digit runs still present that are not pseudonyms; review them by hand."""
    fakes = {fake for table in p.mapping.values() for fake in table.values()}
    found: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            for match in LONG_DIGITS.findall(value):
                if (
                    not any(match in fake for fake in fakes)
                    and key not in NON_PERSONAL_KEYS
                    and "imestamp" not in key
                ):
                    found.append(f"{key}={match}")

    walk(node)
    return found


def anonymize_file(source: Path, target: Path, p: Pseudonymizer, *, keep_text: bool) -> list[str]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    # Two passes: the first collects every JID/phone, the second also replaces real numbers
    # that appear inside other strings (quoted texts, names).
    anonymize(payload, p, keep_text=keep_text)
    clean = anonymize(payload, p, keep_text=keep_text)
    clean = json.loads(p.known_numbers_in(json.dumps(clean, ensure_ascii=False)))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return leftovers(clean, p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="capture file or folder (data/captures)")
    parser.add_argument("target", type=Path, help="output folder for the fixtures")
    parser.add_argument(
        "--mapping", type=Path, help="JSON file to load/save pseudonyms (keep in data/)"
    )
    parser.add_argument(
        "--keep-text", action="store_true", help="keep message texts (fake test texts only)"
    )
    args = parser.parse_args(argv)

    if args.mapping and not args.mapping.resolve().is_relative_to(Path("data").resolve()):
        parser.error("--mapping holds real numbers: keep it under data/ (gitignored)")
    mapping = (
        json.loads(args.mapping.read_text()) if args.mapping and args.mapping.exists() else None
    )
    p = Pseudonymizer(mapping)

    files = sorted(args.source.glob("*.json")) if args.source.is_dir() else [args.source]
    warnings = 0
    for source in files:
        for item in anonymize_file(source, args.target / source.name, p, keep_text=args.keep_text):
            print(f"review {source.name}: {item}", file=sys.stderr)
            warnings += 1
    if args.mapping:
        args.mapping.parent.mkdir(parents=True, exist_ok=True)
        args.mapping.write_text(json.dumps(p.mapping, ensure_ascii=False, indent=2))
    print(f"anonymized {len(files)} file(s) into {args.target} ({warnings} item(s) to review)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
