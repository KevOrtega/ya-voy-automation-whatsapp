"""Let retention anonymize inactive stores and couriers (RNF-10; docs/06 "Retención de datos").

Adds ``anonymized_at`` to ``stores`` and ``couriers``. An anonymized identity has no JID, phone or
``lid``: ``stores.wa_jid`` becomes nullable and both ``has_identity`` checks exempt anonymized rows
(``UNIQUE`` already allows many ``NULL``s).

Retention now also scrubs JIDs and courier names in rows past the window. Rows the first pass
already anonymized (revision 0004) are unmarked so the next run applies the new scrub; every
redaction is idempotent, so redoing the old ones changes nothing else.

Downgrade fails while an anonymized store or courier exists (identifiers cannot be restored).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09 11:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESCRUBBED = (
    "inbound_events",
    "messages",
    "outbound_messages",
    "schedule_overrides",
    "report_revisions",
)


def upgrade() -> None:
    op.add_column("stores", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("couriers", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("stores", "wa_jid", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint(
        op.f("stores_has_identity"), "stores", "anonymized_at IS NOT NULL OR wa_jid IS NOT NULL"
    )
    op.drop_constraint(op.f("couriers_has_identity"), "couriers", type_="check")
    op.create_check_constraint(
        op.f("couriers_has_identity"),
        "couriers",
        "anonymized_at IS NOT NULL OR lid IS NOT NULL OR phone_jid IS NOT NULL",
    )
    for table in RESCRUBBED:
        op.execute(f"UPDATE {table} SET anonymized_at = NULL WHERE anonymized_at IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint(op.f("couriers_has_identity"), "couriers", type_="check")
    op.create_check_constraint(
        op.f("couriers_has_identity"), "couriers", "lid IS NOT NULL OR phone_jid IS NOT NULL"
    )
    op.drop_constraint(op.f("stores_has_identity"), "stores", type_="check")
    op.alter_column("stores", "wa_jid", existing_type=sa.Text(), nullable=False)
    op.drop_column("couriers", "anonymized_at")
    op.drop_column("stores", "anonymized_at")
