"""Tests for S3-event dispatch by object-key prefix.

One bucket carries two kinds of object — Apple Health XML exports under `raw/` and
mobile-SDK JSON batches under `sdk/` — and each goes to a different import task.
"""

import json
from unittest.mock import MagicMock, patch

from app.schemas.providers.apple.apple_xml.aws import SNSNotification
from app.services.apple.apple_xml.sns_service import sns_service

MODULE = "app.services.apple.apple_xml.sns_service"


def _notification_for(object_key: str) -> SNSNotification:
    message = json.dumps(
        {
            "Records": [
                {
                    "eventSource": "aws:s3",
                    "s3": {
                        "bucket": {"name": "ingest-bucket"},
                        "object": {"key": object_key},
                    },
                }
            ]
        }
    )
    return SNSNotification(
        Type="Notification",
        MessageId="msg-1",
        TopicArn="arn:aws:sns:us-east-1:123456789012:ingest",
        Message=message,
        Timestamp="2026-08-22T00:00:00.000Z",
        Signature="sig",
        SignatureVersion="1",
        SigningCertURL="https://sns.us-east-1.amazonaws.com/cert.pem",
    )


@patch(f"{MODULE}.process_s3_sdk_upload")
@patch(f"{MODULE}.process_aws_upload")
def test_sdk_prefix_goes_to_the_sdk_task(
    mock_xml_task: MagicMock,
    mock_sdk_task: MagicMock,
) -> None:
    result = sns_service._process_s3_notification(_notification_for("user-1/sdk/batch-9.json"))

    mock_sdk_task.delay.assert_called_once_with(
        bucket_name="ingest-bucket",
        object_key="user-1/sdk/batch-9.json",
        user_id="user-1",
    )
    mock_xml_task.delay.assert_not_called()
    assert result.status_code == 202


@patch(f"{MODULE}.process_s3_sdk_upload")
@patch(f"{MODULE}.process_aws_upload")
def test_raw_prefix_still_goes_to_the_xml_task(
    mock_xml_task: MagicMock,
    mock_sdk_task: MagicMock,
) -> None:
    result = sns_service._process_s3_notification(_notification_for("user-1/raw/export.xml"))

    mock_xml_task.delay.assert_called_once_with(
        bucket_name="ingest-bucket",
        object_key="user-1/raw/export.xml",
        user_id="user-1",
    )
    mock_sdk_task.delay.assert_not_called()
    assert result.status_code == 202


@patch(f"{MODULE}.process_s3_sdk_upload")
@patch(f"{MODULE}.process_aws_upload")
def test_key_without_enough_segments_dispatches_nothing(
    mock_xml_task: MagicMock,
    mock_sdk_task: MagicMock,
) -> None:
    result = sns_service._process_s3_notification(_notification_for("orphan.json"))

    mock_xml_task.delay.assert_not_called()
    mock_sdk_task.delay.assert_not_called()
    assert "0 tasks dispatched" in result.response
