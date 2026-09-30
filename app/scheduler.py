from __future__ import annotations

from datetime import date

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.keyboards import MAIN_MENU
from app.models import User
from app.repository import day_stats, recalibrate_tdee


async def send_evening_poll(bot: Bot) -> None:
    async with SessionLocal() as session:
        users = list(await session.scalars(select(User).where(User.evening_poll_enabled.is_(True))))
    for user in users:
        try:
            await bot.send_message(
                user.tg_id,
                "Как прошёл день? 👋\nСколько км ты сегодня прошёл? Примерно.",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            continue


async def send_daily_summary(bot: Bot) -> None:
    async with SessionLocal() as session:
        users = list(await session.scalars(select(User).where(User.daily_summary_enabled.is_(True))))
        for user in users:
            stats = await day_stats(session, user, date.today())
            if not stats["foods"] and stats["km_walked"] is None:
                continue
            km_text = "не указано" if stats["km_walked"] is None else f"{stats['km_walked']:.1f} км"
            if user.age < 18:
                text = (
                    f"🧾 <b>Итоги дня</b>\n\n"
                    f"🍽 Записано: {stats['calories']} ккал\n"
                    f"🎯 Дневной ориентир: ~{user.calorie_target} ккал\n"
                    f"🚶 {km_text}\n"
                    f"🔥 Оценочный расход: ~{stats['expenditure']} ккал\n"
                    f"📉 Расчётный баланс: {stats['deficit']:+} ккал\n"
                    f"🥩 Белок: {stats['protein']} г\n\n"
                    "Ориентир не нужно специально добирать.\n"
                    "Хорошего вечера 👋"
                )
            else:
                text = (
                    f"🧾 <b>Итоги дня</b>\n\n"
                    f"🍽 {stats['calories']} / {user.calorie_target} ккал\n"
                    f"🚶 {km_text}\n"
                    f"🔥 Оценочный расход: ~{stats['expenditure']} ккал\n"
                    f"📉 Расчётный баланс: {stats['deficit']:+} ккал\n"
                    f"🥩 Белок: {stats['protein']} г\n\n"
                    f"Хорошего вечера 👋"
                )
            if stats["deficit"] > 900:
                text += (
                    "\n\nСегодня получился очень большой расчётный дефицит. "
                    "Не нужно стремиться делать его как можно больше."
                )
            try:
                await bot.send_message(user.tg_id, text)
            except Exception:
                continue


async def recalibrate_users(bot: Bot) -> None:
    """Daily job; each adult user is actually recalibrated at most every 14 days."""
    async with SessionLocal() as session:
        users = list(await session.scalars(select(User)))
        for user in users:
            try:
                report = await recalibrate_tdee(session, user)
                if not report:
                    continue
                await bot.send_message(
                    user.tg_id,
                    "📐 <b>Калибровка расхода</b>\n\n"
                    f"Прогноз изменения веса: {report['predicted_change']:+.1f} кг\n"
                    f"Факт: {report['actual_change']:+.1f} кг\n"
                    f"Поправка TDEE: {report['delta_correction']:+.0f} ккал/день\n"
                    f"Новая дневная цель: <b>{report['new_target']} ккал</b>\n\n"
                    "<i>Это сглаженная оценка по журналу еды, активности и тренду веса.</i>",
                )
            except Exception:
                await session.rollback()
                continue


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.app_timezone)
    scheduler.add_job(
        send_evening_poll,
        "cron",
        hour=settings.evening_poll_hour,
        minute=0,
        args=[bot],
        id="evening_poll",
        replace_existing=True,
    )
    scheduler.add_job(
        send_daily_summary,
        "cron",
        hour=settings.daily_summary_hour,
        minute=0,
        args=[bot],
        id="daily_summary",
        replace_existing=True,
    )
    # Run the eligibility check daily; per-user last_tdee_recalc_at enforces the
    # 14-day cadence and lets users with insufficient data retry automatically.
    scheduler.add_job(
        recalibrate_users,
        "cron",
        hour=12,
        minute=15,
        args=[bot],
        id="tdee_recalibration",
        replace_existing=True,
    )
    return scheduler
