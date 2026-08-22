"""Tests for the process_s3_sdk_upload Celery task."""

import io
import json
from unittest.mock import MagicMock, patch

from app.integrations.celery.tasks.process_s3_sdk_upload_task import process_s3_sdk_upload

MODULE = "app.integrations.celery.tasks.process_s3_sdk_upload_task"


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


@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_unsupported_provider_is_not_dispatched(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
) -> None:
    mock_get_client.return_value = _s3_client_returning(json.dumps({"provider": "fitbit"}))

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()


@patch(f"{MODULE}.process_sdk_upload")
@patch(f"{MODULE}.get_s3_client")
def test_malformed_json_is_not_dispatched(
    mock_get_client: MagicMock,
    mock_process: MagicMock,
) -> None:
    mock_get_client.return_value = _s3_client_returning("not json at all")

    result = process_s3_sdk_upload(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )

    assert result["status"] == "error"
    mock_process.assert_not_called()


@patch(f"{MODULE}.get_s3_client", return_value=None)
def test_raises_when_s3_is_not_configured(_mock_get_client: MagicMock) -> None:
    try:
        process_s3_sdk_upload(
            bucket_name="ingest-bucket",
            object_key="user-1/sdk/batch-9.json",
            user_id="user-1",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError when the S3 client is unavailable")
