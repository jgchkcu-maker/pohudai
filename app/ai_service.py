from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections import OrderedDict

from google import genai
from google.genai import types
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.nutrition_sources import (
    collect_nutrition_evidence,
    load_cached_nutrition,
    nutrition_lookup_key,
    save_cached_nutrition,
)


openai_client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
gemini_client = genai.Client(api_key=settings.gemini_api_key) if settings.gemini_api_key else None

logger = logging.getLogger(__name__)

PHOTO_DETECT_TIMEOUT_SECONDS = 18
PHOTO_SEARCH_TIMEOUT_SECONDS = 30
PHOTO_SEARCH_ATTEMPTS = 2
PHOTO_SEARCH_RETRY_DELAYS_SECONDS = (3,)


NUTRITION_COACH_SYSTEM = """
Ты — помощник дневника питания. Ты не назначаешь лечение и не заменяешь врача.

Жёсткие правила:
1. Числа maintenance_calories, calorie_target и protein_target_g приходят из детерминированного расчётного движка. Не пересчитывай и не изменяй их сам.
2. Не обещай точность расхода энергии: называй его оценочным.
3. Для пользователей 18 лет и младше не поощряй большие дефициты, голодание, пропуск еды или быстрый сброс веса. Их calorie_target является ориентиром для ведения дневника, а не назначением на похудение.
4. Не подгоняй оценку калорий конкретного блюда под вес, возраст или цель пользователя: состав еды оценивается независимо.
5. В рекомендациях отдавай приоритет обычной еде, достаточному белку, овощам/фруктам и реалистичным порциям. Не делай моральных оценок еды.
6. Если данных мало, прямо говори об неопределённости вместо выдумывания точности.
"""


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
    nutrition_source: str | None = None
    nutrition_confidence: float | None = Field(default=None, ge=0, le=1)


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


class PhotoFoodComponent(StrictModel):
    component_id: str
    name: str
    brand: str | None = None
    preparation: str | None = None
    estimated_grams: float = Field(ge=0.1)
    confidence: float = Field(ge=0, le=1)


class PhotoFoodDetection(StrictModel):
    title: str
    components: list[PhotoFoodComponent]
    confidence: float = Field(ge=0, le=1)
    notes: str | None = None


class NutritionReference(StrictModel):
    component_id: str
    name: str
    calories_100g: float = Field(ge=0)
    protein_g_100g: float = Field(ge=0)
    fat_g_100g: float = Field(ge=0)
    carbs_g_100g: float = Field(ge=0)
    source: str
    confidence: float = Field(ge=0, le=1)


class NutritionReferenceBatch(StrictModel):
    items: list[NutritionReference]


class NutritionSearchUnavailable(RuntimeError):
    """Raised when nutrition could not be verified from external sources."""


class NutritionSearchQuotaExceeded(NutritionSearchUnavailable):
    """Raised when the Gemini Google Search quota/billing does not allow search."""


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


PHOTO_CACHE_MAX = 128
_photo_food_cache: OrderedDict[str, tuple[PhotoFoodDetection, NutritionReferenceBatch]] = OrderedDict()
_photo_latest_key_by_hash: dict[str, str] = {}


def _food_schema() -> dict:
    return FoodAnalysis.model_json_schema()


def _photo_detection_schema() -> dict:
    return PhotoFoodDetection.model_json_schema()


def _nutrition_reference_schema() -> dict:
    return NutritionReferenceBatch.model_json_schema()


def _recipe_schema() -> dict:
    return RecipeSet.model_json_schema()


def _pantry_schema() -> dict:
    return PantryAnalysis.model_json_schema()


def _photo_cache_ids(image_bytes: bytes, clarification: str | None) -> tuple[str, str]:
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    clarification_hash = hashlib.sha256((clarification or "").strip().lower().encode("utf-8")).hexdigest()[:16]
    return image_hash, f"{image_hash}:{clarification_hash}"


def _photo_cache_get(key: str) -> tuple[PhotoFoodDetection, NutritionReferenceBatch] | None:
    value = _photo_food_cache.get(key)
    if value is not None:
        _photo_food_cache.move_to_end(key)
    return value


def _photo_cache_put(
    image_hash: str,
    key: str,
    value: tuple[PhotoFoodDetection, NutritionReferenceBatch],
) -> None:
    _photo_food_cache[key] = value
    _photo_food_cache.move_to_end(key)
    _photo_latest_key_by_hash[image_hash] = key
    while len(_photo_food_cache) > PHOTO_CACHE_MAX:
        old_key, _ = _photo_food_cache.popitem(last=False)
        old_image_hash = old_key.split(":", 1)[0]
        if _photo_latest_key_by_hash.get(old_image_hash) == old_key:
            _photo_latest_key_by_hash.pop(old_image_hash, None)


async def _openai_json_response(
    prompt: str,
    schema: dict,
    image_bytes: bytes | None = None,
    system_instruction: str | None = None,
) -> dict:
    if openai_client is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    content: list[dict] = [{"type": "input_text", "text": prompt}]
    if image_bytes is not None:
        data = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{data}"})

    messages: list[dict] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": content})

    response = await openai_client.responses.create(
        model=settings.openai_model,
        input=messages,
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


async def _gemini_json_response(
    prompt: str,
    schema: dict,
    image_bytes: bytes | None = None,
    system_instruction: str | None = None,
    use_google_search: bool = False,
) -> dict:
    if gemini_client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    contents: list[object] = [prompt]
    if image_bytes is not None:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

    if use_google_search:
        # Use the native GenerateContentConfig shape for the google-genai SDK.
        # Gemini 3.x supports Google Search grounding together with structured JSON output.
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[
                types.Tool(
                    google_search=types.GoogleSearch(),
                )
            ],
            response_mime_type="application/json",
            response_json_schema=schema,
        )
    else:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_json_schema=schema,
        )

    response = await gemini_client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=contents,
        config=config,
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty response")
    return json.loads(response.text)


async def _json_response(
    prompt: str,
    schema: dict,
    image_bytes: bytes | None = None,
    system_instruction: str | None = None,
) -> dict:
    provider = settings.ai_provider.strip().lower()

    if provider == "gemini":
        return await _gemini_json_response(prompt, schema, image_bytes, system_instruction)

    if provider == "openai":
        return await _openai_json_response(prompt, schema, image_bytes, system_instruction)

    if provider == "auto":
        if gemini_client is not None:
            return await _gemini_json_response(prompt, schema, image_bytes, system_instruction)
        if openai_client is not None:
            return await _openai_json_response(prompt, schema, image_bytes, system_instruction)
        raise RuntimeError("Neither GEMINI_API_KEY nor OPENAI_API_KEY is configured")

    raise RuntimeError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")


def _gemini_photo_pipeline_enabled() -> bool:
    provider = settings.ai_provider.strip().lower()
    return provider == "gemini" or (provider == "auto" and gemini_client is not None)


async def _detect_food_photo(
    image_bytes: bytes,
    clarification: str | None = None,
) -> PhotoFoodDetection:
    clarification_text = (
        f"\nУточнение пользователя, которому нужно доверять при разборе фото: {clarification}"
        if clarification
        else ""
    )
    prompt = f"""
Проанализируй фотографию еды. Это ПЕРВЫЙ этап: только визуальное распознавание, без расчёта калорий и КБЖУ.{clarification_text}

Нужно найти ВСЕ видимые съедобные компоненты за один проход.
Правила:
- не объединяй явно разные видимые продукты в один компонент: отдельно гарнир, мясо/рыбу, овощи, сыр, соусы, хлеб, напитки и заметные добавки;
- для однородного составного блюда, где ингредиенты визуально неотделимы (суп, пюре, запеканка, паста уже в соусе), не выдумывай скрытый рецепт: опиши блюдо как один компонент или только действительно различимые части;
- не придумывай масло, сахар, сливки и другие невидимые ингредиенты;
- если видна упаковка или читаемый бренд, укажи точный бренд/вариант; если не виден — brand=null;
- preparation укажи только когда способ приготовления визуально понятен (варёное, жареное, запечённое и т.п.);
- estimated_grams — примерная видимая масса каждого компонента. Это нужно только как внутреннее соотношение порций для последующего пересчёта по реальному весу пользователя; не изображай лабораторную точность;
- component_id должны быть уникальными: c1, c2, c3...;
- confidence отражает уверенность именно в распознавании компонента.

Не считай и не возвращай калории, белки, жиры или углеводы.
"""
    try:
        payload = await asyncio.wait_for(
            _gemini_json_response(
                prompt,
                _photo_detection_schema(),
                image_bytes=image_bytes,
            ),
            timeout=PHOTO_DETECT_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise RuntimeError("Photo recognition timed out") from exc
    return PhotoFoodDetection.model_validate(payload)


def _response_field(value: object, *names: str):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        attr = getattr(value, name, None)
        if attr is not None:
            return attr
    return None


def _google_interaction_evidence(interaction) -> tuple[list[str], int]:
    steps = getattr(interaction, "steps", None) or []
    queries: list[str] = []
    successful_results = 0

    for step in steps:
        step_type = getattr(step, "type", None)
        if step_type == "google_search_call":
            arguments = getattr(step, "arguments", None)
            raw_queries = getattr(arguments, "queries", None) or []
            queries.extend(str(query) for query in raw_queries if query)
        elif step_type == "google_search_result":
            if not getattr(step, "is_error", False):
                successful_results += 1

    return queries, successful_results


def _is_google_search_quota_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "429" in message
        or "resource_exhausted" in message
        or "current quota" in message
        or "rate limit" in message
    )


def _is_transient_google_search_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "500 internal",
            "503",
            "unavailable",
            "504",
            "deadline_exceeded",
        )
    )


async def _gemini_forced_google_search_json(
    prompt: str,
    schema: dict,
) -> tuple[dict, list[str], int]:
    if gemini_client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    def _run():
        return gemini_client.interactions.create(
            model=settings.gemini_model,
            input=prompt,
            tools=[
                {
                    "type": "google_search",
                    "search_types": ["web_search"],
                }
            ],
            generation_config={
                "tool_choice": "any",
            },
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
        )

    interaction = await asyncio.to_thread(_run)
    output_text = getattr(interaction, "output_text", None)
    if not output_text:
        raise RuntimeError("Gemini Interactions returned an empty response")

    queries, successful_results = _google_interaction_evidence(interaction)
    return json.loads(output_text), queries, successful_results


async def _lookup_photo_nutrition_google(detection: PhotoFoodDetection) -> NutritionReferenceBatch:
    detected_json = json.dumps(
        [item.model_dump() for item in detection.components],
        ensure_ascii=False,
    )
    prompt = f"""
Это отдельный поисковый этап после распознавания фотографии еды.
Ниже уже распознанные компоненты:
{detected_json}

СНАЧАЛА обязательно выполни Google Search через доступный тебе инструмент.
Не отвечай по памяти модели.

Для КАЖДОГО component_id найди пищевую ценность на 100 г и верни ровно один результат с тем же component_id.

Приоритет:
1. официальный сайт производителя / точная карточка брендированного товара;
2. государственные и национальные базы состава продуктов;
3. крупные ритейлеры и надёжные базы продуктов.

Правила:
- бренд и вариант продукта должны совпадать максимально точно;
- для обычной еды учитывай preparation;
- нормализуй всё к 100 г;
- estimated_grams не используй для выбора КБЖУ;
- source должен содержать найденный источник/домен, а не фразу вроде "по памяти";
- если точного товара нет, выбери ближайший реально найденный аналог и снизь confidence;
- не пропускай component_id.
"""

    last_error: Exception | None = None

    for attempt in range(PHOTO_SEARCH_ATTEMPTS):
        if attempt:
            await asyncio.sleep(PHOTO_SEARCH_RETRY_DELAYS_SECONDS[attempt - 1])

        try:
            payload, queries, successful_results = await asyncio.wait_for(
                _gemini_forced_google_search_json(
                    prompt,
                    _nutrition_reference_schema(),
                ),
                timeout=PHOTO_SEARCH_TIMEOUT_SECONDS,
            )

            if not queries or successful_results < 1:
                raise NutritionSearchUnavailable(
                    "Interactions API returned no successful Google Search call/result"
                )

            batch = NutritionReferenceBatch.model_validate(payload)
            expected_ids = {item.component_id for item in detection.components}
            returned_ids = {item.component_id for item in batch.items}
            if expected_ids != returned_ids:
                raise NutritionSearchUnavailable(
                    f"Google Search returned incomplete component ids: expected={expected_ids}, got={returned_ids}"
                )

            for item in batch.items:
                source = item.source.strip()
                item.source = (
                    f"Google Search: {source}"
                    if source
                    else "Google Search"
                )

            logger.info(
                "Forced Google Search nutrition lookup succeeded: attempt=%s queries=%s",
                attempt + 1,
                queries,
            )
            return batch

        except Exception as exc:
            last_error = exc

            if _is_google_search_quota_error(exc):
                logger.error(
                    "Google Search quota/billing rejected nutrition lookup: %s",
                    exc,
                    exc_info=True,
                )
                raise NutritionSearchQuotaExceeded(
                    "Google Search quota is exhausted or billing is not enabled"
                ) from exc

            logger.warning(
                "Forced Google Search nutrition lookup attempt %s/%s failed: %s",
                attempt + 1,
                PHOTO_SEARCH_ATTEMPTS,
                exc,
                exc_info=True,
            )

            if not _is_transient_google_search_error(exc):
                break

    raise NutritionSearchUnavailable(
        "Не удалось получить подтверждённые КБЖУ через принудительный Google Search"
    ) from last_error


async def _select_nutrition_from_evidence(
    components: list[PhotoFoodComponent],
    evidence_by_component: dict[str, list[dict]],
) -> NutritionReferenceBatch:
    evidence_payload: dict[str, list[dict]] = {}
    evidence_index: dict[str, dict] = {}

    for component in components:
        rows: list[dict] = []
        for candidate in evidence_by_component.get(component.component_id, []):
            row = dict(candidate)
            local_id = str(row.get("evidence_id") or "e")
            evidence_id = f"{component.component_id}:{local_id}"
            row["evidence_id"] = evidence_id
            evidence_index[evidence_id] = row
            rows.append(row)
        evidence_payload[component.component_id] = rows

    if not any(evidence_payload.values()):
        return NutritionReferenceBatch(items=[])

    detected_json = json.dumps(
        [item.model_dump() for item in components],
        ensure_ascii=False,
    )
    evidence_json = json.dumps(evidence_payload, ensure_ascii=False)

    prompt = f"""
Ты выбираешь КБЖУ ТОЛЬКО из внешних источников, уже полученных приложением.

Распознанные компоненты:
{detected_json}

Кандидаты из Open Food Facts / FatSecret / USDA / веб-поиска:
{evidence_json}

Правила:
- верни только те component_id, для которых есть достаточно надёжный кандидат;
- source ОБЯЗАТЕЛЬНО должен быть ТОЧНО равен evidence_id выбранного кандидата;
- для кандидатов, где уже есть calories_100g/protein_g_100g/fat_g_100g/carbs_g_100g, не меняй эти значения;
- для веб-кандидата без структурированных полей разрешено извлечь КБЖУ только если они явно написаны в title/snippet;
- если хотя бы одно из четырёх значений нельзя подтвердить из evidence, пропусти component_id;
- для брендированного продукта не подменяй его другим брендом без явного снижения confidence;
- не используй знания модели и ничего не додумывай.
"""

    payload = await _gemini_json_response(
        prompt,
        _nutrition_reference_schema(),
    )
    proposed = NutritionReferenceBatch.model_validate(payload)

    accepted: list[NutritionReference] = []
    seen_ids: set[str] = set()
    allowed_component_ids = {item.component_id for item in components}

    for item in proposed.items:
        if item.component_id not in allowed_component_ids or item.component_id in seen_ids:
            continue
        evidence = evidence_index.get(item.source)
        if evidence is None:
            continue

        direct_values = all(
            evidence.get(field) is not None
            for field in (
                "calories_100g",
                "protein_g_100g",
                "fat_g_100g",
                "carbs_g_100g",
            )
        )
        if direct_values:
            item.calories_100g = float(evidence["calories_100g"])
            item.protein_g_100g = float(evidence["protein_g_100g"])
            item.fat_g_100g = float(evidence["fat_g_100g"])
            item.carbs_g_100g = float(evidence["carbs_g_100g"])

        provider = str(evidence.get("source") or evidence.get("provider") or "external source")
        url = str(evidence.get("url") or "").strip()
        item.source = f"{provider} — {url}" if url else provider

        match_score = float(evidence.get("match_score") or 0.5)
        item.confidence = min(item.confidence, max(0.35, match_score))
        accepted.append(item)
        seen_ids.add(item.component_id)

    return NutritionReferenceBatch(items=accepted)


async def _lookup_photo_nutrition(detection: PhotoFoodDetection) -> NutritionReferenceBatch:
    cached_refs: list[NutritionReference] = []
    unresolved: list[PhotoFoodComponent] = []
    cache_keys: dict[str, str] = {}

    for component in detection.components:
        cache_key = nutrition_lookup_key(
            component.name,
            component.brand,
            component.preparation,
        )
        cache_keys[component.component_id] = cache_key
        cached = await load_cached_nutrition(cache_key)
        if cached is None:
            unresolved.append(component)
            continue
        try:
            ref = NutritionReference.model_validate(
                {
                    **cached,
                    "component_id": component.component_id,
                    "name": component.name,
                }
            )
        except Exception:
            unresolved.append(component)
            continue
        cached_refs.append(ref)

    evidence_by_component: dict[str, list[dict]] = {}
    if unresolved:
        evidence_groups = await asyncio.gather(
            *[
                collect_nutrition_evidence(
                    component.name,
                    component.brand,
                    component.preparation,
                )
                for component in unresolved
            ]
        )
        evidence_by_component = {
            component.component_id: evidence
            for component, evidence in zip(unresolved, evidence_groups)
        }

    selected = await _select_nutrition_from_evidence(unresolved, evidence_by_component)
    selected_by_id = {item.component_id: item for item in selected.items}

    still_missing = [
        component
        for component in unresolved
        if component.component_id not in selected_by_id
    ]

    google_refs: list[NutritionReference] = []
    if still_missing:
        # Final fallback: Gemini's native Google Search. This remains strict
        # search-or-fail and is only used after the structured databases/web API.
        fallback_detection = PhotoFoodDetection(
            title=detection.title,
            components=still_missing,
            confidence=detection.confidence,
            notes=detection.notes,
        )
        google_batch = await _lookup_photo_nutrition_google(fallback_detection)
        google_refs = google_batch.items

    all_refs = cached_refs + selected.items + google_refs
    refs_by_id = {item.component_id: item for item in all_refs}
    expected_ids = {item.component_id for item in detection.components}

    if set(refs_by_id) != expected_ids:
        missing = sorted(expected_ids - set(refs_by_id))
        raise NutritionSearchUnavailable(
            f"Nutrition sources did not resolve all components: {missing}"
        )

    for component in detection.components:
        ref = refs_by_id[component.component_id]
        if component.component_id not in {item.component_id for item in cached_refs}:
            payload = ref.model_dump()
            payload.pop("component_id", None)
            payload["name"] = component.name
            try:
                await save_cached_nutrition(
                    cache_keys[component.component_id],
                    " ".join(
                        part
                        for part in [component.brand, component.name, component.preparation]
                        if part
                    ),
                    payload,
                    ref.source,
                )
            except Exception:
                logger.warning(
                    "Failed to persist nutrition cache for %s",
                    component.component_id,
                    exc_info=True,
                )

    return NutritionReferenceBatch(
        items=[refs_by_id[component.component_id] for component in detection.components]
    )

def _compose_photo_food_analysis(
    detection: PhotoFoodDetection,
    nutrition: NutritionReferenceBatch,
    assumed_total_grams: float | None,
) -> FoodAnalysis:
    refs_by_id = {item.component_id: item for item in nutrition.items}
    estimated_total = sum(item.estimated_grams for item in detection.components)
    scale = (
        assumed_total_grams / estimated_total
        if assumed_total_grams is not None and estimated_total > 0
        else 1.0
    )

    components: list[FoodComponent] = []
    unresolved: list[str] = []
    lookup_confidences: list[float] = []

    for detected in detection.components:
        grams = detected.estimated_grams * scale
        reference = refs_by_id.get(detected.component_id)
        if reference is None:
            unresolved.append(detected.name)
            components.append(
                FoodComponent(
                    name=detected.name,
                    brand=detected.brand,
                    grams=round(grams, 1),
                    nutrition_confidence=0.0,
                )
            )
            continue

        lookup_confidences.append(reference.confidence)
        factor = grams / 100.0
        components.append(
            FoodComponent(
                name=detected.name,
                brand=detected.brand,
                grams=round(grams, 1),
                calories=round(reference.calories_100g * factor, 1),
                protein_g=round(reference.protein_g_100g * factor, 1),
                fat_g=round(reference.fat_g_100g * factor, 1),
                carbs_g=round(reference.carbs_g_100g * factor, 1),
                nutrition_source=reference.source,
                nutrition_confidence=reference.confidence,
            )
        )

    calories = round(sum(item.calories for item in components), 1)
    protein_g = round(sum(item.protein_g for item in components), 1)
    fat_g = round(sum(item.fat_g for item in components), 1)
    carbs_g = round(sum(item.carbs_g for item in components), 1)

    lookup_confidence = (
        sum(lookup_confidences) / len(lookup_confidences)
        if lookup_confidences
        else 0.0
    )
    confidence = max(0.0, min(1.0, min(detection.confidence, lookup_confidence)))

    notes: list[str] = []
    if detection.notes:
        notes.append(detection.notes)
    if assumed_total_grams is None:
        notes.append("КБЖУ сверены по внешним базам/веб-источникам; вес порции по фото пока приблизительный.")
    else:
        notes.append("КБЖУ сверены по внешним базам/веб-источникам и пересчитаны на указанный общий вес.")
    if unresolved:
        notes.append("Не удалось надёжно подобрать пищевую ценность для: " + ", ".join(unresolved) + ".")

    return FoodAnalysis(
        title=detection.title,
        total_grams=round(assumed_total_grams, 1) if assumed_total_grams is not None else None,
        calories=calories,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        confidence=confidence,
        components=components,
        needs_weight=assumed_total_grams is None,
        notes=" ".join(notes) if notes else None,
    )


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
    if not _gemini_photo_pipeline_enabled():
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

    image_hash, exact_key = _photo_cache_ids(image_bytes, clarification)

    # Когда пользователь после распознавания вводит общий вес, бот повторно вызывает
    # analyze_food_photo с тем же изображением. Берём последний разбор этого фото из
    # памяти и только масштабируем порцию — без дополнительных Gemini-запросов.
    if assumed_total_grams is not None and clarification is None:
        latest_key = _photo_latest_key_by_hash.get(image_hash)
        if latest_key:
            cached = _photo_cache_get(latest_key)
            if cached is not None:
                return _compose_photo_food_analysis(cached[0], cached[1], assumed_total_grams)

    cached = _photo_cache_get(exact_key)
    if cached is None:
        detection = await _detect_food_photo(image_bytes, clarification=clarification)
        nutrition = await _lookup_photo_nutrition(detection)
        cached = (detection, nutrition)
        _photo_cache_put(image_hash, exact_key, cached)
    else:
        # Этот exact-разбор сейчас показан пользователю; следующий ввод веса
        # должен масштабировать именно его, а не более старое уточнение того же фото.
        _photo_latest_key_by_hash[image_hash] = exact_key

    return _compose_photo_food_analysis(cached[0], cached[1], assumed_total_grams)


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


async def recipes(
    ingredients: str,
    preference: str,
    calories_left: int | None,
    protein_left: int,
    profile_context: str | None = None,
) -> list[Recipe]:
    pref_map = {
        "tasty": "самое вкусное",
        "fast": "максимально быстро",
        "protein": "больше белка",
        "light": "поменьше калорий",
        "cheap": "подешевле",
    }
    profile = f"\nКонтекст пользователя:\n{profile_context}" if profile_context else ""
    energy_context = (
        f"На сегодня осталось примерно {calories_left} ккал."
        if calories_left is not None
        else "Не используй суточный остаток калорий как ограничение: предложи обычную сбалансированную порцию."
    )
    prompt = f"""
Предложи ровно 3 простых домашних рецепта на русском языке.
Доступные продукты: {ingredients}.
Приоритет: {pref_map.get(preference, preference)}.
{energy_context} Желательно добрать около {protein_left} г белка.{profile}
Не требуй экзотических ингредиентов. Можно добавить базовые продукты вроде соли, воды и небольшого количества масла, но явно укажи их.
Калорийность и белок указывай для одной порции. Шаги короткие.
"""
    data = RecipeSet.model_validate(
        await _json_response(
            prompt,
            _recipe_schema(),
            system_instruction=NUTRITION_COACH_SYSTEM,
        )
    )
    return data.recipes[:3]
