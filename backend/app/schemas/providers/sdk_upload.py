"""Request schema for presigned S3 uploads of large mobile-SDK batches."""

from pydantic import BaseModel, Field

from app.schemas.providers.apple.apple_xml.aws import (
    MAX_EXPIRATION_SECONDS,
    MIN_EXPIRATION_SECONDS,
    MIN_FILE_SIZE,
)

SDK_DEFAULT_EXPIRATION_SECONDS = 900  # 15 minutes
SDK_DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
# The ceiling bounds worker memory at roughly *ten times* the object size, not one
# times: process_s3_sdk_upload reads the whole object with .read().decode(), and the
# import path then holds the payload as bytes, as a decoded str, as two parsed JSON
# trees and as validated pydantic models at once. A measured 200MB batch peaked at
# ~2.0GB resident, well past the 4GB the bulk worker task is sized for once two
# batches overlap. 50MB keeps the worst case near 500MB per batch.
SDK_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


class SdkPresignedURLRequest(BaseModel):
    expiration_seconds: int = Field(
        default=SDK_DEFAULT_EXPIRATION_SECONDS,
        ge=MIN_EXPIRATION_SECONDS,
        le=MAX_EXPIRATION_SECONDS,
        description="URL expiration time in seconds (1 min - 1 hour)",
    )
    max_file_size: int = Field(
        default=SDK_DEFAULT_MAX_FILE_SIZE,
        ge=MIN_FILE_SIZE,
        le=SDK_MAX_FILE_SIZE,
        description="Maximum upload size in bytes (1KB - 50MB)",
    )
