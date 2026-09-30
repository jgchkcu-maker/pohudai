from __future__ import annotations

from dataclasses import dataclass


# 2023 Dietary Reference Intakes for Energy (National Academies).
# Coefficients are: intercept + age*age_coef + height_cm*height_coef + weight_kg*weight_coef.
# Activity keys map the bot UX to DRI PAL categories:
# low=inactive, light=low active, medium=active, high=very active.
YOUTH_EER = {
    "male": {
        "low": (-447.51, 3.68, 13.01, 13.15),
        "light": (19.12, 3.68, 8.62, 20.28),
        "medium": (-388.19, 3.68, 12.66, 20.46),
        "high": (-671.75, 3.68, 15.38, 23.25),
    },
    "female": {
        "low": (55.59, -22.25, 8.43, 17.07),
        "light": (-297.54, -22.25, 12.77, 14.73),
        "medium": (-189.55, -22.25, 11.74, 18.34),
        "high": (-709.59, -22.25, 18.22, 14.25),
    },
}

ADULT_EER = {
    "male": {
        "low": (753.07, -10.83, 6.50, 14.10),
        "light": (581.47, -10.83, 8.30, 14.94),
        "medium": (1004.82, -10.83, 6.52, 15.91),
        "high": (-517.88, -10.83, 15.61, 19.11),
    },
    "female": {
        "low": (584.90, -7.01, 5.72, 11.71),
        "light": (575.77, -7.01, 6.60, 12.14),
        "medium": (710.25, -7.01, 6.54, 12.34),
        "high": (511.83, -7.01, 9.07, 12.56),
    },
}


@dataclass(slots=True)
class Targets:
    maintenance: int
    calories: int
    protein_g: int
    sedentary_eer: int
    method: str
    is_youth: bool
    deficit_kcal: int


def estimate_eer(sex: str, age: int, height_cm: float, weight_kg: float, activity: str) -> float:
    table = YOUTH_EER if age <= 18 else ADULT_EER
    sex_table = table.get(sex, table["female"])
    intercept, age_coef, height_coef, weight_coef = sex_table.get(activity, sex_table["light"])
    return intercept + age_coef * age + height_coef * height_cm + weight_coef * weight_kg


def calculate_targets(
    sex: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity: str,
    goal: str,
    target_weight_kg: float | None = None,
) -> Targets:
    maintenance = max(1000.0, estimate_eer(sex, age, height_cm, weight_kg, activity))
    sedentary_eer = max(1000.0, estimate_eer(sex, age, height_cm, weight_kg, "low"))
    is_youth = age <= 18

    # For minors the bot estimates maintenance but does not prescribe an automatic
    # calorie deficit/surplus. Growth and weight-management decisions need more
    # context than a calorie calculator can safely infer.
    deficit = 0
    if is_youth:
        calories = maintenance
    elif goal == "lose":
        # Product default, not a medical prescription:
        # a moderate starting deficit, capped so the initial estimate is not extreme.
        deficit = round(min(500.0, max(250.0, maintenance * 0.15)))
        calories = maintenance - deficit
    elif goal == "gain":
        calories = maintenance + 250
    else:
        calories = maintenance

    protein_basis = weight_kg
    if goal == "lose" and target_weight_kg and 35 <= target_weight_kg < weight_kg:
        protein_basis = target_weight_kg

    if is_youth:
        # DRI RDA for ages 14-18: 0.85 g/kg/day.
        protein_g = round(protein_basis * 0.85)
    else:
        # A practical food-planning target; the calorie engine does not depend on it.
        protein_factor = 1.6 if goal == "lose" else 1.2
        protein_g = round(protein_basis * protein_factor)

    return Targets(
        maintenance=round(maintenance / 10) * 10,
        calories=round(calories / 10) * 10,
        protein_g=max(1, protein_g),
        sedentary_eer=round(sedentary_eer / 10) * 10,
        method="DRI_EER_2023",
        is_youth=is_youth,
        deficit_kcal=deficit,
    )


def estimate_expenditure(
    sedentary_eer: float,
    weight_kg: float,
    steps: int,
    workout_type: str | None,
    workout_minutes: int,
) -> int:
    # DRI inactive EER already includes ordinary daily living, so only walking
    # above a small baseline is added here. This remains an estimate, not a measurement.
    extra_steps = max(0, steps - 3000)
    step_kcal = extra_steps * 0.035 * (weight_kg / 70)

    met = {
        "strength": 5.0,
        "run": 8.0,
        "bike": 6.0,
        "other": 5.0,
    }.get(workout_type or "", 0.0)
    workout_kcal = met * 3.5 * weight_kg / 200 * max(0, workout_minutes)

    return round(sedentary_eer + step_kcal + workout_kcal)
