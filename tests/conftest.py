import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from bot.db import make_engine, make_session_factory

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEST_URL = "postgresql+asyncpg://pay:pay@127.0.0.1:5432/pay_test"
os.environ.setdefault("KEY_ENCRYPTION_KEY", "11" * 32)
os.environ.setdefault("DATABASE_URL", TEST_URL)
os.environ.setdefault("RPC_URL", "http://127.0.0.1:18545")
os.environ.setdefault("CHAIN_ID", "31337")
os.environ.setdefault("RELAYER_PRIVATE_KEY", "0x" + "22" * 32)
os.environ.setdefault("ROUTER_ADDRESS", "0x" + "33" * 20)
os.environ.setdefault("USDC_ADDRESS", "0x" + "44" * 20)


@pytest.fixture(scope="session")
def migrated():
    env = os.environ.copy()
    env["DATABASE_URL"] = TEST_URL
    subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env)
    return TEST_URL


@pytest.fixture
async def sessions(migrated):
    engine = make_engine(migrated)
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            text(
                "TRUNCATE relayer_broadcasts, relayer_txs, transfers, wallets, users, relayer_state "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()
