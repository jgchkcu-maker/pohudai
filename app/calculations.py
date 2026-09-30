from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Iterable

from app.cdc_bmi import teen_bmi_status


BASE_NON_TEF_FACTOR = 1.08
TEF_RATE = 0.10
ADULT_LOSS_RATE = 0.15
ADULT_GAIN_SURPLUS_KCAL = 250.0

# Teen loss is deliberately slower than the adult -15% rule. The rate is a
# product guardrail, not a clinical prescription. Eligibility comes from
# sex- and age-specific CDC BMI-for-age rather than adult BMI cutoffs.
TEEN_OBESITY_LOSS_RATE = 0.075
TEEN_OBESITY_MAX_DEFICIT = 250.0
TEEN_SEVERE_LOSS_RATE = 0.10
TEEN_SEVERE_MAX_DEFICIT = 350.0
TEEN_GAIN_SURPLUS_KCAL = 150.0
TEEN_GROWTH_ALLOWANCE_KCAL = 20.0

WALK_KCAL_PER_KG_KM = 0.50
TERRAIN_MULTIPLIER = {
    "flat": 1.0,
    "mixed": 1.10,
    "hills": 1.20,
}
KJ_PER_KCAL = 4.184


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
    bmi: float | None = None
    bmi_category: str | None = None
    formula_gap_pct: float | None = None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def mifflin_st_jeor(user) -> float:
    base = 10 * user.current_weight_kg + 6.25 * user.height_cm - 5 * user.age
    return base + 5 if user.sex == "male" else base - 161


def teen_ree_molnar(user) -> float:
    """Molnar adolescent REE equation.

    The original equations return kJ/day. Convert explicitly to kcal/day.
    """
    if user.sex == "male":
        ree_kj = (
            50.9 * user.current_weight_kg
            + 25.3 * user.height_cm
            - 50.3 * user.age
            + 26.9
        )
    else:
        ree_kj = (
            51.2 * user.current_weight_kg
            + 24.5 * user.height_cm
            - 207.5 * user.age
            + 1629.8
        )
    return ree_kj / KJ_PER_KCAL


def teen_ree_mifflin_check(user) -> float:
    """Independent sanity check, not the primary teen equation."""
    return mifflin_st_jeor(user)


def teen_ree(user) -> tuple[float, float]:
    """Return primary teen REE and Molnar-vs-Mifflin disagreement in percent."""
    molnar = teen_ree_molnar(user)
    mifflin = teen_ree_mifflin_check(user)
    midpoint = (molnar + mifflin) / 2.0
    gap = abs(molnar - mifflin) / midpoint if midpoint > 0 else 0.0

    # Molnar is primary. If two independent estimates disagree dramatically,
    # use their midpoint so one equation cannot single-handedly create an
    # extreme calorie target.
    ree = midpoint if gap > 0.15 else molnar
    return ree, gap * 100.0


def rmr(user) -> float:
    if user.age < 18:
        return teen_ree(user)[0]
    return mifflin_st_jeor(user)


def ee_walk(user, km: float | None, terrain: str = "flat", pace: str = "normal") -> float:
    """Additional walking cost above the low-activity baseline.

    Pace is stored but does not multiply kcal/km in v3.x.
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


def _teen_base(user, log: DailyLog) -> tuple[float, float]:
    ree, formula_gap_pct = teen_ree(user)
    correction = float(getattr(user, "tdee_correction", 0.0) or 0.0)
    base = (
        ree * BASE_NON_TEF_FACTOR
        + TEEN_GROWTH_ALLOWANCE_KCAL
        + ee_walk(user, log.km_walked, log.terrain, log.pace)
        + max(0.0, float(log.workout_kcal or 0.0))
        + correction
    )
    return base, formula_gap_pct


def calculate_tdee_actual(user, log: DailyLog) -> float:
    correction = float(getattr(user, "tdee_correction", 0.0) or 0.0)
    walk = ee_walk(user, log.km_walked, log.terrain, log.pace)
    workout = max(0.0, float(log.workout_kcal or 0.0))

    if user.age < 18:
        base, _ = _teen_base(user, log)
        return base + tef(log.calories_consumed)

    return (
        rmr(user) * BASE_NON_TEF_FACTOR
        + walk
        + workout
        + tef(log.calories_consumed)
        + correction
    )


def calculate_tdee(user, log: DailyLog) -> float:
    return calculate_tdee_actual(user, log)


def _teen_target(user, log: DailyLog) -> Targets:
    base, formula_gap_pct = _teen_base(user, log)
    maintenance = base / (1.0 - TEF_RATE)
    goal = getattr(user, "goal", "maintain")

    status = teen_bmi_status(
        sex=user.sex,
        birth_date=getattr(user, "birth_date", None),
        height_cm=user.height_cm,
        weight_kg=user.current_weight_kg,
        fallback_age_years=user.age,
    )

    desired_deficit = 0.0
    if goal == "lose" and status:
        if status.category == "severe_obesity":
            desired_deficit = min(
                TEEN_SEVERE_MAX_DEFICIT,
                maintenance * TEEN_SEVERE_LOSS_RATE,
            )
        elif status.category == "obesity":
            desired_deficit = min(
                TEEN_OBESITY_MAX_DEFICIT,
                maintenance * TEEN_OBESITY_LOSS_RATE,
            )
        # For overweight but below the 95th percentile, the automatic engine
        # uses maintenance: continued growth can improve BMI-for-age without
        # forcing weight loss.

    if goal == "gain":
        calories = (base + TEEN_GAIN_SURPLUS_KCAL) / (1.0 - TEF_RATE)
        desired_deficit = 0.0
    elif desired_deficit > 0:
        # Solve TDEE - intake = desired_deficit when TEF = 10% of intake.
        calories = (base - desired_deficit) / (1.0 - TEF_RATE)
    else:
        calories = maintenance

    protein_g = max(1, round(user.current_weight_kg * 0.85))
    ree, _ = teen_ree(user)

    return Targets(
        maintenance=round(maintenance / 10) * 10,
        calories=round(calories / 10) * 10,
        protein_g=protein_g,
        sedentary_eer=round((ree * BASE_NON_TEF_FACTOR + TEEN_GROWTH_ALLOWANCE_KCAL) / 10) * 10,
        method="MOLNAR_CDC_TEEN_V3_2",
        is_youth=True,
        deficit_kcal=round(desired_deficit),
        rmr=round(ree),
        bmi=round(status.bmi, 1) if status else None,
        bmi_category=status.category if status else None,
        formula_gap_pct=round(formula_gap_pct, 1),
    )


def calculate_targets(user, daily_log: DailyLog | None = None) -> Targets:
    """Stable daily target from usual activity; completed days use actual km."""
    log = daily_log or _usual_log(user)

    if user.age < 18:
        return _teen_target(user, log)

    correction = float(getattr(user, "tdee_correction", 0.0) or 0.0)
    walk = ee_walk(user, log.km_walked, log.terrain, log.pace)
    workout = max(0.0, float(log.workout_kcal or 0.0))
    goal = getattr(user, "goal", "maintain")

    rmr_v = rmr(user)
    a = rmr_v * BASE_NON_TEF_FACTOR + walk + workout + correction
    maintenance = a / (1.0 - TEF_RATE)

    if goal == "lose":
        calories = (1.0 - ADULT_LOSS_RATE) * a / (
            1.0 - (1.0 - ADULT_LOSS_RATE) * TEF_RATE
        )
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
    """Adult-only 14+ day adaptive correction.

    Teen correction remains disabled: growth and body-composition changes make
    a short 7700-kcal/kg feedback loop too noisy.
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
