from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime, timedelta
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.ai_service import FoodAnalysis, analyze_food_photo, analyze_food_text, analyze_pantry_photo, recipes
from app.calculations import calculate_targets
from app.db import SessionLocal
from app.intents import is_photo_question, looks_like_question
from app.keyboards import (
    ACTIVITY_KB,
    FOOD_CONFIRM_KB,
    FOOD_REVIEW_KB,
    FOOD_START_KB,
    GOAL_KB,
    MAIN_MENU,
    PROFILE_KB,
    RECIPE_PREF_KB,
    SEX_KB,
    WEIGHT_PRESETS_KB,
    WORKOUT_KB,
    recipe_choices,
)
from app.repository import (
    add_food,
    add_weight,
    create_user,
    day_stats,
    food_days_in_month,
    get_user,
    stats_period,
    upsert_activity,
)


router = Router(name="pohudai")


class Onboarding(StatesGroup):
    age = State()
    height = State()
    weight = State()
    target_weight = State()


class FoodFlow(StatesGroup):
    waiting_input = State()
    waiting_clarification = State()
    waiting_weight = State()
    waiting_custom_weight = State()
    waiting_confirm = State()


class WeightFlow(StatesGroup):
    waiting_weight = State()


class ActivityFlow(StatesGroup):
    waiting_steps = State()
    waiting_duration = State()


class RecipeFlow(StatesGroup):
    waiting_ingredients = State()
    waiting_preference = State()
    choosing_recipe = State()


class SettingsFlow(StatesGroup):
    calorie_target = State()
    target_weight = State()


def _num(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", text.replace(" ", ""))
    return float(match.group(1).replace(",", ".")) if match else None


def _format_food(draft: dict, include_components: bool = False) -> str:
    lines = [f"🍽 <b>{draft['title']}</b>"]
    if draft.get("total_grams"):
        lines.append(f"\nВес: ~{draft['total_grams']:.0f} г")
    lines.extend([
        f"\n🔥 <b>{draft['calories']:.0f} ккал</b>",
        f"🥩 Белки: {draft.get('protein_g', 0):.0f} г",
        f"🥑 Жиры: {draft.get('fat_g', 0):.0f} г",
        f"🍞 Углеводы: {draft.get('carbs_g', 0):.0f} г",
    ])
    if include_components and draft.get("components"):
        lines.append("\nЯ распознал:")
        for item in draft["components"]:
            brand = f" {item['brand']}" if item.get("brand") else ""
            grams = f" — ~{item['grams']:.0f} г" if item.get("grams") else ""
            lines.append(f"• {item['name']}{brand}{grams}")
    if draft.get("notes"):
        lines.append(f"\n<i>{draft['notes']}</i>")
    return "\n".join(lines)


def _draft(analysis: FoodAnalysis, source: str) -> dict:
    result = analysis.model_dump()
    result["source"] = source
    result["details_json"] = json.dumps(result["components"], ensure_ascii=False)
    return result


def _nutrition_profile_context(user) -> str:
    goal_map = {"lose": "снижение веса", "maintain": "поддержание веса", "gain": "набор веса"}
    return (
        f"Возраст: {user.age}. Пол: {user.sex}. Рост: {user.height_cm:.0f} см. "
        f"Текущий вес: {user.current_weight_kg:.1f} кг. "
        f"Цель: {goal_map.get(user.goal, user.goal)}. "
        f"Целевой вес: {user.target_weight_kg if user.target_weight_kg else 'не задан'}. "
        f"Оценочная поддерживающая потребность: {user.maintenance_calories} ккал/день. "
        f"Дневной ориентир дневника: {user.calorie_target} ккал. "
        f"Белковый ориентир: {user.protein_target_g} г/день. "
        f"Возраст до 19 лет: {'да' if user.age <= 18 else 'нет'}."
    )


async def _registered(message: Message) -> bool:
    async with SessionLocal() as session:
        return await get_user(session, message.from_user.id) is not None


async def _get_image(bot: Bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download(tg_file, destination=buf)
    return buf.getvalue()


async def _show_day(message: Message, user_id: int, day: date) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, user_id)
        if not user:
            return
        stats = await day_stats(session, user, day)
    lines = [
        f"📅 <b>{day.strftime('%d.%m.%Y')}</b>",
        f"\n🍽 Съедено: <b>{stats['calories']} ккал</b>",
        f"🎯 Цель: {user.calorie_target} ккал",
        f"🥩 Белок: {stats['protein']} / {user.protein_target_g} г",
        f"🚶 Шаги: {stats['steps']}",
        f"🔥 Оценочный расход: ~{stats['expenditure']} ккал",
        f"📉 Расчётный дефицит: ~{stats['deficit']} ккал",
    ]
    if stats["foods"]:
        lines.append("\n<b>Еда:</b>")
        for food in stats["foods"]:
            lines.append(f"• {food.title} — {food.calories:.0f} ккал")
    else:
        lines.append("\nЕда пока не записана.")
    await message.answer("\n".join(lines), reply_markup=MAIN_MENU)


async def _calendar_markup(user_id: int, year: int, month: int) -> InlineKeyboardMarkup:
    async with SessionLocal() as session:
        user = await get_user(session, user_id)
        filled = await food_days_in_month(session, user, year, month) if user else set()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"{calendar.month_name[month]} {year}", callback_data="noop"))
    builder.row(*[InlineKeyboardButton(text=x, callback_data="noop") for x in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        buttons = []
        for d in week:
            if d == 0:
                buttons.append(InlineKeyboardButton(text=" ", callback_data="noop"))
            else:
                marker = "🟢" if d in filled else "⚪"
                buttons.append(InlineKeyboardButton(text=f"{marker}{d}", callback_data=f"day:{year}-{month:02d}-{d:02d}"))
        builder.row(*buttons)
    prev = date(year, month, 1) - timedelta(days=1)
    nxt = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"cal:{prev.year}-{prev.month:02d}"),
        InlineKeyboardButton(text="▶️", callback_data=f"cal:{nxt.year}-{nxt.month:02d}"),
    )
    return builder.as_markup()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
    if user:
        await message.answer("С возвращением 👋\nЧто записываем?", reply_markup=MAIN_MENU)
        return
    await message.answer(
        "Привет 👋\nЯ помогу считать питание, активность и следить за прогрессом.\n\nДля начала нужно немного узнать о тебе.",
        reply_markup=SEX_KB,
    )


@router.callback_query(F.data.startswith("sex:"))
async def onboarding_sex(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sex=callback.data.split(":", 1)[1])
    await state.set_state(Onboarding.age)
    await callback.message.edit_text("Сколько тебе лет?")
    await callback.answer()


@router.message(Onboarding.age)
async def onboarding_age(message: Message, state: FSMContext) -> None:
    value = _num(message.text)
    if not value or not 14 <= value <= 100:
        await message.answer("Введи возраст числом, например: 27")
        return
    await state.update_data(age=int(value))
    await state.set_state(Onboarding.height)
    await message.answer("Какой у тебя рост? Например: <b>182 см</b>")


@router.message(Onboarding.height)
async def onboarding_height(message: Message, state: FSMContext) -> None:
    value = _num(message.text)
    if not value or not 120 <= value <= 230:
        await message.answer("Введи рост в сантиметрах, например: 182")
        return
    await state.update_data(height_cm=value)
    await state.set_state(Onboarding.weight)
    await message.answer("Какой сейчас вес? Например: <b>84 кг</b>")


@router.message(Onboarding.weight)
async def onboarding_weight(message: Message, state: FSMContext) -> None:
    value = _num(message.text)
    if not value or not 35 <= value <= 350:
        await message.answer("Введи вес в килограммах, например: 84.5")
        return
    await state.update_data(weight_kg=value)
    await message.answer("Какая у тебя обычно активность?", reply_markup=ACTIVITY_KB)


@router.callback_query(F.data.startswith("activity:"))
async def onboarding_activity(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(activity_level=callback.data.split(":", 1)[1])
    await callback.message.edit_text("Какая цель?", reply_markup=GOAL_KB)
    await callback.answer()


@router.callback_query(F.data.startswith("goal:"))
async def onboarding_goal(callback: CallbackQuery, state: FSMContext) -> None:
    goal = callback.data.split(":", 1)[1]
    await state.update_data(goal=goal)
    if goal == "lose":
        data = await state.get_data()
        await state.set_state(Onboarding.target_weight)
        await callback.message.edit_text(f"Сейчас: {data['weight_kg']:.1f} кг\nДо какого веса хочешь похудеть?")
        await callback.answer()
        return
    await _finish_onboarding(callback.message, callback.from_user.id, callback.from_user.full_name, state)
    await callback.answer()


@router.message(Onboarding.target_weight)
async def onboarding_target(message: Message, state: FSMContext) -> None:
    value = _num(message.text)
    data = await state.get_data()
    if not value or value >= data["weight_kg"] or value < 35:
        await message.answer("Введи желаемый вес ниже текущего, например: 75")
        return
    await state.update_data(target_weight_kg=value)
    await _finish_onboarding(message, message.from_user.id, message.from_user.full_name, state)


async def _finish_onboarding(message: Message, tg_id: int, name: str | None, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        user = await create_user(session, tg_id, name, data)
    await state.clear()
    target = f"\nЦель по весу: {user.target_weight_kg:.1f} кг" if user.target_weight_kg else ""
    if user.age <= 18:
        calculation_note = (
            "\n\n<i>Расчёт сделан по возрастной формуле DRI/EER 2023. "
            "Поскольку тебе 18 или меньше, бот не назначает автоматический дефицит: "
            "дневная цель пока используется как ориентир для дневника и оценки привычек.</i>"
        )
    else:
        deficit = max(0, user.maintenance_calories - user.calorie_target)
        calculation_note = (
            f"\n\n<i>Стартовый расчёт: DRI/EER 2023. "
            f"Ориентировочный дефицит: ~{deficit} ккал/день. "
            "Дальше важнее смотреть на фактическую динамику веса и активности.</i>"
        )

    await message.answer(
        f"Готово ✅\n\nОценочная потребность для поддержания:\n"
        f"<b>{user.maintenance_calories} ккал/день</b>\n\n"
        f"Дневной ориентир:\n<b>{user.calorie_target} ккал</b>\n"
        f"Белок: ~{user.protein_target_g} г/день{target}\n"
        f"Начальный вес: {user.initial_weight_kg:.1f} кг"
        f"{calculation_note}",
        reply_markup=MAIN_MENU,
    )


@router.message(F.text == "🍽 Добавить еду")
async def food_start(message: Message, state: FSMContext) -> None:
    if not await _registered(message):
        await start(message, state)
        return
    await state.set_state(FoodFlow.waiting_input)
    await message.answer("Добавь то, что съел.\n\nМожно отправить 📸 фото или ✍️ написать текстом.", reply_markup=FOOD_START_KB)


@router.callback_query(F.data.in_({"food:photo", "food:text"}))
async def food_hint(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FoodFlow.waiting_input)
    text = "Отправь фотографию блюда 📸" if callback.data.endswith("photo") else "Напиши, что съел. Например: <b>гречка 200 г, куриная грудка 150 г</b>"
    await callback.message.edit_text(text)
    await callback.answer()


async def _analyze_message_food(message: Message, state: FSMContext) -> None:
    if message.photo:
        await message.answer("Смотрю, что на фото…")
        file_id = message.photo[-1].file_id
        image = await _get_image(message.bot, file_id)
        analysis = await analyze_food_photo(image)
        draft = _draft(analysis, "photo")
        await state.update_data(food_draft=draft, food_original_file_id=file_id, food_source="photo")
        await state.set_state(FoodFlow.waiting_confirm)
        await message.answer(_format_food(draft, include_components=True) + "\n\nПохоже правильно?", reply_markup=FOOD_REVIEW_KB)
        return
    if not message.text:
        await message.answer("Пришли фото блюда или напиши, что съел.")
        return
    await message.answer("Считаю примерные КБЖУ…")
    analysis = await analyze_food_text(message.text)
    draft = _draft(analysis, "text")
    eaten_date = date.today() - timedelta(days=1) if re.search(r"\bвчера\b", message.text.lower()) else date.today()
    await state.update_data(food_draft=draft, food_original_text=message.text, food_source="text", food_date=eaten_date.isoformat())
    if analysis.needs_weight:
        await state.set_state(FoodFlow.waiting_weight)
        await message.answer(_format_food(draft, include_components=True) + "\n\nКакой примерно был вес порции?", reply_markup=WEIGHT_PRESETS_KB)
    else:
        await state.set_state(FoodFlow.waiting_confirm)
        suffix = "\n\nДобавить за вчера?" if eaten_date != date.today() else "\n\nДобавить в сегодняшний рацион?"
        await message.answer(_format_food(draft) + suffix, reply_markup=FOOD_CONFIRM_KB)


@router.message(FoodFlow.waiting_input)
async def food_input(message: Message, state: FSMContext) -> None:
    await _analyze_message_food(message, state)


@router.callback_query(F.data == "food:recognized_ok")
async def food_recognized_ok(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("food_draft") or {}
    if draft.get("needs_weight") or not draft.get("total_grams"):
        await state.set_state(FoodFlow.waiting_weight)
        await callback.message.edit_text(_format_food(draft, include_components=True) + "\n\nКакой примерно был общий вес блюда?", reply_markup=WEIGHT_PRESETS_KB)
    else:
        await state.set_state(FoodFlow.waiting_confirm)
        await callback.message.edit_text(_format_food(draft) + "\n\nДобавить в сегодняшний рацион?", reply_markup=FOOD_CONFIRM_KB)
    await callback.answer()


@router.callback_query(F.data == "food:clarify")
async def food_clarify(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FoodFlow.waiting_clarification)
    await callback.message.edit_text("Напиши, что я определил неправильно или что нужно добавить.")
    await callback.answer()


@router.message(FoodFlow.waiting_clarification)
async def food_clarification(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    source = data.get("food_source")
    await message.answer("Обновляю состав…")
    if source == "photo" and data.get("food_original_file_id"):
        image = await _get_image(message.bot, data["food_original_file_id"])
        analysis = await analyze_food_photo(image, clarification=message.text)
    else:
        analysis = await analyze_food_text(data.get("food_original_text", ""), clarification=message.text)
    draft = _draft(analysis, source or "text")
    await state.update_data(food_draft=draft)
    await state.set_state(FoodFlow.waiting_confirm)
    await message.answer(_format_food(draft, include_components=True) + "\n\nТеперь правильно?", reply_markup=FOOD_REVIEW_KB)


@router.callback_query(F.data.startswith("portion:"))
async def food_portion(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await state.set_state(FoodFlow.waiting_custom_weight)
        await callback.message.edit_text("Введи примерный вес порции в граммах.")
        await callback.answer()
        return
    await _reanalyze_with_weight(callback.message, callback.bot, state, float(value))
    await callback.answer()


@router.message(FoodFlow.waiting_custom_weight)
async def custom_food_weight(message: Message, state: FSMContext) -> None:
    grams = _num(message.text)
    if not grams or not 10 <= grams <= 5000:
        await message.answer("Введи вес в граммах, например: 380")
        return
    await _reanalyze_with_weight(message, message.bot, state, grams)


async def _reanalyze_with_weight(message: Message, bot: Bot, state: FSMContext, grams: float) -> None:
    data = await state.get_data()
    await message.answer("Пересчитываю порцию…")
    if data.get("food_source") == "photo":
        image = await _get_image(bot, data["food_original_file_id"])
        analysis = await analyze_food_photo(image, assumed_total_grams=grams)
    else:
        analysis = await analyze_food_text(data.get("food_original_text", ""), clarification=f"Общий вес порции примерно {grams:.0f} г")
    draft = _draft(analysis, data.get("food_source", "text"))
    draft["total_grams"] = grams
    draft["needs_weight"] = False
    await state.update_data(food_draft=draft)
    await state.set_state(FoodFlow.waiting_confirm)
    await message.answer(_format_food(draft) + "\n\nДобавить в рацион?", reply_markup=FOOD_CONFIRM_KB)


@router.callback_query(F.data == "food:save")
async def food_save(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("food_draft")
    if not draft:
        await callback.answer("Черновик еды не найден", show_alert=True)
        return
    food_day = date.fromisoformat(data.get("food_date", date.today().isoformat()))
    eaten_at = datetime.combine(food_day, datetime.now().time())
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        if not user:
            await callback.answer("Сначала запусти /start", show_alert=True)
            return
        await add_food(session, user, draft, eaten_at=eaten_at)
        stats = await day_stats(session, user, food_day)
    await state.clear()
    remaining = user.calorie_target - stats["calories"]
    await callback.message.edit_text(
        f"Добавлено ✅\n\nСъедено: <b>{stats['calories']} / {user.calorie_target} ккал</b>\n"
        f"Осталось: <b>{remaining} ккал</b>\nБелки: {stats['protein']} / {user.protein_target_g} г"
    )
    await callback.message.answer("Что дальше?", reply_markup=MAIN_MENU)
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.message.answer("Главное меню", reply_markup=MAIN_MENU)
    await callback.answer()


@router.message(F.text == "⚖️ Записать вес")
async def weight_start(message: Message, state: FSMContext) -> None:
    await state.set_state(WeightFlow.waiting_weight)
    await message.answer("Какой сейчас вес? Например: <b>81.7</b>")


@router.message(WeightFlow.waiting_weight)
async def weight_save(message: Message, state: FSMContext) -> None:
    weight = _num(message.text)
    if not weight or not 35 <= weight <= 350:
        await message.answer("Введи вес числом, например: 81.7")
        return
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            await state.clear()
            await start(message, state)
            return
        await add_weight(session, user, weight)
        initial = user.initial_weight_kg
    await state.clear()
    await message.answer(
        f"Записал ✅\n\nТекущий вес: <b>{weight:.1f} кг</b>\nНачальный: {initial:.1f} кг\nИзменение: {weight - initial:+.1f} кг",
        reply_markup=MAIN_MENU,
    )


@router.message(F.text == "🏃 Активность")
async def activity_start(message: Message, state: FSMContext) -> None:
    await state.set_state(ActivityFlow.waiting_steps)
    await message.answer("Сколько шагов сегодня прошёл?")


@router.message(ActivityFlow.waiting_steps)
async def activity_steps(message: Message, state: FSMContext) -> None:
    steps = _num(message.text)
    if steps is None or not 0 <= steps <= 100000:
        await message.answer("Введи количество шагов числом, например: 8500")
        return
    await state.update_data(steps=int(steps))
    await message.answer("Была тренировка?", reply_markup=WORKOUT_KB)


@router.callback_query(F.data.startswith("workout:"))
async def activity_workout(callback: CallbackQuery, state: FSMContext) -> None:
    workout = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if workout == "none":
        await _save_activity(callback.message, callback.from_user.id, state, data.get("steps", 0), None, 0)
    else:
        await state.update_data(workout_type=workout)
        await state.set_state(ActivityFlow.waiting_duration)
        await callback.message.edit_text("Сколько примерно минут длилась тренировка?")
    await callback.answer()


@router.message(ActivityFlow.waiting_duration)
async def activity_duration(message: Message, state: FSMContext) -> None:
    minutes = _num(message.text)
    if minutes is None or not 1 <= minutes <= 600:
        await message.answer("Введи длительность в минутах, например: 60")
        return
    data = await state.get_data()
    await _save_activity(message, message.from_user.id, state, data.get("steps", 0), data.get("workout_type"), int(minutes))


async def _save_activity(message: Message, tg_id: int, state: FSMContext, steps: int, workout: str | None, minutes: int) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, tg_id)
        if not user:
            await state.clear()
            return
        await upsert_activity(session, user, date.today(), steps, workout, minutes)
        stats = await day_stats(session, user, date.today())
    await state.clear()
    await message.answer(
        f"Активность записана ✅\n\n🚶 {steps} шагов\n🔥 Оценочный расход сегодня: ~{stats['expenditure']} ккал",
        reply_markup=MAIN_MENU,
    )


@router.message(F.text == "👤 Профиль")
async def profile(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
    if not user:
        return
    target = f"{user.target_weight_kg:.1f} кг" if user.target_weight_kg else "не задана"
    left = f"\nДо цели: {user.current_weight_kg - user.target_weight_kg:.1f} кг" if user.target_weight_kg else ""
    await message.answer(
        f"👤 <b>{user.name or 'Профиль'}</b>\n\nРост: {user.height_cm:.0f} см\nНачальный вес: {user.initial_weight_kg:.1f} кг\n"
        f"Текущий вес: {user.current_weight_kg:.1f} кг\nЦель: {target}\nПрогресс: {user.current_weight_kg - user.initial_weight_kg:+.1f} кг{left}\n\n"
        f"Дневная цель: {user.calorie_target} ккал",
        reply_markup=PROFILE_KB,
    )


@router.callback_query(F.data.startswith("stats:"))
async def profile_stats(callback: CallbackQuery) -> None:
    days = int(callback.data.split(":", 1)[1])
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        stats = await stats_period(session, user, days) if user else None
    if not user or not stats:
        await callback.answer()
        return
    await callback.message.answer(
        f"📊 <b>Последние {days} дней</b>\n\nСредние калории: {stats['avg_calories']} ккал\n"
        f"Средний расчётный дефицит: ~{stats['avg_deficit']} ккал/день\nСредние шаги: {stats['avg_steps']}\n"
        f"Белок: {stats['avg_protein']} г/день\n\nВес: {stats['first_weight']:.1f} → {stats['last_weight']:.1f} кг "
        f"({stats['weight_change']:+.1f} кг)\nДней с данными: {stats['tracked_days']}\nВ районе цели по калориям: {stats['in_target_days']}"
    )
    await callback.answer()


@router.callback_query(F.data == "profile:settings")
async def profile_settings(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
    if not user:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎯 Калории: {user.calorie_target}", callback_data="settings:calories")],
        [InlineKeyboardButton(text=f"⚖️ Цель веса: {user.target_weight_kg or '—'}", callback_data="settings:target_weight")],
        [InlineKeyboardButton(text=f"🌙 Вечерний опрос: {'вкл' if user.evening_poll_enabled else 'выкл'}", callback_data="settings:evening")],
        [InlineKeyboardButton(text=f"🧾 Итог дня: {'вкл' if user.daily_summary_enabled else 'выкл'}", callback_data="settings:summary")],
    ])
    await callback.message.answer("⚙️ <b>Настройки</b>", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "settings:calories")
async def settings_calories(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsFlow.calorie_target)
    await callback.message.answer("Введи новую дневную цель калорий.")
    await callback.answer()


@router.message(SettingsFlow.calorie_target)
async def settings_calories_save(message: Message, state: FSMContext) -> None:
    value = _num(message.text)
    if not value or not 800 <= value <= 6000:
        await message.answer("Введи значение от 800 до 6000 ккал.")
        return
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            await state.clear()
            return
        if user.age <= 18 and value < user.maintenance_calories:
            await message.answer(
                f"Для пользователей до 19 лет я не ставлю автоматическую цель ниже "
                f"оценочной потребности поддержания ({user.maintenance_calories} ккал). "
                "Можно продолжать вести дневник по этому ориентиру."
            )
            return
        user.calorie_target = int(value)
        user.calorie_target_manual = True
        await session.commit()
    await state.clear()
    await message.answer(f"Дневная цель изменена: <b>{int(value)} ккал</b>", reply_markup=MAIN_MENU)


@router.callback_query(F.data == "settings:target_weight")
async def settings_target(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsFlow.target_weight)
    await callback.message.answer("Введи новую цель по весу в кг.")
    await callback.answer()


@router.message(SettingsFlow.target_weight)
async def settings_target_save(message: Message, state: FSMContext) -> None:
    value = _num(message.text)
    if not value or not 35 <= value <= 350:
        await message.answer("Введи вес числом, например: 75")
        return
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            await state.clear()
            return
        user.target_weight_kg = value
        targets = calculate_targets(
            user.sex,
            user.age,
            user.height_cm,
            user.current_weight_kg,
            user.activity_level,
            user.goal,
            user.target_weight_kg,
        )
        user.maintenance_calories = targets.maintenance
        user.protein_target_g = targets.protein_g
        if not user.calorie_target_manual:
            user.calorie_target = targets.calories
        await session.commit()
    await state.clear()
    await message.answer(
        f"Цель по весу изменена: <b>{value:.1f} кг</b>\n"
        f"Белковый ориентир: ~{user.protein_target_g} г/день",
        reply_markup=MAIN_MENU,
    )


@router.callback_query(F.data.in_({"settings:evening", "settings:summary"}))
async def settings_toggle(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        if not user:
            return
        if callback.data.endswith("evening"):
            user.evening_poll_enabled = not user.evening_poll_enabled
            value = user.evening_poll_enabled
            label = "Вечерний опрос"
        else:
            user.daily_summary_enabled = not user.daily_summary_enabled
            value = user.daily_summary_enabled
            label = "Итог дня"
        await session.commit()
    await callback.message.answer(f"{label}: {'включён' if value else 'выключен'}")
    await callback.answer()


@router.message(F.text == "📅 Календарь")
async def show_calendar(message: Message) -> None:
    today = date.today()
    await message.answer("📅 Дневник по дням", reply_markup=await _calendar_markup(message.from_user.id, today.year, today.month))


@router.callback_query(F.data.startswith("cal:"))
async def calendar_nav(callback: CallbackQuery) -> None:
    ym = callback.data.split(":", 1)[1]
    year, month = map(int, ym.split("-"))
    await callback.message.edit_reply_markup(reply_markup=await _calendar_markup(callback.from_user.id, year, month))
    await callback.answer()


@router.callback_query(F.data.startswith("day:"))
async def calendar_day(callback: CallbackQuery) -> None:
    day = date.fromisoformat(callback.data.split(":", 1)[1])
    await _show_day(callback.message, callback.from_user.id, day)
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(F.text == "🍳 Что приготовить")
async def recipe_start(message: Message, state: FSMContext) -> None:
    await state.set_state(RecipeFlow.waiting_ingredients)
    await message.answer(
        "Из чего будем готовить?\n\n"
        "Напиши продукты, которые есть дома, или отправь 📸 фото продуктов/холодильника."
    )


@router.message(RecipeFlow.waiting_ingredients)
async def recipe_ingredients(message: Message, state: FSMContext) -> None:
    if message.photo:
        await message.answer("Смотрю, какие продукты есть на фото…")
        image = await _get_image(message.bot, message.photo[-1].file_id)
        pantry = await analyze_pantry_photo(image)

        if not pantry.ingredients:
            await message.answer(
                "Не смог уверенно распознать продукты на этом фото. "
                "Попробуй снять их чуть ближе или напиши список текстом."
            )
            return

        ingredients = ", ".join(pantry.ingredients)
        await state.update_data(recipe_ingredients=ingredients)
        await state.set_state(RecipeFlow.waiting_preference)
        note = f"\n\n<i>{pantry.notes}</i>" if pantry.notes else ""
        await message.answer(
            f"На фото вижу: <b>{ingredients}</b>{note}\n\nЧто важнее?",
            reply_markup=RECIPE_PREF_KB,
        )
        return

    if not message.text:
        await message.answer("Напиши список продуктов или отправь их фото 📸")
        return

    if is_photo_question(message.text):
        await message.answer(
            "Да 👍 Отправь фото продуктов, холодильника или того, что лежит на столе — "
            "я попробую распознать продукты и предложу рецепты."
        )
        return

    if looks_like_question(message.text):
        await message.answer(
            "Сейчас я жду именно список продуктов или их фото. "
            "Например: <b>яйца, сыр, макароны, помидоры</b>.\n\n"
            "Фото тоже можно отправить 📸"
        )
        return

    ingredients = message.text.strip()
    if len(ingredients) < 2:
        await message.answer("Напиши хотя бы один продукт или отправь фото 📸")
        return

    await state.update_data(recipe_ingredients=ingredients)
    await state.set_state(RecipeFlow.waiting_preference)
    await message.answer("Что важнее?", reply_markup=RECIPE_PREF_KB)


@router.callback_query(F.data.startswith("recipe_pref:"))
async def recipe_preference(callback: CallbackQuery, state: FSMContext) -> None:
    pref = callback.data.split(":", 1)[1]
    data = await state.get_data()
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        today_stats = await day_stats(session, user, date.today()) if user else None
    if not user or not today_stats:
        return
    left = max(200, user.calorie_target - today_stats["calories"])
    protein_left = max(0, user.protein_target_g - today_stats["protein"])
    await callback.message.answer("Подбираю варианты…")
    items = await recipes(
        data["recipe_ingredients"],
        pref,
        left,
        protein_left,
        profile_context=_nutrition_profile_context(user),
    )
    payload = [x.model_dump() for x in items]
    await state.update_data(recipes=payload, recipe_pref=pref)
    await state.set_state(RecipeFlow.choosing_recipe)
    lines = [f"У тебя осталось примерно <b>{left} ккал</b>.\n"]
    for idx, item in enumerate(payload, 1):
        lines.append(f"<b>{idx}. {item['title']}</b>\n~{item['calories']} ккал · {item['protein_g']} г белка · {item['minutes']} мин\n")
    await callback.message.answer("\n".join(lines), reply_markup=recipe_choices(len(payload)))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^recipe:(?:\d+|more)$"))
async def recipe_pick(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if value == "more":
        async with SessionLocal() as session:
            user = await get_user(session, callback.from_user.id)
            stats = await day_stats(session, user, date.today()) if user else None
        if not user or not stats:
            return
        items = await recipes(
            data["recipe_ingredients"],
            data.get("recipe_pref", "tasty"),
            max(200, user.calorie_target - stats["calories"]),
            max(0, user.protein_target_g - stats["protein"]),
            profile_context=_nutrition_profile_context(user),
        )
        payload = [x.model_dump() for x in items]
        await state.update_data(recipes=payload)
        await callback.message.answer("Ещё варианты:", reply_markup=recipe_choices(len(payload)))
        await callback.answer()
        return
    idx = int(value)
    items = data.get("recipes", [])
    if idx >= len(items):
        await callback.answer("Рецепт не найден", show_alert=True)
        return
    item = items[idx]
    await state.update_data(selected_recipe=idx)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Приготовил", callback_data="recipe:cooked")],
        [InlineKeyboardButton(text="🔄 Другой рецепт", callback_data="recipe:another")],
    ])
    text = [f"🍳 <b>{item['title']}</b>\n", "<b>Тебе понадобится:</b>"]
    text.extend(f"• {x}" for x in item["ingredients"])
    text.append("")
    text.extend(f"{i}. {step}" for i, step in enumerate(item["steps"], 1))
    text.append(f"\n~{item['calories']} ккал · {item['protein_g']} г белка")
    await callback.message.answer("\n".join(text), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "recipe:another")
async def recipe_another(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.message.answer("Выбери другой вариант:", reply_markup=recipe_choices(len(data.get("recipes", []))))
    await callback.answer()


@router.callback_query(F.data == "recipe:cooked")
async def recipe_cooked(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("selected_recipe")
    items = data.get("recipes", [])
    if idx is None or idx >= len(items):
        await callback.answer("Сначала выбери рецепт", show_alert=True)
        return
    item = items[idx]
    draft = {
        "title": item["title"],
        "total_grams": None,
        "calories": item["calories"],
        "protein_g": item["protein_g"],
        "fat_g": 0,
        "carbs_g": 0,
        "source": "recipe",
        "details_json": json.dumps(item, ensure_ascii=False),
    }
    async with SessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        await add_food(session, user, draft)
        stats = await day_stats(session, user, date.today())
    await state.clear()
    await callback.message.answer(f"Добавил блюдо в рацион ✅\nСегодня: {stats['calories']} / {user.calorie_target} ккал", reply_markup=MAIN_MENU)
    await callback.answer()


@router.message(F.photo)
async def direct_photo(message: Message, state: FSMContext) -> None:
    if await _registered(message):
        await _analyze_message_food(message, state)


@router.message(F.text)
async def natural_text(message: Message, state: FSMContext) -> None:
    if not await _registered(message):
        await start(message, state)
        return
    text = message.text.lower().strip()

    weight_match = re.search(r"(?:запиши\s+вес|вес)\D*(\d+(?:[.,]\d+)?)", text)
    if weight_match:
        weight = float(weight_match.group(1).replace(",", "."))
        async with SessionLocal() as session:
            user = await get_user(session, message.from_user.id)
            await add_weight(session, user, weight)
        await message.answer(f"Вес записан ✅ {weight:.1f} кг", reply_markup=MAIN_MENU)
        return

    steps_match = re.search(r"(?:прош[её]л|шаг(?:ов|а)?)\D*(\d{2,6})", text)
    if steps_match:
        steps = int(steps_match.group(1))
        async with SessionLocal() as session:
            user = await get_user(session, message.from_user.id)
            await upsert_activity(session, user, date.today(), steps)
        await message.answer(f"Записал ✅ {steps} шагов", reply_markup=MAIN_MENU)
        return

    if "сколько" in text and "калор" in text and any(x in text for x in ["остал", "можно", "съесть"]):
        async with SessionLocal() as session:
            user = await get_user(session, message.from_user.id)
            stats = await day_stats(session, user, date.today())
        await message.answer(f"Сегодня съедено {stats['calories']} / {user.calorie_target} ккал. Осталось примерно <b>{user.calorie_target - stats['calories']} ккал</b>.")
        return

    if any(x in text for x in ["что сегодня ел", "что я сегодня ел", "сегодня съел"]):
        await _show_day(message, message.from_user.id, date.today())
        return

    await _analyze_message_food(message, state)
