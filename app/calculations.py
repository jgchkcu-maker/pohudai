from dataclasses import dataclass


ACTIVITY_FACTORS = {
    "low": 1.20,
    "light": 1.35,
    "medium": 1.50,
    "high": 1.70,
}


@dataclass(slots=True)
class Targets:
    bmr: int
    maintenance: int
    calories: int
    protein_g: int


def calculate_targets(sex: str, age: int, height_cm: float, weight_kg: float, activity: str, goal: str) -> Targets:
    sex_adjustment = 5 if sex == "male" else -161
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + sex_adjustment
    maintenance = bmr * ACTIVITY_FACTORS.get(activity, 1.35)

    if goal == "lose":
        calories = maintenance * 0.82
    elif goal == "gain":
        calories = maintenance + 250
    else:
        calories = maintenance

    floor = 1500 if sex == "male" else 1200
    if goal == "lose":
        calories = max(calories, floor)

    protein_factor = 1.6 if goal in {"lose", "gain"} else 1.4
    return Targets(
        bmr=round(bmr),
        maintenance=round(maintenance / 10) * 10,
        calories=round(calories / 10) * 10,
        protein_g=round(weight_kg * protein_factor),
    )


def estimate_expenditure(bmr: float, weight_kg: float, steps: int, workout_type: str | None, workout_minutes: int) -> int:
    base = bmr * 1.15
    step_kcal = max(0, steps) * 0.04 * (weight_kg / 70)
    met = {
        "strength": 5.0,
        "run": 8.0,
        "bike": 6.0,
        "other": 5.0,
    }.get(workout_type or "", 0.0)
    workout_kcal = met * 3.5 * weight_kg / 200 * max(0, workout_minutes)
    return round(base + step_kcal + workout_kcal)
