"""Tests for ``karaoke_shared.storage.s3_storage.S3Storage``.

The boto3 client is mocked at construction time so the suite never opens
a TCP connection. We assert that:

  * the explicit Config (signature_version, retries, timeouts) reaches boto3
  * upload() guesses Content-Type from the key extension
  * upload(), download_to_file(), download(), delete(), head/exists,
    ensure_bucket() all delegate to the right underlying boto3 method
  * exists() swallows ClientError and returns False
  * presigned_url() uses the dedicated presign client when a public base is set
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from karaoke_shared.storage.s3_storage import S3Storage


@pytest.fixture
def s3_setup():
    """Patch ``boto3.client`` so each call returns a fresh MagicMock client.

    Returns the patcher and a list capturing every (args, kwargs) invocation.
    """
    created_clients: list[MagicMock] = []
    captured_kwargs: list[dict] = []

    def fake_boto3_client(*args, **kwargs):
        captured_kwargs.append(kwargs)
        client = MagicMock()
        client.exceptions = MagicMock()
        client.exceptions.ClientError = ClientError
        created_clients.append(client)
        return client

    with patch("boto3.client", side_effect=fake_boto3_client):
        yield created_clients, captured_kwargs


def test_init_passes_explicit_retry_config(s3_setup):
    _, captured = s3_setup
    S3Storage(bucket="b", endpoint_url="http://minio:9000",
              access_key="ak", secret_key="sk")

    cfg = captured[0]["config"]
    assert cfg.signature_version == "s3v4"
    assert cfg.retries == {"max_attempts": 5, "mode": "adaptive"}
    assert cfg.connect_timeout == 10
    assert cfg.read_timeout == 60


def test_init_creates_separate_presign_client_when_public_base_set(s3_setup):
    clients, captured = s3_setup
    S3Storage(bucket="b", endpoint_url="http://minio:9000",
              access_key="ak", secret_key="sk",
              presigned_url_base="http://public.example.com")

    # Two distinct boto3.client invocations: internal + presign
    assert len(clients) == 2
    assert captured[0]["endpoint_url"] == "http://minio:9000"
    assert captured[1]["endpoint_url"] == "http://public.example.com"


def test_init_reuses_main_client_for_presign_when_no_public_base(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")
    assert len(clients) == 1
    assert storage._client is storage._presign_client


async def test_upload_sets_content_type_from_extension(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")

    await storage.upload("uploads/track.mp3", b"...")

    clients[0].put_object.assert_called_once()
    kwargs = clients[0].put_object.call_args.kwargs
    assert kwargs["Bucket"] == "b"
    assert kwargs["Key"] == "uploads/track.mp3"
    assert kwargs["Body"] == b"..."
    assert kwargs["ContentType"] == "audio/mpeg"


async def test_upload_omits_content_type_for_unknown_extension(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")

    await storage.upload("uploads/track.weird-ext-xyz", b"...")

    kwargs = clients[0].put_object.call_args.kwargs
    assert "ContentType" not in kwargs


async def test_download_to_file_calls_boto3_download(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")

    result = await storage.download_to_file("k", "/tmp/out.mp3")

    clients[0].download_file.assert_called_once_with("b", "k", "/tmp/out.mp3")
    assert result == "/tmp/out.mp3"


async def test_download_returns_body_bytes(s3_setup):
    clients, _ = s3_setup
    body = MagicMock()
    body.read.return_value = b"contents"
    clients_will_be = clients
    storage = S3Storage(bucket="b")
    clients_will_be[0].get_object.return_value = {"Body": body}

    result = await storage.download("uploads/x.mp3")

    assert result == b"contents"
    clients_will_be[0].get_object.assert_called_once_with(Bucket="b", Key="uploads/x.mp3")


async def test_delete_calls_boto3_delete(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")

    await storage.delete("uploads/x.mp3")

    clients[0].delete_object.assert_called_once_with(Bucket="b", Key="uploads/x.mp3")


async def test_exists_true_when_head_succeeds(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")
    clients[0].head_object.return_value = {}

    assert await storage.exists("k") is True


async def test_exists_false_on_client_error(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")
    clients[0].head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )

    assert await storage.exists("missing") is False


def test_presigned_url_delegates_to_presign_client(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b", presigned_url_base="http://public.example.com")
    presign = clients[1]
    presign.generate_presigned_url.return_value = "https://signed-url"

    url = storage.presigned_url("uploads/x.mp3", expires_in=900)

    assert url == "https://signed-url"
    presign.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "b", "Key": "uploads/x.mp3"},
        ExpiresIn=900,
    )


async def test_ensure_bucket_skips_when_head_succeeds(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")

    await storage.ensure_bucket()

    clients[0].head_bucket.assert_called_once_with(Bucket="b")
    clients[0].create_bucket.assert_not_called()


async def test_ensure_bucket_creates_when_head_raises(s3_setup):
    clients, _ = s3_setup
    storage = S3Storage(bucket="b")
    clients[0].head_bucket.side_effect = RuntimeError("missing")

    await storage.ensure_bucket()

    clients[0].create_bucket.assert_called_once_with(Bucket="b")
