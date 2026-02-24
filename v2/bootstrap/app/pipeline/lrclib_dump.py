"""LRC library dump handler.

Loads a JSON-lines dump from lrc-lib into a temporary in-memory SQLite database
for fast fuzzy searching by artist and title. The dump format is one JSON
object per line:

    {"artist": "...", "title": "...", "lrc": "[00:12.34]First line\\n..."}

The search normalises both the query and the stored values (lowercase, strip
articles, strip non-alphanumeric characters) so that minor differences in
punctuation or capitalisation do not prevent a match.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Articles stripped from the start of artist/title strings before matching.
_ARTICLES = {"the", "a", "an"}

# Compiled pattern for removing non-alphanumeric characters (keeps spaces).
_NON_ALNUM_RE = re.compile(r"[^\w\s]", re.UNICODE)

# LRC timestamp pattern: [MM:SS.xx] or [MM:SS.xxx]
_LRC_TIMESTAMP_RE = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\]")


def _normalise(text: str) -> str:
    """Normalise artist or title for fuzzy matching.

    Steps:
    1. Lowercase.
    2. Strip leading articles ("the", "a", "an").
    3. Remove non-alphanumeric characters (punctuation, hyphens, etc.).
    4. Collapse multiple spaces to one and strip.

    Args:
        text: Raw artist or title string.

    Returns:
        Normalised string suitable for LIKE comparison.
    """
    lowered = text.lower().strip()

    # Strip leading article followed by a space.
    for article in _ARTICLES:
        prefix = article + " "
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break

    without_punct = _NON_ALNUM_RE.sub(" ", lowered)
    collapsed = " ".join(without_punct.split())
    return collapsed


class LRCLibDump:
    """In-memory index of an lrc-lib JSON dump for fast fuzzy search.

    The entire dump is loaded into an in-memory SQLite database at
    construction time. Searches use normalised LIKE queries so minor
    differences in punctuation or casing are tolerated.

    Args:
        dump_path: Path to the lrc-lib dump file (one JSON object per line).
    """

    def __init__(self, dump_path: Path) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._build_index(dump_path)

    def _build_index(self, dump_path: Path) -> None:
        """Load the dump file into the in-memory SQLite database.

        Creates a table with normalised artist/title columns alongside the
        raw LRC text. Builds an index on the normalised columns for fast
        LIKE queries.

        Args:
            dump_path: Path to the JSON-lines dump file.
        """
        self._conn.execute(
            """
            CREATE TABLE lrc_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_norm TEXT NOT NULL,
                title_norm  TEXT NOT NULL,
                lrc_text    TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX idx_artist_title ON lrc_entries (artist_norm, title_norm)"
        )

        inserted = 0
        skipped = 0

        with open(dump_path, encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue

                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    logger.warning(
                        "lrclib_dump.invalid_json",
                        line=line_no,
                        preview=raw_line[:80],
                    )
                    skipped += 1
                    continue

                artist = record.get("artist", "")
                title = record.get("title", "")
                lrc = record.get("lrc", "")

                if not artist or not title or not lrc:
                    skipped += 1
                    continue

                self._conn.execute(
                    "INSERT INTO lrc_entries (artist_norm, title_norm, lrc_text)"
                    " VALUES (?, ?, ?)",
                    (_normalise(artist), _normalise(title), lrc),
                )
                inserted += 1

                if inserted % 100_000 == 0:
                    # Commit periodically to keep memory usage predictable.
                    self._conn.commit()
                    logger.info("lrclib_dump.loading", inserted=inserted)

        self._conn.commit()
        logger.info(
            "lrclib_dump.loaded",
            dump_path=str(dump_path),
            inserted=inserted,
            skipped=skipped,
        )

    def search(self, artist: str, title: str) -> str | None:
        """Search for an LRC string matching the given artist and title.

        Both the query values and the stored values are normalised before
        comparison. The search uses SQL LIKE with a leading and trailing
        wildcard to accommodate minor differences in punctuation.

        Args:
            artist: Track artist name.
            title: Track title.

        Returns:
            The raw LRC string if a match is found, or ``None``.
        """
        artist_norm = _normalise(artist)
        title_norm = _normalise(title)

        # Try an exact normalised match first (fast, uses index).
        cursor = self._conn.execute(
            "SELECT lrc_text FROM lrc_entries"
            " WHERE artist_norm = ? AND title_norm = ?"
            " LIMIT 1",
            (artist_norm, title_norm),
        )
        row = cursor.fetchone()
        if row:
            return row["lrc_text"]

        # Fall back to LIKE wildcards for partial matches.
        cursor = self._conn.execute(
            "SELECT lrc_text FROM lrc_entries"
            " WHERE artist_norm LIKE ? AND title_norm LIKE ?"
            " LIMIT 1",
            (f"%{artist_norm}%", f"%{title_norm}%"),
        )
        row = cursor.fetchone()
        if row:
            return row["lrc_text"]

        return None

    @staticmethod
    def parse_lrc(lrc_text: str) -> list[dict]:
        """Parse an LRC format string into a list of timed lyric lines.

        Each LRC line has the format ``[MM:SS.xx]Lyric text``.  Lines without
        a valid timestamp are ignored.  End times are inferred from the start
        of the next line; the last line is given a 3-second end time.

        Args:
            lrc_text: Raw LRC string with timestamp tags.

        Returns:
            List of dicts with keys ``"text"`` (str), ``"start_ms"`` (int),
            and ``"end_ms"`` (int).  Ordered by start time.
        """
        timed_lines: list[tuple[int, str]] = []

        for raw_line in lrc_text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            match = _LRC_TIMESTAMP_RE.match(raw_line)
            if not match:
                continue

            minutes = int(match.group(1))
            seconds = int(match.group(2))
            centiseconds_raw = match.group(3)

            # Normalise to centiseconds regardless of 2 or 3 decimal digits.
            if len(centiseconds_raw) == 3:
                centiseconds = int(centiseconds_raw) // 10
            else:
                centiseconds = int(centiseconds_raw)

            start_ms = (minutes * 60 + seconds) * 1000 + centiseconds * 10

            # The text follows the closing bracket.
            text = raw_line[match.end():].strip()
            if text:
                timed_lines.append((start_ms, text))

        timed_lines.sort(key=lambda item: item[0])

        result: list[dict] = []
        for i, (start_ms, text) in enumerate(timed_lines):
            if i + 1 < len(timed_lines):
                end_ms = timed_lines[i + 1][0]
            else:
                end_ms = start_ms + 3000  # 3-second tail for the last line

            result.append({"text": text, "start_ms": start_ms, "end_ms": end_ms})

        return result

    def close(self) -> None:
        """Close and discard the in-memory SQLite database."""
        self._conn.close()
        logger.debug("lrclib_dump.closed")
