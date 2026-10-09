"""Deterministic offline ``ScheduleIntentParser`` (tests and ``LLM_PROVIDER=fake``).

Understands the phrasings of docs/05 and close variants only; anything else is "not a schedule
instruction". It is not a substitute for the real model.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

from ya_voy.domain.enums import ScheduleOverrideKind as K
from ya_voy.llm.extractor import ScheduleContext
from ya_voy.llm.schemas import ScheduleOverrideIntent

_REASON = re.compile(r"\bpor (?:el |la |los |las )?(?P<reason>[a-z ]+?)\s*$")
_RANGE = re.compile(
    r"\bde (?P<start>\d{1,2})(?::(?P<sm>\d{2}))? a (?P<end>\d{1,2})(?::(?P<em>\d{2}))?"
)
_UNTIL = re.compile(r"\bhasta (?:las |la )?(?P<hour>\d{1,2})(?::(?P<m>\d{2}))?\b")
_OPEN_AT = re.compile(r"\babrimos a las (?P<hour>\d{1,2})(?::(?P<m>\d{2}))?\b")


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[¿?¡!.,]", "", plain).strip()


def _time(hour: str, minute: str | None) -> dt.time:
    return dt.time(int(hour) % 24, int(minute or 0))


class FakeScheduleParser:
    async def parse(self, text: str, context: ScheduleContext) -> ScheduleOverrideIntent:
        t = _plain(text)
        day = context.business_date + dt.timedelta(days=1 if "manana" in t else 0)
        reason_match = _REASON.search(t)
        reason = reason_match.group("reason") if reason_match else None

        if "de nuevo" in t and "tomamos" in t:
            return ScheduleOverrideIntent(kind=K.REOPEN)
        if "nuevo aviso" in t:
            return ScheduleOverrideIntent(kind=K.CLOSED_UNTIL_FURTHER_NOTICE, reason=reason)
        if (match := _RANGE.search(t)) and " no " in f" {t} ":
            start, end = _time(match["start"], match["sm"]), _time(match["end"], match["em"])
            if start < context.hours_start:  # "de 3 a 6" -> 15:00-18:00 (docs/05)
                start = start.replace(hour=start.hour + 12)
                end = end.replace(hour=(end.hour + 12) % 24)
            return ScheduleOverrideIntent(
                kind=K.CLOSED_WINDOW, date=day, start_time=start, end_time=end, reason=reason
            )
        if match := _OPEN_AT.search(t):
            return ScheduleOverrideIntent(
                kind=K.OPEN_WINDOW,
                date=day,
                start_time=_time(match["hour"], match["m"]),
                end_time=context.hours_start,
            )
        if "medianoche" in t:
            return ScheduleOverrideIntent(
                kind=K.OPEN_WINDOW, date=day, start_time=context.hours_end, end_time=dt.time(0)
            )
        if (match := _UNTIL.search(t)) and "no tomamos" not in t:
            return ScheduleOverrideIntent(
                kind=K.OPEN_WINDOW,
                date=day,
                start_time=context.hours_end,
                end_time=_time(match["hour"], match["m"]),
            )
        if "no tomamos pedidos" in t or "no se toman pedidos" in t:
            return ScheduleOverrideIntent(kind=K.CLOSED_TODAY, reason=reason)
        return ScheduleOverrideIntent()
