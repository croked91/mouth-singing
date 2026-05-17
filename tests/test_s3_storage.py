"""Tests for ``karaoke_shared.storage.s3_storage.S3Storage``.

Both the synchronous boto3 presign client and the aioboto3 async client
are mocked at construction time, so the suite never opens a TCP
connection. We assert that:

  * the synchronous presign client receives the explicit boto3 Config
    (signature_version, retries, timeouts)
  * connect() enters the aioboto3 client's async context manager and
    close() exits it
  * upload() guesses Content-Type from the key extension and forwards
    to put_object
  * download_to_file(), download(), delete(), head/exists,
    ensure_bucket() all delegate to the right underlying async method
  * exists() swallows ClientError and returns False
  * presigned_url() uses the dedicated synchronous presign client
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from karaoke_shared.storage.s3_storage import S3Storage


def _make_aio_client() -> AsyncMock:
    """Build an AsyncMock that mimics an aiobotocore S3 client."""
    client = AsyncMock()
    # generate_presigned_url and other sync attributes return MagicMock
    # by default — that's fine because tests don't call them via the
    # async client (presigned_url goes through the sync boto3 client).
    return client


@pytest.fixture
def s3_setup():
    """Patch boto3.client and aioboto3.Session so no network is opened.

    Returns a 3-tuple:
      * list of every sync boto3 client created (for presign assertions)
      * list of every kwargs dict passed to boto3.client (sync side)
      * AsyncMock representing the entered aioboto3 S3 client (for
        async-method assertions; available only after connect()).
    """
    sync_clients: list[MagicMock] = []
    sync_kwargs: list[dict] = []

    def fake_boto3_client(*args, **kwargs):
        sync_kwargs.append(kwargs)
        client = MagicMock()
        client.exceptions = MagicMock()
        client.exceptions.ClientError = ClientError
        sync_clients.append(client)
        return client

    aio_client = _make_aio_client()

    @asynccontextmanager
    async def fake_client_ctx(*args, **kwargs):
        yield aio_client

    fake_session = MagicMock()
    fake_session.client.side_effect = lambda *a, **kw: fake_client_ctx(*a, **kw)

    with (
        patch("boto3.client", side_effect=fake_boto3_client),
        patch("aioboto3.Session", return_value=fake_session),
    ):
        yield sync_clients, sync_kwargs, aio_client


def test_init_passes_explicit_retry_config_to_presign_client(s3_setup):
    sync_clients, sync_kwargs, _ = s3_setup
    S3Storage(bucket="b", endpoint_url="http://minio:9000",
              access_key="ak", secret_key="sk")

    # Exactly one sync boto3 client is created (for presigned URLs).
    assert len(sync_clients) == 1
    cfg = sync_kwargs[0]["config"]
    assert cfg.signature_version == "s3v4"
    assert cfg.retries == {"max_attempts": 5, "mode": "adaptive"}
    assert cfg.connect_timeout == 10
    assert cfg.read_timeout == 60


def test_init_signs_presign_with_public_base_when_set(s3_setup):
    _, sync_kwargs, _ = s3_setup
    S3Storage(bucket="b", endpoint_url="http://minio:9000",
              access_key="ak", secret_key="sk",
              presigned_url_base="http://public.example.com")
    assert sync_kwargs[0]["endpoint_url"] == "http://public.example.com"


def test_init_signs_presign_with_internal_endpoint_when_no_public_base(s3_setup):
    _, sync_kwargs, _ = s3_setup
    S3Storage(bucket="b", endpoint_url="http://minio:9000")
    assert sync_kwargs[0]["endpoint_url"] == "http://minio:9000"


async def test_methods_raise_before_connect(s3_setup):
    storage = S3Storage(bucket="b")
    with pytest.raises(RuntimeError, match="not connected"):
        await storage.upload("k", b"x")


async def test_connect_is_idempotent(s3_setup):
    storage = S3Storage(bucket="b")
    await storage.connect()
    first_client = storage._client
    await storage.connect()
    assert storage._client is first_client


async def test_close_is_idempotent(s3_setup):
    storage = S3Storage(bucket="b")
    await storage.close()  # before connect — no-op
    await storage.connect()
    await storage.close()
    await storage.close()  # second close — no-op
    assert storage._client is None


async def test_upload_sets_content_type_from_extension(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()

    await storage.upload("uploads/track.mp3", b"...")

    aio_client.put_object.assert_awaited_once()
    kwargs = aio_client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "b"
    assert kwargs["Key"] == "uploads/track.mp3"
    assert kwargs["Body"] == b"..."
    assert kwargs["ContentType"] == "audio/mpeg"


async def test_upload_omits_content_type_for_unknown_extension(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()

    await storage.upload("uploads/track.weird-ext-xyz", b"...")

    kwargs = aio_client.put_object.call_args.kwargs
    assert "ContentType" not in kwargs


async def test_download_to_file_calls_aio_download_file(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()

    result = await storage.download_to_file("k", "/tmp/out.mp3")

    aio_client.download_file.assert_awaited_once_with("b", "k", "/tmp/out.mp3")
    assert result == "/tmp/out.mp3"


async def test_download_returns_body_bytes(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()

    # Simulate an aiobotocore StreamingBody: async context manager with
    # an async read() method returning the payload.
    stream = AsyncMock()
    stream.read = AsyncMock(return_value=b"contents")
    body_ctx = AsyncMock()
    body_ctx.__aenter__.return_value = stream
    body_ctx.__aexit__.return_value = None

    aio_client.get_object.return_value = {"Body": body_ctx}

    result = await storage.download("uploads/x.mp3")

    assert result == b"contents"
    aio_client.get_object.assert_awaited_once_with(
        Bucket="b", Key="uploads/x.mp3"
    )


async def test_delete_calls_aio_delete_object(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()

    await storage.delete("uploads/x.mp3")

    aio_client.delete_object.assert_awaited_once_with(
        Bucket="b", Key="uploads/x.mp3"
    )


async def test_exists_true_when_head_succeeds(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()
    aio_client.head_object.return_value = {}

    assert await storage.exists("k") is True


async def test_exists_false_on_client_error(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()
    aio_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )

    assert await storage.exists("missing") is False


def test_presigned_url_delegates_to_sync_presign_client(s3_setup):
    sync_clients, _, _ = s3_setup
    storage = S3Storage(bucket="b", presigned_url_base="http://public.example.com")
    presign = sync_clients[0]
    presign.generate_presigned_url.return_value = "https://signed-url"

    url = storage.presigned_url("uploads/x.mp3", expires_in=900)

    assert url == "https://signed-url"
    presign.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "b", "Key": "uploads/x.mp3"},
        ExpiresIn=900,
    )


async def test_ensure_bucket_skips_when_head_succeeds(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()

    await storage.ensure_bucket()

    aio_client.head_bucket.assert_awaited_once_with(Bucket="b")
    aio_client.create_bucket.assert_not_called()


async def test_ensure_bucket_creates_when_head_raises(s3_setup):
    _, _, aio_client = s3_setup
    storage = S3Storage(bucket="b")
    await storage.connect()
    aio_client.head_bucket.side_effect = RuntimeError("missing")

    await storage.ensure_bucket()

    aio_client.create_bucket.assert_awaited_once_with(Bucket="b")
