#!/usr/bin/env python3
"""Migrate data from SQLite to PostgreSQL.

Transfers tracks, artists, catalog_clusters, mood_tags, and play_history.
Skips transient tables (sessions, participants, queue_entries, job_queue).

The PostgreSQL schema must already exist (init_pg.sql applied at startup).

Usage::

    # Dry-run (read-only, show counts)
    python scripts/migrate_sqlite_to_pg.py \
        --sqlite /data/sqlite/karaoke.db \
        --pg-dsn 'postgresql://karaoke:karaoke@postgres:5432/karaoke' \
        --dry-run

    # Run migration
    python scripts/migrate_sqlite_to_pg.py \
        --sqlite /data/sqlite/karaoke.db \
        --pg-dsn 'postgresql://karaoke:karaoke@postgres:5432/karaoke'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone

import asyncpg


def _parse_ts(val: str | None) -> datetime | None:
    """Parse ISO timestamp string from SQLite into datetime."""
    if not val:
        return None
    # Handle various ISO formats from SQLite
    val = val.strip()
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        pass
    # Fallback: strip trailing Z
    if val.endswith("Z"):
        return datetime.fromisoformat(val[:-1]).replace(tzinfo=timezone.utc)
    return None


_BATCH_SIZE = 500


async def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite → PostgreSQL")
    parser.add_argument("--sqlite", required=True, help="Path to SQLite database")
    parser.add_argument("--pg-dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument("--dry-run", action="store_true", help="Print counts only")
    args = parser.parse_args()

    # --- Connect ---
    conn = sqlite3.connect(args.sqlite)
    conn.row_factory = sqlite3.Row

    pool = await asyncpg.create_pool(args.pg_dsn, min_size=2, max_size=5)

    try:
        await _migrate(conn, pool, args.dry_run)
    finally:
        await pool.close()
        conn.close()


async def _migrate(conn: sqlite3.Connection, pool: asyncpg.Pool, dry_run: bool) -> None:
    # --- 1. Tracks ---
    rows = conn.execute(
        "SELECT * FROM tracks WHERE status = 'ready' ORDER BY created_at"
    ).fetchall()
    print(f"tracks: {len(rows)} rows")

    if not dry_run and rows:
        async with pool.acquire() as pg:
            # Check if tracks already exist
            existing = await pg.fetchval("SELECT COUNT(*) FROM tracks")
            if existing > 0:
                print(f"  WARNING: PostgreSQL tracks table already has {existing} rows!")
                print("  Skipping tracks to avoid duplicates. Truncate first if needed.")
            else:
                inserted = 0
                for i in range(0, len(rows), _BATCH_SIZE):
                    batch = rows[i : i + _BATCH_SIZE]
                    records = []
                    for r in batch:
                        rd = dict(r)
                        # Map instrumental_path → instrumental_key (S3 style)
                        instr_path = rd.get("instrumental_path")
                        instrumental_key = None
                        if instr_path:
                            # /data/media/instrumental/UUID_(Instrumental)_model.mp3 → instrumentals/UUID.mp3
                            fname = instr_path.rsplit("/", 1)[-1]
                            track_uuid = fname.split("_(")[0] if "_(" in fname else fname.replace(".mp3", "")
                            instrumental_key = f"instrumentals/{track_uuid}.mp3"

                        # Strip null bytes (PostgreSQL rejects \x00 in text)
                        for k in ("lyrics_text", "artist", "title", "error_message"):
                            if rd.get(k) and "\x00" in rd[k]:
                                rd[k] = rd[k].replace("\x00", "")

                        # Parse syllable_timings from JSON text → dict for JSONB
                        timings = rd.get("syllable_timings")
                        if isinstance(timings, str) and timings:
                            timings = json.loads(timings)
                        else:
                            timings = None

                        records.append((
                            rd["id"],
                            rd["artist"],
                            rd["title"],
                            rd.get("duration_sec"),
                            instrumental_key,
                            rd.get("lyrics_text"),
                            json.dumps(timings) if timings else None,
                            rd.get("language"),
                            rd["source"],
                            rd["status"],
                            rd.get("error_message"),
                            rd.get("play_count", 0),
                            rd.get("qdrant_synced", 0),
                            rd.get("popularity_category", "regular"),
                            rd.get("chart_count", 0),
                            _parse_ts(rd.get("chart_last_seen")),
                            rd.get("catalog_cluster_id"),
                            rd.get("rec_cluster_id"),
                            _parse_ts(rd["created_at"]),
                            _parse_ts(rd["updated_at"]),
                        ))

                    await pg.executemany(
                        """
                        INSERT INTO tracks (
                            id, artist, title, duration_sec, instrumental_key,
                            lyrics_text, syllable_timings, language, source, status,
                            error_message, play_count, qdrant_synced, popularity_category,
                            chart_count, chart_last_seen, catalog_cluster_id, rec_cluster_id,
                            created_at, updated_at
                        ) VALUES (
                            $1, $2, $3, $4, $5,
                            $6, $7::jsonb, $8, $9, $10,
                            $11, $12, $13, $14,
                            $15, $16::timestamptz, $17, $18,
                            $19::timestamptz, $20                        )
                        """,
                        records,
                    )
                    inserted += len(batch)
                    if inserted % 5000 < _BATCH_SIZE:
                        print(f"  {inserted}/{len(rows)} tracks inserted...")

                print(f"  {inserted} tracks inserted.")

                # Backfill search_vector
                print("  Backfilling search_vector...")
                await pg.execute("""
                    UPDATE tracks SET search_vector =
                        setweight(to_tsvector('simple', COALESCE(artist, '')), 'A') ||
                        setweight(to_tsvector('simple', COALESCE(title, '')), 'B') ||
                        setweight(to_tsvector('simple', COALESCE(lyrics_text, '')), 'C')
                    WHERE search_vector IS NULL
                """)
                print("  search_vector backfilled.")

    # --- 2. Artists ---
    rows = conn.execute("SELECT * FROM artists ORDER BY name").fetchall()
    print(f"artists: {len(rows)} rows")

    if not dry_run and rows:
        async with pool.acquire() as pg:
            existing = await pg.fetchval("SELECT COUNT(*) FROM artists")
            if existing > 0:
                print(f"  WARNING: already has {existing} rows, skipping.")
            else:
                records = [
                    (r["name"], r["image_path"], r["source"],
                     _parse_ts(r["created_at"]), _parse_ts(r["updated_at"]))
                    for r in rows
                ]
                await pg.executemany(
                    """
                    INSERT INTO artists (name, image_path, source, created_at, updated_at)
                    VALUES ($1, $2, $3, $4::timestamptz, $5::timestamptz)
                    """,
                    records,
                )
                print(f"  {len(records)} artists inserted.")

    # --- 3. Catalog clusters ---
    rows = conn.execute("SELECT * FROM catalog_clusters ORDER BY id").fetchall()
    print(f"catalog_clusters: {len(rows)} rows")

    if not dry_run and rows:
        async with pool.acquire() as pg:
            existing = await pg.fetchval("SELECT COUNT(*) FROM catalog_clusters")
            if existing > 0:
                print(f"  WARNING: already has {existing} rows, skipping.")
            else:
                for r in rows:
                    # SERIAL id — need to insert with explicit id to preserve mapping
                    await pg.execute(
                        """
                        INSERT INTO catalog_clusters (id, centroid_audio, centroid_lyrics, track_count, created_at, updated_at)
                        VALUES ($1, $2::jsonb, $3::jsonb, $4, $5::timestamptz, $6::timestamptz)
                        """,
                        r["id"],
                        r["centroid_audio"],  # already JSON string
                        r["centroid_lyrics"],
                        r["track_count"],
                        _parse_ts(r["created_at"]),
                        _parse_ts(r["updated_at"]),
                    )
                # Reset sequence to max id + 1
                await pg.execute(
                    "SELECT setval('catalog_clusters_id_seq', (SELECT COALESCE(MAX(id), 0) FROM catalog_clusters))"
                )
                print(f"  {len(rows)} clusters inserted.")

    # --- 4. Mood tags ---
    rows = conn.execute("SELECT * FROM mood_tags ORDER BY id").fetchall()
    print(f"mood_tags: {len(rows)} rows")

    if not dry_run and rows:
        async with pool.acquire() as pg:
            existing = await pg.fetchval("SELECT COUNT(*) FROM mood_tags")
            if existing > 0:
                print(f"  WARNING: already has {existing} rows, skipping.")
            else:
                for r in rows:
                    await pg.execute(
                        """
                        INSERT INTO mood_tags (id, name, cluster_id, created_at)
                        VALUES ($1, $2, $3, $4::timestamptz)
                        """,
                        r["id"], r["name"], r["cluster_id"], _parse_ts(r["created_at"]),
                    )
                await pg.execute(
                    "SELECT setval('mood_tags_id_seq', (SELECT COALESCE(MAX(id), 0) FROM mood_tags))"
                )
                print(f"  {len(rows)} mood tags inserted.")

    # --- 5. Play history ---
    rows = conn.execute("SELECT * FROM play_history ORDER BY played_at").fetchall()
    print(f"play_history: {len(rows)} rows")

    if not dry_run and rows:
        async with pool.acquire() as pg:
            existing = await pg.fetchval("SELECT COUNT(*) FROM play_history")
            if existing > 0:
                print(f"  WARNING: already has {existing} rows, skipping.")
            else:
                records = [
                    (r["id"], r["session_id"], r["participant_id"],
                     r["track_id"], _parse_ts(r["played_at"]), r["completed"] if "completed" in r.keys() else 0)
                    for r in rows
                ]
                await pg.executemany(
                    """
                    INSERT INTO play_history (id, session_id, participant_id, track_id, played_at, completed)
                    VALUES ($1, $2, $3, $4, $5::timestamptz, $6)
                    """,
                    records,
                )
                print(f"  {len(records)} play_history rows inserted.")

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
