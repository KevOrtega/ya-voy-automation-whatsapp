"""Add parked_reactions (reactions that arrive before the group post id is known, docs/06 D-19).

A reaction whose target is not yet an order's ``group_message_id`` is parked here; when the post
id is recorded the pending ones are applied in ``reacted_at`` order, and bounded deferred retries
discard those that never match.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 02:51:29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "parked_reactions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("group_message_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("courier_id", sa.BigInteger(), nullable=False),
        sa.Column("emoji", sa.Text(), nullable=True),
        sa.Column("inbound_event_id", sa.BigInteger(), nullable=True),
        sa.Column("reacted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind = 'removed' OR emoji IS NOT NULL", name=op.f("parked_reactions_emoji_when_added")
        ),
        sa.CheckConstraint(
            "kind IN ('added', 'removed')", name=op.f("parked_reactions_kind_check")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'discarded')",
            name=op.f("parked_reactions_status_check"),
        ),
        sa.ForeignKeyConstraint(
            ["courier_id"], ["couriers.id"], name=op.f("parked_reactions_courier_id_fkey")
        ),
        sa.ForeignKeyConstraint(
            ["inbound_event_id"],
            ["inbound_events.id"],
            name=op.f("parked_reactions_inbound_event_id_fkey"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("parked_reactions_pkey")),
        sa.UniqueConstraint("inbound_event_id", name=op.f("parked_reactions_inbound_event_id_key")),
    )
    op.create_index(
        "parked_reactions_pending_target_idx",
        "parked_reactions",
        ["group_message_id", "reacted_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "parked_reactions_pending_target_idx",
        table_name="parked_reactions",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("parked_reactions")
