from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Iterable


# Adult engine:
# - Mifflin-St Jeor RMR
# - 1.08 * RMR as a non-TEF low-activity baseline
# - net walking + active workout calories
# - TEF from actual intake for completed-day expenditure
#
# Teen engine:
# - DRI 2023 inactive EER as the daily baseline (already a TEE/EER model)
# - net walking + active workout calories
# - no extra TEF term, because EER already represents total energy requirement.
BASE_NON_TEF_FACTOR = 1.08
TEF_RATE = 0.10
ADULT_LOSS_RATE = 0.15
ADULT_GAIN_SURPLUS_KCAL = 250.0

# Conservative MVP guardrail for ages 14-17. This is intentionally not the
# adult -15% rule. The app should not use the adult 7700-kcal/kg adaptive loop
# for minors because growth and short-term water changes make it unreliable.
TEEN_LOSS_RATE = 0.05
TEEN_MAX_DEFICIT_KCAL = 200.0
TEEN_GAIN_SURPLUS_KCAL = 150.0

WALK_KCAL_PER_KG_KM = 0.50
TERRAIN_MULTIPLIER = {
    "flat": 1.0,
    "mixed": 1.10,
    "hills": 1.20,
}

# DRI 2023 inactive EER coefficients for ages 3-18:
# intercept + age*coef + height_cm*coef + weight_kg*coef + growth allowance.
YOUTH_INACTIVE_EER = {
    "male": (-447.51, 3.68, 13.01, 13.15),
    "female": (55.59, -22.25, 8.43, 17.07),
}


@dataclass(slots=True)
class DailyLog:
    km_walked: float | None = None
    terrain: str = "flat"
    pace: str = "normal"
    calories_consumed: float = 0.0
    workout_kcal: float = 0.0
    day: date | None = None


@dataclass(slots=True)
class Targets:
    maintenance: int
    calories: int
    protein_g: int
    sedentary_eer: int
    method: str
    is_youth: bool
    deficit_kcal: int
    rmr: int | None = None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def mifflin_st_jeor(user) -> float:
    base = 10 * user.current_weight_kg + 6.25 * user.height_cm - 5 * user.age
    return base + 5 if user.sex == "male" else base - 161


def rmr(user) -> float:
    """Resting metabolic rate for adults.

    Teen energy needs use DRI EER directly and deliberately do not flow through
    this function, because EER is a total daily requirement, not an RMR.
    """
    if user.age < 18:
        raise ValueError("Teen users use DRI EER, not an RMR equation")
    return mifflin_st_jeor(user)


def youth_inactive_eer(user) -> float:
    sex = user.sex if user.sex in YOUTH_INACTIVE_EER else "female"
    intercept, age_coef, height_coef, weight_coef = YOUTH_INACTIVE_EER[sex]
    growth_allowance = 20.0
    return (
        intercept
        + age_coef * user.age
        + height_coef * user.height_cm
        + weight_coef * user.current_weight_kg
        + growth_allowance
    )


def ee_walk(user, km: float | None, terrain: str = "flat", pace: str = "normal") -> float:
    """Additional walking cost above the baseline day.

    Pace is stored for future refinement but is intentionally not multiplied
    into kcal/km in v3.1: speed changes kcal/min much more cleanly than kcal/km.
    """
    if km is None:
        return 0.0
    km = max(0.0, float(km))
    terrain_k = TERRAIN_MULTIPLIER.get(terrain, 1.0)
    return WALK_KCAL_PER_KG_KM * user.current_weight_kg * km * terrain_k


def tef(calories_consumed: float) -> float:
    return TEF_RATE * max(0.0, float(calories_consumed))


def _usual_log(user) -> DailyLog:
    return DailyLog(
        km_walked=max(0.0, float(getattr(user, "usual_km", 0.0) or 0.0)),
        terrain="flat",
        pace="normal",
        calories_consumed=0.0,
        workout_kcal=0.0,
    )


def calculate_tdee_actual(user, log: DailyLog) -> float:
    correction = float(getattr(user, "tdee_correction", 0.0) or 0.0)
    walk = ee_walk(user, log.km_walked, log.terrain, log.pace)
    workout = max(0.0, float(log.workout_kcal or 0.0))

    if user.age < 18:
        # DRI EER is already a total daily energy requirement, so no separate
        # TEF term is added for teens.
        return youth_inactive_eer(user) + walk + workout + correction

    return (
        rmr(user) * BASE_NON_TEF_FACTOR
        + walk
        + workout
        + tef(log.calories_consumed)
        + correction
    )


def calculate_tdee(user, log: DailyLog) -> float:
    """Compatibility alias for callers that want completed-day expenditure."""
    return calculate_tdee_actual(user, log)


def calculate_targets(user, daily_log: DailyLog | None = None) -> Targets:
    """Calculate the stable daily target from the user's usual activity.

    The target intentionally uses usual_km, not today's partial km, so it does
    not jump late in the evening. Completed-day expenditure uses actual km.
    """
    log = daily_log or _usual_log(user)
    correction = float(getattr(user, "tdee_correction", 0.0) or 0.0)
    walk = ee_walk(user, log.km_walked, log.terrain, log.pace)
    workout = max(0.0, float(log.workout_kcal or 0.0))
    goal = getattr(user, "goal", "maintain")

    if user.age < 18:
        maintenance = youth_inactive_eer(user) + walk + workout + correction
        if goal == "lose":
            deficit = min(TEEN_MAX_DEFICIT_KCAL, maintenance * TEEN_LOSS_RATE)
            calories = maintenance - deficit
        elif goal == "gain":
            deficit = 0.0
            calories = maintenance + TEEN_GAIN_SURPLUS_KCAL
        else:
            deficit = 0.0
            calories = maintenance

        protein_basis = user.current_weight_kg
        target_weight = getattr(user, "target_weight_kg", None)
        if goal == "lose" and target_weight and 35 <= target_weight < user.current_weight_kg:
            protein_basis = target_weight

        return Targets(
            maintenance=round(maintenance / 10) * 10,
            calories=round(calories / 10) * 10,
            protein_g=max(1, round(protein_basis * 0.85)),
            sedentary_eer=round(youth_inactive_eer(user) / 10) * 10,
            method="DRI_EER_2023_TEEN",
            is_youth=True,
            deficit_kcal=round(deficit),
            rmr=None,
        )

    rmr_v = rmr(user)
    # A excludes TEF. Solve the intake<->TEF relationship algebraically so the
    # maintenance target is self-consistent instead of silently omitting TEF.
    a = rmr_v * BASE_NON_TEF_FACTOR + walk + workout + correction

    maintenance = a / (1.0 - TEF_RATE)
    if goal == "lose":
        calories = ADULT_LOSS_RATE
        calories = (1.0 - ADULT_LOSS_RATE) * a / (1.0 - (1.0 - ADULT_LOSS_RATE) * TEF_RATE)
        calories = max(calories, rmr_v * 1.10)
    elif goal == "gain":
        calories = (a + ADULT_GAIN_SURPLUS_KCAL) / (1.0 - TEF_RATE)
    else:
        calories = maintenance

    deficit = max(0.0, maintenance - calories)
    protein_basis = user.current_weight_kg
    target_weight = getattr(user, "target_weight_kg", None)
    if goal == "lose" and target_weight and 35 <= target_weight < user.current_weight_kg:
        protein_basis = target_weight
    protein_factor = 1.6 if goal == "lose" else 1.2

    return Targets(
        maintenance=round(maintenance / 10) * 10,
        calories=round(calories / 10) * 10,
        protein_g=max(1, round(protein_basis * protein_factor)),
        sedentary_eer=round((rmr_v * BASE_NON_TEF_FACTOR) / 10) * 10,
        method="MIFFLIN_V3_1",
        is_youth=False,
        deficit_kcal=round(deficit),
        rmr=round(rmr_v),
    )


def _weight_date(entry) -> date:
    raw = getattr(entry, "measured_at", getattr(entry, "date", None))
    if raw is None:
        raise ValueError("Weight entry has no date")
    return raw.date() if hasattr(raw, "date") else raw


def _weight_kg(entry) -> float:
    return float(getattr(entry, "weight_kg", getattr(entry, "kg", 0.0)))


def recalc_from_history(user, weight_entries: Iterable, daily_logs: Iterable[DailyLog]) -> float:
    """Return a smoothed adult TDEE correction from 14+ days of history.

    Uses a linear weight trend rather than first/last weight. Each update is
    limited to 100 kcal/day and the total correction to +/-400 kcal/day.
    """
    current = float(getattr(user, "tdee_correction", 0.0) or 0.0)
    if user.age < 18:
        return current

    weights = sorted(list(weight_entries), key=_weight_date)
    logs = [x for x in daily_logs if x.day is not None and x.km_walked is not None and x.calories_consumed > 0]
    if len(weights) < 6 or len(logs) < 10:
        return current

    first_date = _weight_date(weights[0])
    last_date = _weight_date(weights[-1])
    if (last_date - first_date).days < 14:
        return current

    xs = [(_weight_date(w) - first_date).days for w in weights]
    ys = [_weight_kg(w) for w in weights]
    x_bar = mean(xs)
    y_bar = mean(ys)
    denom = sum((x - x_bar) ** 2 for x in xs)
    if denom <= 0:
        return current

    slope_kg_per_day = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denom
    avg_intake = mean(log.calories_consumed for log in logs)
    observed_tdee = avg_intake - slope_kg_per_day * 7700.0
    avg_predicted = mean(calculate_tdee_actual(user, log) for log in logs)
    error = observed_tdee - avg_predicted

    if abs(error) < 50.0:
        return current

    delta = clamp(0.5 * error, -100.0, 100.0)
    return clamp(current + delta, -400.0, 400.0)
