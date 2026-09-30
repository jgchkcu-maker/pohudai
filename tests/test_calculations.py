from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.calculations import (
    DailyLog,
    calculate_targets,
    ee_walk,
    mifflin_st_jeor,
    recalc_from_history,
)


def user(**overrides):
    data = {
        "sex": "male",
        "age": 30,
        "height_cm": 180.0,
        "current_weight_kg": 80.0,
        "target_weight_kg": 72.0,
        "usual_km": 8.0,
        "tdee_correction": 0.0,
        "goal": "lose",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_mifflin_male():
    assert mifflin_st_jeor(user()) == pytest.approx(1780.0)


def test_mifflin_female():
    u = user(sex="female", age=25, height_cm=165.0, current_weight_kg=60.0)
    assert mifflin_st_jeor(u) == pytest.approx(1345.25)


def test_walk_zero_km():
    assert ee_walk(user(), 0.0) == 0.0


def test_walk_terrain_multiplier():
    u = user()
    flat = ee_walk(u, 8.0, "flat")
    mixed = ee_walk(u, 8.0, "mixed")
    hills = ee_walk(u, 8.0, "hills")
    assert hills > mixed > flat


def test_adult_loss_target_is_below_maintenance():
    t = calculate_targets(user())
    assert not t.is_youth
    assert t.calories < t.maintenance
    assert t.deficit_kcal > 0
    assert t.method == "MIFFLIN_V3_1"


def test_teen_gets_small_automatic_deficit():
    teen = user(age=16, goal="lose", current_weight_kg=80.0, usual_km=5.0)
    t = calculate_targets(teen)
    assert t.is_youth
    assert 0 < t.deficit_kcal <= 200
    assert t.calories < t.maintenance
    assert t.method == "DRI_EER_2023_TEEN"


def test_recalc_sign_and_clamp():
    u = user(usual_km=5.0)
    start = datetime(2026, 1, 1)
    # Six measurements spanning 14 days with a clear downward trend.
    weights = [
        SimpleNamespace(measured_at=start + timedelta(days=d), weight_kg=80.0 - 0.04 * d)
        for d in [0, 3, 6, 9, 12, 14]
    ]
    logs = [
        DailyLog(
            day=date(2026, 1, 1) + timedelta(days=d),
            km_walked=5.0,
            calories_consumed=2200.0,
        )
        for d in range(15)
    ]
    new_correction = recalc_from_history(u, weights, logs)
    assert -400 <= new_correction <= 400
    assert abs(new_correction - u.tdee_correction) <= 100


def test_recalc_no_history_does_not_crash():
    u = user()
    assert recalc_from_history(u, [], []) == 0.0


def test_recalc_disabled_for_teens():
    teen = user(age=16, tdee_correction=25.0)
    assert recalc_from_history(teen, [], []) == 25.0
