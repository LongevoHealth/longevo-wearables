"""End-to-end test that the ingest batch window reaches the sync-status metadata."""

import json
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from tests.factories import UserFactory

SDK_PAYLOAD: dict[str, object] = {
    "provider": "apple",
    "sdkVersion": "1.0.0",
    "syncTimestamp": "2026-09-11T00:00:00Z",
    "data": {
        "records": [
            {
                "id": "sample-1",
                "type": "HKQuantityTypeIdentifierStepCount",
                "unit": "count",
                "value": 100,
                "startDate": "2026-09-10T22:14:00Z",
                "endDate": "2026-09-10T22:14:00Z",
                "source": {"name": "Test Device", "bundleIdentifier": "test"},
            },
            {
                "id": "sample-2",
                "type": "HKQuantityTypeIdentifierHeartRate",
                "unit": "count/min",
                "value": 62,
                "startDate": "2026-09-11T06:00:00Z",
                "endDate": "2026-09-11T06:00:00Z",
                "source": {"name": "Test Device", "bundleIdentifier": "test"},
            },
        ]
    },
}


@patch("app.integrations.celery.tasks.process_sdk_upload_task.completed")
@patch("app.integrations.celery.tasks.process_sdk_upload_task.SessionLocal")
@patch("app.integrations.celery.tasks.process_sdk_upload_task.UserRepository")
def test_completed_metadata_carries_the_window(
    mock_user_repo_class: MagicMock,
    mock_session_local: MagicMock,
    mock_completed: MagicMock,
    db: Session,
) -> None:
    """The window computed by load_data must reach the terminal sync-status event's metadata."""
    user = UserFactory()
    mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
    mock_session_local.return_value.__exit__ = MagicMock(return_value=None)

    mock_user_repo = MagicMock()
    mock_user_repo.get.return_value = user
    mock_user_repo_class.return_value = mock_user_repo

    process_sdk_upload(
        content=json.dumps(SDK_PAYLOAD),
        content_type="application/json",
        user_id=str(user.id),
        provider="apple",
        batch_id="b1",
    )

    metadata = mock_completed.call_args.kwargs["metadata"]
    assert metadata["window_start"] is not None
    assert metadata["window_end"] >= metadata["window_start"]
    assert metadata["types"]
