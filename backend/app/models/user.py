from uuid import UUID
from datetime import datetime

from sqlalchemy.orm import Mapped, relationship

from app.database import BaseDbModel
from app.mappings import PrimaryKey, Unique, email, str_10, str_100, str_255


class User(BaseDbModel):
    """Data owner model"""

    id: Mapped[PrimaryKey[UUID]]

    first_name: Mapped[str_100 | None]
    last_name: Mapped[str_100 | None]
    email: Mapped[email | None]

    external_user_id: Mapped[Unique[str_255] | None]

    # Red de seguridad para muestras sin `zone_offset` (el SDK de HealthKit no
    # lo manda). Mismo formato `+HH:MM` que las muestras; la de la muestra gana.
    timezone_offset: Mapped[str_10 | None]

    personal_record: Mapped["PersonalRecord | None"] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
