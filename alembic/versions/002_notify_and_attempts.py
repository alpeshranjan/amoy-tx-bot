"""Record unsent transfer notices and simulation attempts.

Revision ID: 002
"""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transfers", sa.Column("notified_at", sa.DateTime(timezone=True)))
    op.add_column(
        "relayer_txs",
        sa.Column("sim_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    # Rows finished before this column existed are already known to the sender.
    op.execute(
        "UPDATE transfers SET notified_at = CURRENT_TIMESTAMP "
        "WHERE notified_at IS NULL AND status IN ('mined', 'failed')"
    )


def downgrade() -> None:
    op.drop_column("relayer_txs", "sim_attempts")
    op.drop_column("transfers", "notified_at")
