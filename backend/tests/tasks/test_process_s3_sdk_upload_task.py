"""Tests for the process_s3_sdk_upload Celery task."""

import io
import json
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.integrations.celery.tasks.process_s3_sdk_upload_task import process_s3_sdk_upload

MODULE = "app.integrations.celery.tasks.process_s3_sdk_upload_task"

USER_ID = "123e4567-e89b-12d3-a456-426614174000"


def _s3_client_returning(body: str) -> MagicMock:
    client = MagicMock()
    client.get_object.return_value = {"Body": io.BytesIO(body.encode("utf-8"))}
    return client


@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_downloads_the_object_and_delegates_to_the_sdk_import(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
) -> None:
    body = json.dumps({"provider": "apple", "data": {"records": []}})
    mock_get_client.return_value = _s3_client_returning(body)
    mock_process.return_value = {"status": "success"}

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )

    mock_get_client.return_value.get_object.assert_called_once_with(
        Bucket="ingest-bucket", Key="user-1/sdk/batch-9.json"
    )
    mock_process.assert_called_once_with(
        content=body,
        content_type="application/json",
        user_id="user-1",
        provider="apple",
        batch_id="batch-9",
    )
    assert result == {"status": "success"}


@patch(f"{MODULE}.failed")
@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_unsupported_provider_is_not_dispatched_and_is_reported(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
    mock_failed: MagicMock,
) -> None:
    """A bad `provider` field must not silently lose the batch: the task acks (a retry
    would fail identically) but records a terminal failure the user can see."""
    mock_get_client.return_value = _s3_client_returning(json.dumps({"provider": "fitbit"}))

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key=f"{USER_ID}/sdk/batch-9.json",
        user_id=USER_ID,
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()
    mock_failed.assert_called_once()
    assert mock_failed.call_args.args[0] == UUID(USER_ID)
    assert mock_failed.call_args.kwargs["run_id"] == "batch-9"
    assert "unsupported_provider" in mock_failed.call_args.kwargs["error"]


@patch(f"{MODULE}.failed")
@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_malformed_json_is_not_dispatched_and_is_reported(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
    mock_failed: MagicMock,
) -> None:
    mock_get_client.return_value = _s3_client_returning("not json at all")

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key=f"{USER_ID}/sdk/batch-9.json",
        user_id=USER_ID,
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()
    mock_failed.assert_called_once()
    assert mock_failed.call_args.kwargs["error"] == "malformed_json"


@patch(f"{MODULE}.failed")
@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_json_that_is_not_an_object_is_rejected(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
    mock_failed: MagicMock,
) -> None:
    """Valid JSON that is not an object has no provider to route on. It must be
    rejected like malformed JSON, not blow up on `.get` and get redelivered forever."""
    mock_get_client.return_value = _s3_client_returning("[]")

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key=f"{USER_ID}/sdk/batch-9.json",
        user_id=USER_ID,
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()
    mock_failed.assert_called_once()


@patch(f"{MODULE}.failed")
@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_rejection_survives_a_non_uuid_user_id(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
    mock_failed: MagicMock,
) -> None:
    """The user id is read out of the object key, so it may not be a UUID at all. Sync
    status cannot record that, and trying must not turn the rejection into an
    exception — which is what would put the batch back on the queue."""
    mock_get_client.return_value = _s3_client_returning(json.dumps({"provider": "fitbit"}))

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key="not-a-uuid/sdk/batch-9.json",
        user_id="not-a-uuid",
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()
    mock_failed.assert_not_called()


def test_raises_when_s3_is_not_configured() -> None:
    with patch(f"{MODULE}.get_s3_client", return_value=None), pytest.raises(RuntimeError):
        process_s3_sdk_upload(
            bucket_name="ingest-bucket",
            object_key="user-1/sdk/batch-9.json",
            user_id="user-1",
        )
