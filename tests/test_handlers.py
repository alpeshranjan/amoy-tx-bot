import os
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message, SharedUser, Update, User, UsersShared
from sqlalchemy import select

from bot.config import Settings
from bot.handlers import App, build_router
from bot.identity import touch_user
from bot.limits import ParseLimiter
from bot.models import Transfer
from bot.worker import Worker

KEY = "11" * 32


class Capture(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method, timeout: int | None = None) -> TelegramType:
        if not isinstance(method, SendMessage):
            raise AssertionError(type(method))
        self.texts.append(method.text)
        return Message(
            message_id=len(self.texts),
            date=datetime.now(timezone.utc),
            chat=Chat(id=int(method.chat_id), type="private"),
            text=method.text,
        )

    async def stream_content(self, url: str, headers=None, timeout: int = 30, chunk_size: int = 65536, raise_for_status: bool = True):
        raise AssertionError(url)


class Inbox:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


class Count:
    def __init__(self, value: int) -> None:
        self.value = value

    async def transaction_count(self) -> int:
        return self.value


def _settings() -> Settings:
    return Settings(
        key_encryption_key=KEY,
        database_url=os.environ["DATABASE_URL"],
        rpc_url="http://127.0.0.1:9",
        chain_id=31337,
        relayer_private_key="0x" + "22" * 32,
        router_address="0x" + "33" * 20,
        usdc_address="0x" + "44" * 20,
    )


async def _awaiting(sessions, telegram_id: int, request_id: int):
    async with sessions() as session:
        async with session.begin():
            user = await touch_user(session, telegram_id=telegram_id, username="ada", first_name="Ada")
            transfer = Transfer(
                idempotency_key=f"tg:pick-{request_id}",
                sender_id=user.id,
                picker_request_id=request_id,
                amount=1_000_000,
                status="awaiting_recipient",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            session.add(transfer)
            await session.flush()
            return transfer.id


async def _pick(sessions, settings: Settings, telegram_id: int, request_id: int, chosen: int) -> list[str]:
    capture = Capture()
    bot = Bot(token="1:test", session=capture)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(App(sessions, None, settings, ParseLimiter(20))))
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=User(id=telegram_id, is_bot=False, first_name="Ada"),
        users_shared=UsersShared(request_id=request_id, users=[SharedUser(user_id=chosen)]),
    )
    try:
        await dispatcher.feed_update(bot, Update(update_id=request_id, message=message))
    finally:
        await bot.session.close()
    return capture.texts


@pytest.mark.asyncio
async def test_picking_yourself_does_not_get_a_second_message(sessions):
    settings = _settings()
    transfer_id = await _awaiting(sessions, 11, 7)
    texts = await _pick(sessions, settings, 11, 7, 11)
    assert texts == ["You can't pay yourself."]
    async with sessions() as session:
        transfer = await session.get(Transfer, transfer_id)
        assert transfer.status == "failed"
        assert transfer.error == "self-transfer"
        assert transfer.notified_at is not None
    inbox = Inbox()
    await Worker(sessions, Count(0), settings, bot=inbox).tick()
    assert inbox.messages == []


@pytest.mark.asyncio
async def test_picking_someone_who_has_not_started_does_not_get_a_second_message(sessions):
    settings = _settings()
    transfer_id = await _awaiting(sessions, 11, 8)
    texts = await _pick(sessions, settings, 11, 8, 99)
    assert texts == ["They need to open this bot and send /start."]
    async with sessions() as session:
        transfer = await session.scalar(select(Transfer).where(Transfer.id == transfer_id))
        assert transfer.status == "failed"
        assert transfer.error == "recipient has not started"
        assert transfer.notified_at is not None
    inbox = Inbox()
    await Worker(sessions, Count(0), settings, bot=inbox).tick()
    assert inbox.messages == []
