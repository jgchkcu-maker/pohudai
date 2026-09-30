from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sex: Mapped[str] = mapped_column(String(16))
    age: Mapped[int] = mapped_column(Integer)
    height_cm: Mapped[float] = mapped_column(Float)
    initial_weight_kg: Mapped[float] = mapped_column(Float)
    current_weight_kg: Mapped[float] = mapped_column(Float)
    target_weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity_level: Mapped[str] = mapped_column(String(32))
    goal: Mapped[str] = mapped_column(String(16))
    maintenance_calories: Mapped[int] = mapped_column(Integer)
    calorie_target: Mapped[int] = mapped_column(Integer)
    protein_target_g: Mapped[int] = mapped_column(Integer)
    evening_poll_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_summary_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    foods: Mapped[list["FoodEntry"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    weights: Mapped[list["WeightEntry"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    activities: Mapped[list["ActivityEntry"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class FoodEntry(Base):
    __tablename__ = "food_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    eaten_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    meal_type: Mapped[str] = mapped_column(String(32), default="other")
    title: Mapped[str] = mapped_column(String(255))
    grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    calories: Mapped[float] = mapped_column(Float)
    protein_g: Mapped[float] = mapped_column(Float, default=0)
    fat_g: Mapped[float] = mapped_column(Float, default=0)
    carbs_g: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(24), default="text")
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="foods")


class WeightEntry(Base):
    __tablename__ = "weight_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    weight_kg: Mapped[float] = mapped_column(Float)

    user: Mapped["User"] = relationship(back_populates="weights")


class ActivityEntry(Base):
    __tablename__ = "activity_entries"
    __table_args__ = (UniqueConstraint("user_id", "activity_date", name="uq_activity_user_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    activity_date: Mapped[date] = mapped_column(Date, index=True)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    workout_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workout_minutes: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship(back_populates="activities")
