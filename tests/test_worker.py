import json
import os
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from eth_account import Account
from sqlalchemy import select
from web3 import Web3
from web3.exceptions import ContractLogicError

from bot.chain import Chain
from bot.config import Settings
from bot.crypto import encrypt_key
from bot.errors import StartupError
from bot.identity import create_wallet, touch_user
from bot.models import RelayerBroadcast, RelayerState, RelayerTx, Transfer, User, Wallet
from bot.present import mined_text
from bot.worker import Worker

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ANVIL = os.path.expanduser("~/.foundry/bin/anvil")
PORT = 18562
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
KEY = "11" * 32


class Inbox:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


class BoomBot:
    async def send_message(self, chat_id: int, text: str) -> None:
        raise RuntimeError("Forbidden: bot can't initiate conversation with a user")


class Count:
    def __init__(self, value: int) -> None:
        self.value = value

    async def transaction_count(self) -> int:
        return self.value


def _settings(**overrides) -> Settings:
    values = {
        "key_encryption_key": KEY,
        "database_url": os.environ["DATABASE_URL"],
        "rpc_url": f"http://127.0.0.1:{PORT}",
        "chain_id": 31337,
        "relayer_private_key": "0x" + "22" * 32,
        "router_address": "0x" + "33" * 20,
        "usdc_address": "0x" + "44" * 20,
        "relayer_pol_threshold": 10**15,
        "permit_ttl_seconds": 30,
        "bump_after_seconds": 120,
        "rebroadcast_seconds": 15,
    }
    values.update(overrides)
    return Settings(**values)


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _wait_port(port: int) -> None:
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("anvil did not start")


def _artifact(name: str) -> tuple[list, str]:
    path = os.path.join(ROOT, "contracts", "out", name)
    data = json.loads(open(path).read())
    return data["abi"], data["bytecode"]["object"]


def _send(w3: Web3, tx: dict, key: str) -> str:
    signed = Account.sign_transaction(tx, key)
    w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(signed.hash)
    assert receipt.status == 1
    return receipt.contractAddress


def _base(w3: Web3, account: str, nonce: int) -> dict:
    return {
        "from": account,
        "nonce": nonce,
        "chainId": 31337,
        "gas": 4_000_000,
        "maxFeePerGas": 2_000_000_000,
        "maxPriorityFeePerGas": 1_000_000_000,
        "type": 2,
    }


@pytest.fixture
def anvil():
    if not os.path.exists(ANVIL):
        pytest.fail(f"Anvil is required at {ANVIL}. Install Foundry and run foundryup.")
    proc = subprocess.Popen(
        [ANVIL, "--port", str(PORT), "--chain-id", "31337", "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(PORT)
        yield Web3(Web3.HTTPProvider(f"http://127.0.0.1:{PORT}", request_kwargs={"timeout": 10}))
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _deploy(w3: Web3) -> Settings:
    deployer = Account.from_key(DEPLOYER_KEY)
    relayer = Account.create()
    fund = {
        **_base(w3, deployer.address, 0),
        "to": relayer.address,
        "value": w3.to_wei(5, "ether"),
        "gas": 21_000,
    }
    _send(w3, fund, DEPLOYER_KEY)
    usdc_abi, usdc_code = _artifact("MockUSDC.sol/MockUSDC.json")
    router_abi, router_code = _artifact("PaymentRouter.sol/PaymentRouter.json")
    usdc = w3.eth.contract(abi=usdc_abi, bytecode=usdc_code)
    usdc_address = _send(w3, usdc.constructor(deployer.address).build_transaction(_base(w3, deployer.address, 1)), DEPLOYER_KEY)
    usdc = w3.eth.contract(address=usdc_address, abi=usdc_abi)
    router = w3.eth.contract(abi=router_abi, bytecode=router_code)
    router_address = _send(
        w3,
        router.constructor(usdc_address, relayer.address, 100_000_000, deployer.address).build_transaction(
            _base(w3, deployer.address, 2)
        ),
        DEPLOYER_KEY,
    )
    role = usdc.functions.MINTER_ROLE().call()
    _send(
        w3,
        usdc.functions.grantRole(role, relayer.address).build_transaction(_base(w3, deployer.address, 3)),
        DEPLOYER_KEY,
    )
    return _settings(
        relayer_private_key=relayer.key.hex(),
        router_address=router_address,
        usdc_address=usdc_address,
    )


async def _pair(session, settings: Settings):
    sender = await touch_user(session, telegram_id=11, username="ada", first_name="Ada")
    recipient = await touch_user(session, telegram_id=12, username="maya2", first_name="Maya")
    create_wallet(session, sender.id, settings.key_encryption_key)
    create_wallet(session, recipient.id, settings.key_encryption_key)
    session.add(RelayerState(id=1, base_nonce=0))
    await session.flush()
    return sender, recipient


def _worker(sessions, settings: Settings, inbox: Inbox | None = None, clock: ManualClock | None = None) -> Worker:
    return Worker(sessions, Chain(settings), settings, clock=clock, bot=inbox or Inbox())


async def _mint(sessions, worker: Worker, sender_id, w3: Web3) -> None:
    async with sessions() as session:
        async with session.begin():
            session.add(RelayerTx(kind="mint", user_id=sender_id, amount=5_000_000, starter=True, status="queued"))
    await worker.tick()
    w3.provider.make_request("evm_mine", [])
    await worker.tick()
    async with sessions() as session:
        row = await session.scalar(select(RelayerTx).where(RelayerTx.kind == "mint"))
        assert row.status == "mined"


@pytest.mark.asyncio
async def test_connection_error_during_tick_does_not_stop_the_worker(sessions):
    class Down:
        async def transaction_count(self):
            raise requests.exceptions.ConnectionError("https://polygon-amoy.g.alchemy.com/v2/secret-key")

    worker = Worker(sessions, Down(), _settings())
    await worker.tick()
    assert worker.paused is False


@pytest.mark.asyncio
async def test_admin_alert_cannot_stop_the_process(sessions):
    settings = _settings(admin_telegram_id=99)
    worker = Worker(sessions, Count(0), settings, bot=BoomBot())
    await worker._pause(None, None, "insufficient funds")
    assert worker.paused is True


@pytest.mark.asyncio
async def test_startup_refuses_a_base_nonce_ahead_of_the_chain(sessions):
    async with sessions() as session:
        async with session.begin():
            session.add(RelayerState(id=1, base_nonce=5))
    worker = Worker(sessions, Count(0), _settings())
    with pytest.raises(StartupError, match="ahead"):
        await worker._reconcile(startup=True)


@pytest.mark.asyncio
async def test_polygonscan_reply_uses_the_amoy_host():
    assert mined_text(80002, "0xabc") == "Sent. https://amoy.polygonscan.com/tx/0xabc"


@pytest.mark.asyncio
async def test_send_notifies_the_sender_and_a_revert_does_not_store_the_rpc_url(sessions, anvil):
    settings = _deploy(anvil)
    inbox = Inbox()
    worker = _worker(sessions, settings, inbox)
    async with sessions() as session:
        async with session.begin():
            sender, recipient = await _pair(session, settings)
            sender_id, recipient_id = sender.id, recipient.id
    await _mint(sessions, worker, sender_id, anvil)
    chain = worker.chain
    async with sessions() as session:
        wallet = await session.get(Wallet, sender_id)
        address = wallet.address
    assert await chain.usdc_balance(address) == 5_000_000

    async with sessions() as session:
        async with session.begin():
            transfer = Transfer(
                idempotency_key="tg:1",
                sender_id=sender_id,
                recipient_id=recipient_id,
                amount=1_000_000,
                status="queued",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                queued_at=datetime.now(timezone.utc),
            )
            session.add(transfer)
            await session.flush()
            session.add(
                RelayerTx(
                    kind="transfer",
                    user_id=sender_id,
                    transfer_id=transfer.id,
                    amount=1_000_000,
                    starter=False,
                    status="queued",
                )
            )
    await worker.tick()
    anvil.provider.make_request("evm_mine", [])
    await worker.tick()
    async with sessions() as session:
        sender_wallet = await session.get(Wallet, sender_id)
        recipient_wallet = await session.get(Wallet, recipient_id)
        transfer = await session.scalar(select(Transfer).where(Transfer.idempotency_key == "tg:1"))
        sender_balance = sender_wallet.address
        recipient_balance = recipient_wallet.address
        tx_hash = transfer.tx_hash
        status = transfer.status
    assert status == "mined"
    assert await chain.usdc_balance(sender_balance) == 4_000_000
    assert await chain.usdc_balance(recipient_balance) == 1_000_000
    assert inbox.messages[-1] == (11, f"Sent. {tx_hash}")

    async def revert(to, calldata):
        raise ContractLogicError("execution reverted https://polygon-amoy.g.alchemy.com/v2/secret-key")

    worker.chain.estimate_gas = revert
    async with sessions() as session:
        async with session.begin():
            transfer = Transfer(
                idempotency_key="tg:2",
                sender_id=sender_id,
                recipient_id=recipient_id,
                amount=1_000_000,
                status="queued",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                queued_at=datetime.now(timezone.utc),
            )
            session.add(transfer)
            await session.flush()
            row = RelayerTx(
                kind="transfer",
                user_id=sender_id,
                transfer_id=transfer.id,
                amount=1_000_000,
                starter=False,
                status="queued",
            )
            session.add(row)
            await session.flush()
            row_id = row.id
    await worker.tick()
    async with sessions() as session:
        transfer = await session.scalar(select(Transfer).where(Transfer.idempotency_key == "tg:2"))
        row = await session.get(RelayerTx, row_id)
        assert transfer.status == "failed"
        assert transfer.error is not None
        assert "secret-key" not in transfer.error
        assert "http" not in transfer.error
        assert row.status == "failed"
        assert row.nonce is None
    assert any("secret-key" not in text and "http" not in text for _, text in inbox.messages)


@pytest.mark.asyncio
async def test_restart_rebroadcasts_the_committed_raw_transaction(sessions, anvil):
    settings = _deploy(anvil)
    worker = _worker(sessions, settings)
    async with sessions() as session:
        async with session.begin():
            sender, _recipient = await _pair(session, settings)
            sender_id = sender.id
            session.add(RelayerTx(kind="mint", user_id=sender_id, amount=5_000_000, starter=True, status="queued"))
    await worker.sign_next(broadcast=False)
    async with sessions() as session:
        stored = await session.scalar(select(RelayerBroadcast))
        raw = bytes(stored.raw_tx)
    assert await worker.chain.transaction_count() == 0

    sent: list[bytes] = []

    async def capture(raw_tx: bytes) -> None:
        sent.append(bytes(raw_tx))

    restarted = _worker(sessions, settings)
    restarted.chain.send_raw = capture
    await restarted.tick()
    assert sent == [raw]


@pytest.mark.asyncio
async def test_fee_bump_and_original_hash_after_replacement(sessions, anvil):
    settings = _deploy(anvil)
    clock = ManualClock()
    worker = _worker(sessions, settings, clock=clock)
    anvil.provider.make_request("anvil_setAutomine", [False])
    async with sessions() as session:
        async with session.begin():
            sender, _recipient = await _pair(session, settings)
            session.add(RelayerTx(kind="mint", user_id=sender.id, amount=5_000_000, starter=True, status="queued"))
    await worker.tick()
    clock.advance(121)
    await worker.tick()
    async with sessions() as session:
        broadcasts = (
            await session.scalars(select(RelayerBroadcast).order_by(RelayerBroadcast.created_at, RelayerBroadcast.id))
        ).all()
        assert len(broadcasts) == 2
        assert broadcasts[0].tx_hash != broadcasts[1].tx_hash
        assert broadcasts[1].max_priority_fee_per_gas > broadcasts[0].max_priority_fee_per_gas
        assert broadcasts[1].max_fee_per_gas > broadcasts[0].max_fee_per_gas
        row = await session.get(RelayerTx, broadcasts[0].relayer_tx_id)
        assert row.nonce == 0


@pytest.mark.asyncio
async def test_original_hash_mines_after_a_replacement_was_signed(sessions, anvil):
    settings = _deploy(anvil)
    worker = _worker(sessions, settings)
    async with sessions() as session:
        async with session.begin():
            sender, _recipient = await _pair(session, settings)
            session.add(RelayerTx(kind="mint", user_id=sender.id, amount=5_000_000, starter=True, status="queued"))
    await worker.sign_next(broadcast=False)
    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(select(RelayerTx))
            latest = await session.scalar(select(RelayerBroadcast))
            first_hash = latest.tx_hash
            first_raw = bytes(latest.raw_tx)
            bumped = worker._sign_bump(row, latest)
            worker._store_signed(row, bumped, cancel=False)
            second_hash = bumped.tx_hash
    await worker.chain.send_raw(first_raw)
    anvil.eth.wait_for_transaction_receipt(first_hash)
    await worker.tick()
    async with sessions() as session:
        row = await session.scalar(select(RelayerTx))
        assert row.status == "mined"
    assert anvil.eth.get_transaction_receipt(first_hash).status == 1
    with pytest.raises(Exception):
        anvil.eth.get_transaction_receipt(second_hash)


@pytest.mark.asyncio
async def test_cancel_after_the_permit_deadline(sessions, anvil):
    settings = _deploy(anvil)
    inbox = Inbox()
    worker = _worker(sessions, settings, inbox)
    async with sessions() as session:
        async with session.begin():
            sender, recipient = await _pair(session, settings)
            sender_id, recipient_id = sender.id, recipient.id
    await _mint(sessions, worker, sender_id, anvil)
    async with sessions() as session:
        async with session.begin():
            transfer = Transfer(
                idempotency_key="tg:cancel",
                sender_id=sender_id,
                recipient_id=recipient_id,
                amount=1_000_000,
                status="queued",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                queued_at=datetime.now(timezone.utc),
            )
            session.add(transfer)
            await session.flush()
            session.add(
                RelayerTx(
                    kind="transfer",
                    user_id=sender_id,
                    transfer_id=transfer.id,
                    amount=1_000_000,
                    starter=False,
                    status="queued",
                )
            )
    await worker.sign_next(broadcast=False)
    anvil.provider.make_request("evm_increaseTime", [90])
    anvil.provider.make_request("evm_mine", [])
    await worker.tick()
    anvil.provider.make_request("evm_mine", [])
    await worker.tick()
    async with sessions() as session:
        transfer = await session.scalar(select(Transfer).where(Transfer.idempotency_key == "tg:cancel"))
        assert transfer.status == "failed"
        assert transfer.error == "cancelled"
    assert any("cancelled" in text for _, text in inbox.messages)


@pytest.mark.asyncio
async def test_startup_refuses_a_nonce_with_no_receipt_and_runtime_does_not_pause(sessions, anvil):
    settings = _deploy(anvil)
    chain = Chain(settings)
    signed = chain.sign_transaction(
        nonce=0,
        to=Account.from_key(settings.relayer_private_key).address,
        calldata=b"",
        gas_limit=21_000,
        max_fee=2_000_000_000,
        priority=1_000_000_000,
        permit_deadline=None,
    )
    await chain.send_raw(signed.raw)
    anvil.eth.wait_for_transaction_receipt(signed.tx_hash)
    async with sessions() as session:
        async with session.begin():
            user = User(telegram_id=21, username="ada")
            session.add(user)
            await session.flush()
            ciphertext, nonce = encrypt_key(settings.key_encryption_key, user.id, b"\x01" * 32)
            session.add(Wallet(user_id=user.id, address=Account.create().address, ciphertext=ciphertext, nonce=nonce))
            row = RelayerTx(
                kind="mint",
                user_id=user.id,
                amount=1,
                starter=False,
                status="submitted",
                nonce=0,
                tx_to=Account.from_key(settings.relayer_private_key).address,
                calldata=b"",
                gas_limit=21_000,
                last_broadcast_at=datetime.now(timezone.utc),
            )
            session.add(row)
            await session.flush()
            session.add(
                RelayerBroadcast(
                    relayer_tx_id=row.id,
                    raw_tx=b"\x02",
                    tx_hash="0x" + "ab" * 32,
                    max_fee_per_gas=1,
                    max_priority_fee_per_gas=1,
                    cancel=False,
                )
            )
    worker = _worker(sessions, settings)
    with pytest.raises(StartupError, match="no receipt"):
        await worker.startup()

    runtime = _worker(sessions, settings)
    await runtime.tick()
    assert runtime.paused is False


@pytest.mark.asyncio
async def test_simulation_connection_error_leaves_the_transfer_queued(sessions, anvil):
    settings = _deploy(anvil)
    worker = _worker(sessions, settings)
    async with sessions() as session:
        async with session.begin():
            sender, recipient = await _pair(session, settings)
            sender_id, recipient_id = sender.id, recipient.id
    await _mint(sessions, worker, sender_id, anvil)

    async def down(to, calldata):
        raise requests.exceptions.ConnectionError("https://polygon-amoy.g.alchemy.com/v2/secret-key")

    worker.chain.estimate_gas = down
    async with sessions() as session:
        async with session.begin():
            transfer = Transfer(
                idempotency_key="tg:down",
                sender_id=sender_id,
                recipient_id=recipient_id,
                amount=1_000_000,
                status="queued",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                queued_at=datetime.now(timezone.utc),
            )
            session.add(transfer)
            await session.flush()
            row = RelayerTx(
                kind="transfer",
                user_id=sender_id,
                transfer_id=transfer.id,
                amount=1_000_000,
                starter=False,
                status="queued",
            )
            session.add(row)
            await session.flush()
            row_id = row.id
    await worker.tick()
    async with sessions() as session:
        transfer = await session.scalar(select(Transfer).where(Transfer.idempotency_key == "tg:down"))
        row = await session.get(RelayerTx, row_id)
        assert transfer.status == "queued"
        assert transfer.error is None
        assert row.status == "queued"
        assert row.nonce is None
