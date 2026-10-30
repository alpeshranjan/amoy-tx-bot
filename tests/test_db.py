import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from bot.cli import init_base_nonce
from bot.identity import create_wallet, touch_user
from bot.models import RelayerState, RelayerTx, Transfer, User


def _transfer(sender: uuid.UUID, status: str = "queued") -> Transfer:
    now = datetime.now(timezone.utc)
    return Transfer(
        idempotency_key=f"tg:{uuid.uuid4()}",
        sender_id=sender,
        amount=1,
        status=status,
        expires_at=now + timedelta(minutes=10),
        queued_at=now,
    )


@pytest.mark.asyncio
async def test_username_is_cleared_from_the_previous_owner(sessions):
    async with sessions() as session:
        async with session.begin():
            first = await touch_user(session, telegram_id=1, username="maya", first_name="Maya")
            second = await touch_user(session, telegram_id=2, username="maya", first_name="Other")
            await session.refresh(first)
            assert first.username is None
            assert second.username == "maya"
            assert second.username_observed_at is not None


@pytest.mark.asyncio
async def test_one_open_transfer_per_sender(sessions):
    async with sessions() as session:
        async with session.begin():
            user = User(telegram_id=7, username="ada")
            session.add(user)
            await session.flush()
            session.add(_transfer(user.id))
            await session.flush()
            session.add(_transfer(user.id))
            with pytest.raises(IntegrityError):
                await session.flush()


@pytest.mark.asyncio
async def test_one_starter_mint_and_one_wallet(sessions):
    async with sessions() as session:
        async with session.begin():
            user = User(telegram_id=8, username="ada2")
            session.add(user)
            await session.flush()
            create_wallet(session, user.id, "11" * 32)
            session.add(RelayerTx(kind="mint", user_id=user.id, amount=1, starter=True, status="queued"))
            await session.flush()
            session.add(RelayerTx(kind="mint", user_id=user.id, amount=1, starter=True, status="queued"))
            with pytest.raises(IntegrityError):
                await session.flush()


@pytest.mark.asyncio
async def test_base_nonce_is_one_time(sessions):
    await init_base_nonce(4)
    with pytest.raises(SystemExit):
        await init_base_nonce(5)
    async with sessions() as session:
        state = await session.get(RelayerState, 1)
        assert state.base_nonce == 4
