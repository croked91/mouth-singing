#!/usr/bin/env python3
"""Create mood tags for catalog clusters.

Run AFTER cluster_catalog.py has created clusters. Reviews the tracks
in each cluster and inserts mood tags into the mood_tags table.

Usage::

    python scripts/create_mood_tags.py --db /data/sqlite/karaoke.db
    python scripts/create_mood_tags.py --db /data/sqlite/karaoke.db --show-clusters
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone


def show_clusters(conn: sqlite3.Connection) -> None:
    """Print each cluster's ID, size, and top tracks for manual tag creation."""
    clusters = conn.execute(
        "SELECT id, track_count FROM catalog_clusters ORDER BY id"
    ).fetchall()

    for cluster_id, track_count in clusters:
        print(f"\n{'='*60}")
        print(f"Cluster {cluster_id} ({track_count} tracks)")
        print(f"{'='*60}")

        top = conn.execute(
            """
            SELECT artist, title, play_count, popularity_category
            FROM tracks
            WHERE catalog_cluster_id = ? AND status = 'ready'
            ORDER BY play_count DESC
            LIMIT 15
            """,
            (cluster_id,),
        ).fetchall()

        for artist, title, plays, cat in top:
            print(f"  {artist} — {title}  (plays: {plays}, {cat})")


def create_tags(conn: sqlite3.Connection, tags: dict[int, list[str]]) -> int:
    """Insert mood tags for the given cluster→names mapping."""
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for cluster_id, names in tags.items():
        for name in names:
            conn.execute(
                "INSERT INTO mood_tags (name, cluster_id, created_at) VALUES (?, ?, ?)",
                (name, cluster_id, now),
            )
            count += 1
    conn.commit()
    return count


# Example tags — replace with real ones after reviewing clusters
EXAMPLE_TAGS: dict[int, list[str]] = {
    1: ["Костёр на даче", "Душевное", "Осенний вечер"],
    2: ["Мощь", "Да пошёл он...", "Погнали"],
    3: ["Шальная императрица", "Девичник", "Танцпол"],
    4: ["Качает", "Раёны", "Читай"],
    5: ["Про жизнь", "Ночной город", "По душам"],
    6: ["Назад в СССР", "Классика", "Для мамы"],
    7: ["Дискотека 90-х", "Угар", "Ностальгия"],
    8: ["Все эти песни о тебе", "Романтика", "Мурашки"],
    9: ["У костра", "Друзья", "С гитарой"],
    10: ["Жара", "Латина", "Отпуск"],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create mood tags for clusters")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--show-clusters", action="store_true",
                        help="Show cluster contents and exit (for manual tag creation)")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing tags before creating new ones")
    parser.add_argument("--example", action="store_true",
                        help="Insert example tags (for testing)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.show_clusters:
        show_clusters(conn)
        conn.close()
        return

    if args.clear:
        conn.execute("DELETE FROM mood_tags")
        conn.commit()
        print("Cleared existing tags.")

    if args.example:
        # Only insert tags for clusters that exist
        existing = {row[0] for row in conn.execute("SELECT id FROM catalog_clusters").fetchall()}
        tags_to_insert = {k: v for k, v in EXAMPLE_TAGS.items() if k in existing}
        count = create_tags(conn, tags_to_insert)
        print(f"Inserted {count} example tags for {len(tags_to_insert)} clusters.")
    else:
        print("Use --show-clusters to review clusters, then edit EXAMPLE_TAGS in this script.")
        print("Or use --example to insert placeholder tags.")

    conn.close()


if __name__ == "__main__":
    main()
