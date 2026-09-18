"""Fecha local de un instante, con la zona en cascada.

Cada muestra puede traer su propio `zone_offset`. Cuando no lo trae —el SDK
nativo de HealthKit no lo manda nunca— se usa la zona del usuario, y si
tampoco hay, UTC. El orden importa: la zona de la muestra es la verdad del
dispositivo en ese instante; la del usuario es una aproximación que se
actualiza cuando abre sesión.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Date, Interval, cast, func, select
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.sql import ColumnElement

from app.database import DbSession
from app.models.user import User

# Una columna mapeada (`Model.col`) o una expresión ya construida: las dos
# entran en la aritmética de fechas, pero para el tipado son cosas distintas.
SqlExpr = ColumnElement[Any] | QueryableAttribute[Any]

UTC_OFFSET = "+00:00"


def user_timezone_offset(db_session: DbSession, user_id: UUID | str) -> str | None:
    """Zona declarada del usuario (`+HH:MM`), o None si nunca la informó."""
    return db_session.execute(select(User.timezone_offset).where(User.id == user_id)).scalar_one_or_none()


def local_date_expr(
    instant: SqlExpr,
    record_offset: SqlExpr,
    user_offset: str | None,
) -> ColumnElement[Any]:
    """`instant` convertido a fecha local: zona de la muestra, si no la del usuario, si no UTC."""
    fallbacks = [record_offset]
    if user_offset:
        fallbacks.append(user_offset)
    fallbacks.append(UTC_OFFSET)
    return cast(instant + cast(func.coalesce(*fallbacks), Interval), Date)
