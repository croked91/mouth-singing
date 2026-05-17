"""S3-compatible object storage client (async-native via aioboto3).

Works with AWS S3, MinIO, Yandex Object Storage, and any S3-compatible
provider. Network I/O uses ``aioboto3`` natively (no ``asyncio.to_thread``
wrappers). The presigned URL helper stays synchronous and uses a small
``boto3`` client — it's pure-crypto, no network call, and keeps the
existing call sites synchronous.

Lifecycle::

    storage = S3Storage(bucket="karaoke", endpoint_url="http://minio:9000",
                        access_key="minioadmin", secret_key="minioadmin")
    await storage.connect()
    try:
        await storage.upload("uploads/abc.mp3", content)
        url = storage.presigned_url("instrumentals/abc.mp3")
    finally:
        await storage.close()

``connect()`` enters an async context manager on the underlying aioboto3
client and keeps it open for the process lifetime; ``close()`` releases
the connection pool. Call ``connect()`` once at service startup before
any other method (``upload``, ``download_to_file``, ``download``,
``delete``, ``exists``, ``ensure_bucket``).
"""

from __future__ import annotations

import mimetypes
from typing import Any, BinaryIO

import aioboto3
import boto3
import structlog
from aiobotocore.config import AioConfig
from botocore.config import Config
from botocore.exceptions import ClientError

logger = structlog.get_logger(__name__)


class S3Storage:
    """S3-compatible object storage abstraction (aioboto3-backed).

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
        self._endpoint_url = endpoint_url or None
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

        # Explicit retry/timeout policy mirrors the previous boto3 behaviour:
        # adaptive mode + 5 attempts handles transient MinIO/S3 hiccups
        # without burning a worker job on a single network blip.
        self._aio_config = AioConfig(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=60,
        )

        # Persistent aioboto3 client: created lazily in connect(), entered
        # as an async context manager, kept open until close().
        self._session: aioboto3.Session | None = None
        self._client_ctx: Any = None
        self._client: Any = None

        # Synchronous presign client: generate_presigned_url is pure crypto
        # (no network call), so a regular boto3 client keeps the existing
        # synchronous presigned_url() signature. Signs with the public
        # endpoint when given so the browser-facing URL matches.
        sync_config = Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=60,
        )
        presign_endpoint = presigned_url_base or endpoint_url
        self._presign_client = boto3.client(
            "s3",
            endpoint_url=presign_endpoint or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=sync_config,
        )

        logger.info(
            "s3_storage_initialized",
            bucket=bucket,
            endpoint=endpoint_url or "AWS S3",
        )

    async def connect(self) -> None:
        """Open the persistent aioboto3 client. Idempotent."""
        if self._client is not None:
            return
        self._session = aioboto3.Session()
        self._client_ctx = self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
            config=self._aio_config,
        )
        self._client = await self._client_ctx.__aenter__()
        logger.info("s3_storage_connected", bucket=self.bucket)

    async def close(self) -> None:
        """Close the persistent aioboto3 client. Idempotent."""
        if self._client_ctx is None:
            return
        await self._client_ctx.__aexit__(None, None, None)
        self._client = None
        self._client_ctx = None
        self._session = None
        logger.info("s3_storage_closed", bucket=self.bucket)

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError(
                "S3Storage not connected. Call `await storage.connect()` "
                "during application startup."
            )
        return self._client

    async def upload(self, key: str, data: bytes | BinaryIO) -> str:
        """Upload data to S3 and return the key.

        Args:
            key: S3 object key (e.g. "uploads/abc.mp3").
            data: File content as bytes or file-like object.

        Returns:
            The object key.
        """
        client = self._require_client()
        extra: dict[str, str] = {}
        content_type, _ = mimetypes.guess_type(key)
        if content_type:
            extra["ContentType"] = content_type

        await client.put_object(
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
        client = self._require_client()
        # aioboto3 patches download_file with a fully-async implementation
        # that uses aiofiles for non-blocking disk writes and concurrent
        # range-get requests for large objects.
        await client.download_file(self.bucket, key, local_path)
        logger.info("s3_object_downloaded", key=key, local_path=local_path)
        return local_path

    async def download(self, key: str) -> bytes:
        """Download an S3 object and return its contents as bytes.

        Args:
            key: S3 object key.

        Returns:
            The object content.
        """
        client = self._require_client()
        response = await client.get_object(Bucket=self.bucket, Key=key)
        async with response["Body"] as stream:
            return await stream.read()

    async def delete(self, key: str) -> None:
        """Delete an S3 object.

        Args:
            key: S3 object key.
        """
        client = self._require_client()
        await client.delete_object(Bucket=self.bucket, Key=key)
        logger.info("s3_object_deleted", key=key)

    async def exists(self, key: str) -> bool:
        """Check if an S3 object exists.

        Args:
            key: S3 object key.

        Returns:
            True if the object exists.
        """
        client = self._require_client()
        try:
            await client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a presigned URL for downloading an S3 object.

        Synchronous on purpose: ``generate_presigned_url`` is pure crypto
        (HMAC-SHA256) and does not touch the network. Uses a dedicated
        boto3 client configured with the public endpoint so the signature
        matches the Host header the browser will send.

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
        client = self._require_client()
        try:
            await client.head_bucket(Bucket=self.bucket)
        except Exception:
            await client.create_bucket(Bucket=self.bucket)
            logger.info("s3_bucket_created", bucket=self.bucket)
