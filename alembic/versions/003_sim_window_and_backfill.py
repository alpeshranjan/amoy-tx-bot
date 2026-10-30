"""Remember when a simulation first failed, and mark old results sent.

Revision ID: 003

Databases that already applied the first version of 002 added notified_at
without backfilling it. Finished rows from before that column would otherwise
be sent again on the next tick.
"""

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("relayer_txs", sa.Column("sim_first_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE transfers SET notified_at = CURRENT_TIMESTAMP "
        "WHERE notified_at IS NULL AND status IN ('mined', 'failed')"
    )


def downgrade() -> None:
    op.drop_column("relayer_txs", "sim_first_at")
