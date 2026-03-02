#!/usr/bin/env python3
"""
Find tracks for a list of artists in the lrclib SQLite dump.
Max 50 unique tracks per artist, deduplicated, with hitmotop search URLs.

Usage:
  python3 find_tracks_for_artists.py artists.txt /path/to/lrclib.sqlite3 output.csv
"""

import csv
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

MAX_PER_ARTIST = 50

_STRIP_CHARS_RE = re.compile(r"[&\-!+()\[\]]")


def _normalize(text: str) -> str:
    lowered = text.lower().strip()
    stripped = _STRIP_CHARS_RE.sub(" ", lowered)
    return " ".join(stripped.split())


def _make_search_url(artist: str, title: str) -> str:
    query = f"{artist} {title}"
    return f"https://rus.hitmotop.com/search?q={urllib.parse.quote(query)}"


def main():
    if len(sys.argv) != 4:
        print("Usage: python3 find_tracks_for_artists.py artists.txt lrclib.sqlite3 output.csv")
        sys.exit(1)

    artists_file = sys.argv[1]
    db_path = sys.argv[2]
    output_csv = sys.argv[3]

    artists = [
        line.strip()
        for line in Path(artists_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    print(f"Artists: {len(artists)}", flush=True)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Global dedup by (norm_artist, norm_title)
    seen: set[tuple[str, str]] = set()

    out = open(output_csv, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out, fieldnames=["Артист", "Название", "URL"])
    writer.writeheader()

    total = 0
    for i, artist in enumerate(artists, 1):
        norm = _normalize(artist)
        if not norm:
            continue

        cursor = conn.execute(
            "SELECT DISTINCT artist_name, name FROM tracks "
            "WHERE artist_name_lower = ? AND last_lyrics_id IS NOT NULL",
            (norm,),
        )

        count = 0
        for row in cursor:
            if count >= MAX_PER_ARTIST:
                break

            title = row["name"]
            key = (norm, _normalize(title))
            if key in seen:
                continue
            seen.add(key)

            writer.writerow({
                "Артист": row["artist_name"],
                "Название": title,
                "URL": _make_search_url(row["artist_name"], title),
            })
            count += 1

        out.flush()
        total += count
        print(f"[{i}/{len(artists)}] {artist}: {count} (total: {total})", flush=True)

    out.close()
    conn.close()
    print(f"\nDone! {total} tracks → {output_csv}", flush=True)


if __name__ == "__main__":
    main()
