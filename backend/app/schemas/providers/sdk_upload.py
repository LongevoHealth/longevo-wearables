"""Request schema for presigned S3 uploads of large mobile-SDK batches."""

from pydantic import BaseModel, Field

from app.schemas.providers.apple.apple_xml.aws import (
    MAX_EXPIRATION_SECONDS,
    MIN_EXPIRATION_SECONDS,
    MIN_FILE_SIZE,
)

SDK_DEFAULT_EXPIRATION_SECONDS = 900  # 15 minutes
SDK_DEFAULT_MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB
SDK_MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1GiB


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
        description="Maximum upload size in bytes (1KB - 1GiB)",
    )
