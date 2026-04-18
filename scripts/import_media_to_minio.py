#!/usr/bin/env python3
"""Import instrumental files from backup into MinIO with S3-compatible key naming.

Renames files during upload:
  {uuid}_(Instrumental)_model_bs_roformer_ep_317_sdr_12.mp3 → instrumentals/{uuid}.mp3

Usage::

    python scripts/import_media_to_minio.py \
        --source "/media/ubuntu/TOSHIBA EXT/backups/volumes/media_data/instrumental" \
        --endpoint http://localhost:9000 \
        --bucket karaoke \
        --workers 8
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from botocore.config import Config


def parse_uuid_from_filename(fname: str) -> str | None:
    """Extract UUID prefix from instrumental filename."""
    # e.g. "e78f8750-5129-4e01-8676-4cff3c882720_(Instrumental)_model_bs_roformer_ep_317_sdr_12.mp3"
    if "_(" in fname:
        return fname.split("_(")[0]
    return fname.replace(".mp3", "")


def upload_file(s3_client, bucket: str, local_path: str, s3_key: str) -> str:
    s3_client.upload_file(local_path, bucket, s3_key)
    return s3_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Import instrumentals into MinIO")
    parser.add_argument("--source", required=True, help="Path to instrumental directory")
    parser.add_argument("--endpoint", default="http://localhost:9000")
    parser.add_argument("--bucket", default="karaoke")
    parser.add_argument("--access-key", default="minioadmin")
    parser.add_argument("--secret-key", default="minioadmin")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source_dir = Path(args.source)
    if not source_dir.is_dir():
        print(f"ERROR: {source_dir} is not a directory")
        sys.exit(1)

    files = sorted(source_dir.glob("*.mp3"))
    print(f"Found {len(files)} MP3 files in {source_dir}")

    # Build upload plan: local_path → s3_key
    plan = []
    skipped = 0
    for f in files:
        uuid_part = parse_uuid_from_filename(f.name)
        if not uuid_part:
            skipped += 1
            continue
        s3_key = f"instrumentals/{uuid_part}.mp3"
        plan.append((str(f), s3_key))

    print(f"Upload plan: {len(plan)} files, {skipped} skipped")

    if args.dry_run:
        for local, key in plan[:5]:
            print(f"  {Path(local).name} → {key}")
        if len(plan) > 5:
            print(f"  ... and {len(plan) - 5} more")
        return

    s3_client = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        aws_access_key_id=args.access_key,
        aws_secret_access_key=args.secret_key,
        config=Config(
            signature_version="s3v4",
            max_pool_connections=args.workers + 2,
        ),
    )

    # Ensure bucket exists
    try:
        s3_client.head_bucket(Bucket=args.bucket)
    except Exception:
        s3_client.create_bucket(Bucket=args.bucket)
        print(f"Created bucket: {args.bucket}")

    uploaded = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(upload_file, s3_client, args.bucket, local, key): key
            for local, key in plan
        }
        for future in as_completed(futures):
            try:
                future.result()
                uploaded += 1
                if uploaded % 500 == 0:
                    print(f"  {uploaded}/{len(plan)} uploaded...")
            except Exception as exc:
                errors += 1
                key = futures[future]
                print(f"  ERROR uploading {key}: {exc}")

    print(f"\nDone: {uploaded} uploaded, {errors} errors out of {len(plan)} total")


if __name__ == "__main__":
    main()
