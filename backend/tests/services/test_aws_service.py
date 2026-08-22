"""Tests for the boto3 client factory used by the S3/SNS ingest path."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

from pydantic import SecretStr
import pytest

import app.services.apple.apple_xml.aws_service as aws_service


@pytest.fixture(autouse=True)
def mock_external_apis() -> Generator[dict[str, MagicMock], None, None]:
    """No-op override of the global mock_external_apis fixture.

    Unit tests in this module test the actual implementations,
    not the mocked versions used in API integration tests.
    This module-level fixture override applies only to this file,
    preserving the global mock for other tests in the services directory.
    """
    yield {}


@patch("app.services.apple.apple_xml.aws_service.boto3.client")
def test_get_s3_client_omits_credentials_when_unset(mock_boto_client: MagicMock) -> None:
    """Without static keys the client must fall back to the default provider chain
    (the ECS task role), not return None."""
    with (
        patch.object(aws_service.settings, "aws_access_key_id", None),
        patch.object(aws_service.settings, "aws_secret_access_key", None),
    ):
        client = aws_service.get_s3_client()

    assert client is mock_boto_client.return_value
    kwargs = mock_boto_client.call_args.kwargs
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


@patch("app.services.apple.apple_xml.aws_service.boto3.client")
def test_get_s3_client_passes_static_credentials_when_set(mock_boto_client: MagicMock) -> None:
    """Static keys stay supported for local development and S3-compatible endpoints."""
    with (
        patch.object(aws_service.settings, "aws_access_key_id", "AKIA_TEST"),
        patch.object(aws_service.settings, "aws_secret_access_key", SecretStr("secret-value")),
    ):
        aws_service.get_s3_client()

    kwargs = mock_boto_client.call_args.kwargs
    assert kwargs["aws_access_key_id"] == "AKIA_TEST"
    assert kwargs["aws_secret_access_key"] == "secret-value"


@patch("app.services.apple.apple_xml.aws_service.boto3.client")
def test_get_sns_client_omits_credentials_when_unset(mock_boto_client: MagicMock) -> None:
    with (
        patch.object(aws_service.settings, "aws_access_key_id", None),
        patch.object(aws_service.settings, "aws_secret_access_key", None),
    ):
        client = aws_service.get_sns_client()

    assert client is mock_boto_client.return_value
    assert "aws_secret_access_key" not in mock_boto_client.call_args.kwargs
