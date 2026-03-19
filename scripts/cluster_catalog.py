#!/usr/bin/env python3
"""Cluster the catalog into vibe-based groups using audio+lyrics vectors.

Reads all vectors from QDrant (audio_features + lyrics_embeddings),
builds a fused representation, runs K-Means, and stores the results
in the SQLite ``catalog_clusters`` table + updates ``tracks.catalog_cluster_id``.

Usage::

    python scripts/cluster_catalog.py \\
        --db /data/sqlite/karaoke.db \\
        --qdrant-host localhost --qdrant-port 6333 \\
        --n-clusters 15

    python scripts/cluster_catalog.py --db /data/sqlite/karaoke.db --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np
from qdrant_client import QdrantClient
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

_AUDIO_COLLECTION = "audio_features"
_LYRICS_COLLECTION = "lyrics_embeddings"
_AUDIO_DIM = 45
_LYRICS_DIM = 384

# Scale factor for lyrics in the fused vector.
# sqrt(0.3 / 0.7) ≈ 0.655 — ensures cosine distance in the fused space
# approximates the 70/30 weighted fusion.
_LYRICS_SCALE = 0.655


def _scroll_all(client: QdrantClient, collection: str) -> dict[str, list[float]]:
    """Scroll all vectors from a QDrant collection. Returns {point_id: vector}."""
    result: dict[str, list[float]] = {}
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            offset=offset,
            limit=500,
            with_vectors=True,
            with_payload=False,
        )
        for p in points:
            result[str(p.id)] = p.vector  # type: ignore[assignment]
        if next_offset is None:
            break
        offset = next_offset
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster catalog by vibe")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--n-clusters", type=int, default=15, help="Number of clusters")
    parser.add_argument("--dry-run", action="store_true", help="Print stats only")
    args = parser.parse_args()

    client = QdrantClient(
        host=args.qdrant_host, port=args.qdrant_port,
        timeout=300, check_compatibility=False,
    )

    # --- Step 1: Load vectors ---
    print("Loading audio vectors...")
    audio_vecs = _scroll_all(client, _AUDIO_COLLECTION)
    print(f"  {len(audio_vecs)} audio vectors")

    print("Loading lyrics vectors...")
    lyrics_vecs = _scroll_all(client, _LYRICS_COLLECTION)
    print(f"  {len(lyrics_vecs)} lyrics vectors")

    # Only use tracks that have BOTH vectors
    common_ids = sorted(set(audio_vecs) & set(lyrics_vecs))
    if not common_ids:
        print("ERROR: No tracks with both audio and lyrics vectors.")
        sys.exit(1)
    print(f"\n{len(common_ids)} tracks with both vectors")

    # --- Step 2: Build fused matrix ---
    print("Building fused vectors...")
    audio_matrix = np.array([audio_vecs[tid] for tid in common_ids], dtype=np.float64)
    lyrics_matrix = np.array([lyrics_vecs[tid] for tid in common_ids], dtype=np.float64)

    # L2-normalize each before concatenation
    audio_norms = np.linalg.norm(audio_matrix, axis=1, keepdims=True)
    audio_norms = np.where(audio_norms < 1e-8, 1.0, audio_norms)
    audio_normed = audio_matrix / audio_norms

    lyrics_norms = np.linalg.norm(lyrics_matrix, axis=1, keepdims=True)
    lyrics_norms = np.where(lyrics_norms < 1e-8, 1.0, lyrics_norms)
    lyrics_normed = (lyrics_matrix / lyrics_norms) * _LYRICS_SCALE

    fused = np.hstack([audio_normed, lyrics_normed])  # (N, 45+384)
    print(f"  Fused matrix shape: {fused.shape}")

    # --- Step 3: K-Means ---
    k = min(args.n_clusters, len(common_ids))
    print(f"\nRunning K-Means with k={k}...")
    kmeans = KMeans(n_clusters=k, n_init=10, max_iter=300, random_state=42)
    labels = kmeans.fit_predict(fused)

    sil = silhouette_score(fused, labels, sample_size=min(5000, len(common_ids)))
    print(f"  Silhouette score: {sil:.3f}")

    # --- Step 4: Extract centroids ---
    centroids_fused = kmeans.cluster_centers_  # (k, 45+384)
    centroids_audio = centroids_fused[:, :_AUDIO_DIM]
    centroids_lyrics = centroids_fused[:, _AUDIO_DIM:] / _LYRICS_SCALE  # undo scale

    # L2-normalize centroids
    for i in range(k):
        norm_a = np.linalg.norm(centroids_audio[i])
        if norm_a > 1e-8:
            centroids_audio[i] /= norm_a
        norm_l = np.linalg.norm(centroids_lyrics[i])
        if norm_l > 1e-8:
            centroids_lyrics[i] /= norm_l

    # --- Step 5: Print statistics ---
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*60}")
    print(f"{'Cluster':>8} {'Size':>6} {'Top tracks'}")
    print(f"{'='*60}")

    cluster_track_ids: dict[int, list[str]] = {}
    for idx, tid in enumerate(common_ids):
        label = int(labels[idx])
        cluster_track_ids.setdefault(label, []).append(tid)

    for label in sorted(cluster_track_ids):
        tids = cluster_track_ids[label]
        # Get top-5 tracks by play_count
        placeholders = ",".join("?" * len(tids))
        cursor = conn.execute(
            f"SELECT artist, title, play_count FROM tracks "
            f"WHERE id IN ({placeholders}) ORDER BY play_count DESC LIMIT 5",
            tids,
        )
        top_tracks = cursor.fetchall()
        top_str = "; ".join(f"{r['artist']} — {r['title']}" for r in top_tracks)
        print(f"  {label:>5}   {len(tids):>5}   {top_str}")

    if args.dry_run:
        print(f"\n[DRY RUN] No changes written.")
        conn.close()
        return

    # --- Step 6: Write to SQLite ---
    print(f"\nWriting to database...")
    now = datetime.now(timezone.utc).isoformat()

    # Clear existing clusters and their mood tags
    conn.execute("DELETE FROM mood_tags")
    conn.execute("DELETE FROM catalog_clusters")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='catalog_clusters'")
    conn.execute("UPDATE tracks SET catalog_cluster_id = NULL")

    # Insert clusters and build label→db_id mapping
    label_to_db_id: dict[int, int] = {}
    for label in sorted(cluster_track_ids):
        cursor = conn.execute(
            """
            INSERT INTO catalog_clusters (centroid_audio, centroid_lyrics, track_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                json.dumps(centroids_audio[label].tolist()),
                json.dumps(centroids_lyrics[label].tolist()),
                len(cluster_track_ids[label]),
                now,
                now,
            ),
        )
        label_to_db_id[label] = cursor.lastrowid

    # Assign tracks to clusters
    for idx, tid in enumerate(common_ids):
        label = int(labels[idx])
        db_id = label_to_db_id[label]
        conn.execute(
            "UPDATE tracks SET catalog_cluster_id = ? WHERE id = ?",
            (db_id, tid),
        )

    conn.commit()
    conn.close()
    print(f"  {k} clusters created, {len(common_ids)} tracks assigned.")


if __name__ == "__main__":
    main()
