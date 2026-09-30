from __future__ import annotations

from datetime import date, datetime, time, timedelta
from statistics import mean
from types import SimpleNamespace

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calculations import DailyLog, calculate_targets, calculate_tdee_actual, recalc_from_history
from app.models import ActivityEntry, FoodEntry, User, WeightEntry


async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


def _target_log(user: User) -> DailyLog:
    return DailyLog(km_walked=user.usual_km, terrain="flat", pace="normal")


def _apply_targets(user: User) -> None:
    targets = calculate_targets(user, _target_log(user))
    user.maintenance_calories = targets.maintenance
    user.protein_target_g = targets.protein_g
    if not user.calorie_target_manual:
        user.calorie_target = targets.calories


async def create_user(session: AsyncSession, tg_id: int, name: str | None, data: dict) -> User:
    profile = SimpleNamespace(
        sex=data["sex"],
        age=data["age"],
        height_cm=data["height_cm"],
        current_weight_kg=data["weight_kg"],
        target_weight_kg=data.get("target_weight_kg"),
        usual_km=float(data["usual_km"]),
        tdee_correction=0.0,
        goal=data["goal"],
    )
    targets = calculate_targets(profile, DailyLog(km_walked=profile.usual_km))
    user = User(
        tg_id=tg_id,
        name=name,
        sex=data["sex"],
        age=data["age"],
        height_cm=data["height_cm"],
        initial_weight_kg=data["weight_kg"],
        current_weight_kg=data["weight_kg"],
        target_weight_kg=data.get("target_weight_kg"),
        activity_level="legacy",
        usual_km=profile.usual_km,
        tdee_correction=0.0,
        goal=data["goal"],
        maintenance_calories=targets.maintenance,
        calorie_target=targets.calories,
        calorie_target_manual=False,
        protein_target_g=targets.protein_g,
    )
    session.add(user)
    await session.flush()
    session.add(WeightEntry(user_id=user.id, measured_at=datetime.now(), weight_kg=user.current_weight_kg))
    await session.commit()
    return user


async def add_weight(session: AsyncSession, user: User, weight_kg: float, measured_at: datetime | None = None) -> None:
    user.current_weight_kg = weight_kg
    _apply_targets(user)
    session.add(WeightEntry(user_id=user.id, measured_at=measured_at or datetime.now(), weight_kg=weight_kg))
    await session.commit()


async def add_food(session: AsyncSession, user: User, draft: dict, eaten_at: datetime | None = None) -> FoodEntry:
    item = FoodEntry(
        user_id=user.id,
        eaten_at=eaten_at or datetime.now(),
        title=draft["title"],
        grams=draft.get("total_grams"),
        calories=float(draft["calories"]),
        protein_g=float(draft.get("protein_g", 0)),
        fat_g=float(draft.get("fat_g", 0)),
        carbs_g=float(draft.get("carbs_g", 0)),
        source=draft.get("source", "text"),
        details_json=draft.get("details_json"),
    )
    session.add(item)
    await session.commit()
    return item


async def _refresh_usual_km(session: AsyncSession, user: User, anchor_day: date) -> None:
    start = anchor_day - timedelta(days=13)
    values = list(
        await session.scalars(
            select(ActivityEntry.km_walked)
            .where(
                ActivityEntry.user_id == user.id,
                ActivityEntry.activity_date >= start,
                ActivityEntry.activity_date <= anchor_day,
                ActivityEntry.km_walked.is_not(None),
            )
            .order_by(ActivityEntry.activity_date)
        )
    )
    if not values:
        return
    # A two-week rolling mean makes "usual km" adapt to actual evening answers
    # while keeping the daily calorie target stable enough for normal use.
    user.usual_km = round(sum(float(v) for v in values) / len(values), 2)
    _apply_targets(user)


async def upsert_activity(
    session: AsyncSession,
    user: User,
    activity_date: date,
    km_walked: float,
    terrain: str = "flat",
    pace: str = "normal",
    workout_kcal: float = 0.0,
) -> ActivityEntry:
    item = await session.scalar(
        select(ActivityEntry).where(ActivityEntry.user_id == user.id, ActivityEntry.activity_date == activity_date)
    )
    if item is None:
        item = ActivityEntry(user_id=user.id, activity_date=activity_date)
        session.add(item)

    item.km_walked = max(0.0, float(km_walked))
    item.terrain = terrain if terrain in {"flat", "mixed", "hills"} else "flat"
    item.pace = pace if pace in {"slow", "normal", "fast"} else "normal"
    item.workout_kcal = max(0.0, float(workout_kcal))

    await session.flush()
    await _refresh_usual_km(session, user, activity_date)
    await session.commit()
    return item


async def foods_for_day(session: AsyncSession, user: User, day: date) -> list[FoodEntry]:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    rows = await session.scalars(
        select(FoodEntry)
        .where(FoodEntry.user_id == user.id, FoodEntry.eaten_at >= start, FoodEntry.eaten_at < end)
        .order_by(FoodEntry.eaten_at)
    )
    return list(rows)


async def activity_for_day(session: AsyncSession, user: User, day: date) -> ActivityEntry | None:
    return await session.scalar(
        select(ActivityEntry).where(ActivityEntry.user_id == user.id, ActivityEntry.activity_date == day)
    )


async def day_stats(session: AsyncSession, user: User, day: date) -> dict:
    foods = await foods_for_day(session, user, day)
    activity = await activity_for_day(session, user, day)
    calories = sum(x.calories for x in foods)
    protein = sum(x.protein_g for x in foods)
    fat = sum(x.fat_g for x in foods)
    carbs = sum(x.carbs_g for x in foods)

    km_walked = activity.km_walked if activity else None
    terrain = activity.terrain if activity else "flat"
    pace = activity.pace if activity else "normal"
    workout_kcal = activity.workout_kcal if activity else 0.0

    log = DailyLog(
        day=day,
        km_walked=km_walked,
        terrain=terrain,
        pace=pace,
        workout_kcal=workout_kcal,
        calories_consumed=calories,
    )
    expenditure = round(calculate_tdee_actual(user, log))

    return {
        "foods": foods,
        "calories": round(calories),
        "protein": round(protein),
        "fat": round(fat),
        "carbs": round(carbs),
        "km_walked": km_walked,
        "terrain": terrain,
        "pace": pace,
        "workout_kcal": round(workout_kcal),
        # legacy keys kept temporarily for callers that have not migrated yet
        "steps": activity.steps if activity else 0,
        "workout_type": activity.workout_type if activity else None,
        "workout_minutes": activity.workout_minutes if activity else 0,
        "expenditure": expenditure,
        "deficit": round(expenditure - calories),
    }


async def stats_period(session: AsyncSession, user: User, days: int) -> dict:
    today = date.today()
    start_day = today - timedelta(days=days - 1)
    daily = [await day_stats(session, user, start_day + timedelta(days=i)) for i in range(days)]
    tracked = [d for d in daily if d["foods"] or d["km_walked"] is not None or d["workout_kcal"]]
    denom = len(tracked) or 1

    weights = list(
        await session.scalars(
            select(WeightEntry)
            .where(WeightEntry.user_id == user.id, WeightEntry.measured_at >= datetime.combine(start_day, time.min))
            .order_by(WeightEntry.measured_at)
        )
    )
    first_weight = weights[0].weight_kg if weights else user.current_weight_kg
    last_weight = weights[-1].weight_kg if weights else user.current_weight_kg
    in_target = sum(
        1
        for d in tracked
        if abs(d["calories"] - user.calorie_target) <= max(100, user.calorie_target * 0.08)
    )
    km_days = [d["km_walked"] for d in tracked if d["km_walked"] is not None]
    return {
        "avg_calories": round(sum(d["calories"] for d in tracked) / denom),
        "avg_deficit": round(sum(d["deficit"] for d in tracked) / denom),
        "avg_km": round(sum(km_days) / len(km_days), 1) if km_days else 0.0,
        "avg_protein": round(sum(d["protein"] for d in tracked) / denom),
        "tracked_days": len(tracked),
        "in_target_days": in_target,
        "first_weight": first_weight,
        "last_weight": last_weight,
        "weight_change": round(last_weight - first_weight, 1),
    }


async def _history_logs(session: AsyncSession, user: User, start_day: date, end_day: date) -> list[DailyLog]:
    logs: list[DailyLog] = []
    day = start_day
    while day <= end_day:
        foods = await foods_for_day(session, user, day)
        activity = await activity_for_day(session, user, day)
        if foods and activity and activity.km_walked is not None:
            logs.append(
                DailyLog(
                    day=day,
                    km_walked=activity.km_walked,
                    terrain=activity.terrain,
                    pace=activity.pace,
                    workout_kcal=activity.workout_kcal,
                    calories_consumed=sum(x.calories for x in foods),
                )
            )
        day += timedelta(days=1)
    return logs


async def recalibrate_tdee(session: AsyncSession, user: User, now: datetime | None = None) -> dict | None:
    """Recalculate adult correction at most once per 14 days."""
    now = now or datetime.now()
    if user.age < 18:
        return None
    if user.last_tdee_recalc_at and now - user.last_tdee_recalc_at < timedelta(days=14):
        return None

    start_dt = now - timedelta(days=28)
    weights = list(
        await session.scalars(
            select(WeightEntry)
            .where(WeightEntry.user_id == user.id, WeightEntry.measured_at >= start_dt)
            .order_by(WeightEntry.measured_at)
        )
    )
    logs = await _history_logs(session, user, start_dt.date(), now.date())
    if len(weights) < 6 or len(logs) < 10:
        return None
    span_days = (weights[-1].measured_at.date() - weights[0].measured_at.date()).days
    if span_days < 14:
        return None

    old_correction = float(user.tdee_correction or 0.0)
    avg_intake = mean(x.calories_consumed for x in logs)
    avg_predicted = mean(calculate_tdee_actual(user, x) for x in logs)
    predicted_change = (avg_intake - avg_predicted) * span_days / 7700.0
    actual_change = weights[-1].weight_kg - weights[0].weight_kg

    new_correction = recalc_from_history(user, weights, logs)
    user.tdee_correction = new_correction
    user.last_tdee_recalc_at = now
    _apply_targets(user)
    await session.commit()

    return {
        "old_correction": old_correction,
        "new_correction": new_correction,
        "delta_correction": new_correction - old_correction,
        "predicted_change": predicted_change,
        "actual_change": actual_change,
        "new_target": user.calorie_target,
    }


async def food_days_in_month(session: AsyncSession, user: User, year: int, month: int) -> set[int]:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    start = date(year, month, 1)
    rows = await session.execute(
        select(func.date(FoodEntry.eaten_at)).where(
            FoodEntry.user_id == user.id,
            FoodEntry.eaten_at >= datetime.combine(start, time.min),
            FoodEntry.eaten_at < datetime.combine(next_month, time.min),
        ).group_by(func.date(FoodEntry.eaten_at))
    )
    result = set()
    for (raw,) in rows:
        try:
            result.add(date.fromisoformat(str(raw)).day)
        except ValueError:
            continue
    return result


async def frequent_foods(session: AsyncSession, user: User, limit: int = 3) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(FoodEntry.title, func.count(FoodEntry.id).label("cnt"))
        .where(FoodEntry.user_id == user.id)
        .group_by(FoodEntry.title)
        .order_by(desc("cnt"))
        .limit(limit)
    )
    return [(title, int(count)) for title, count in rows]
