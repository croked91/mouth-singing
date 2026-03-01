#!/usr/bin/env python3
"""Minimal HTTP server wrapping an lrclib SQLite database.

Runs on VPS with zero external dependencies (stdlib only).
Exposes a single endpoint for synced lyrics lookup::

    GET /search?artist=Кино&title=Группа+крови

Returns JSON ``{"synced_lyrics": "..."}`` on hit, ``{}`` on miss.

Usage::

    python3 lrclib_server.py /path/to/lrclib.db [--port 9876]
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

_STRIP_CHARS_RE = re.compile(r"[&\-!+()\[\]]")


def _normalize(text: str) -> str:
    lowered = text.lower().strip()
    stripped = _STRIP_CHARS_RE.sub(" ", lowered)
    return " ".join(stripped.split())


class LRCHandler(BaseHTTPRequestHandler):
    db_path: str = ""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/search":
            self._json_response({"error": "not found"}, 404)
            return

        params = parse_qs(parsed.query)
        artist = params.get("artist", [""])[0]
        title = params.get("title", [""])[0]

        if not artist or not title:
            self._json_response({"error": "artist and title required"}, 400)
            return

        result = self._search(artist, title)
        if result:
            self._json_response({"synced_lyrics": result})
        else:
            self._json_response({})

    def _search(self, artist: str, title: str) -> str | None:
        artist_norm = _normalize(artist)
        title_norm = _normalize(title)

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # Exact match.
            cur = conn.execute(
                """
                SELECT l.synced_lyrics
                FROM tracks t
                JOIN lyrics l ON l.id = t.last_lyrics_id
                WHERE t.artist_name_lower = ?
                  AND t.name_lower = ?
                  AND l.synced_lyrics IS NOT NULL
                  AND l.synced_lyrics != ''
                LIMIT 1
                """,
                (artist_norm, title_norm),
            )
            row = cur.fetchone()
            if row:
                return row["synced_lyrics"]

            # Fallback: exact artist + title prefix.
            cur = conn.execute(
                """
                SELECT l.synced_lyrics
                FROM tracks t
                JOIN lyrics l ON l.id = t.last_lyrics_id
                WHERE t.artist_name_lower = ?
                  AND t.name_lower LIKE ?
                  AND l.synced_lyrics IS NOT NULL
                  AND l.synced_lyrics != ''
                LIMIT 1
                """,
                (artist_norm, f"{title_norm}%"),
            )
            row = cur.fetchone()
            if row:
                return row["synced_lyrics"]

            return None
        finally:
            conn.close()

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # Compact logging.
        sys.stderr.write(f"[lrclib] {args[0]} {args[1]}\n")


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <db_path> [--port PORT]", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    port = 9876
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    LRCHandler.db_path = db_path

    server = HTTPServer(("0.0.0.0", port), LRCHandler)
    print(f"lrclib server listening on 0.0.0.0:{port} (db: {db_path})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
