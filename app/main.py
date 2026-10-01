import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import router
from app.config import settings
from app.db import init_db
from app.scheduler import build_scheduler
from app.subscription import SubscriptionMiddleware


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    subscription_middleware = SubscriptionMiddleware()
    dp.message.outer_middleware(subscription_middleware)
    dp.callback_query.outer_middleware(subscription_middleware)

    dp.include_router(router)
    scheduler = build_scheduler(bot)
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
