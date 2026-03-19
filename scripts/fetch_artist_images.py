#!/usr/bin/env python3
"""Fetch artist images from Spotify and save locally.

Usage::

    python scripts/fetch_artist_images.py \\
        --db /data/sqlite/karaoke.db \\
        --output-dir /data/media/artists \\
        --spotify-client-id YOUR_ID \\
        --spotify-client-secret YOUR_SECRET

    python scripts/fetch_artist_images.py --db /data/sqlite/karaoke.db --dry-run
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

HEADERS = {
    "User-Agent": "KaraokeApp/1.0",
}


def get_spotify_token(client_id: str, client_secret: str) -> str | None:
    """Get Spotify API access token via client credentials flow."""
    try:
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:
        print(f"  [ERROR] Failed to get Spotify token: {exc}")
        return None


def search_spotify_artist(name: str, token: str) -> str | None:
    """Search for artist image URL on Spotify."""
    try:
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": name, "type": "artist", "limit": 1},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("artists", {}).get("items", [])
        if items and items[0].get("images"):
            # Get medium-sized image (usually index 1)
            images = items[0]["images"]
            img = images[1] if len(images) > 1 else images[0]
            return img["url"]
    except Exception:
        pass
    return None


def download_image(url: str, output_dir: Path, name: str) -> str | None:
    """Download image and save with hashed filename. Returns relative path."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        # Use hash of artist name for filename
        name_hash = hashlib.md5(name.encode()).hexdigest()[:12]
        ext = "jpg"
        filename = f"{name_hash}.{ext}"
        filepath = output_dir / filename

        filepath.write_bytes(resp.content)
        return filename
    except Exception as exc:
        print(f"  [WARN] Failed to download image for {name}: {exc}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch artist images")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--output-dir", default="/data/media/artists", help="Image output directory")
    parser.add_argument("--spotify-client-id", default=None)
    parser.add_argument("--spotify-client-secret", default=None)
    parser.add_argument("--limit", type=int, default=0, help="Max artists to process (0=all)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Find artists without images
    cursor = conn.execute("""
        SELECT DISTINCT t.artist FROM tracks t
        LEFT JOIN artists a ON t.artist = a.name
        WHERE (a.image_path IS NULL OR a.name IS NULL)
          AND t.status = 'ready'
        ORDER BY t.play_count DESC
    """)
    artists = [row[0] for row in cursor.fetchall()]
    if args.limit > 0:
        artists = artists[:args.limit]

    print(f"Found {len(artists)} artists without images")

    if args.dry_run:
        for a in artists[:20]:
            print(f"  {a}")
        if len(artists) > 20:
            print(f"  ... and {len(artists) - 20} more")
        conn.close()
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get Spotify token
    token = None
    if args.spotify_client_id and args.spotify_client_secret:
        token = get_spotify_token(args.spotify_client_id, args.spotify_client_secret)
        if token:
            print("Spotify auth OK")

    now = datetime.now(timezone.utc).isoformat()
    fetched = 0
    failed = 0

    for i, name in enumerate(artists):
        image_url = None
        source = "placeholder"

        if token:
            image_url = search_spotify_artist(name, token)
            if image_url:
                source = "spotify"

        if image_url:
            filename = download_image(image_url, output_dir, name)
            if filename:
                conn.execute(
                    """
                    INSERT INTO artists (name, image_path, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET image_path = ?, source = ?, updated_at = ?
                    """,
                    (name, filename, source, now, now, filename, source, now),
                )
                fetched += 1
            else:
                failed += 1
        else:
            # Insert placeholder record
            conn.execute(
                """
                INSERT INTO artists (name, image_path, source, created_at, updated_at)
                VALUES (?, NULL, 'placeholder', ?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                (name, now, now),
            )
            failed += 1

        if (i + 1) % 50 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(artists)} (fetched: {fetched}, failed: {failed})")

    conn.commit()
    conn.close()
    print(f"\nDone. Fetched: {fetched}, Failed/Placeholder: {failed}")


if __name__ == "__main__":
    main()
