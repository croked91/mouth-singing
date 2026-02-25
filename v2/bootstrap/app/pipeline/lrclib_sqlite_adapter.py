"""LRCLib SQLite database adapter.

Provides the same search interface as ``LRCLibDump`` but queries a large
SQLite database file directly, without loading it into memory.  Suitable
for databases that are too large to fit in RAM (e.g. the 78 GB lrclib dump).

The database is expected to have a ``tracks`` table with at least these
columns: ``artist_name``, ``name`` (track title), ``artist_name_lower``,
``synced_lyrics``.

Usage::

    adapter = LRCLibSQLiteAdapter(Path("/path/to/lrclib.db"))
    lrc = adapter.search("Кино", "Группа крови")
    if lrc:
        lines = LRCLibDump.parse_lrc(lrc)
    adapter.close()
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Characters stripped from artist names to match ``artist_name_lower`` in the DB.
_STRIP_CHARS_RE = re.compile(r"[&\-!]")


def _normalize_for_db(text: str) -> str:
    """Normalize text to match the lrclib ``artist_name_lower`` convention.

    The DB strips ``&``, ``-``, ``!`` characters and lowercases the result.

    Args:
        text: Raw artist or title string.

    Returns:
        Normalized string matching the DB convention.
    """
    lowered = text.lower().strip()
    stripped = _STRIP_CHARS_RE.sub(" ", lowered)
    collapsed = " ".join(stripped.split())
    return collapsed


class LRCLibSQLiteAdapter:
    """Read-only adapter for a large lrclib SQLite database.

    Opens the database in read-only mode and queries it on disk.  The OS
    page cache handles I/O buffering — no data is loaded into process memory.

    Args:
        db_path: Path to the lrclib SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        uri = f"file:{db_path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        logger.info("lrclib_sqlite.opened", db_path=str(db_path))

    def search(self, artist: str, title: str) -> str | None:
        """Search for synced lyrics matching the given artist and title.

        Uses the indexed ``artist_name_lower`` column for efficient artist
        matching and a normalized comparison for the title.  Tries exact
        match first, then falls back to LIKE wildcards.

        Args:
            artist: Track artist name.
            title: Track title.

        Returns:
            The raw LRC string (``synced_lyrics``) if found, or ``None``.
        """
        artist_norm = _normalize_for_db(artist)
        title_norm = _normalize_for_db(title)

        # Exact match on artist_name_lower + normalized title.
        cursor = self._conn.execute(
            """
            SELECT synced_lyrics FROM tracks
            WHERE artist_name_lower = ?
              AND LOWER(name) = ?
              AND synced_lyrics IS NOT NULL
              AND synced_lyrics != ''
            LIMIT 1
            """,
            (artist_norm, title_norm),
        )
        row = cursor.fetchone()
        if row:
            return row["synced_lyrics"]

        # Fallback: LIKE wildcards for partial matches.
        cursor = self._conn.execute(
            """
            SELECT synced_lyrics FROM tracks
            WHERE artist_name_lower LIKE ?
              AND LOWER(name) LIKE ?
              AND synced_lyrics IS NOT NULL
              AND synced_lyrics != ''
            LIMIT 1
            """,
            (f"%{artist_norm}%", f"%{title_norm}%"),
        )
        row = cursor.fetchone()
        if row:
            return row["synced_lyrics"]

        return None

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
        logger.debug("lrclib_sqlite.closed")
