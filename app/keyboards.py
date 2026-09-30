from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍽 Добавить еду"), KeyboardButton(text="🍳 Что приготовить")],
        [KeyboardButton(text="📅 Календарь"), KeyboardButton(text="🏃 Активность")],
        [KeyboardButton(text="⚖️ Записать вес"), KeyboardButton(text="👤 Профиль")],
    ],
    resize_keyboard=True,
)

SEX_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Мужской", callback_data="sex:male"),
    InlineKeyboardButton(text="Женский", callback_data="sex:female"),
]])

ACTIVITY_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛋 Почти не двигаюсь", callback_data="activity:low")],
    [InlineKeyboardButton(text="🚶 Немного двигаюсь", callback_data="activity:light")],
    [InlineKeyboardButton(text="🏃 Средняя активность", callback_data="activity:medium")],
    [InlineKeyboardButton(text="💪 Очень активный", callback_data="activity:high")],
])

GOAL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔥 Похудеть", callback_data="goal:lose")],
    [InlineKeyboardButton(text="⚖️ Поддерживать вес", callback_data="goal:maintain")],
    [InlineKeyboardButton(text="💪 Набрать вес", callback_data="goal:gain")],
])

FOOD_START_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📸 Фото", callback_data="food:photo"), InlineKeyboardButton(text="✍️ Текстом", callback_data="food:text")],
    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
])

FOOD_REVIEW_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Всё верно", callback_data="food:recognized_ok"),
    InlineKeyboardButton(text="✏️ Уточнить", callback_data="food:clarify"),
]])

WEIGHT_PRESETS_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="150 г", callback_data="portion:150"), InlineKeyboardButton(text="200 г", callback_data="portion:200")],
    [InlineKeyboardButton(text="250 г", callback_data="portion:250"), InlineKeyboardButton(text="300 г", callback_data="portion:300")],
    [InlineKeyboardButton(text="✏️ Ввести самому", callback_data="portion:custom")],
])

FOOD_CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Добавить", callback_data="food:save"), InlineKeyboardButton(text="✏️ Изменить", callback_data="food:clarify")],
    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
])

WORKOUT_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Нет", callback_data="workout:none")],
    [InlineKeyboardButton(text="🏋️ Силовая", callback_data="workout:strength"), InlineKeyboardButton(text="🏃 Бег", callback_data="workout:run")],
    [InlineKeyboardButton(text="🚴 Велосипед", callback_data="workout:bike"), InlineKeyboardButton(text="⚽ Другая", callback_data="workout:other")],
])

RECIPE_PREF_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="😋 Самое вкусное", callback_data="recipe_pref:tasty"), InlineKeyboardButton(text="⚡ Быстро", callback_data="recipe_pref:fast")],
    [InlineKeyboardButton(text="🥩 Больше белка", callback_data="recipe_pref:protein"), InlineKeyboardButton(text="🔥 Поменьше калорий", callback_data="recipe_pref:light")],
    [InlineKeyboardButton(text="💸 Подешевле", callback_data="recipe_pref:cheap")],
])

PROFILE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📊 7 дней", callback_data="stats:7"), InlineKeyboardButton(text="📊 30 дней", callback_data="stats:30")],
    [InlineKeyboardButton(text="⚙️ Настройки", callback_data="profile:settings")],
])


def recipe_choices(count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i in range(count):
        builder.button(text=f"{i + 1}️⃣ Вариант {i + 1}", callback_data=f"recipe:{i}")
    builder.button(text="🔄 Ещё варианты", callback_data="recipe:more")
    builder.adjust(1)
    return builder.as_markup()
