"""Spanish texts of the daily report: captions, ``{changes_list}``, ``{schedule_note}`` and the
cell labels of the workbook (doc 07 "Alertas y reporte", docs/05 "Contenido del archivo")."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from ya_voy.messaging import catalog
from ya_voy.reports.model import Change, ChangeType, ReportData


def fmt_report_date(day: dt.date) -> str:
    return day.strftime("%d/%m/%Y")


def _line_key(change: Change) -> str:
    if change.type == ChangeType.ADDED and change.to_courier is None:
        return "added_cancelled"
    return str(change.type)


def change_line(change: Change) -> str:
    """One line of ``{changes_list}`` (doc 07 "Textos de cambios")."""
    return catalog.CHANGE_LINES[_line_key(change)].format(
        code=change.code, from_courier=change.from_courier, to_courier=change.to_courier
    )


def changes_list(changes: Sequence[Change]) -> str:
    return "\n".join(change_line(c) for c in changes)


def change_detail(change: Change) -> str:
    """``Detalle`` in the ``Correcciones`` sheet: the same line without bullet or bold marks."""
    return change_line(change).removeprefix("• ").replace("*", "")


def before_label(change: Change) -> str:
    if change.type == ChangeType.ADDED:
        return catalog.NOT_IN_PREVIOUS_VERSION
    return catalog.TAKEN_BY.format(courier=change.from_courier)


def after_label(change: Change) -> str:
    if change.to_courier is None:
        return catalog.ORDER_STATUS_LABELS["cancelled"]
    return catalog.TAKEN_BY.format(courier=change.to_courier)


def schedule_note(items: Sequence[str]) -> str:
    return catalog.SCHEDULE_NOTE.format(items=", ".join(items)) if items else ""


def caption(data: ReportData) -> tuple[str, str]:
    """``(template_key, body)`` of the document message for this version."""
    counts = {
        "report_date": fmt_report_date(data.report_date),
        "orders_count": data.orders_count,
        "assigned_count": data.assigned_count,
        "cancelled_count": data.cancelled_count,
    }
    if data.is_correction:
        key = catalog.ADMIN_DAILY_REPORT_CORRECTION
        return key, catalog.render(
            key, revision=data.revision, changes_list=changes_list(data.changes), **counts
        )
    key = catalog.ADMIN_DAILY_REPORT
    return key, catalog.render(
        key,
        couriers_count=data.couriers_count,
        schedule_note=schedule_note(data.schedule_changes),
        **counts,
    )


def pending_reminder_variables(
    report_date: dt.date, codes: Sequence[str], *, correction: bool
) -> dict[str, str]:
    """``{code}`` in "Para cancelar uno: *cancelar {code}*" is the first pending code."""
    label = catalog.REPORT_LABEL_CORRECTION if correction else catalog.REPORT_LABEL_INITIAL
    return {
        "report_label": label,
        "ready_label": (
            catalog.READY_LABEL_CORRECTION if correction else catalog.READY_LABEL_INITIAL
        ),
        "report_date": fmt_report_date(report_date),
        "pending_codes": ", ".join(codes),
        "code": codes[0],
    }


def pending_reminder(report_date: dt.date, codes: Sequence[str], *, correction: bool) -> str:
    return catalog.render(
        catalog.ADMIN_REPORT_PENDING_REMINDER,
        **pending_reminder_variables(report_date, codes, correction=correction),
    )


def report_failed_variables(report_date: dt.date, revision: int, error: str) -> dict[str, object]:
    return {"report_date": fmt_report_date(report_date), "revision": revision, "error": error}


def report_failed(report_date: dt.date, revision: int, error: str) -> str:
    return catalog.render(
        catalog.ADMIN_DAILY_REPORT_FAILED, **report_failed_variables(report_date, revision, error)
    )
