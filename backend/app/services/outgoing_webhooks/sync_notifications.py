"""Notificación de corrida de ingesta terminada hacia SNS.

El evento dice qué cambió y en qué ventana. **Nunca lleva dato de salud**:
el consumidor resuelve el detalle por la External API. Ver la sección 4 del
diseño 2026-09-11.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.config import settings
from app.schemas.sync_status import SyncStatusEvent

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Slugs de series que representan una actividad y no una métrica continua.
# Se derivan de los conteos del batch, no de una lista de tipos.
_ACTIVITY_FROM_COUNT = {"workouts_saved": "workout", "sleep_saved": "sleep"}


def is_enabled() -> bool:
    return settings.sync_notifications_enabled and settings.aws_sync_events_topic_arn is not None


def _iso(value: Any) -> str | None:
    """Normalizar un timestamp a ISO-8601 con offset (p. ej. ``+00:00``).

    ``value`` llega como ``datetime`` cuando ``build_payload`` se llama
    directamente, pero como ``str`` cuando el evento viajó por
    ``model_dump_json`` / ``model_validate_json`` (la Celery task lo hace):
    ``metadata`` es ``dict[str, Any]``, así que pydantic no revalida sus
    valores como ``datetime`` — quedan como el string que produjo la
    serialización (con sufijo ``Z``). Parseamos ese string para que ambos
    caminos produzcan exactamente el mismo output.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            logger.warning("Could not parse timestamp %r — passing through as-is", value)
            return value
    return str(value)


def build_payload(event: SyncStatusEvent, external_user_id: str | None) -> dict[str, Any]:
    """Construir el evento `sync.completed` v1 a partir de un evento terminal."""
    metadata = event.metadata or {}
    activity_types = sorted(marker for key, marker in _ACTIVITY_FROM_COUNT.items() if int(metadata.get(key) or 0) > 0)
    return {
        "event_type": "sync.completed",
        "schema_version": SCHEMA_VERSION,
        "user_id": str(event.user_id),
        "external_user_id": external_user_id,
        "provider": event.provider,
        "batch_id": event.run_id,
        "occurred_at": _iso(event.ended_at),
        "metrics": list(metadata.get("types") or []),
        "activity_types": activity_types,
        "earliest_record_start_at": _iso(metadata.get("window_start")),
        "latest_record_end_at": _iso(metadata.get("window_end")),
        "counts": {
            "records": int(metadata.get("records_saved") or 0),
            "workouts": int(metadata.get("workouts_saved") or 0),
            "sleep": int(metadata.get("sleep_saved") or 0),
        },
    }


def _create_sns_client() -> Any:
    """Cliente SNS con las credenciales de settings, o el rol de la task si no hay.

    Mismo patrón condicional que `raw_payload_storage._create_s3_client`: sin este
    condicional, correr con rol de task rompe.
    """
    try:
        import boto3

        kwargs: dict[str, Any] = {"region_name": settings.aws_region}
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            kwargs["aws_access_key_id"] = settings.aws_access_key_id
            kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
        return boto3.client("sns", **kwargs)
    except Exception:
        logger.exception("Could not create the SNS client — sync notifications disabled")
        return None


def publish(payload: dict[str, Any]) -> bool:
    """Publicar en el topic FIFO. Devuelve False en vez de propagar."""
    if not is_enabled():
        return False
    client = _create_sns_client()
    if client is None:
        return False
    try:
        client.publish(
            TopicArn=settings.aws_sync_events_topic_arn,
            Message=json.dumps(payload),
            # Ordena por usuario: dos corridas del mismo usuario nunca se procesan
            # en paralelo del lado del consumidor (snapshot-replace).
            MessageGroupId=payload["external_user_id"] or payload["user_id"],
            MessageDeduplicationId=payload["batch_id"],
        )
        return True
    except Exception:
        logger.exception("Failed to publish sync.completed for user %s", payload["user_id"])
        return False
