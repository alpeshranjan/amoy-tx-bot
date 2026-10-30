"""Let a failing simulation wait without blocking the other unsigned rows.

Revision ID: 004
"""

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("relayer_txs", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("relayer_txs", "next_attempt_at")
