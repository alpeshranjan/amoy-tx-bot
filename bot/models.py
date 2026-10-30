import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    username_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Wallet(Base):
    __tablename__ = "wallets"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer, default=1)


class RelayerState(Base):
    __tablename__ = "relayer_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_nonce: Mapped[int | None] = mapped_column(Integer)


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_recipient', 'pending', 'queued', 'submitted', 'mined', 'failed', 'cancelled', 'expired')",
            name="transfer_status",
        ),
        Index(
            "one_open_transfer_per_sender",
            "sender_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'submitted')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    picker_request_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32))
    relayer_tx_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tx_hash: Mapped[str | None] = mapped_column(String(66), unique=True)
    error: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RelayerTx(Base):
    __tablename__ = "relayer_txs"
    __table_args__ = (
        CheckConstraint("kind IN ('transfer', 'mint')", name="relayer_kind"),
        Index(
            "one_starter_mint_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("kind = 'mint' AND starter"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16))
    nonce: Mapped[int | None] = mapped_column(Integer, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transfers.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    starter: Mapped[bool] = mapped_column(Boolean, default=False)
    sim_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sim_first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32))
    tx_to: Mapped[str | None] = mapped_column(String(42))
    calldata: Mapped[bytes | None] = mapped_column(LargeBinary)
    gas_limit: Mapped[int | None] = mapped_column(Integer)
    permit_deadline: Mapped[int | None] = mapped_column(BigInteger)
    last_broadcast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RelayerBroadcast(Base):
    __tablename__ = "relayer_broadcasts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    relayer_tx_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("relayer_txs.id"))
    raw_tx: Mapped[bytes] = mapped_column(LargeBinary)
    tx_hash: Mapped[str] = mapped_column(String(66), unique=True)
    max_fee_per_gas: Mapped[int] = mapped_column(BigInteger)
    max_priority_fee_per_gas: Mapped[int] = mapped_column(BigInteger)
    cancel: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
