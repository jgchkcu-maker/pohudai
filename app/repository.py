from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calculations import calculate_targets, estimate_expenditure
from app.models import ActivityEntry, FoodEntry, User, WeightEntry


async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def create_user(session: AsyncSession, tg_id: int, name: str | None, data: dict) -> User:
    targets = calculate_targets(
        data["sex"], data["age"], data["height_cm"], data["weight_kg"], data["activity_level"], data["goal"]
    )
    user = User(
        tg_id=tg_id,
        name=name,
        sex=data["sex"],
        age=data["age"],
        height_cm=data["height_cm"],
        initial_weight_kg=data["weight_kg"],
        current_weight_kg=data["weight_kg"],
        target_weight_kg=data.get("target_weight_kg"),
        activity_level=data["activity_level"],
        goal=data["goal"],
        maintenance_calories=targets.maintenance,
        calorie_target=targets.calories,
        protein_target_g=targets.protein_g,
    )
    session.add(user)
    await session.flush()
    session.add(WeightEntry(user_id=user.id, measured_at=datetime.now(), weight_kg=user.current_weight_kg))
    await session.commit()
    return user


async def add_weight(session: AsyncSession, user: User, weight_kg: float, measured_at: datetime | None = None) -> None:
    user.current_weight_kg = weight_kg
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


async def upsert_activity(
    session: AsyncSession,
    user: User,
    activity_date: date,
    steps: int,
    workout_type: str | None = None,
    workout_minutes: int = 0,
) -> ActivityEntry:
    item = await session.scalar(
        select(ActivityEntry).where(ActivityEntry.user_id == user.id, ActivityEntry.activity_date == activity_date)
    )
    if item is None:
        item = ActivityEntry(user_id=user.id, activity_date=activity_date, steps=steps)
        session.add(item)
    item.steps = steps
    item.workout_type = workout_type
    item.workout_minutes = workout_minutes
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
    steps = activity.steps if activity else 0
    workout_type = activity.workout_type if activity else None
    workout_minutes = activity.workout_minutes if activity else 0
    targets = calculate_targets(user.sex, user.age, user.height_cm, user.current_weight_kg, user.activity_level, user.goal)
    expenditure = estimate_expenditure(targets.bmr, user.current_weight_kg, steps, workout_type, workout_minutes)
    return {
        "foods": foods,
        "calories": round(calories),
        "protein": round(protein),
        "fat": round(fat),
        "carbs": round(carbs),
        "steps": steps,
        "workout_type": workout_type,
        "workout_minutes": workout_minutes,
        "expenditure": expenditure,
        "deficit": round(expenditure - calories),
    }


async def stats_period(session: AsyncSession, user: User, days: int) -> dict:
    today = date.today()
    start_day = today - timedelta(days=days - 1)
    daily = [await day_stats(session, user, start_day + timedelta(days=i)) for i in range(days)]
    tracked = [d for d in daily if d["foods"] or d["steps"] or d["workout_minutes"]]
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
    in_target = sum(1 for d in tracked if abs(d["calories"] - user.calorie_target) <= max(100, user.calorie_target * 0.08))
    return {
        "avg_calories": round(sum(d["calories"] for d in tracked) / denom),
        "avg_deficit": round(sum(d["deficit"] for d in tracked) / denom),
        "avg_steps": round(sum(d["steps"] for d in tracked) / denom),
        "avg_protein": round(sum(d["protein"] for d in tracked) / denom),
        "tracked_days": len(tracked),
        "in_target_days": in_target,
        "first_weight": first_weight,
        "last_weight": last_weight,
        "weight_change": round(last_weight - first_weight, 1),
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
