from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.schemas.sync_status import SyncSource, SyncStage, SyncStatus
from app.services import sync_status_service

USER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def enabled_sync_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sync_notifications_enabled", True)
    monkeypatch.setattr(settings, "aws_sync_events_topic_arn", "arn:aws:sns:eu-north-1:123456789012:sync-events.fifo")


@pytest.fixture
def dispatch() -> Generator[MagicMock, None, None]:
    target = "app.integrations.celery.tasks.publish_sync_notification_task.publish_sync_notification.delay"
    with patch(target) as mock:
        yield mock


def test_emits_on_a_successful_terminal_event(dispatch: MagicMock, enabled_sync_notifications: None) -> None:
    sync_status_service.completed(USER, "apple", SyncSource.SDK, run_id="b1")

    assert dispatch.call_count == 1


@pytest.mark.parametrize("stage", [SyncStage.STARTED, SyncStage.PROCESSING])
def test_does_not_emit_on_non_terminal_stages(
    dispatch: MagicMock, enabled_sync_notifications: None, stage: SyncStage
) -> None:
    sync_status_service.emit_event(
        user_id=USER, provider="apple", source=SyncSource.SDK, stage=stage, status=SyncStatus.SUCCESS
    )

    assert dispatch.call_count == 0


@pytest.mark.parametrize("status", [SyncStatus.SKIPPED, SyncStatus.FAILED])
def test_does_not_emit_on_skipped_or_failed(
    dispatch: MagicMock, enabled_sync_notifications: None, status: SyncStatus
) -> None:
    """`webhook_delivered` usa SKIPPED para entregas duplicadas y no-op."""
    sync_status_service.emit_event(
        user_id=USER,
        provider="whoop",
        source=SyncSource.WEBHOOK,
        stage=SyncStage.COMPLETED,
        status=status,
    )

    assert dispatch.call_count == 0


def test_does_not_emit_when_the_flag_is_off(dispatch: MagicMock) -> None:
    sync_status_service.completed(USER, "apple", SyncSource.SDK, run_id="b1")

    assert dispatch.call_count == 0


def test_a_broker_failure_never_breaks_ingestion(dispatch: MagicMock, enabled_sync_notifications: None) -> None:
    """La ingesta nunca puede fallar porque el bus esté caído."""
    dispatch.side_effect = ConnectionError("redis down")

    event = sync_status_service.completed(USER, "apple", SyncSource.SDK, run_id="b1")

    assert event.run_id == "b1"
