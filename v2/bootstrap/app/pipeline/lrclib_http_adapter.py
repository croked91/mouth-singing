"""HTTP client adapter for a remote lrclib server.

Same ``search()`` interface as ``LRCLibSQLiteAdapter`` but queries the
lrclib HTTP server (``lrclib_server.py``) running on a remote host.

Usage::

    adapter = LRCLibHTTPAdapter("http://130.49.170.186:9876")
    lrc = adapter.search("Кино", "Группа крови")
    adapter.close()
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

import structlog

logger = structlog.get_logger(__name__)


class LRCLibHTTPAdapter:
    """HTTP client adapter for remote lrclib server.

    Args:
        base_url: Base URL of the lrclib server (e.g. ``http://host:9876``).
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        logger.info("lrclib_http.connected", base_url=self._base_url)

    def search(self, artist: str, title: str) -> str | None:
        """Search for synced lyrics via the remote lrclib server.

        Args:
            artist: Track artist name.
            title: Track title.

        Returns:
            The raw LRC string if found, or ``None``.
        """
        params = urllib.parse.urlencode({"artist": artist, "title": title})
        url = f"{self._base_url}/search?{params}"

        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                lrc = data.get("synced_lyrics")
                if lrc:
                    return lrc
                return None
        except Exception:
            logger.warning("lrclib_http.request_failed", url=url)
            return None

    def close(self) -> None:
        """No-op — HTTP is stateless."""
        pass
