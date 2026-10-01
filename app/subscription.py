from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from app.config import settings
from app.db import SessionLocal
from app.keyboards import MAIN_MENU, SEX_KB
from app.repository import get_user


logger = logging.getLogger(__name__)

SUBSCRIPTION_CHECK_CALLBACK = "subscription:check"


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=settings.required_channel_url)],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data=SUBSCRIPTION_CHECK_CALLBACK)],
        ]
    )


def _member_has_access(member: Any) -> bool:
    if member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True

    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))

    return False


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """Return True only while the user is a member of the required channel."""
    try:
        member = await bot.get_chat_member(
            chat_id=settings.required_channel,
            user_id=user_id,
        )
    except Exception:
        # Fail closed: if Telegram cannot confirm membership, protected bot
        # functionality must not be available.
        logger.exception(
            "Failed to verify Telegram channel membership",
            extra={"user_id": user_id, "channel": settings.required_channel},
        )
        return False

    return _member_has_access(member)


async def _clear_state(state: FSMContext | None) -> None:
    if state is not None:
        await state.clear()


async def _send_subscription_required(event: Message | CallbackQuery, state: FSMContext | None) -> None:
    await _clear_state(state)
    text = (
        "Чтобы пользоваться ботом, подпишись на наш Telegram-канал "
        f"<b>{settings.required_channel}</b> 👇\n\n"
        "После подписки нажми <b>«Я подписался»</b>."
    )

    if isinstance(event, CallbackQuery):
        await event.answer("Сначала подпишись на канал")
        if event.message is not None:
            await event.message.answer(text, reply_markup=subscription_keyboard())
        return

    await event.answer(text, reply_markup=subscription_keyboard())


async def _handle_subscription_check(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext | None,
) -> None:
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.answer(
            "Подписка пока не найдена. Подпишись на канал и попробуй ещё раз.",
            show_alert=True,
        )
        return

    await _clear_state(state)
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)

    await callback.answer("Подписка подтверждена ✅")

    if callback.message is None:
        return

    if user:
        await callback.message.answer(
            "Подписка подтверждена ✅\nЧто записываем?",
            reply_markup=MAIN_MENU,
        )
        return

    await callback.message.answer(
        "Подписка подтверждена ✅\n\n"
        "Привет 👋\n"
        "Я помогу считать питание, активность и следить за прогрессом.\n\n"
        "Для начала нужно немного узнать о тебе.",
        reply_markup=SEX_KB,
    )


class SubscriptionMiddleware(BaseMiddleware):
    """Block every bot interaction unless the user follows the required channel."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        user = event.from_user
        if user is None or user.is_bot:
            return await handler(event, data)

        bot: Bot = data["bot"]
        state: FSMContext | None = data.get("state")

        if isinstance(event, CallbackQuery) and event.data == SUBSCRIPTION_CHECK_CALLBACK:
            await _handle_subscription_check(event, bot, state)
            return None

        if not await is_subscribed(bot, user.id):
            await _send_subscription_required(event, state)
            return None

        return await handler(event, data)
