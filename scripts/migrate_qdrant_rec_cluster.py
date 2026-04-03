#!/usr/bin/env python3
"""Backfill rec_cluster_id into QDrant payloads from SQLite.

One-time migration: reads rec_cluster_id for every track from SQLite
and sets it on the corresponding QDrant points in both collections.
Also creates a payload index for efficient filtered KNN.

Usage::

    python scripts/migrate_qdrant_rec_cluster.py \
        --db /data/sqlite/karaoke.db \
        --qdrant-host localhost --qdrant-port 6333

    python scripts/migrate_qdrant_rec_cluster.py --db /data/sqlite/karaoke.db --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict

from qdrant_client import QdrantClient

_COLLECTIONS = ["audio_features", "lyrics_embeddings"]
_BATCH_SIZE = 100


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill rec_cluster_id in QDrant")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--dry-run", action="store_true", help="Print stats only")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Load all (track_id, rec_cluster_id) pairs.
    cursor = conn.execute(
        "SELECT id, rec_cluster_id FROM tracks "
        "WHERE rec_cluster_id IS NOT NULL AND status = 'ready'"
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No tracks with rec_cluster_id found.")
        sys.exit(0)

    # Group by rec_cluster_id.
    by_cluster: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        by_cluster[row["rec_cluster_id"]].append(row["id"])

    total_tracks = len(rows)
    n_clusters = len(by_cluster)
    print(f"{total_tracks} tracks across {n_clusters} clusters")

    if args.dry_run:
        for cid in sorted(by_cluster):
            print(f"  cluster {cid}: {len(by_cluster[cid])} tracks")
        print("\n[DRY RUN] No changes written.")
        return

    client = QdrantClient(
        host=args.qdrant_host, port=args.qdrant_port,
        timeout=300, check_compatibility=False,
    )

    # Update payloads per cluster per collection.
    updated = 0
    for cid, track_ids in sorted(by_cluster.items()):
        for coll in _COLLECTIONS:
            for i in range(0, len(track_ids), _BATCH_SIZE):
                batch = track_ids[i : i + _BATCH_SIZE]
                client.set_payload(
                    collection_name=coll,
                    payload={"rec_cluster_id": cid},
                    points=batch,
                )
        updated += len(track_ids)
        if updated % 1000 < len(track_ids):
            print(f"  {updated}/{total_tracks} tracks updated...")

    print(f"\nUpdated {updated} tracks in both collections.")

    # Create payload index for efficient filtered KNN.
    print("Creating payload indexes...")
    for coll in _COLLECTIONS:
        client.create_payload_index(
            collection_name=coll,
            field_name="rec_cluster_id",
            field_schema="integer",
        )
        print(f"  {coll}: rec_cluster_id index created")

    print("Done.")


if __name__ == "__main__":
    main()
