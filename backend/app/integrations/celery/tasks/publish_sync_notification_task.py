"""Publicar el evento `sync.completed` fuera del camino de ingesta."""

from __future__ import annotations

from logging import getLogger
from typing import Any

from celery import shared_task
from pydantic import ValidationError

from app.database import SessionLocal
from app.models import User
from app.repositories.user_repository import UserRepository
from app.schemas.sync_status import SyncStatusEvent
from app.services.outgoing_webhooks import sync_notifications

logger = getLogger(__name__)


@shared_task(
    name="app.integrations.celery.tasks.publish_sync_notification_task.publish_sync_notification",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def publish_sync_notification(self: Any, event_json: str) -> dict[str, Any]:
    if not sync_notifications.is_enabled():
        logger.debug("Sync notifications are not configured — skipping publish")
        return {"published": False}

    try:
        event = SyncStatusEvent.model_validate_json(event_json)
    except ValidationError:
        # Malformed input can never become valid by retrying it — same choice the
        # consumer side makes for a structurally invalid event: log and discard.
        logger.exception("Dropping a malformed sync.completed event instead of retrying")
        return {"published": False, "dropped": True}

    with SessionLocal() as db:
        user = UserRepository(User).get(db, event.user_id)
        external_user_id = user.external_user_id if user else None

    try:
        payload = sync_notifications.build_payload(event, external_user_id)
    except (TypeError, ValueError):
        logger.exception("Dropping sync.completed batch %s — payload could not be built", event.run_id)
        return {"published": False, "dropped": True}

    if not sync_notifications.publish(payload):
        raise self.retry(exc=RuntimeError(f"SNS publish failed for batch {payload['batch_id']}"))
    return {"batch_id": payload["batch_id"], "published": True}
