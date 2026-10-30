import logging
import uuid
from datetime import datetime, timezone

from eth_account import Account
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.chain import Chain, SignedRaw, classify_rpc
from bot.config import Settings
from bot.crypto import decrypt_key
from bot.errors import FeeCeiling, LowPol, StartupError, UserReject
from bot.models import RelayerBroadcast, RelayerState, RelayerTx, Transfer, Wallet

log = logging.getLogger(__name__)
LOCK_KEY = 84218421


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class Worker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        chain: Chain,
        settings: Settings,
        clock: Clock | None = None,
        bot=None,
    ) -> None:
        self.sessions = sessions
        self.chain = chain
        self.settings = settings
        self.clock = clock or Clock()
        self.bot = bot
        self.paused = False
        self.pause_reason: str | None = None

    async def startup(self) -> None:
        await self.chain.assert_boot()
        await self._reconcile(startup=True)

    async def tick(self, *, broadcast: bool = True) -> None:
        if self.paused:
            return
        await self._reconcile(startup=False)
        if self.paused:
            return
        if broadcast:
            await self._maintain()
        if self.paused or await self._has_submitted():
            return
        await self.sign_next(broadcast=broadcast)

    async def sign_next(self, *, broadcast: bool = True) -> None:
        raw: bytes | None = None
        tx_id: uuid.UUID | None = None
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
                if await self._submitted_in(session):
                    return
                row = await session.scalar(
                    select(RelayerTx)
                    .where(RelayerTx.status == "queued", RelayerTx.nonce.is_(None))
                    .order_by(RelayerTx.created_at, RelayerTx.id)
                    .limit(1)
                )
                if row is None:
                    return
                try:
                    nonce = await self._next_nonce(session)
                except StartupError:
                    log.error("base nonce is not set; refusing to sign")
                    return
                try:
                    signed = await self._prepare(session, row, nonce)
                except UserReject as exc:
                    row.status = "failed"
                    await self._fail_transfer(session, row, str(exc))
                    return
                except LowPol as exc:
                    await self._pause(str(exc))
                    return
                except FeeCeiling as exc:
                    await self._pause(str(exc))
                    return
                self._store_signed(row, signed, cancel=False)
                session.add(row)
                if row.transfer_id is not None:
                    transfer = await session.get(Transfer, row.transfer_id)
                    if transfer is not None:
                        transfer.status = "submitted"
                        transfer.tx_hash = signed.tx_hash
                        transfer.relayer_tx_id = row.id
                raw = signed.raw
                tx_id = row.id
        if broadcast and raw is not None and tx_id is not None and not self.paused:
            await self._send(tx_id, raw)

    async def _prepare(self, session: AsyncSession, row: RelayerTx, nonce: int) -> SignedRaw:
        if await self.chain.pol_balance() < self.settings.relayer_pol_threshold:
            raise LowPol("relayer POL is below the threshold")
        if row.kind == "mint":
            wallet = await session.get(Wallet, row.user_id)
            if wallet is None:
                raise UserReject("User has no wallet.")
            calldata = self.chain.encode_mint(wallet.address, row.amount)
            to = self.chain.usdc.address
            deadline = None
        elif row.kind == "transfer":
            calldata, to, deadline = await self._transfer_calldata(session, row)
        else:
            raise UserReject("Unknown relayer kind.")
        try:
            gas_limit = await self.chain.estimate_gas(to, calldata)
            await self.chain.eth_call(to, calldata, gas_limit)
        except Exception as exc:
            raise UserReject(str(exc)) from exc
        max_fee, priority = await self.chain.initial_fees()
        return self.chain.sign_transaction(
            nonce=nonce,
            to=to,
            calldata=calldata,
            gas_limit=gas_limit,
            max_fee=max_fee,
            priority=priority,
            permit_deadline=deadline,
        )

    async def _transfer_calldata(self, session: AsyncSession, row: RelayerTx) -> tuple[bytes, str, int]:
        transfer = await session.get(Transfer, row.transfer_id)
        if transfer is None or transfer.recipient_id is None:
            raise UserReject("Transfer is missing a recipient.")
        cap = await self.chain.max_transfer()
        if transfer.amount > cap:
            raise UserReject("Amount is above the on-chain transfer cap.")
        if await self._daily_total(session, transfer.sender_id) + transfer.amount > self.settings.daily_send_cap:
            raise UserReject("That would exceed today's send cap.")
        sender = await session.get(Wallet, transfer.sender_id)
        recipient = await session.get(Wallet, transfer.recipient_id)
        if sender is None or recipient is None:
            raise UserReject("Sender or recipient has no wallet.")
        if await self.chain.usdc_balance(sender.address) < transfer.amount:
            raise UserReject("Not enough USDC.")
        private_key = decrypt_key(
            self.settings.key_encryption_key, transfer.sender_id, sender.ciphertext, sender.nonce
        )
        try:
            owner = Account.from_key(private_key).address
            permit_nonce = await self.chain.permit_nonce(owner)
            deadline = await self.chain.block_timestamp() + self.settings.permit_ttl_seconds
            v, r, s = self.chain.sign_permit(private_key, owner, transfer.amount, permit_nonce, deadline)
        finally:
            del private_key
        calldata = self.chain.encode_transfer(owner, recipient.address, transfer.amount, deadline, v, r, s)
        return calldata, self.chain.router.address, deadline

    async def _daily_total(self, session: AsyncSession, sender_id: uuid.UUID) -> int:
        start = self.clock.now().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        total = await session.scalar(
            select(func.coalesce(func.sum(Transfer.amount), 0)).where(
                Transfer.sender_id == sender_id,
                Transfer.status.in_(("submitted", "mined")),
                Transfer.queued_at >= start,
            )
        )
        return int(total or 0)

    def _store_signed(self, row: RelayerTx, signed: SignedRaw, *, cancel: bool) -> RelayerBroadcast:
        row.nonce = signed.nonce
        row.status = "submitted"
        if not cancel:
            row.tx_to = signed.to
            row.calldata = signed.calldata
            row.gas_limit = signed.gas_limit
            row.permit_deadline = signed.permit_deadline
        broadcast = RelayerBroadcast(
            relayer_tx_id=row.id,
            raw_tx=signed.raw,
            tx_hash=signed.tx_hash,
            max_fee_per_gas=signed.max_fee_per_gas,
            max_priority_fee_per_gas=signed.max_priority_fee_per_gas,
            cancel=cancel,
        )
        from sqlalchemy.orm import object_session

        object_session(row).add(broadcast)
        return broadcast

    async def _maintain(self) -> None:
        async with self.sessions() as session:
            rows = (
                await session.scalars(select(RelayerTx).where(RelayerTx.status == "submitted").order_by(RelayerTx.nonce))
            ).all()
        for row in rows:
            if self.paused:
                return
            await self._maintain_one(row.id)

    async def _maintain_one(self, tx_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            row = await session.get(RelayerTx, tx_id)
            if row is None or row.status != "submitted":
                return
            latest = await self._latest_broadcast(session, row.id)
            if latest is None:
                return
            chain_time = await self.chain.block_timestamp()
            now = self.clock.now()
            deadline_passed = (
                row.kind == "transfer"
                and row.permit_deadline is not None
                and chain_time > row.permit_deadline
            )
            action = "rebroadcast"
            if deadline_passed and not latest.cancel:
                action = "cancel"
            elif (now - latest.created_at).total_seconds() >= self.settings.bump_after_seconds:
                action = "bump"
            elif row.last_broadcast_at is not None and (
                (now - row.last_broadcast_at).total_seconds() < self.settings.rebroadcast_seconds
            ):
                return
            if action == "rebroadcast":
                raw = latest.raw_tx
                await session.commit()
                await self._send(tx_id, raw)
                return
            cancel = action == "cancel" or latest.cancel
            try:
                signed = self._sign_cancel(row, latest) if cancel else self._sign_bump(row, latest)
            except FeeCeiling as exc:
                await self._pause(str(exc))
                return
            self._store_signed(row, signed, cancel=cancel)
            if row.transfer_id is not None:
                transfer = await session.get(Transfer, row.transfer_id)
                if transfer is not None:
                    transfer.tx_hash = signed.tx_hash
            raw = signed.raw
            await session.commit()
        await self._send(tx_id, raw)

    def _sign_bump(self, row: RelayerTx, latest: RelayerBroadcast) -> SignedRaw:
        max_fee, priority = self.chain.bump_fees(latest.max_fee_per_gas, latest.max_priority_fee_per_gas)
        return self.chain.sign_transaction(
            nonce=row.nonce,
            to=row.tx_to,
            calldata=row.calldata if not latest.cancel else b"",
            gas_limit=21000 if latest.cancel else row.gas_limit,
            max_fee=max_fee,
            priority=priority,
            permit_deadline=row.permit_deadline,
        )

    def _sign_cancel(self, row: RelayerTx, latest: RelayerBroadcast) -> SignedRaw:
        max_fee, priority = self.chain.bump_fees(latest.max_fee_per_gas, latest.max_priority_fee_per_gas)
        return self.chain.sign_transaction(
            nonce=row.nonce,
            to=self.chain.relayer.address,
            calldata=b"",
            gas_limit=21000,
            max_fee=max_fee,
            priority=priority,
            permit_deadline=row.permit_deadline,
        )

    async def _send(self, tx_id: uuid.UUID, raw: bytes) -> None:
        try:
            await self.chain.send_raw(raw)
        except Exception as exc:
            await self._on_send_error(tx_id, exc)
            return
        await self._stamp(tx_id)

    async def _on_send_error(self, tx_id: uuid.UUID, exc: Exception) -> None:
        kind = classify_rpc(exc)
        log.info("broadcast %s for %s", kind, tx_id)
        if kind == "already_known":
            await self._stamp(tx_id)
            return
        if kind == "nonce_too_low":
            await self._reconcile(startup=False)
            return
        if kind == "underpriced":
            await self._force_bump(tx_id)
            return
        if kind == "insufficient_funds":
            await self._pause("insufficient funds")
            return
        log.error("broadcast failed for %s: %s", tx_id, type(exc).__name__)

    async def _force_bump(self, tx_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            row = await session.get(RelayerTx, tx_id)
            if row is None or row.status != "submitted":
                return
            latest = await self._latest_broadcast(session, row.id)
            if latest is None:
                return
            try:
                signed = self._sign_cancel(row, latest) if latest.cancel else self._sign_bump(row, latest)
            except FeeCeiling as exc:
                await self._pause(str(exc))
                return
            self._store_signed(row, signed, cancel=latest.cancel)
            if row.transfer_id is not None:
                transfer = await session.get(Transfer, row.transfer_id)
                if transfer is not None:
                    transfer.tx_hash = signed.tx_hash
            raw = signed.raw
            await session.commit()
        await self._send(tx_id, raw)

    async def _stamp(self, tx_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            row = await session.get(RelayerTx, tx_id)
            if row is not None:
                row.last_broadcast_at = self.clock.now()
            await session.commit()

    async def _reconcile(self, *, startup: bool) -> None:
        chain_nonce = await self.chain.transaction_count()
        async with self.sessions() as session:
            state = await session.get(RelayerState, 1)
            rows = (
                await session.scalars(select(RelayerTx).where(RelayerTx.nonce.is_not(None)).order_by(RelayerTx.nonce))
            ).all()
            committed = {row.nonce: row for row in rows if row.nonce is not None}
            if committed:
                low = min(committed)
                if chain_nonce > low:
                    for nonce in range(low, chain_nonce):
                        row = committed.get(nonce)
                        if row is None:
                            raise StartupError(f"Nonce {nonce} was used outside the relayer table.")
                        if row.status == "submitted":
                            ok = await self._resolve_row(session, row)
                            if not ok:
                                if startup:
                                    raise StartupError(f"Nonce {nonce} is below the chain count and has no receipt.")
                                await self._pause(f"Nonce {nonce} has no receipt.")
                                return
            elif state is not None and state.base_nonce is not None and chain_nonce > state.base_nonce:
                raise StartupError("Relayer nonce moved before any recorded transaction.")
            await session.commit()

    async def _resolve_row(self, session: AsyncSession, row: RelayerTx) -> bool:
        broadcasts = (
            await session.scalars(select(RelayerBroadcast).where(RelayerBroadcast.relayer_tx_id == row.id))
        ).all()
        found: tuple[RelayerBroadcast, object] | None = None
        for broadcast in broadcasts:
            receipt = await self.chain.receipt(broadcast.tx_hash)
            if receipt is not None:
                found = (broadcast, receipt)
                break
        if found is None:
            return False
        broadcast, receipt = found
        success = int(receipt["status"]) == 1 and not broadcast.cancel
        row.status = "mined" if success else ("cancelled" if broadcast.cancel else "reverted")
        if row.transfer_id is not None:
            transfer = await session.get(Transfer, row.transfer_id)
            if transfer is not None:
                transfer.tx_hash = broadcast.tx_hash
                if success:
                    transfer.status = "mined"
                    transfer.error = None
                else:
                    transfer.status = "failed"
                    transfer.error = "cancelled" if broadcast.cancel else "reverted"
        return True

    async def _next_nonce(self, session: AsyncSession) -> int:
        current = await session.scalar(select(func.max(RelayerTx.nonce)))
        if current is not None:
            return int(current) + 1
        base = await session.scalar(select(RelayerState.base_nonce).where(RelayerState.id == 1))
        if base is None:
            raise StartupError("base nonce is not set")
        return int(base)

    async def _submitted_in(self, session: AsyncSession) -> bool:
        found = await session.scalar(select(RelayerTx.id).where(RelayerTx.status == "submitted").limit(1))
        return found is not None

    async def _has_submitted(self) -> bool:
        async with self.sessions() as session:
            return await self._submitted_in(session)

    async def _latest_broadcast(self, session: AsyncSession, tx_id: uuid.UUID) -> RelayerBroadcast | None:
        return await session.scalar(
            select(RelayerBroadcast)
            .where(RelayerBroadcast.relayer_tx_id == tx_id)
            .order_by(RelayerBroadcast.created_at.desc(), RelayerBroadcast.id.desc())
            .limit(1)
        )

    async def _fail_transfer(self, session: AsyncSession, row: RelayerTx, reason: str) -> None:
        if row.transfer_id is None:
            return
        transfer = await session.get(Transfer, row.transfer_id)
        if transfer is not None and transfer.status in {"queued", "submitted"}:
            transfer.status = "failed"
            transfer.error = reason[:500]

    async def _pause(self, reason: str) -> None:
        self.paused = True
        self.pause_reason = reason
        log.error("relayer paused: %s", reason)
        if self.bot is not None and self.settings.admin_telegram_id is not None:
            await self.bot.send_message(self.settings.admin_telegram_id, f"Relayer paused: {reason}")
