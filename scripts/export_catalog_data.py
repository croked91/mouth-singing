#!/usr/bin/env python3
"""Export catalog_clusters and mood_tags from PostgreSQL to JSON.

Produces /data/models/catalog_data.json for rec-service in-memory use.

Usage::

    python scripts/export_catalog_data.py \
        --pg-dsn 'postgresql://karaoke:karaoke@postgres:5432/karaoke' \
        --output /data/models/catalog_data.json
"""

from __future__ import annotations

import argparse
import asyncio
import json

import asyncpg


async def main() -> None:
    parser = argparse.ArgumentParser(description="Export catalog data to JSON")
    parser.add_argument("--pg-dsn", required=True)
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    pool = await asyncpg.create_pool(args.pg_dsn, min_size=1, max_size=3)

    clusters_rows = await pool.fetch(
        "SELECT id, centroid_audio, centroid_lyrics, track_count FROM catalog_clusters ORDER BY id"
    )
    tags_rows = await pool.fetch(
        "SELECT id, name, cluster_id FROM mood_tags ORDER BY id"
    )
    await pool.close()

    clusters = []
    for r in clusters_rows:
        clusters.append({
            "id": r["id"],
            "centroid_audio": json.loads(r["centroid_audio"]) if isinstance(r["centroid_audio"], str) else r["centroid_audio"],
            "centroid_lyrics": json.loads(r["centroid_lyrics"]) if isinstance(r["centroid_lyrics"], str) else r["centroid_lyrics"],
            "track_count": r["track_count"],
        })

    tags = [{"id": r["id"], "name": r["name"], "cluster_id": r["cluster_id"]} for r in tags_rows]

    data = {"clusters": clusters, "tags": tags}

    with open(args.output, "w") as f:
        json.dump(data, f)

    print(f"Exported {len(clusters)} clusters, {len(tags)} tags → {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
