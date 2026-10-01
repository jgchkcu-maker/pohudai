from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import NutritionCache


logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 8.0
MAX_CANDIDATES_PER_PROVIDER = 5

_fatsecret_token: str | None = None
_fatsecret_token_expires_at = 0.0


def nutrition_lookup_key(name: str, brand: str | None, preparation: str | None) -> str:
    parts = [brand or "", name, preparation or ""]
    normalized = " | ".join(_normalize_text(part) for part in parts)
    return normalized[:500]


def _normalize_text(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _query_text(name: str, brand: str | None, preparation: str | None) -> str:
    return " ".join(part for part in [brand, name, preparation] if part).strip()


def _candidate_score(
    query_name: str,
    query_brand: str | None,
    candidate_name: str,
    candidate_brand: str | None,
) -> float:
    query = _normalize_text(" ".join(part for part in [query_brand, query_name] if part))
    candidate = _normalize_text(
        " ".join(part for part in [candidate_brand, candidate_name] if part)
    )
    if not query or not candidate:
        return 0.0

    sequence = SequenceMatcher(None, query, candidate).ratio()
    q_tokens = set(query.split())
    c_tokens = set(candidate.split())
    overlap = len(q_tokens & c_tokens) / max(1, len(q_tokens))
    score = 0.55 * sequence + 0.45 * overlap

    if query_brand:
        q_brand = _normalize_text(query_brand)
        c_brand = _normalize_text(candidate_brand or "")
        if q_brand and q_brand == c_brand:
            score += 0.18
        elif q_brand and q_brand in candidate:
            score += 0.10

    return min(1.0, score)


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _complete_macros(candidate: dict[str, Any]) -> bool:
    return all(
        _float(candidate.get(field)) is not None
        for field in ("calories_100g", "protein_g_100g", "fat_g_100g", "carbs_g_100g")
    )


def _trim_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates.sort(key=lambda item: float(item.get("match_score", 0)), reverse=True)
    return candidates[:MAX_CANDIDATES_PER_PROVIDER]


async def load_cached_nutrition(cache_key: str) -> dict[str, Any] | None:
    cutoff = datetime.utcnow() - timedelta(days=max(1, settings.nutrition_cache_days))
    async with SessionLocal() as session:
        result = await session.execute(
            select(NutritionCache).where(
                NutritionCache.cache_key == cache_key,
                NutritionCache.updated_at >= cutoff,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            return None
        payload["cache_hit"] = True
        return payload


async def save_cached_nutrition(
    cache_key: str,
    query: str,
    payload: dict[str, Any],
    source: str,
) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(NutritionCache).where(NutritionCache.cache_key == cache_key)
        )
        row = result.scalar_one_or_none()
        encoded = json.dumps(payload, ensure_ascii=False)
        now = datetime.utcnow()
        if row is None:
            session.add(
                NutritionCache(
                    cache_key=cache_key,
                    query=query[:500],
                    payload_json=encoded,
                    source=source[:255],
                    updated_at=now,
                )
            )
        else:
            row.query = query[:500]
            row.payload_json = encoded
            row.source = source[:255]
            row.updated_at = now
        await session.commit()


async def search_open_food_facts(
    name: str,
    brand: str | None,
    preparation: str | None,
) -> list[dict[str, Any]]:
    query = _query_text(name, brand, preparation)
    params = {
        "search_terms": query,
        "search_simple": "1",
        "action": "process",
        "json": "1",
        "page_size": "8",
        "fields": "code,product_name,brands,nutriments,url",
    }
    headers = {"User-Agent": "pohudai/1.0 nutrition-bot"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, headers=headers) as client:
            response = await client.get(
                "https://world.openfoodfacts.org/cgi/search.pl",
                params=params,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("Open Food Facts lookup failed for %r: %s", query, exc)
        return []

    candidates: list[dict[str, Any]] = []
    for product in data.get("products") or []:
        nutriments = product.get("nutriments") or {}
        calories = _float(
            nutriments.get("energy-kcal_100g")
            or nutriments.get("energy-kcal")
        )
        candidate = {
            "provider": "open_food_facts",
            "name": product.get("product_name") or "",
            "brand": product.get("brands") or None,
            "calories_100g": calories,
            "protein_g_100g": _float(nutriments.get("proteins_100g")),
            "fat_g_100g": _float(nutriments.get("fat_100g")),
            "carbs_g_100g": _float(nutriments.get("carbohydrates_100g")),
            "url": product.get("url")
            or (
                f"https://world.openfoodfacts.org/product/{product.get('code')}"
                if product.get("code")
                else None
            ),
        }
        if not candidate["name"] or not _complete_macros(candidate):
            continue
        candidate["match_score"] = round(
            _candidate_score(name, brand, candidate["name"], candidate["brand"]),
            3,
        )
        candidate["source"] = "Open Food Facts"
        candidates.append(candidate)

    return _trim_candidates(candidates)


def _usda_nutrients(food: dict[str, Any]) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        "calories_100g": None,
        "protein_g_100g": None,
        "fat_g_100g": None,
        "carbs_g_100g": None,
    }
    for nutrient in food.get("foodNutrients") or []:
        name = str(nutrient.get("nutrientName") or "").lower()
        unit = str(nutrient.get("unitName") or "").upper()
        value = _float(nutrient.get("value"))
        if value is None:
            continue
        if "energy" in name and unit == "KCAL" and result["calories_100g"] is None:
            result["calories_100g"] = value
        elif name == "protein" and result["protein_g_100g"] is None:
            result["protein_g_100g"] = value
        elif "total lipid" in name and result["fat_g_100g"] is None:
            result["fat_g_100g"] = value
        elif "carbohydrate" in name and result["carbs_g_100g"] is None:
            result["carbs_g_100g"] = value
    return result


async def search_usda(
    name: str,
    brand: str | None,
    preparation: str | None,
) -> list[dict[str, Any]]:
    if not settings.usda_api_key:
        return []

    query = _query_text(name, brand, preparation)
    payload = {
        "query": query,
        "pageSize": 8,
        "dataType": ["Branded", "Foundation", "SR Legacy"],
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://api.nal.usda.gov/fdc/v1/foods/search",
                params={"api_key": settings.usda_api_key},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("USDA lookup failed for %r: %s", query, exc)
        return []

    candidates: list[dict[str, Any]] = []
    for food in data.get("foods") or []:
        nutrients = _usda_nutrients(food)
        candidate = {
            "provider": "usda",
            "name": food.get("description") or "",
            "brand": food.get("brandOwner") or food.get("brandName"),
            **nutrients,
            "url": (
                f"https://fdc.nal.usda.gov/food-details/{food.get('fdcId')}/nutrients"
                if food.get("fdcId")
                else None
            ),
        }
        if not candidate["name"] or not _complete_macros(candidate):
            continue
        candidate["match_score"] = round(
            _candidate_score(name, brand, candidate["name"], candidate["brand"]),
            3,
        )
        candidate["source"] = "USDA FoodData Central"
        candidates.append(candidate)

    return _trim_candidates(candidates)


async def _fatsecret_access_token() -> str | None:
    global _fatsecret_token, _fatsecret_token_expires_at

    if not settings.fatsecret_client_id or not settings.fatsecret_client_secret:
        return None
    if _fatsecret_token and time.time() < _fatsecret_token_expires_at - 120:
        return _fatsecret_token

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://oauth.fatsecret.com/connect/token",
                auth=(settings.fatsecret_client_id, settings.fatsecret_client_secret),
                data={
                    "grant_type": "client_credentials",
                    "scope": "premier localization",
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("FatSecret OAuth failed: %s", exc)
        return None

    token = data.get("access_token")
    if not token:
        return None
    _fatsecret_token = str(token)
    _fatsecret_token_expires_at = time.time() + int(data.get("expires_in") or 3600)
    return _fatsecret_token


def _fatsecret_100g_serving(food: dict[str, Any]) -> dict[str, Any] | None:
    servings = ((food.get("servings") or {}).get("serving") or [])
    if isinstance(servings, dict):
        servings = [servings]

    best: dict[str, Any] | None = None
    for serving in servings:
        amount = _float(serving.get("metric_serving_amount"))
        unit = str(serving.get("metric_serving_unit") or "").lower()
        if amount is None or amount <= 0 or unit != "g":
            continue
        if abs(amount - 100.0) < 0.01:
            best = serving
            break
        if best is None:
            best = serving

    if best is None:
        return None

    amount = _float(best.get("metric_serving_amount"))
    if amount is None or amount <= 0:
        return None
    factor = 100.0 / amount
    calories = _float(best.get("calories"))
    protein = _float(best.get("protein"))
    fat = _float(best.get("fat"))
    carbs = _float(best.get("carbohydrate"))
    if None in (calories, protein, fat, carbs):
        return None
    return {
        "calories_100g": round(calories * factor, 2),
        "protein_g_100g": round(protein * factor, 2),
        "fat_g_100g": round(fat * factor, 2),
        "carbs_g_100g": round(carbs * factor, 2),
    }


async def search_fatsecret(
    name: str,
    brand: str | None,
    preparation: str | None,
) -> list[dict[str, Any]]:
    token = await _fatsecret_access_token()
    if not token:
        return []

    query = _query_text(name, brand, preparation)
    params = {
        "search_expression": query,
        "page_number": "0",
        "max_results": "8",
        "region": settings.nutrition_region,
        "language": settings.nutrition_language,
        "format": "json",
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, headers=headers) as client:
            response = await client.get(
                "https://platform.fatsecret.com/rest/foods/search/v5",
                params=params,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("FatSecret lookup failed for %r: %s", query, exc)
        return []

    foods = (((data.get("foods_search") or {}).get("results") or {}).get("food") or [])
    if isinstance(foods, dict):
        foods = [foods]

    candidates: list[dict[str, Any]] = []
    for food in foods:
        nutrients = _fatsecret_100g_serving(food)
        if nutrients is None:
            continue
        candidate = {
            "provider": "fatsecret",
            "name": food.get("food_name") or "",
            "brand": food.get("brand_name"),
            **nutrients,
            "url": food.get("food_url"),
            "source": "FatSecret",
        }
        if not candidate["name"]:
            continue
        candidate["match_score"] = round(
            _candidate_score(name, brand, candidate["name"], candidate["brand"]),
            3,
        )
        candidates.append(candidate)

    return _trim_candidates(candidates)


async def search_serper(
    name: str,
    brand: str | None,
    preparation: str | None,
) -> list[dict[str, Any]]:
    if not settings.serper_api_key:
        return []

    query = _query_text(name, brand, preparation)
    query = f'{query} калорийность белки жиры углеводы на 100 г'
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "gl": settings.nutrition_region.lower(),
        "hl": settings.nutrition_language,
        "num": 8,
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, headers=headers) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("Serper lookup failed for %r: %s", query, exc)
        return []

    candidates: list[dict[str, Any]] = []
    for item in (data.get("organic") or [])[:8]:
        title = item.get("title") or ""
        snippet = item.get("snippet") or ""
        if not title and not snippet:
            continue
        candidate = {
            "provider": "serper",
            "name": title,
            "brand": None,
            "snippet": snippet,
            "url": item.get("link"),
            "source": title or item.get("link") or "Google via Serper",
            "match_score": round(_candidate_score(name, brand, title, None), 3),
        }
        candidates.append(candidate)

    return _trim_candidates(candidates)


def _has_strong_database_match(
    candidates: list[dict[str, Any]],
    brand: str | None,
) -> bool:
    threshold = 0.72 if brand else 0.64
    return any(
        candidate.get("provider") != "serper"
        and _complete_macros(candidate)
        and float(candidate.get("match_score") or 0) >= threshold
        for candidate in candidates
    )


async def collect_nutrition_evidence(
    name: str,
    brand: str | None,
    preparation: str | None,
) -> list[dict[str, Any]]:
    database_results = await asyncio.gather(
        search_open_food_facts(name, brand, preparation),
        search_fatsecret(name, brand, preparation),
        search_usda(name, brand, preparation),
    )
    candidates = [item for group in database_results for item in group]

    if not _has_strong_database_match(candidates, brand):
        candidates.extend(await search_serper(name, brand, preparation))

    candidates.sort(
        key=lambda item: (
            1 if _complete_macros(item) else 0,
            float(item.get("match_score") or 0),
        ),
        reverse=True,
    )

    evidence: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates[:12], start=1):
        item = dict(candidate)
        item["evidence_id"] = f"e{index}"
        evidence.append(item)
    return evidence
