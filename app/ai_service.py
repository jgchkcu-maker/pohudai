from __future__ import annotations

import base64
import json

from google import genai
from google.genai import types
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings


openai_client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
gemini_client = genai.Client(api_key=settings.gemini_api_key) if settings.gemini_api_key else None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FoodComponent(StrictModel):
    name: str
    brand: str | None = None
    grams: float | None = None
    calories: float = 0
    protein_g: float = 0
    fat_g: float = 0
    carbs_g: float = 0


class FoodAnalysis(StrictModel):
    title: str
    total_grams: float | None = None
    calories: float
    protein_g: float
    fat_g: float
    carbs_g: float
    confidence: float = Field(ge=0, le=1)
    components: list[FoodComponent]
    needs_weight: bool
    notes: str | None = None


class Recipe(StrictModel):
    title: str
    calories: int
    protein_g: int
    minutes: int
    ingredients: list[str]
    steps: list[str]


class RecipeSet(StrictModel):
    recipes: list[Recipe]


class PantryAnalysis(StrictModel):
    ingredients: list[str]
    confidence: float = Field(ge=0, le=1)
    notes: str | None = None


def _food_schema() -> dict:
    return FoodAnalysis.model_json_schema()


def _recipe_schema() -> dict:
    return RecipeSet.model_json_schema()


def _pantry_schema() -> dict:
    return PantryAnalysis.model_json_schema()


async def _openai_json_response(prompt: str, schema: dict, image_bytes: bytes | None = None) -> dict:
    if openai_client is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    content: list[dict] = [{"type": "input_text", "text": prompt}]
    if image_bytes is not None:
        data = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{data}"})

    response = await openai_client.responses.create(
        model=settings.openai_model,
        input=[{"role": "user", "content": content}],
        text={
            "format": {
                "type": "json_schema",
                "name": "structured_answer",
                "strict": True,
                "schema": schema,
            }
        },
    )
    return json.loads(response.output_text)


async def _gemini_json_response(prompt: str, schema: dict, image_bytes: bytes | None = None) -> dict:
    if gemini_client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    contents: list[object] = [prompt]
    if image_bytes is not None:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

    response = await gemini_client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema,
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty response")
    return json.loads(response.text)


async def _json_response(prompt: str, schema: dict, image_bytes: bytes | None = None) -> dict:
    provider = settings.ai_provider.strip().lower()

    if provider == "gemini":
        return await _gemini_json_response(prompt, schema, image_bytes)

    if provider == "openai":
        return await _openai_json_response(prompt, schema, image_bytes)

    if provider == "auto":
        if gemini_client is not None:
            return await _gemini_json_response(prompt, schema, image_bytes)
        if openai_client is not None:
            return await _openai_json_response(prompt, schema, image_bytes)
        raise RuntimeError("Neither GEMINI_API_KEY nor OPENAI_API_KEY is configured")

    raise RuntimeError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")


async def analyze_food_text(text: str, clarification: str | None = None) -> FoodAnalysis:
    context = f"\nУточнение пользователя: {clarification}" if clarification else ""
    prompt = f"""
Ты анализатор питания для Telegram-дневника. Пользователь написал: {text!r}.{context}
Верни реалистичную оценку калорий и КБЖУ. Учитывай указанные граммы, количество, бренд и способ приготовления.
Если точный бренд/этикетка неизвестны, не выдумывай, что нашёл официальный продукт: оцени как типичный аналог и напиши это в notes.
Если общий вес/количество недостаточны для разумной оценки порции, поставь total_grams=null и needs_weight=true.
Если вес компонентов указан, total_grams должен быть суммой известных весов, а needs_weight=false.
Название сделай коротким и человеческим. Все числа — для всей порции, не на 100 г.
"""
    return FoodAnalysis.model_validate(await _json_response(prompt, _food_schema()))


async def analyze_food_photo(
    image_bytes: bytes,
    clarification: str | None = None,
    assumed_total_grams: float | None = None,
) -> FoodAnalysis:
    extra = ""
    if clarification:
        extra += f" Уточнение пользователя: {clarification}."
    if assumed_total_grams:
        extra += f" Общий вес блюда считать примерно {assumed_total_grams} г и распределить между компонентами."

    prompt = f"""
Определи еду на фотографии для дневника питания.{extra}
Перечисли видимые компоненты и оцени КБЖУ. Не притворяйся, что знаешь точный состав, масло, соусы или скрытые ингредиенты: отражай неопределённость в notes и confidence.
Если общий вес не задан и по фото его нельзя надёжно знать, total_grams=null и needs_weight=true. Калории при этом дай как черновую оценку типичной видимой порции.
Все числа — для всей порции.
"""
    return FoodAnalysis.model_validate(
        await _json_response(prompt, _food_schema(), image_bytes=image_bytes)
    )


async def analyze_pantry_photo(image_bytes: bytes) -> PantryAnalysis:
    prompt = """
Посмотри на фотографию продуктов, холодильника, кухонного стола или кладовой.
Верни только те продукты и ингредиенты, которые действительно видны и достаточно узнаваемы.
Не выдумывай скрытые продукты и не превращай готовое блюдо в длинный список предполагаемых ингредиентов.
Названия делай короткими: например "яйца", "сыр", "помидоры", "куриная грудка".
Если продукт не удаётся уверенно определить, не добавляй его в список, а кратко упомяни неопределённость в notes.
"""
    return PantryAnalysis.model_validate(
        await _json_response(prompt, _pantry_schema(), image_bytes=image_bytes)
    )


async def recipes(ingredients: str, preference: str, calories_left: int, protein_left: int) -> list[Recipe]:
    pref_map = {
        "tasty": "самое вкусное",
        "fast": "максимально быстро",
        "protein": "больше белка",
        "light": "поменьше калорий",
        "cheap": "подешевле",
    }
    prompt = f"""
Предложи ровно 3 простых домашних рецепта на русском языке.
Доступные продукты: {ingredients}.
Приоритет: {pref_map.get(preference, preference)}.
На сегодня осталось примерно {calories_left} ккал и желательно добрать около {protein_left} г белка.
Не требуй экзотических ингредиентов. Можно добавить базовые продукты вроде соли, воды и небольшого количества масла, но явно укажи их.
Калорийность и белок указывай для одной порции. Шаги короткие.
"""
    data = RecipeSet.model_validate(await _json_response(prompt, _recipe_schema()))
    return data.recipes[:3]
