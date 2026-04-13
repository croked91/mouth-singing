"""S3-compatible object storage client.

Works with AWS S3, MinIO, Yandex Object Storage, and any S3-compatible provider.
Uses boto3 synchronously, wrapped in asyncio.to_thread for async usage.

Usage::

    storage = S3Storage(
        bucket="karaoke",
        endpoint_url="http://minio:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
    )
    await storage.upload("uploads/abc.mp3", content)
    url = storage.presigned_url("instrumentals/abc.mp3")
"""

from __future__ import annotations

import asyncio
import mimetypes
from typing import BinaryIO

import boto3
import structlog
from botocore.config import Config

logger = structlog.get_logger(__name__)


class S3Storage:
    """S3-compatible object storage abstraction.

    Args:
        bucket: S3 bucket name.
        endpoint_url: S3 endpoint URL (e.g. http://minio:9000). Use None for AWS S3.
        access_key: AWS access key or MinIO root user.
        secret_key: AWS secret key or MinIO root password.
        region: AWS region (default us-east-1).
        presigned_url_base: Optional base URL for presigned URLs (e.g. public MinIO endpoint).
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        presigned_url_base: str | None = None,
    ) -> None:
        self.bucket = bucket

        s3_config = Config(signature_version="s3v4")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=s3_config,
        )

        # Separate client for presigned URLs: signs with the public endpoint
        # so that the browser can reach the URL and the signature matches.
        if presigned_url_base:
            self._presign_client = boto3.client(
                "s3",
                endpoint_url=presigned_url_base,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=s3_config,
            )
        else:
            self._presign_client = self._client

        logger.info(
            "s3_storage_initialized",
            bucket=bucket,
            endpoint=endpoint_url or "AWS S3",
        )

    async def upload(self, key: str, data: bytes | BinaryIO) -> str:
        """Upload data to S3 and return the key.

        Args:
            key: S3 object key (e.g. "uploads/abc.mp3").
            data: File content as bytes or file-like object.

        Returns:
            The object key.
        """
        extra: dict[str, str] = {}
        content_type, _ = mimetypes.guess_type(key)
        if content_type:
            extra["ContentType"] = content_type

        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            **extra,
        )
        logger.info("s3_object_uploaded", key=key)
        return key

    async def download_to_file(self, key: str, local_path: str) -> str:
        """Download an S3 object to a local file.

        Args:
            key: S3 object key.
            local_path: Local filesystem path to write to.

        Returns:
            The local_path argument.
        """
        await asyncio.to_thread(
            self._client.download_file,
            self.bucket,
            key,
            local_path,
        )
        logger.info("s3_object_downloaded", key=key, local_path=local_path)
        return local_path

    async def download(self, key: str) -> bytes:
        """Download an S3 object and return its contents as bytes.

        Args:
            key: S3 object key.

        Returns:
            The object content.
        """
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self.bucket,
            Key=key,
        )
        return response["Body"].read()

    async def delete(self, key: str) -> None:
        """Delete an S3 object.

        Args:
            key: S3 object key.
        """
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self.bucket,
            Key=key,
        )
        logger.info("s3_object_deleted", key=key)

    async def exists(self, key: str) -> bool:
        """Check if an S3 object exists.

        Args:
            key: S3 object key.

        Returns:
            True if the object exists.
        """
        try:
            await asyncio.to_thread(
                self._client.head_object,
                Bucket=self.bucket,
                Key=key,
            )
            return True
        except self._client.exceptions.ClientError:
            return False

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a presigned URL for downloading an S3 object.

        Uses a dedicated client configured with the public endpoint so that
        the signature matches the Host header the browser will send.

        Args:
            key: S3 object key.
            expires_in: URL validity in seconds (default 1 hour).

        Returns:
            A presigned URL string.
        """
        return self._presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    async def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist (idempotent)."""
        try:
            await asyncio.to_thread(
                self._client.head_bucket,
                Bucket=self.bucket,
            )
        except Exception:
            await asyncio.to_thread(
                self._client.create_bucket,
                Bucket=self.bucket,
            )
            logger.info("s3_bucket_created", bucket=self.bucket)
