"""Storage abstraction for the karaoke application.

Provides S3-compatible object storage via boto3.

    from karaoke_shared.storage import S3Storage
"""

from karaoke_shared.storage.s3_storage import S3Storage

__all__ = ["S3Storage"]
