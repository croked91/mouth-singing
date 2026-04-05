#!/usr/bin/env python3
"""Enrich QDrant payloads with full track metadata from PostgreSQL.

Adds artist, title, duration_sec, language, popularity_category,
catalog_cluster_id to every point in both collections. Creates payload
indexes for new filterable fields.

Usage::

    python scripts/enrich_qdrant_payloads.py \
        --pg-dsn 'postgresql://karaoke:karaoke@postgres:5432/karaoke' \
        --qdrant-host qdrant --qdrant-port 6333

    python scripts/enrich_qdrant_payloads.py --pg-dsn ... --dry-run
"""

from __future__ import annotations

import argparse
import asyncio

import asyncpg
from qdrant_client import QdrantClient

_COLLECTIONS = ["audio_features", "lyrics_embeddings"]
_BATCH_SIZE = 100


async def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich QDrant payloads")
    parser.add_argument("--pg-dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pool = await asyncpg.create_pool(args.pg_dsn, min_size=2, max_size=5)

    rows = await pool.fetch(
        """
        SELECT id, artist, title, duration_sec, language,
               popularity_category, rec_cluster_id, catalog_cluster_id, status
        FROM tracks WHERE status = 'ready'
        """
    )
    await pool.close()

    print(f"{len(rows)} tracks to enrich")

    if args.dry_run:
        for r in rows[:5]:
            print(f"  {r['id'][:8]}  {r['artist']} — {r['title']}  lang={r['language']}  pop={r['popularity_category']}")
        print(f"  ... ({len(rows)} total)")
        print("\n[DRY RUN] No changes.")
        return

    client = QdrantClient(
        host=args.qdrant_host, port=args.qdrant_port,
        timeout=300, check_compatibility=False,
    )

    updated = 0
    for i in range(0, len(rows), _BATCH_SIZE):
        batch = rows[i : i + _BATCH_SIZE]
        for r in batch:
            payload = {
                "track_id": r["id"],
                "artist": r["artist"],
                "title": r["title"],
                "duration_sec": r["duration_sec"],
                "language": r["language"],
                "popularity_category": r["popularity_category"],
                "rec_cluster_id": r["rec_cluster_id"],
                "catalog_cluster_id": r["catalog_cluster_id"],
                "status": r["status"],
            }
            for coll in _COLLECTIONS:
                try:
                    client.set_payload(
                        collection_name=coll,
                        payload=payload,
                        points=[r["id"]],
                    )
                except Exception:
                    pass  # point may not exist in this collection

        updated += len(batch)
        if updated % 5000 < _BATCH_SIZE:
            print(f"  {updated}/{len(rows)}...")

    print(f"Updated {updated} tracks.")

    # Create payload indexes for new filterable fields.
    print("Creating payload indexes...")
    for coll in _COLLECTIONS:
        for field, schema in [
            ("language", "keyword"),
            ("popularity_category", "keyword"),
            ("catalog_cluster_id", "integer"),
        ]:
            try:
                client.create_payload_index(
                    collection_name=coll,
                    field_name=field,
                    field_schema=schema,
                )
                print(f"  {coll}.{field}: created")
            except Exception as e:
                print(f"  {coll}.{field}: {e}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
