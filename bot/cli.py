import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot.chain import Chain
from bot.config import get_settings
from bot.db import make_engine, make_session_factory
from bot.errors import StartupError
from bot.handlers import App, build_public_router, build_router
from bot.limits import ParseLimiter
from bot.models import RelayerState
from bot.worker import Worker

log = logging.getLogger(__name__)


async def init_base_nonce(value: int) -> None:
    if value < 0:
        raise SystemExit("Nonce must be zero or greater.")
    settings = get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        state = await session.get(RelayerState, 1)
        if state is not None and state.base_nonce is not None:
            raise SystemExit("Base nonce is already set.")
        if state is None:
            session.add(RelayerState(id=1, base_nonce=value))
        else:
            state.base_nonce = value
        await session.commit()
    await engine.dispose()
    print(f"base nonce set to {value}")


async def run_bot() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is required to start the bot.")
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    chain = Chain(settings)
    worker = Worker(sessions, chain, settings)
    try:
        await worker.startup()
    except StartupError as exc:
        raise SystemExit(str(exc)) from exc
    bot = Bot(settings.bot_token)
    worker.bot = bot
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Create your testnet wallet"),
            BotCommand(command="balance", description="Show your USDC balance"),
            BotCommand(command="history", description="Show recent transfers"),
            BotCommand(command="faucet", description="Mint test USDC"),
            BotCommand(command="help", description="How to send"),
        ]
    )
    dispatcher = Dispatcher()
    app = App(sessions, chain, settings, ParseLimiter(settings.parse_limit_per_minute))
    dispatcher.include_router(build_public_router())
    dispatcher.include_router(build_router(app))

    async def loop() -> None:
        try:
            while True:
                await worker.tick()
                await asyncio.sleep(2)
        except StartupError:
            log.exception("relayer ledger no longer matches the chain")
            os._exit(1)
        except Exception:
            log.exception("relayer worker stopped")
            os._exit(1)

    asyncio.create_task(loop())
    await dispatcher.start_polling(bot)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if len(sys.argv) >= 2 and sys.argv[1] == "init-base-nonce":
        if len(sys.argv) != 3:
            raise SystemExit("Usage: python -m bot init-base-nonce <n>")
        asyncio.run(init_base_nonce(int(sys.argv[2])))
        return
    asyncio.run(run_bot())
