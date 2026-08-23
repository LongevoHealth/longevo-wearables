"""Tests for the presigned S3 upload endpoint used by large SDK batches."""

from logging import getLogger
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.testclient import TestClient

from app.schemas.providers.apple.apple_xml.aws import PresignedURLResponse
from app.schemas.providers.sdk_upload import SdkPresignedURLRequest
from app.services.sdk_token_service import create_sdk_user_token
from app.services.sdk_upload_service import SdkUploadService

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_USER_ID = "99999999-e89b-12d3-a456-426614174000"


def test_object_key_has_three_segments_for_sns_parsing() -> None:
    """The SNS handler derives the user id from the first segment and requires at
    least three segments, so this layout is structural."""
    service = SdkUploadService(getLogger("test"))

    key = service.generate_file_key("user-1", "batch-9")

    assert key == "user-1/sdk/batch-9.json"
    assert len(key.split("/")) >= 3


def test_endpoint_returns_the_upload_form(client: TestClient, api_v1_prefix: str) -> None:
    token = create_sdk_user_token("app_123", USER_ID)
    presigned = PresignedURLResponse(
        upload_url="https://bucket.s3.amazonaws.com/",
        form_fields={"key": f"{USER_ID}/sdk/batch-9.json"},
        file_key=f"{USER_ID}/sdk/batch-9.json",
        expires_in=900,
        max_file_size=50 * 1024 * 1024,
        bucket="test-bucket",
    )

    with patch("app.api.routes.v1.sdk_sync.sdk_upload_service") as service:
        service.create_presigned_url.return_value = presigned
        response = client.post(
            f"{api_v1_prefix}/sdk/users/{USER_ID}/sync/s3",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["file_key"].split("/")[1] == "sdk"
    assert body["form_fields"]["key"].endswith(".json")


def test_endpoint_rejects_a_token_issued_for_another_user(client: TestClient, api_v1_prefix: str) -> None:
    token = create_sdk_user_token("app_123", USER_ID)

    response = client.post(
        f"{api_v1_prefix}/sdk/users/{OTHER_USER_ID}/sync/s3",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert response.status_code == 403


def test_endpoint_rejects_a_user_id_that_is_not_a_uuid(client: TestClient, api_v1_prefix: str) -> None:
    """The user id is interpolated into the S3 object key and `api_key` auth does not
    pin it to the caller, so the key's shape depends on it being a plain id: an id
    carrying extra path segments would land the object under another user's prefix,
    which the SNS handler reads back as that user's batch. Reject anything that is not
    a UUID before it reaches the key.

    400, not 422: the app maps every RequestValidationError to 400 (see
    app/utils/exceptions.py), which is what any other malformed path id returns here."""
    token = create_sdk_user_token("app_123", USER_ID)

    response = client.post(
        f"{api_v1_prefix}/sdk/users/not-a-uuid/sync/s3",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert response.status_code == 400

    # A slash-bearing id does not even reach the endpoint: the path param cannot match
    # a segment separator, encoded or not. Pinned so the routing half of the guarantee
    # is not lost silently either.
    escaped = client.post(
        f"{api_v1_prefix}/sdk/users/{USER_ID}%2Fsdk/sync/s3",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert escaped.status_code == 404


def test_endpoint_requires_authentication(client: TestClient, api_v1_prefix: str) -> None:
    response = client.post(f"{api_v1_prefix}/sdk/users/{USER_ID}/sync/s3", json={})

    assert response.status_code in (401, 403)


def test_service_returns_503_when_s3_is_not_configured() -> None:
    with patch("app.services.sdk_upload_service.get_s3_client", return_value=None):
        service = SdkUploadService(getLogger("test"))

    with pytest.raises(HTTPException) as exc_info:
        service.create_presigned_url("user-1", "batch-9", SdkPresignedURLRequest())

    assert exc_info.value.status_code == 503


def test_service_returns_503_when_bucket_is_not_configured() -> None:
    """A configured client with no bucket name is just as unusable as no client —
    presigning against `None` would blow up downstream, so this must 503 too."""
    s3_client = MagicMock()

    with patch("app.services.sdk_upload_service.get_s3_client", return_value=s3_client):
        service = SdkUploadService(getLogger("test"))

    with (
        patch("app.services.sdk_upload_service.AWS_BUCKET_NAME", None),
        pytest.raises(HTTPException) as exc_info,
    ):
        service.create_presigned_url("user-1", "batch-9", SdkPresignedURLRequest())

    assert exc_info.value.status_code == 503
    s3_client.generate_presigned_post.assert_not_called()


def test_presigned_post_pins_json_content_type() -> None:
    s3_client = MagicMock()
    s3_client.generate_presigned_post.return_value = {"url": "https://s3", "fields": {"key": "k"}}

    with patch("app.services.sdk_upload_service.get_s3_client", return_value=s3_client):
        service = SdkUploadService(getLogger("test"))

    service.create_presigned_url("user-1", "batch-9", SdkPresignedURLRequest())

    kwargs = s3_client.generate_presigned_post.call_args.kwargs
    assert kwargs["Key"] == "user-1/sdk/batch-9.json"
    assert kwargs["Fields"]["Content-Type"] == "application/json"
    assert ["content-length-range", 1, 50 * 1024 * 1024] in kwargs["Conditions"]


def test_presigned_url_request_enforces_50mb_ceiling() -> None:
    """Verify that max_file_size ceiling prevents workers from exhausting memory.

    The object is held several times over while it is imported — bytes, decoded str,
    parsed JSON trees, validated models — so peak worker memory runs about ten times
    the object size. Requests with max_file_size > 50 MB must raise ValidationError.
    """
    with pytest.raises(ValidationError):
        SdkPresignedURLRequest(max_file_size=51 * 1024 * 1024)

    assert SdkPresignedURLRequest(max_file_size=50 * 1024 * 1024).max_file_size == 50 * 1024 * 1024
