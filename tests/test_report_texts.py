"""Captions and lists of the daily report, verbatim from doc 07."""

from __future__ import annotations

import datetime as dt

from tests.report_helpers import courier_row, order_row
from ya_voy.messaging import report_texts as texts
from ya_voy.reports.model import Change, ChangeType, ReportData

D = dt.date(2026, 10, 9)
J = (7, "Joel")


def test_initial_caption() -> None:
    data = ReportData(
        report_date=D,
        revision=1,
        orders=[order_row(1, "YV-0001", "assigned", courier=J), order_row(2, "YV-0002")],
        couriers=[courier_row(7, "Joel", taken=1)],
    )
    assert texts.caption(data) == (
        "admin_daily_report",
        "📊 Reporte del 09/10/2026: 2 pedidos publicados, 1 tomados, 1 cancelados, "
        "1 moteros con actividad.",
    )


def test_initial_caption_with_schedule_changes() -> None:
    data = ReportData(
        report_date=D,
        revision=1,
        orders=[],
        couriers=[],
        schedule_changes=[
            "cierre de 15:00 a 18:00 por lluvia",
            "apertura del 09/10/2026 de 23:00 a 00:00 (del 10/10)",
        ],
    )
    assert texts.caption(data)[1] == (
        "📊 Reporte del 09/10/2026: 0 pedidos publicados, 0 tomados, 0 cancelados, "
        "0 moteros con actividad.\nCambios de horario: cierre de 15:00 a 18:00 por lluvia, "
        "apertura del 09/10/2026 de 23:00 a 00:00 (del 10/10)"
    )


def test_correction_caption_matches_doc_07_example() -> None:
    data = ReportData(
        report_date=D,
        revision=2,
        orders=[order_row(1, "YV-0123", "assigned", courier=(8, "Emiliano"))],
        couriers=[],
        changes=[Change("YV-0123", ChangeType.COURIER_CHANGED, "Joel", "Emiliano")],
    )
    assert texts.caption(data) == (
        "admin_daily_report_correction",
        "🛠️ *Corrección* del reporte del 09/10/2026 (versión 2):\n"
        "• El pedido *YV-0123* pasó de Joel a Emiliano.\n\n"
        "Totales actualizados: 1 pedidos publicados, 1 tomados, 0 cancelados. "
        "Este archivo reemplaza a la versión anterior.",
    )


def test_change_lines() -> None:
    assert texts.changes_list(
        [
            Change("YV-0124", ChangeType.CANCELLED, "Joel", None),
            Change("YV-0130", ChangeType.ADDED, None, "Joel"),
            Change("YV-0131", ChangeType.ADDED, None, None),
        ]
    ) == (
        "• El pedido *YV-0124* fue cancelado (estaba asignado a Joel).\n"
        "• Pedido agregado *YV-0130* (tomado por Joel)\n"
        "• Pedido agregado *YV-0131* (cancelado)"
    )


def test_pending_reminder() -> None:
    assert texts.pending_reminder(D, ["YV-0057", "YV-0058"], correction=False) == (
        "⏳ El reporte del 09/10/2026 está listo, pero lo retengo porque quedan pedidos sin tomar: "
        "YV-0057, YV-0058. Cuando estén tomados o cancelados te lo mando. Para cancelar uno: "
        "*cancelar YV-0057*."
    )
    assert texts.pending_reminder(D, ["YV-0130"], correction=True).startswith(
        "⏳ La corrección del reporte del 09/10/2026 está listo"
    )


def test_report_failed() -> None:
    assert texts.report_failed(D, 2, "no pude armar el archivo") == (
        "❗ No pude generar o enviar el reporte del 09/10/2026 (versión 2). "
        "Error: no pude armar el archivo."
    )
