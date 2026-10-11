"""Сборка и запуск бота."""

import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Config, get_config
from ..db.session import make_engine, make_sessionmaker
from ..logs import setup_logging
from ..services import access, reference
from .handlers import admin, common, me, payroll, reports, stock, work
from .middlewares import ContextMiddleware
from .storage import PostgresStorage

log = structlog.get_logger(__name__)

PRIVATE_COMMANDS = [
    ("start", "Главное меню"),
    ("my_work", "Моя работа"),
    ("my_salary", "Мой расчёт"),
    ("my_stock", "Товар на руках"),
    ("request_payout", "Запросить выплату"),
    ("my_payouts", "Мои выплаты"),
    ("help", "Справка"),
    ("admin", "Администрирование"),
]
GROUP_COMMANDS = [
    ("op", "Внести операцию"),
    ("pending", "Ждут подтверждения"),
    ("ops", "Сводка операций"),
    ("stock", "Остатки"),
    ("transit", "Товар в пути"),
    ("help", "Справка"),
]


def build_dispatcher(sessionmaker: async_sessionmaker[AsyncSession], config: Config) -> Dispatcher:
    dp = Dispatcher(storage=PostgresStorage(sessionmaker))
    middleware = ContextMiddleware(sessionmaker, config)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    # Порядок важен: состояние мастера ввода (work, me) проверяется раньше общих текстовых кнопок.
    for router in (common.router, work.router, me.router, admin.router, stock.router, payroll.router, reports.router):
        dp.include_router(router)
    return dp


async def bootstrap(sessionmaker: async_sessionmaker[AsyncSession], config: Config) -> None:
    async with sessionmaker() as session, session.begin():
        await reference.ensure_reference_data(session)
        for telegram_id in config.owner_telegram_ids:
            await access.ensure_owner(session, telegram_id)
    removed = await PostgresStorage(sessionmaker).cleanup()
    if removed:
        log.info("fsm_cleanup", removed=removed)


async def run() -> None:
    config = get_config()
    setup_logging(config.log_level, config.log_json)
    if not config.bot_token:
        raise SystemExit("BOT_TOKEN не задан")
    if not config.owner_telegram_ids:
        log.warning("owner_ids_empty", hint="Задайте OWNER_TELEGRAM_IDS, иначе некому будет настроить бота")
    engine = make_engine(config.database_url)
    sessionmaker = make_sessionmaker(engine)
    await bootstrap(sessionmaker, config)
    session = AiohttpSession(proxy=config.telegram_proxy) if config.telegram_proxy else None
    bot = Bot(config.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(sessionmaker, config)
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in PRIVATE_COMMANDS], scope=BotCommandScopeAllPrivateChats()
        )
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in GROUP_COMMANDS], scope=BotCommandScopeAllGroupChats()
        )
        log.info("bot_started", owners=len(config.owner_telegram_ids))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())
