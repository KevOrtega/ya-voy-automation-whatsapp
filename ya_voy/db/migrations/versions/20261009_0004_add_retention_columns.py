"""Add anonymized_at columns and retention indexes (RNF-10, DC-11; docs/06 "Retención de datos").

``apply_data_retention`` anonymizes rows older than ``DATA_RETENTION_DAYS`` without deleting them
and marks them with ``anonymized_at``, so a run skips what an earlier run already did. The partial
indexes list only rows still to anonymize.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 03:08:33
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inbound_events", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "inbound_events_retention_idx",
        "inbound_events",
        ["received_at"],
        unique=False,
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.add_column("messages", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "messages_retention_idx",
        "messages",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.add_column(
        "order_events", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "order_events_retention_idx",
        "order_events",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.create_index(
        "orders_retention_idx",
        "orders",
        ["business_date"],
        unique=False,
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.add_column(
        "outbound_messages", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "outbound_messages_retention_idx",
        "outbound_messages",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.add_column(
        "report_revisions", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "schedule_overrides", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("schedule_overrides", "anonymized_at")
    op.drop_column("report_revisions", "anonymized_at")
    op.drop_index(
        "outbound_messages_retention_idx",
        table_name="outbound_messages",
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.drop_column("outbound_messages", "anonymized_at")
    op.drop_index(
        "orders_retention_idx",
        table_name="orders",
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.drop_index(
        "order_events_retention_idx",
        table_name="order_events",
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.drop_column("order_events", "anonymized_at")
    op.drop_index(
        "messages_retention_idx",
        table_name="messages",
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.drop_column("messages", "anonymized_at")
    op.drop_index(
        "inbound_events_retention_idx",
        table_name="inbound_events",
        postgresql_where=sa.text("anonymized_at IS NULL"),
    )
    op.drop_column("inbound_events", "anonymized_at")
