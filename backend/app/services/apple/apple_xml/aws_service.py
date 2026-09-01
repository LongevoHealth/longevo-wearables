from logging import getLogger
from typing import Any

import boto3
from botocore.exceptions import NoCredentialsError

from app.config import settings
from app.utils.structured_logging import log_structured

AWS_BUCKET_NAME = settings.aws_bucket_name
AWS_REGION = settings.aws_region
logger = getLogger(__name__)


def _client_kwargs() -> dict[str, Any]:
    """boto3 kwargs: static credentials when configured, otherwise the default
    provider chain — which on ECS resolves to the task role."""
    kwargs: dict[str, Any] = {"region_name": AWS_REGION}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
    return kwargs


def get_s3_client():  # noqa: ANN201
    try:
        return boto3.client("s3", **_client_kwargs())
    except (NoCredentialsError, AttributeError):
        log_structured(logger, "warning", "AWS credentials not configured")
        return None


def get_sns_client():  # noqa: ANN201
    try:
        return boto3.client("sns", **_client_kwargs())
    except (NoCredentialsError, AttributeError):
        log_structured(logger, "warning", "AWS credentials not configured")
        return None
