from __future__ import annotations

from datetime import date

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.keyboards import MAIN_MENU
from app.models import User
from app.repository import day_stats


async def send_evening_poll(bot: Bot) -> None:
    async with SessionLocal() as session:
        users = list(await session.scalars(select(User).where(User.evening_poll_enabled.is_(True))))
    for user in users:
        try:
            await bot.send_message(user.tg_id, "Как прошёл день? 👋\nСколько сегодня примерно шагов?", reply_markup=MAIN_MENU)
        except Exception:
            continue


async def send_daily_summary(bot: Bot) -> None:
    async with SessionLocal() as session:
        users = list(await session.scalars(select(User).where(User.daily_summary_enabled.is_(True))))
        for user in users:
            stats = await day_stats(session, user, date.today())
            if not stats["foods"] and not stats["steps"]:
                continue
            if user.age <= 18:
                text = (
                    f"🧾 <b>Итоги дня</b>\n\n🍽 Записано: {stats['calories']} ккал\n"
                    f"🚶 {stats['steps']} шагов\n"
                    f"🥩 Белок: {stats['protein']} г\n\nХорошего вечера 👋"
                )
            else:
                text = (
                    f"🧾 <b>Итоги дня</b>\n\n🍽 {stats['calories']} / {user.calorie_target} ккал\n"
                    f"🚶 {stats['steps']} шагов\n🔥 Расчётный дефицит: ~{stats['deficit']} ккал\n"
                    f"🥩 Белок: {stats['protein']} г\n\nХорошего вечера 👋"
                )
                if stats["deficit"] > 900:
                    text += "\n\nСегодня получился довольно большой расчётный дефицит. Необязательно стараться делать его как можно больше — устойчивый режим обычно удобнее соблюдать."
            try:
                await bot.send_message(user.tg_id, text)
            except Exception:
                continue


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.app_timezone)
    scheduler.add_job(send_evening_poll, "cron", hour=settings.evening_poll_hour, minute=0, args=[bot], id="evening_poll", replace_existing=True)
    scheduler.add_job(send_daily_summary, "cron", hour=settings.daily_summary_hour, minute=0, args=[bot], id="daily_summary", replace_existing=True)
    return scheduler
