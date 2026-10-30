import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.amounts import format_amount
from bot.chain import Chain
from bot.config import Settings
from bot.identity import create_wallet, touch_user
from bot.limits import ParseLimiter
from bot.models import RelayerTx, Transfer, User, Wallet
from bot.parser import parse_message
from bot.policy import EntityRef, PolicyError, decide_send, precheck_text
from bot.present import confirm_text, paused_text, tx_link

log = logging.getLogger(__name__)


class App:
    def __init__(self, sessions, chain: Chain, settings: Settings, limiter: ParseLimiter, worker=None) -> None:
        self.sessions = sessions
        self.chain = chain
        self.settings = settings
        self.limiter = limiter
        self.worker = worker


def build_router(app: App) -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    @router.message.middleware()
    @router.callback_query.middleware()
    async def remember_user(handler, event, data):
        person = event.from_user
        async with app.sessions() as session:
            async with session.begin():
                user = await touch_user(
                    session,
                    telegram_id=person.id,
                    username=person.username,
                    first_name=person.first_name,
                )
                data["db_user_id"] = user.id
        return await handler(event, data)

    @router.message(Command("start"))
    async def start(message: Message, db_user_id: uuid.UUID):
        async with app.sessions() as session:
            async with session.begin():
                user = await session.get(User, db_user_id)
                wallet = await session.get(Wallet, db_user_id)
                created = False
                if wallet is None:
                    wallet = create_wallet(session, db_user_id, app.settings.key_encryption_key)
                    created = True
                    await session.flush()
                minted = await _ensure_starter(session, user.id, True, app.settings.starter_mint)
                address = wallet.address
                username = user.username
        lines = [
            "Your testnet wallet is ready." if created else "This wallet is already registered.",
            address,
            "Send with: send 5 USDC to @name",
        ]
        if username is None:
            lines.append(
                "You have no Telegram username, so @mentions cannot pay you. "
                "Someone can still pay you with the contact button."
            )
        if minted:
            lines.append(f"A starter {format_amount(app.settings.starter_mint)} USDC mint is queued.")
        await message.answer("\n".join(lines))

    @router.message(Command("balance"))
    async def balance(message: Message, db_user_id: uuid.UUID):
        async with app.sessions() as session:
            wallet = await session.get(Wallet, db_user_id)
        if wallet is None:
            await message.answer("Send /start first.")
            return
        amount = await app.chain.usdc_balance(wallet.address)
        await message.answer(f"Balance: {format_amount(amount)} USDC")

    @router.message(Command("history"))
    async def history(message: Message, db_user_id: uuid.UUID):
        async with app.sessions() as session:
            rows = (
                await session.scalars(
                    select(Transfer)
                    .where((Transfer.sender_id == db_user_id) | (Transfer.recipient_id == db_user_id))
                    .order_by(Transfer.created_at.desc())
                    .limit(10)
                )
            ).all()
            names = {}
            for row in rows:
                for person_id in (row.sender_id, row.recipient_id):
                    if person_id and person_id not in names:
                        person = await session.get(User, person_id)
                        if person:
                            names[person_id] = f"@{person.username}" if person.username else (person.first_name or "someone")
        if not rows:
            await message.answer("No transfers yet.")
            return
        lines = []
        for row in rows:
            link = tx_link(app.settings.chain_id, row.tx_hash) if row.tx_hash else row.status
            direction = "out" if row.sender_id == db_user_id else "in"
            other = row.recipient_id if direction == "out" else row.sender_id
            lines.append(f"{direction} {format_amount(row.amount)} USDC {names.get(other, '')} — {link}")
        await message.answer("\n".join(lines))

    @router.message(Command("faucet"))
    async def faucet(message: Message, db_user_id: uuid.UUID):
        async with app.sessions() as session:
            async with session.begin():
                wallet = await session.get(Wallet, db_user_id)
                if wallet is None:
                    await message.answer("Send /start first.")
                    return
                if await _mint_in_flight(session, db_user_id):
                    await message.answer("A mint is already queued.")
                    return
                if await _minted_today(session, db_user_id) + app.settings.faucet_mint > app.settings.faucet_daily_cap:
                    await message.answer("Today's faucet cap is used.")
                    return
                session.add(
                    RelayerTx(
                        kind="mint",
                        user_id=db_user_id,
                        amount=app.settings.faucet_mint,
                        starter=False,
                        status="queued",
                    )
                )
        await message.answer(f"Queued {format_amount(app.settings.faucet_mint)} USDC.")

    @router.message(Command("help"))
    async def help_command(message: Message):
        await message.answer(
            "This bot sends test USDC on Polygon Amoy.\n"
            "send 5 USDC to @name\n"
            "/balance  /history  /faucet\n"
            "Nothing is sent until you tap Confirm."
        )

    @router.message(F.users_shared)
    async def picked(message: Message, db_user_id: uuid.UUID):
        shared = message.users_shared
        chosen = shared.users[0].user_id if shared and shared.users else None
        if chosen is None:
            return
        async with app.sessions() as session:
            async with session.begin():
                transfer = await session.scalar(
                    select(Transfer).where(Transfer.picker_request_id == shared.request_id).with_for_update()
                )
                if transfer is None or transfer.sender_id != db_user_id:
                    return
                if transfer.status != "awaiting_recipient":
                    return
                if transfer.expires_at <= _now():
                    transfer.status = "expired"
                    await message.answer("That request expired. Send it again.", reply_markup=ReplyKeyboardRemove())
                    return
                if chosen == message.from_user.id:
                    transfer.status = "failed"
                    transfer.error = "self-transfer"
                    transfer.notified_at = _now()
                    await message.answer("You can't pay yourself.", reply_markup=ReplyKeyboardRemove())
                    return
                recipient = await session.scalar(select(User).where(User.telegram_id == chosen))
                recipient_wallet = await session.get(Wallet, recipient.id) if recipient else None
                if recipient is None or recipient_wallet is None:
                    transfer.status = "failed"
                    transfer.error = "recipient has not started"
                    transfer.notified_at = _now()
                    await message.answer("They need to open this bot and send /start.", reply_markup=ReplyKeyboardRemove())
                    return
                transfer.recipient_id = recipient.id
                transfer.status = "pending"
                transfer.expires_at = _now() + timedelta(seconds=app.settings.confirm_ttl_seconds)
                text = confirm_text(transfer.amount, recipient, recipient_wallet)
                markup = _confirm_markup(transfer.id)
        await message.answer(text, reply_markup=ReplyKeyboardRemove())
        await message.answer("Confirm to send.", reply_markup=markup)

    @router.message(F.text & ~F.text.startswith("/"))
    async def free_text(message: Message, db_user_id: uuid.UUID):
        text = message.text or ""
        if not app.limiter.allow(message.from_user.id, time.monotonic()):
            await message.answer("Too many messages. Wait a minute.")
            return
        vague = precheck_text(text)
        if vague:
            await message.answer(vague)
            return
        try:
            intent = await parse_message(text, app.settings)
        except Exception:
            log.exception("parser failed")
            await message.answer("I couldn't read that. Try: send 5 USDC to @name")
            return
        if intent.action == "balance":
            await balance(message, db_user_id)
            return
        if intent.action == "history":
            await history(message, db_user_id)
            return
        if intent.action != "send":
            await message.answer(intent.reject_reason or "I can send USDC, or show your balance and history.")
            return
        try:
            decision = decide_send(text, intent, _entities(message))
        except PolicyError as exc:
            await message.answer(str(exc))
            return
        async with app.sessions() as session:
            async with session.begin():
                sender = await session.get(User, db_user_id)
                wallet = await session.get(Wallet, db_user_id)
                if wallet is None:
                    await message.answer("Send /start first.")
                    return
                existing = await session.scalar(select(Transfer).where(Transfer.idempotency_key == _key(message)))
                if existing is not None:
                    await message.answer(f"Already recorded ({existing.status}).")
                    return
                if await _hourly_full(session, db_user_id, app.settings.hourly_transfer_limit):
                    await message.answer("Hourly transfer limit reached.")
                    return
                if decision.mode == "picker":
                    request_id = await _request_id(session)
                    transfer = Transfer(
                        idempotency_key=_key(message),
                        sender_id=db_user_id,
                        recipient_id=None,
                        picker_request_id=request_id,
                        amount=decision.amount,
                        status="awaiting_recipient",
                        expires_at=_now() + timedelta(seconds=app.settings.confirm_ttl_seconds),
                    )
                    session.add(transfer)
                    await session.flush()
                    markup = ReplyKeyboardMarkup(
                        keyboard=[
                            [
                                KeyboardButton(
                                    text="Choose recipient",
                                    request_users=KeyboardButtonRequestUsers(
                                        request_id=request_id,
                                        user_is_bot=False,
                                        max_quantity=1,
                                    ),
                                )
                            ]
                        ],
                        resize_keyboard=True,
                        one_time_keyboard=True,
                    )
                    await message.answer(
                        f"Who should receive {format_amount(decision.amount)} USDC?",
                        reply_markup=markup,
                    )
                    return
                recipient = await _find_recipient(session, decision.telegram_id, decision.username)
                recipient_wallet = await session.get(Wallet, recipient.id) if recipient else None
                if recipient is None or recipient_wallet is None:
                    await message.answer("They need to open this bot and send /start.")
                    return
                if recipient.id == sender.id:
                    await message.answer("You can't pay yourself.")
                    return
                transfer = Transfer(
                    idempotency_key=_key(message),
                    sender_id=sender.id,
                    recipient_id=recipient.id,
                    amount=decision.amount,
                    status="pending",
                    expires_at=_now() + timedelta(seconds=app.settings.confirm_ttl_seconds),
                )
                session.add(transfer)
                await session.flush()
                card = confirm_text(decision.amount, recipient, recipient_wallet)
                markup = _confirm_markup(transfer.id)
        await message.answer(card, reply_markup=markup)

    @router.callback_query(F.data.startswith("c:"))
    async def confirm(query: CallbackQuery, db_user_id: uuid.UUID):
        transfer_id = _callback_id(query.data)
        if transfer_id is None:
            await query.answer("Unknown confirmation.")
            return
        async with app.sessions() as session:
            try:
                async with session.begin():
                    transfer = await session.get(Transfer, transfer_id, with_for_update=True)
                    if transfer is None:
                        await query.answer("Unknown confirmation.")
                        return
                    if transfer.sender_id != db_user_id:
                        await query.answer("That confirmation is not yours.")
                        return
                    if transfer.status in {"submitted", "mined"} and transfer.tx_hash:
                        await query.message.answer(tx_link(app.settings.chain_id, transfer.tx_hash))
                        await query.answer()
                        return
                    if transfer.status != "pending":
                        await query.answer(f"This transfer is {transfer.status}.")
                        return
                    if transfer.expires_at <= _now():
                        transfer.status = "expired"
                        await query.answer("Expired. Send it again.")
                        return
                    if app.worker is not None and app.worker.paused:
                        await query.answer("Relayer is paused.")
                        if query.message is not None:
                            await query.message.answer(paused_text())
                        return
                    if await _hourly_full(session, db_user_id, app.settings.hourly_transfer_limit):
                        await query.answer("Hourly transfer limit reached.")
                        return
                    open_row = await session.scalar(
                        select(Transfer.id).where(
                            Transfer.sender_id == db_user_id,
                            Transfer.status.in_(("queued", "submitted")),
                        )
                    )
                    if open_row is not None:
                        await query.answer("A transfer is already in flight.")
                        return
                    transfer.status = "queued"
                    transfer.queued_at = _now()
                    row = RelayerTx(
                        kind="transfer",
                        user_id=transfer.sender_id,
                        transfer_id=transfer.id,
                        amount=transfer.amount,
                        starter=False,
                        status="queued",
                    )
                    session.add(row)
                    await session.flush()
                    transfer.relayer_tx_id = row.id
            except IntegrityError:
                await query.answer("A transfer is already in flight.")
                return
        await query.answer("Queued.")
        await query.message.answer("Queued. I'll reply with the transaction once it is mined.")

    @router.callback_query(F.data.startswith("x:"))
    async def cancel(query: CallbackQuery, db_user_id: uuid.UUID):
        transfer_id = _callback_id(query.data)
        async with app.sessions() as session:
            async with session.begin():
                transfer = await session.get(Transfer, transfer_id, with_for_update=True) if transfer_id else None
                if transfer is None or transfer.sender_id != db_user_id:
                    await query.answer("That confirmation is not yours.")
                    return
                if transfer.status not in {"pending", "awaiting_recipient"}:
                    await query.answer(f"This transfer is {transfer.status}.")
                    return
                transfer.status = "cancelled"
        await query.answer("Cancelled.")
        await query.message.answer("Cancelled. Nothing was sent.")

    return router


def build_public_router() -> Router:
    router = Router()

    @router.message(F.chat.type != ChatType.PRIVATE)
    async def groups(message: Message):
        await message.answer("Transfers work in a private chat. Open this bot and message me there.")

    return router


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(message: Message) -> str:
    return f"tg:{message.chat.id}:{message.message_id}"


def _callback_id(data: str | None) -> uuid.UUID | None:
    if not data or len(data) < 3:
        return None
    try:
        return uuid.UUID(data[2:])
    except ValueError:
        return None


def _confirm_markup(transfer_id: uuid.UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Confirm", callback_data=f"c:{transfer_id}"),
                InlineKeyboardButton(text="Cancel", callback_data=f"x:{transfer_id}"),
            ]
        ]
    )


def _entities(message: Message) -> list[EntityRef]:
    refs: list[EntityRef] = []
    text = message.text or ""
    for entity in message.entities or []:
        kind = getattr(entity.type, "value", entity.type)
        span = text[entity.offset : entity.offset + entity.length]
        if kind == "mention":
            refs.append(EntityRef("mention", span, username=span.lstrip("@").lower()))
        elif kind == "text_mention" and entity.user is not None:
            refs.append(
                EntityRef(
                    "text_mention",
                    span,
                    telegram_id=entity.user.id,
                    first_name=entity.user.first_name,
                )
            )
    return refs


async def _find_recipient(session: AsyncSession, telegram_id: int | None, username: str | None) -> User | None:
    if telegram_id is not None:
        return await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if username:
        return await session.scalar(select(User).where(User.username == username.lower()))
    return None


async def _hourly_full(session: AsyncSession, sender_id: uuid.UUID, limit: int) -> bool:
    since = _now() - timedelta(hours=1)
    count = await session.scalar(
        select(func.count())
        .select_from(Transfer)
        .where(Transfer.sender_id == sender_id, Transfer.queued_at >= since)
    )
    return int(count or 0) >= limit


async def _request_id(session: AsyncSession) -> int:
    for _ in range(8):
        request_id = secrets.randbelow(2**31 - 1) + 1
        taken = await session.scalar(select(Transfer.id).where(Transfer.picker_request_id == request_id))
        if taken is None:
            return request_id
    raise RuntimeError("Could not allocate a picker request id.")


async def _ensure_starter(session: AsyncSession, user_id: uuid.UUID, has_wallet: bool, amount: int) -> bool:
    if not has_wallet:
        return False
    existing = await session.scalar(
        select(RelayerTx.id).where(RelayerTx.user_id == user_id, RelayerTx.starter.is_(True))
    )
    if existing is not None:
        return False
    session.add(
        RelayerTx(
            kind="mint",
            user_id=user_id,
            amount=amount,
            starter=True,
            status="queued",
        )
    )
    return True


async def _mint_in_flight(session: AsyncSession, user_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(RelayerTx.id).where(
            RelayerTx.user_id == user_id,
            RelayerTx.kind == "mint",
            RelayerTx.status.in_(("queued", "submitted")),
        )
    )
    return found is not None


async def _minted_today(session: AsyncSession, user_id: uuid.UUID) -> int:
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.coalesce(func.sum(RelayerTx.amount), 0)).where(
            RelayerTx.user_id == user_id,
            RelayerTx.kind == "mint",
            RelayerTx.starter.is_(False),
            RelayerTx.status.in_(("queued", "submitted", "mined")),
            RelayerTx.created_at >= start,
        )
    )
    return int(total or 0)
