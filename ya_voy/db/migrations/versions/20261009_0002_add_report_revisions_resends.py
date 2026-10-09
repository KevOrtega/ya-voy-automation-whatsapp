"""Add report_revisions.resends (manual resend of failed reports, docs/06).

Counts the manual resends of a revision; it names the outbox keys of each round
(``daily_report:{D}:v{N}:r{K}:{phone}``) so a resend never collides with an earlier copy.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 02:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "report_revisions",
        sa.Column("resends", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("report_revisions", "resends")
