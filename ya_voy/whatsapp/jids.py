"""WhatsApp JID helpers (``5491155551234@s.whatsapp.net``, ``123@lid``, ``123@g.us``)."""

from __future__ import annotations

USER_SUFFIX = "@s.whatsapp.net"
LID_SUFFIX = "@lid"
GROUP_SUFFIX = "@g.us"
STATUS_BROADCAST = "status@broadcast"


def is_group(jid: str | None) -> bool:
    return bool(jid) and jid.endswith(GROUP_SUFFIX)


def is_lid(jid: str | None) -> bool:
    return bool(jid) and jid.endswith(LID_SUFFIX)


def is_status_broadcast(jid: str | None) -> bool:
    return jid == STATUS_BROADCAST


def is_broadcast(jid: str | None) -> bool:
    return bool(jid) and jid.endswith("@broadcast")


def phone_from_jid(jid: str | None) -> str | None:
    """``5491155551234@s.whatsapp.net`` -> ``5491155551234``; ``None`` for ``@lid``/groups."""
    if not jid or not jid.endswith(USER_SUFFIX):
        return None
    user = jid.removesuffix(USER_SUFFIX).split(":", 1)[0]  # drop a device suffix ("549...:12")
    return user if user.isdigit() else None


def jid_for_phone(phone: str) -> str:
    return f"{phone}{USER_SUFFIX}"
