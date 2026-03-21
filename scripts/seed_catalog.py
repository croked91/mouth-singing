"""Seed SQLite with tracks + jobs from MP3/TXT directories.

Usage (inside Docker):
    python3 -m scripts.seed_catalog \
        --audio-dir /input/audio \
        --lyrics-dir /input/lyrics \
        --db /data/sqlite/karaoke.db \
        --media-root /data/media

Files are matched by stem name:  "Artist - Title.mp3" ↔ "Artist - Title.txt"
Artist and title are parsed from filename via split(" - ", 1).
Language is detected from lyrics content (Cyrillic ratio).
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import shutil
import uuid
from datetime import datetime, timezone

import aiosqlite


def detect_language(text: str) -> str:
    """Detect from lyrics content: >50% Cyrillic → ru."""
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    lat = sum(1 for c in text if "A" <= c <= "z")
    return "ru" if cyr > lat else "en"


def parse_filename(stem: str) -> tuple[str, str] | None:
    """Parse 'Artist - Title' from filename stem.

    Returns (artist, title) or None if no ' - ' separator found.
    """
    if " - " not in stem:
        return None
    artist, title = stem.split(" - ", 1)
    artist, title = artist.strip(), title.strip()
    if not artist or not title:
        return None
    return artist, title


def read_lyrics(path: pathlib.Path) -> str | None:
    """Read lyrics file. Try UTF-8 first, fallback to cp1251 (Windows Cyrillic)."""
    for enc in ("utf-8", "cp1251"):
        try:
            text = path.read_text(encoding=enc)
            if text.strip():
                return text.strip()
        except (UnicodeDecodeError, ValueError):
            continue
    return None


async def main() -> None:
    p = argparse.ArgumentParser(description="Seed database from MP3 + TXT dirs")
    p.add_argument("--audio-dir", required=True, help="Directory with *.mp3 files")
    p.add_argument("--lyrics-dir", required=True, help="Directory with *.txt files")
    p.add_argument("--db", required=True, help="Path to SQLite database")
    p.add_argument("--media-root", required=True, help="Media root for uploads/")
    p.add_argument("--dry-run", action="store_true", help="Report only, don't write")
    args = p.parse_args()

    audio_dir = pathlib.Path(args.audio_dir)
    lyrics_dir = pathlib.Path(args.lyrics_dir)
    uploads_dir = pathlib.Path(args.media_root) / "uploads"

    if not audio_dir.is_dir():
        print(f"ERROR: audio-dir does not exist: {audio_dir}")
        return
    if not lyrics_dir.is_dir():
        print(f"ERROR: lyrics-dir does not exist: {lyrics_dir}")
        return

    if not args.dry_run:
        uploads_dir.mkdir(parents=True, exist_ok=True)

    # Scan MP3 files
    mp3s = sorted(audio_dir.glob("*.mp3"))
    print(f"MP3 files found: {len(mp3s)}")

    # Build lyrics lookup (stem → path)
    lkup: dict[str, pathlib.Path] = {}
    for txt_path in lyrics_dir.glob("*.txt"):
        lkup[txt_path.stem] = txt_path
    print(f"TXT files found: {len(lkup)}")

    # Open database
    conn = await aiosqlite.connect(args.db)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")

    stats = {
        "seeded": 0,
        "dup": 0,
        "no_txt": 0,
        "bad_enc": 0,
        "bad_name": 0,
        "copy_err": 0,
    }
    now = datetime.now(timezone.utc).isoformat()

    try:
        await conn.execute("BEGIN")

        for i, mp3 in enumerate(mp3s):
            # Parse artist/title from filename
            parsed = parse_filename(mp3.stem)
            if parsed is None:
                stats["bad_name"] += 1
                continue
            artist, title = parsed

            # Find matching lyrics file
            txt = lkup.get(mp3.stem)
            if txt is None:
                stats["no_txt"] += 1
                continue

            # Read lyrics (UTF-8 / cp1251 fallback)
            lyrics = read_lyrics(txt)
            if lyrics is None:
                stats["bad_enc"] += 1
                continue

            # Deduplication: skip if artist+title already in DB
            cur = await conn.execute(
                "SELECT id FROM tracks WHERE artist = ? AND title = ? LIMIT 1",
                (artist, title),
            )
            if await cur.fetchone() is not None:
                stats["dup"] += 1
                continue

            if args.dry_run:
                stats["seeded"] += 1
                if (i + 1) % 5000 == 0:
                    print(f"  [dry-run] {i + 1}/{len(mp3s)} scanned")
                continue

            track_id = str(uuid.uuid4())
            lang = detect_language(lyrics)
            dest = uploads_dir / f"{track_id}.mp3"

            # Copy MP3 to uploads/ (not symlink — Docker path safety)
            try:
                shutil.copy2(mp3, dest)
            except OSError as exc:
                print(f"  WARN: copy failed for {mp3.name}: {exc}")
                stats["copy_err"] += 1
                continue

            # INSERT track
            await conn.execute(
                """INSERT INTO tracks
                   (id, artist, title, mp3_path, lyrics_text, language,
                    source, status, play_count, qdrant_synced,
                    popularity_category, chart_count, created_at, updated_at)
                   VALUES (?,?,?,?,?,?, 'catalog','pending',
                           0,0,'regular',0, ?,?)""",
                (track_id, artist, title, str(dest), lyrics, lang, now, now),
            )

            # INSERT job
            job_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO job_queue
                   (id, track_id, priority, status, attempts,
                    max_attempts, progress, created_at, updated_at)
                   VALUES (?,?, 1,'pending', 0,3, 0, ?,?)""",
                (job_id, track_id, now, now),
            )

            stats["seeded"] += 1

            # Batch commit every 1000 records (avoid 53k individual fsyncs)
            if (i + 1) % 1000 == 0:
                await conn.commit()
                await conn.execute("BEGIN")
                print(f"  {i + 1}/{len(mp3s)} (seeded: {stats['seeded']})")

        # Final commit
        await conn.commit()

    finally:
        await conn.close()

    print(f"\n{'=== Dry-run complete ===' if args.dry_run else '=== Seed complete ==='}")
    print(f"  Seeded:             {stats['seeded']}")
    print(f"  Skipped (dup):      {stats['dup']}")
    print(f"  Skipped (no txt):   {stats['no_txt']}")
    print(f"  Skipped (encoding): {stats['bad_enc']}")
    print(f"  Skipped (bad name): {stats['bad_name']}")
    if stats["copy_err"]:
        print(f"  Copy errors:        {stats['copy_err']}")


if __name__ == "__main__":
    asyncio.run(main())
