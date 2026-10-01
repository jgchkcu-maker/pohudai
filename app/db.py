from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _add_column_if_missing(sync_conn, table: str, column: str, ddl: str) -> bool:
    columns = {c["name"] for c in inspect(sync_conn).get_columns(table)}
    if column in columns:
        return False
    sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    return True


def _migrate_schema(sync_conn) -> None:
    tables = set(inspect(sync_conn).get_table_names())
    if "users" in tables:
        added_usual_km = _add_column_if_missing(
            sync_conn, "users", "usual_km", "usual_km FLOAT DEFAULT 0 NOT NULL"
        )
        _add_column_if_missing(
            sync_conn, "users", "tdee_correction", "tdee_correction FLOAT DEFAULT 0 NOT NULL"
        )
        _add_column_if_missing(
            sync_conn, "users", "last_tdee_recalc_at", "last_tdee_recalc_at TIMESTAMP NULL"
        )
        _add_column_if_missing(
            sync_conn, "users", "birth_date", "birth_date DATE NULL"
        )

        # One-time bootstrap for users created under the old PAL questionnaire.
        # These values are only a migration seed; activity_level is ignored after
        # the user starts recording real kilometres.
        if added_usual_km:
            sync_conn.execute(
                text(
                    """
                    UPDATE users
                    SET usual_km = CASE activity_level
                        WHEN 'low' THEN 2.0
                        WHEN 'light' THEN 5.0
                        WHEN 'medium' THEN 8.0
                        WHEN 'high' THEN 12.0
                        ELSE 0.0
                    END
                    """
                )
            )

    if "activity_entries" in tables:
        _add_column_if_missing(
            sync_conn, "activity_entries", "km_walked", "km_walked FLOAT NULL"
        )
        _add_column_if_missing(
            sync_conn, "activity_entries", "terrain", "terrain VARCHAR(16) DEFAULT 'flat' NOT NULL"
        )
        _add_column_if_missing(
            sync_conn, "activity_entries", "pace", "pace VARCHAR(16) DEFAULT 'normal' NOT NULL"
        )
        _add_column_if_missing(
            sync_conn, "activity_entries", "workout_kcal", "workout_kcal FLOAT DEFAULT 0 NOT NULL"
        )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)
