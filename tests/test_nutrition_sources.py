import os

os.environ.setdefault("BOT_TOKEN", "123456:test")

from app.nutrition_sources import (
    _candidate_score,
    _fatsecret_100g_serving,
    _normalize_text,
    _usda_nutrients,
    nutrition_lookup_key,
)


def test_normalize_russian_product_name():
    assert _normalize_text("Соус «Цезарь» Смик, 30 г") == "соус цезарь смик 30 г"


def test_lookup_key_is_stable():
    a = nutrition_lookup_key("Соус Цезарь", "Смик", None)
    b = nutrition_lookup_key("  соус   цезарь ", "СМИК", "")
    assert a == b


def test_brand_exact_match_scores_higher():
    exact = _candidate_score(
        "Соус Цезарь",
        "Смик",
        "Соус Цезарь",
        "Смик",
    )
    wrong_brand = _candidate_score(
        "Соус Цезарь",
        "Смик",
        "Соус Цезарь",
        "Другой бренд",
    )
    assert exact > wrong_brand
    assert exact >= 0.9


def test_fatsecret_serving_normalizes_to_100g():
    food = {
        "servings": {
            "serving": [
                {
                    "metric_serving_amount": "30",
                    "metric_serving_unit": "g",
                    "calories": "135",
                    "protein": "0.3",
                    "fat": "14",
                    "carbohydrate": "1",
                }
            ]
        }
    }
    result = _fatsecret_100g_serving(food)
    assert result is not None
    assert result["calories_100g"] == 450.0
    assert result["protein_g_100g"] == 1.0


def test_usda_nutrient_mapping():
    food = {
        "foodNutrients": [
            {"nutrientName": "Energy", "unitName": "KCAL", "value": 250},
            {"nutrientName": "Protein", "unitName": "G", "value": 12},
            {"nutrientName": "Total lipid (fat)", "unitName": "G", "value": 8},
            {
                "nutrientName": "Carbohydrate, by difference",
                "unitName": "G",
                "value": 32,
            },
        ]
    }
    result = _usda_nutrients(food)
    assert result == {
        "calories_100g": 250.0,
        "protein_g_100g": 12.0,
        "fat_g_100g": 8.0,
        "carbs_g_100g": 32.0,
    }
