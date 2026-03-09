#!/usr/bin/env python3
"""Post-hoc z-score reindex of audio_features QDrant collection.

Run ONCE after bootstrap is complete to fix the scale-dominance bug:
raw audio features had wildly different scales (tempo ~120 BPM,
spectral_rolloff ~8000 Hz, flatness ~0.001), making cosine similarity
dominated by 2-3 high-magnitude features.

This script:
1. Scrolls all vectors from the ``audio_features`` collection.
2. Computes per-dimension mean and std across the entire catalog.
3. Saves those statistics to a JSON file (used at runtime for new tracks).
4. Applies z-score normalization + L2-renormalization to every vector.
5. Upserts the corrected vectors back into QDrant.

Usage::

    python reindex_audio_features.py \\
        --qdrant-host localhost --qdrant-port 6333 \\
        --stats-path /mnt/data/root/bootstrap_output/feature_normalization_stats.json \\
        [--sqlite-path /path/to/karaoke.db]

The ``--sqlite-path`` flag is optional.  When provided the script resets
``portrait_vector`` for all participants (portraits were computed from the
old, un-normalised vectors and must be rebuilt during the next session).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

_COLLECTION = "audio_features"
_BATCH_SIZE = 200


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-score reindex audio_features")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument(
        "--stats-path",
        default="feature_normalization_stats.json",
        help="Path to save mean/std JSON (used by FeatureExtractor at runtime)",
    )
    parser.add_argument(
        "--sqlite-path",
        default=None,
        help="Path to karaoke SQLite DB; if given, resets portrait_vector",
    )
    args = parser.parse_args()

    client = QdrantClient(
        host=args.qdrant_host,
        port=args.qdrant_port,
        timeout=300,
        check_compatibility=False,
    )

    # --- Step 1: Scroll all vectors ---
    print(f"Scrolling all points from '{_COLLECTION}'...")
    all_points: list = []
    offset = None
    while True:
        result, next_offset = client.scroll(
            collection_name=_COLLECTION,
            offset=offset,
            limit=500,
            with_vectors=True,
            with_payload=True,
        )
        all_points.extend(result)
        if next_offset is None:
            break
        offset = next_offset

    n = len(all_points)
    if n == 0:
        print("No points found — nothing to do.")
        sys.exit(0)
    print(f"Loaded {n} points")

    ids = [str(p.id) for p in all_points]
    payloads = [p.payload or {} for p in all_points]
    matrix = np.array([p.vector for p in all_points], dtype=np.float64)  # (N, 45)

    # --- Step 2: Compute per-dimension statistics ---
    mean = matrix.mean(axis=0)  # (45,)
    std = matrix.std(axis=0)  # (45,)
    std = np.where(std < 1e-8, 1.0, std)  # guard zero-variance dims

    # --- Step 3: Save statistics for runtime ---
    stats_path = Path(args.stats_path)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats = {"mean": mean.tolist(), "std": std.tolist()}
    stats_path.write_text(json.dumps(stats, indent=2))
    print(f"Saved normalization stats: {stats_path}")

    # --- Step 4: Z-score + L2-renormalize ---
    zscored = (matrix - mean) / std  # (N, 45)
    norms = np.linalg.norm(zscored, axis=1, keepdims=True)
    norms = np.where(norms < 1e-8, 1.0, norms)
    final = (zscored / norms).astype(np.float32)  # (N, 45)

    # --- Step 5: Upsert back in batches ---
    for i in range(0, n, _BATCH_SIZE):
        j = min(i + _BATCH_SIZE, n)
        batch = [
            PointStruct(id=ids[k], vector=final[k].tolist(), payload=payloads[k])
            for k in range(i, j)
        ]
        client.upsert(collection_name=_COLLECTION, points=batch)
        print(f"  Upserted {j}/{n}")

    print("Reindex complete.")

    # --- Step 6: Reset portrait vectors in SQLite (optional) ---
    if args.sqlite_path:
        db_path = Path(args.sqlite_path)
        if not db_path.exists():
            print(f"WARNING: SQLite DB not found at {db_path}, skipping portrait reset")
            return
        conn = sqlite3.connect(str(db_path))
        affected = conn.execute(
            "UPDATE participants SET portrait_vector = NULL"
        ).rowcount
        conn.commit()
        conn.close()
        print(f"Reset portrait_vector for {affected} participant(s)")


if __name__ == "__main__":
    main()
