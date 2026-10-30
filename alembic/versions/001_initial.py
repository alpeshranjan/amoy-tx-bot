"""Initial schema.

Revision ID: 001
"""

import sqlalchemy as sa
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("username", sa.String(64), unique=True),
        sa.Column("first_name", sa.String(128)),
        sa.Column("username_observed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "wallets",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("address", sa.String(42), nullable=False, unique=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "relayer_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base_nonce", sa.Integer()),
    )
    op.create_table(
        "transfers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(80), nullable=False, unique=True),
        sa.Column("sender_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("picker_request_id", sa.Integer(), unique=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("relayer_tx_id", sa.Uuid()),
        sa.Column("tx_hash", sa.String(66), unique=True),
        sa.Column("error", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('awaiting_recipient', 'pending', 'queued', 'submitted', 'mined', 'failed', 'cancelled', 'expired')",
            name="transfer_status",
        ),
    )
    op.create_index(
        "one_open_transfer_per_sender",
        "transfers",
        ["sender_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'submitted')"),
    )
    op.create_table(
        "relayer_txs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("nonce", sa.Integer(), unique=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("transfer_id", sa.Uuid(), sa.ForeignKey("transfers.id")),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("starter", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("tx_to", sa.String(42)),
        sa.Column("calldata", sa.LargeBinary()),
        sa.Column("gas_limit", sa.Integer()),
        sa.Column("permit_deadline", sa.BigInteger()),
        sa.Column("last_broadcast_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('transfer', 'mint')", name="relayer_kind"),
    )
    op.create_index(
        "one_starter_mint_per_user",
        "relayer_txs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'mint' AND starter"),
    )
    op.create_table(
        "relayer_broadcasts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("relayer_tx_id", sa.Uuid(), sa.ForeignKey("relayer_txs.id"), nullable=False),
        sa.Column("raw_tx", sa.LargeBinary(), nullable=False),
        sa.Column("tx_hash", sa.String(66), nullable=False, unique=True),
        sa.Column("max_fee_per_gas", sa.BigInteger(), nullable=False),
        sa.Column("max_priority_fee_per_gas", sa.BigInteger(), nullable=False),
        sa.Column("cancel", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("relayer_broadcasts")
    op.drop_table("relayer_txs")
    op.drop_table("transfers")
    op.drop_table("relayer_state")
    op.drop_table("wallets")
    op.drop_table("users")
