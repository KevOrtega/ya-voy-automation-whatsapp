"""Snapshots and net changes of the daily report (docs/05 report_revisions, RN-21, RN-23)."""

from __future__ import annotations

import datetime as dt

from tests.report_helpers import courier_row, order_row
from ya_voy.reports.model import (
    Change,
    ChangeType,
    ReportData,
    diff_snapshots,
    file_name,
    snapshot_of,
)

D = dt.date(2026, 10, 9)


def test_snapshot_has_the_documented_shape() -> None:
    rows = [order_row(42, "YV-0123", "assigned", courier=(7, "Joel")), order_row(43, "YV-0124")]
    assert snapshot_of(rows) == {
        "42": {"code": "YV-0123", "status": "assigned", "courier_id": 7, "courier_name": "Joel"},
        "43": {"code": "YV-0124", "status": "cancelled", "courier_id": None, "courier_name": None},
    }


def snap(*rows):
    return snapshot_of(list(rows))


def test_courier_changed() -> None:
    before = snap(order_row(1, "YV-0123", "assigned", courier=(7, "Joel")))
    after = snap(order_row(1, "YV-0123", "assigned", courier=(8, "Emiliano")))
    assert diff_snapshots(before, after) == [
        Change("YV-0123", ChangeType.COURIER_CHANGED, "Joel", "Emiliano")
    ]


def test_release_and_retake_by_the_same_courier_is_no_change_rn23() -> None:
    before = snap(order_row(1, "YV-0123", "assigned", courier=(7, "Joel")))
    after = snap(order_row(1, "YV-0123", "assigned", courier=(7, "Joel (nuevo nombre)")))
    assert diff_snapshots(before, after) == []


def test_cancelled_after_report() -> None:
    before = snap(order_row(1, "YV-0123", "assigned", courier=(7, "Joel")))
    after = snap(order_row(1, "YV-0123", "cancelled"))
    assert diff_snapshots(before, after) == [Change("YV-0123", ChangeType.CANCELLED, "Joel", None)]


def test_cancelled_stays_cancelled() -> None:
    before = snap(order_row(1, "YV-0123", "cancelled"))
    assert diff_snapshots(before, snap(order_row(1, "YV-0123", "cancelled"))) == []


def test_added_orders_sorted_by_code() -> None:
    before = snap(order_row(1, "YV-0123", "assigned", courier=(7, "Joel")))
    after = snap(
        order_row(1, "YV-0123", "assigned", courier=(7, "Joel")),
        order_row(3, "YV-0131", "assigned", courier=(8, "Emiliano")),
        order_row(2, "YV-0130", "cancelled"),
    )
    assert diff_snapshots(before, after) == [
        Change("YV-0130", ChangeType.ADDED, None, None),
        Change("YV-0131", ChangeType.ADDED, None, "Emiliano"),
    ]


def test_no_diff_for_identical_snapshots() -> None:
    rows = snap(order_row(1, "YV-0123", "assigned", courier=(7, "Joel")))
    assert diff_snapshots(rows, dict(rows)) == []


def test_changes_round_trip_through_json() -> None:
    change = Change("YV-0123", ChangeType.COURIER_CHANGED, "Joel", "Emiliano")
    assert change.as_json() == {
        "code": "YV-0123",
        "type": "courier_changed",
        "from": "Joel",
        "to": "Emiliano",
    }
    assert Change.from_json(change.as_json()) == change


def test_file_names_from_doc_05() -> None:
    assert file_name(D, 1) == "reporte-ya-voy-2026-10-09.xlsx"
    assert file_name(D, 2) == "reporte-ya-voy-2026-10-09-correccion-2.xlsx"


def test_counts() -> None:
    data = ReportData(
        report_date=D,
        revision=1,
        orders=[
            order_row(1, "YV-0001", "assigned", courier=(7, "Joel")),
            order_row(2, "YV-0002", "cancelled"),
            order_row(3, "YV-0003", "assigned", courier=(8, "Emiliano")),
        ],
        couriers=[courier_row(7, "Joel", taken=1), courier_row(8, "Emiliano", taken=1)],
    )
    assert (data.orders_count, data.assigned_count, data.cancelled_count) == (3, 2, 1)
    assert data.couriers_count == 2
    assert not data.is_correction
