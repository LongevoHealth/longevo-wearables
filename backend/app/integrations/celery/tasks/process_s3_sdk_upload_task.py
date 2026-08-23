"""Process a mobile-SDK batch uploaded to S3.

Mirror of process_aws_upload for JSON batches: downloads the object and hands the
payload to the same import path the direct-POST endpoint uses, so there is exactly
one implementation of the SDK import.
"""

import json
from logging import getLogger
from typing import Any
from uuid import UUID

from celery import shared_task

from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from app.schemas.sync_status import SyncSource
from app.services.apple.apple_xml.aws_service import get_s3_client
from app.services.sync_status_service import failed
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

SUPPORTED_PROVIDERS = ("apple", "samsung", "google")


def _reject(
    *,
    reason: str,
    provider: str,
    user_id: str,
    batch_id: str,
    object_key: str,
) -> dict[str, Any]:
    """Report a batch that cannot be imported, and return the failure without raising.

    Raising would leave the message unacked and the broker would redeliver a payload
    that fails identically every time, so the failure is reported instead of retried:
    Sentry for the operator, and a terminal sync-status event so the user's backfill
    does not disappear silently — the object itself expires from the bucket in 30 days.
    """
    log_and_capture_error(
        ValueError(f"S3 SDK batch rejected: {reason}"),
        logger,
        f"S3 SDK batch rejected ({reason})",
        extra={
            "reason": reason,
            "provider": provider,
            "user_id": user_id,
            "batch_id": batch_id,
            "object_key": object_key,
        },
    )

    # user_id comes from the object key, so it is not guaranteed to be a real user id;
    # sync status is keyed by UUID and has nowhere to record a failure for a bogus one.
    # Sentry above still carries it.
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        log_structured(
            logger,
            "warning",
            "Cannot record sync status for a non-UUID user id",
            action="s3_sdk_batch_rejected",
            batch_id=batch_id,
            user_id=user_id,
            object_key=object_key,
        )
    else:
        failed(
            user_uuid,
            provider or "unknown",
            SyncSource.SDK,
            run_id=batch_id,
            error=reason,
            message="S3 SDK batch rejected before import",
            metadata={"batch_id": batch_id, "object_key": object_key, "reason": reason},
        )

    return {"status": "error", "reason": reason, "batch_id": batch_id, "object_key": object_key}


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
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None

    # A batch that is not a JSON object has no provider to route on, same as one whose
    # provider we do not support.
    if not isinstance(payload, dict):
        return _reject(
            reason="malformed_json",
            provider="",
            user_id=user_id,
            batch_id=batch_id,
            object_key=object_key,
        )

    provider = str(payload.get("provider") or "").lower()

    if provider not in SUPPORTED_PROVIDERS:
        return _reject(
            reason=f"unsupported_provider: {provider}",
            provider=provider,
            user_id=user_id,
            batch_id=batch_id,
            object_key=object_key,
        )

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
