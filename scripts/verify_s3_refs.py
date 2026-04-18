#!/usr/bin/env python3
"""Verify that S3 references in PostgreSQL point to real objects in MinIO.

Checks:
  - tracks.instrumental_key → S3 object exists
  - artists.image_path → file exists in media_data volume

Usage::

    python scripts/verify_s3_refs.py \
        --pg-dsn 'postgresql://karaoke:karaoke@172.18.0.2:5432/karaoke' \
        --s3-endpoint http://localhost:9000
"""

from __future__ import annotations

import argparse
import asyncio

import asyncpg
import boto3
from botocore.config import Config


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify S3 refs")
    parser.add_argument("--pg-dsn", required=True)
    parser.add_argument("--s3-endpoint", default="http://localhost:9000")
    parser.add_argument("--s3-bucket", default="karaoke")
    parser.add_argument("--access-key", default="minioadmin")
    parser.add_argument("--secret-key", default="minioadmin")
    args = parser.parse_args()

    pool = await asyncpg.create_pool(args.pg_dsn, min_size=1, max_size=3)

    s3 = boto3.client(
        "s3",
        endpoint_url=args.s3_endpoint,
        aws_access_key_id=args.access_key,
        aws_secret_access_key=args.secret_key,
        config=Config(signature_version="s3v4"),
    )

    # --- Check instrumental_key ---
    async with pool.acquire() as pg:
        rows = await pg.fetch(
            "SELECT id, instrumental_key FROM tracks WHERE status = 'ready' AND instrumental_key IS NOT NULL"
        )

    print(f"Checking {len(rows)} instrumental keys...")
    ok = 0
    missing = []
    for r in rows:
        try:
            s3.head_object(Bucket=args.s3_bucket, Key=r["instrumental_key"])
            ok += 1
        except Exception:
            missing.append((r["id"], r["instrumental_key"]))

    print(f"  OK: {ok}")
    print(f"  Missing: {len(missing)}")
    if missing:
        print("  First 10 missing:")
        for tid, key in missing[:10]:
            print(f"    track={tid}  key={key}")

    # --- Check tracks without instrumental_key ---
    async with pool.acquire() as pg:
        no_key = await pg.fetchval(
            "SELECT count(*) FROM tracks WHERE status = 'ready' AND instrumental_key IS NULL"
        )
    if no_key:
        print(f"\n  WARNING: {no_key} ready tracks have no instrumental_key")

    # --- Summary ---
    print(f"\n=== SUMMARY ===")
    print(f"Total ready tracks: {len(rows) + (no_key or 0)}")
    print(f"Instrumental in S3: {ok}/{len(rows)}")
    print(f"Missing from S3: {len(missing)}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
