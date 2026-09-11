"""Tests for publish_sync_notification Celery task.

Verifies the task resolves external_user_id from the database and that a
failed SNS publish schedules a retry instead of silently dropping the event.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.celery.tasks.publish_sync_notification_task import publish_sync_notification
from app.schemas.sync_status import SyncSource, SyncStage, SyncStatus, SyncStatusEvent
from tests.factories import UserFactory

USER_ID = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def enable_sync_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_notifications_enabled", True)
    monkeypatch.setattr(settings, "aws_sync_events_topic_arn", "arn:aws:sns:eu-north-1:123456789012:sync-events.fifo")


@pytest.fixture
def event_json() -> str:
    event = SyncStatusEvent(
        run_id="b1",
        user_id=USER_ID,
        provider="apple",
        source=SyncSource.SDK,
        stage=SyncStage.COMPLETED,
        status=SyncStatus.SUCCESS,
        items_processed=1243,
        metadata={
            "types": ["steps", "heart_rate"],
            "records_saved": 1240,
            "workouts_saved": 2,
            "sleep_saved": 1,
            "window_start": datetime(2026, 9, 10, 22, 14, tzinfo=timezone.utc),
            "window_end": datetime(2026, 9, 11, 13, 58, tzinfo=timezone.utc),
        },
        ended_at=datetime(2026, 9, 11, 14, 3, 12, tzinfo=timezone.utc),
    )
    return event.model_dump_json()


@patch("app.integrations.celery.tasks.publish_sync_notification_task.SessionLocal")
def test_resolves_external_user_id_from_the_database(
    mock_session_local: MagicMock,
    enable_sync_notifications: None,
    db: Session,
    event_json: str,
) -> None:
    mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
    mock_session_local.return_value.__exit__ = MagicMock(return_value=None)
    seeded_user = UserFactory(id=USER_ID, external_user_id="uw-123")
    db.flush()

    with patch("app.services.outgoing_webhooks.sync_notifications.publish", return_value=True) as mock_publish:
        publish_sync_notification(event_json)

    assert mock_publish.call_args[0][0]["external_user_id"] == seeded_user.external_user_id


@patch("app.integrations.celery.tasks.publish_sync_notification_task.SessionLocal")
def test_returns_none_external_user_id_when_the_user_is_unknown(
    mock_session_local: MagicMock,
    enable_sync_notifications: None,
    db: Session,
    event_json: str,
) -> None:
    mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
    mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

    with patch("app.services.outgoing_webhooks.sync_notifications.publish", return_value=True) as mock_publish:
        publish_sync_notification(event_json)

    assert mock_publish.call_args[0][0]["external_user_id"] is None


@patch("app.integrations.celery.tasks.publish_sync_notification_task.SessionLocal")
def test_retries_when_the_publish_fails(
    mock_session_local: MagicMock,
    enable_sync_notifications: None,
    db: Session,
    event_json: str,
) -> None:
    mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
    mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

    retry_exc = Retry("will retry", RuntimeError("SNS publish failed"))
    with (
        patch.object(publish_sync_notification, "retry", return_value=retry_exc),
        patch("app.services.outgoing_webhooks.sync_notifications.publish", return_value=False),
        pytest.raises(Retry),
    ):
        publish_sync_notification(event_json)


@patch("app.integrations.celery.tasks.publish_sync_notification_task.SessionLocal")
def test_skips_without_touching_the_database_when_disabled(
    mock_session_local: MagicMock,
    event_json: str,
) -> None:
    """sync_notifications_enabled defaults to False: the task must no-op, never retry."""
    with patch("app.services.outgoing_webhooks.sync_notifications.publish") as mock_publish:
        result = publish_sync_notification(event_json)

    assert result == {"published": False}
    mock_publish.assert_not_called()
    mock_session_local.assert_not_called()


def test_drops_a_malformed_event_instead_of_retrying(enable_sync_notifications: None) -> None:
    """Malformed JSON can never become valid by retrying — drop it, like the consumer side does."""
    with patch("app.services.outgoing_webhooks.sync_notifications.publish") as mock_publish:
        result = publish_sync_notification("this is not json")

    assert result == {"published": False, "dropped": True}
    mock_publish.assert_not_called()


@patch("app.integrations.celery.tasks.publish_sync_notification_task.SessionLocal")
def test_drops_an_event_with_non_numeric_counts_instead_of_retrying(
    mock_session_local: MagicMock,
    enable_sync_notifications: None,
    db: Session,
) -> None:
    """A count that can't be coerced to int can never succeed on retry either — drop it."""
    mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
    mock_session_local.return_value.__exit__ = MagicMock(return_value=None)
    event = SyncStatusEvent(
        run_id="b1",
        user_id=USER_ID,
        provider="apple",
        source=SyncSource.SDK,
        stage=SyncStage.COMPLETED,
        status=SyncStatus.SUCCESS,
        items_processed=1243,
        metadata={"records_saved": "not-a-number"},
        ended_at=datetime(2026, 9, 11, 14, 3, 12, tzinfo=timezone.utc),
    )

    with patch("app.services.outgoing_webhooks.sync_notifications.publish") as mock_publish:
        result = publish_sync_notification(event.model_dump_json())

    assert result == {"published": False, "dropped": True}
    mock_publish.assert_not_called()
