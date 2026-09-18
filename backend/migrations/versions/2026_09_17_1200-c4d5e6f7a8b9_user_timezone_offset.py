"""user_timezone_offset

Zona horaria del usuario, formato `+HH:MM`, como red de seguridad para las
muestras que llegan sin `zone_offset`. El SDK nativo de HealthKit no manda la
zona en ninguna muestra, así que sin este dato todo se agrupa por día UTC y a
un usuario en UTC-3 le corre el día tres horas. La zona de la muestra, cuando
existe, sigue teniendo prioridad.

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a1

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b2c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user", sa.Column("timezone_offset", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "timezone_offset")
