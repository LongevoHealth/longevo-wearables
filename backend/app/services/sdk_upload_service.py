"""Presigned S3 uploads for large mobile-SDK batches.

Small batches post JSON straight to /sdk/users/{user_id}/sync, which validates
synchronously. Batches above the SDK's size threshold — historical backfill — upload
to S3 instead; the bucket event reaches /sns/notification and dispatches processing.
"""

from logging import Logger, getLogger

from botocore.exceptions import ClientError
from fastapi import HTTPException, status

from app.schemas.providers.apple.apple_xml.aws import PresignedURLResponse
from app.schemas.providers.sdk_upload import SdkPresignedURLRequest
from app.services.apple.apple_xml.aws_service import AWS_BUCKET_NAME, get_s3_client

# The SNS handler derives the user id from the first segment of the object key and
# requires at least three segments, so this middle segment is structural.
SDK_KEY_PREFIX = "sdk"


class SdkUploadService:
    def __init__(self, log: Logger) -> None:
        self.log = log
        self.s3_client = get_s3_client()

    def generate_file_key(self, user_id: str, batch_id: str) -> str:
        return f"{user_id}/{SDK_KEY_PREFIX}/{batch_id}.json"

    def create_presigned_url(
        self,
        user_id: str,
        batch_id: str,
        request: SdkPresignedURLRequest,
    ) -> PresignedURLResponse:
        if not self.s3_client:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="S3 client not configured",
            )

        file_key = self.generate_file_key(user_id, batch_id)

        try:
            presigned_post = self.s3_client.generate_presigned_post(
                Bucket=AWS_BUCKET_NAME,
                Key=file_key,
                Fields={"Content-Type": "application/json"},
                Conditions=[
                    ["content-length-range", 1, request.max_file_size],
                    {"Content-Type": "application/json"},
                ],
                ExpiresIn=request.expiration_seconds,
            )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"S3 error: {error_code}",
            ) from e

        return PresignedURLResponse(
            upload_url=presigned_post["url"],
            form_fields=presigned_post["fields"],
            file_key=file_key,
            expires_in=request.expiration_seconds,
            max_file_size=request.max_file_size,
            bucket=AWS_BUCKET_NAME,
        )


sdk_upload_service = SdkUploadService(getLogger(__name__))
