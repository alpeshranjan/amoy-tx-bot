import uuid
from datetime import datetime, timezone

from eth_account import Account
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crypto import encrypt_key
from bot.models import User, Wallet


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def touch_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> User:
    """Upsert a Telegram user. A username is cleared from any other row first."""
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, first_name=first_name)
        session.add(user)
        await session.flush()
    user.first_name = first_name
    normalized = username.lower() if username else None
    if normalized:
        await session.execute(
            update(User)
            .where(User.username == normalized, User.id != user.id)
            .values(username=None)
        )
        user.username = normalized
        user.username_observed_at = _now()
    else:
        user.username = None
    return user


def create_wallet(session: AsyncSession, user_id: uuid.UUID, key_hex: str) -> Wallet:
    account = Account.create()
    ciphertext, nonce = encrypt_key(key_hex, user_id, account.key)
    wallet = Wallet(
        user_id=user_id,
        address=account.address,
        ciphertext=ciphertext,
        nonce=nonce,
        key_version=1,
    )
    session.add(wallet)
    return wallet
