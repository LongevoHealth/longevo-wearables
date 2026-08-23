"""Process a mobile-SDK batch uploaded to S3.

Mirror of process_aws_upload for JSON batches: downloads the object and hands the
payload to the same import path the direct-POST endpoint uses, so there is exactly
one implementation of the SDK import.
"""

import json
from logging import getLogger
from typing import Any

from celery import shared_task

from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from app.services.apple.apple_xml.aws_service import get_s3_client
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

SUPPORTED_PROVIDERS = ("apple", "samsung", "google")


@shared_task(
    queue="sdk_sync",
    # Same bound as the delegate: the download plus the import must stay well inside the
    # broker's 3600s visibility timeout, or `task_acks_late` starts a second worker on
    # the same object. See process_sdk_upload_task for the reasoning.
    soft_time_limit=1800,  # 30 min soft limit — raises SoftTimeLimitExceeded
    time_limit=1860,  # 31 min hard limit
)
def process_s3_sdk_upload(bucket_name: str, object_key: str, user_id: str) -> dict[str, Any]:
    """Download an SDK batch from S3 and import it.

    Args:
        bucket_name: S3 bucket name
        object_key: S3 object key, shaped `{user_id}/sdk/{batch_id}.json`
        user_id: user the batch belongs to
    """
    s3_client = get_s3_client()
    if not s3_client:
        err = RuntimeError("S3 client not configured — cannot process SDK upload")
        log_and_capture_error(
            err,
            logger,
            "S3 client unavailable in process_s3_sdk_upload task",
            extra={"bucket_name": bucket_name, "object_key": object_key, "user_id": user_id},
        )
        raise err

    content = s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read().decode("utf-8")

    # batch_id travels in the key so the whole pipeline shares one correlation id.
    batch_id = object_key.rsplit("/", 1)[-1].removesuffix(".json")

    try:
        provider = str(json.loads(content).get("provider") or "").lower()
    except json.JSONDecodeError:
        provider = ""

    if provider not in SUPPORTED_PROVIDERS:
        log_structured(
            logger,
            "warning",
            f"Unsupported or missing provider in S3 SDK batch: {provider!r}",
            action="s3_sdk_batch_rejected",
            batch_id=batch_id,
            user_id=user_id,
            object_key=object_key,
        )
        return {
            "status": "error",
            "reason": f"unsupported_provider: {provider}",
            "batch_id": batch_id,
            "object_key": object_key,
        }

    log_structured(
        logger,
        "info",
        f"{provider.capitalize()} S3 batch received",
        action=f"{provider}_s3_batch_received",
        batch_id=batch_id,
        user_id=user_id,
        provider=provider,
        object_key=object_key,
    )

    return process_sdk_upload(
        content=content,
        content_type="application/json",
        user_id=user_id,
        provider=provider,
        batch_id=batch_id,
    )
