"""Two-step lyrics search with web-search fallback.

Primary path (cheap, ~$0.0005):
  Step 1: gpt-4o-mini identifies artist + title from ASR transcript.
  Step 2: Genius API search + page scrape for real lyrics.

Fallback path (when primary fails, ~$0.003):
  gpt-4o-mini with web_search tool finds lyrics page URLs.
  We scrape the found URLs ourselves.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)

_IDENTIFY_PROMPT = """\
You are a music identification assistant. The user provides an approximate \
speech recognition transcript of a song (may contain errors), the detected \
language, and optionally the artist/title from the filename.

Your task: identify the song from the transcript.

Return ONLY a JSON object with these fields:
- "found": true/false
- "artist": string (canonical artist name, in the song's original language)
- "title": string (canonical song title, in the song's original language)
- "confidence": "high" | "medium" | "low"
- "not_found_reason": string (only if found=false)

IMPORTANT:
- Return ONLY the JSON, no markdown, no explanation.
- If you cannot identify the song, return found=false.
- Use the canonical (correct) spelling of artist and title."""

_WEB_SEARCH_PROMPT = """\
You identify songs and find lyrics page URLs for a karaoke system.
Search the web and return a JSON object:
{"found":true, "artist":"canonical name", "title":"canonical title", \
"lyrics_urls":["url1","url2"]}

Return 1-3 URLs to lyrics pages (genius.com, amalgama-lab.com, \
teksty-pesenok.ru, musixmatch.com, or similar).
Return ONLY the JSON, no other text.
If the song cannot be found, return {"found":false, "reason":"..."}"""

_EXTRACT_PROMPT = """\
Extract song lyrics from the web page text below.

Rules:
- Return ONLY the lyrics lines, from the very first to the very last
- Remove navigation, headers, footers, ads, metadata, comments
- Remove section labels like [Verse], [Chorus], [Intro], [Куплет], [Припев]
- Keep the original line breaks. Separate verses with one blank line.
- Output must be COMPLETE — every line from start to finish
- If lyrics not found, output exactly: NOT_FOUND

<example_input>
LyricsSite | Song Title by Artist
Home Search
[Verse 1]
First line of the song
Second line of the song
[Chorus]
Chorus line one
Chorus line two
[Verse 2]
Third line here
Fourth line here
Share | Comments (5)
</example_input>
<example_output>
First line of the song
Second line of the song

Chorus line one
Chorus line two

Third line here
Fourth line here
</example_output>"""

_GENIUS_SEARCH_URL = "https://api.genius.com/search"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


@dataclass
class LyricsResult:
    """Structured lyrics search result."""
    artist: str
    title: str
    lyrics: str
    language: str
    confidence: str
    source_note: str


class LyricsSearchError(Exception):
    """Base class for lyrics search errors."""


class LyricsNotFoundError(LyricsSearchError):
    """Song could not be identified or lyrics not found."""


class LyricsAPIError(LyricsSearchError):
    """Network or API error (retryable)."""


class LyricsSearcher:
    """Lyrics searcher with Genius primary + web-search fallback.

    Args:
        openai_api_key: OpenAI API key for song identification.
        genius_token: Genius API bearer token for search + scraping.
        model: OpenAI model name.
        timeout: HTTP timeout in seconds.
        max_retries: Number of retries on transient errors.
        openai_base_url: Override for testing.
    """

    def __init__(
        self,
        openai_api_key: str,
        genius_token: str,
        model: str = "gpt-4o-mini",
        timeout: float = 30.0,
        max_retries: int = 2,
        openai_base_url: str = "https://api.openai.com",
    ) -> None:
        self._openai_key = openai_api_key
        self._genius_token = genius_token
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._openai_base_url = openai_base_url.rstrip("/")

    async def search(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None = None,
        title_hint: str | None = None,
    ) -> LyricsResult:
        """Search for song lyrics using ASR text.

        Primary: LLM identify → Genius search + scrape.
        Fallback: web_search → scrape found URLs.

        Raises:
            LyricsNotFoundError: If all paths exhausted.
            LyricsAPIError: If API requests fail after retries.
        """
        # === PRIMARY PATH ===
        primary_error: Exception | None = None
        try:
            return await self._primary_path(
                asr_text, detected_language, artist_hint, title_hint,
            )
        except LyricsSearchError as exc:
            primary_error = exc
            logger.warning("primary_path_failed", error=str(exc))

        # === FALLBACK: web search ===
        logger.info("trying_web_search_fallback")
        try:
            return await self._web_search_fallback(
                asr_text, detected_language, artist_hint, title_hint,
            )
        except LyricsSearchError as exc:
            logger.warning("web_search_fallback_failed", error=str(exc))
            raise LyricsNotFoundError(
                f"Primary: {primary_error}; Fallback: {exc}"
            ) from exc

    # ==================================================================
    # Primary path: LLM identify → Genius
    # ==================================================================

    async def _primary_path(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None,
        title_hint: str | None,
    ) -> LyricsResult:
        identification = await self._identify_song(
            asr_text, detected_language, artist_hint, title_hint,
        )
        artist = identification["artist"]
        title = identification["title"]
        confidence = identification["confidence"]

        logger.info("song_identified", artist=artist, title=title, confidence=confidence)

        lyrics = await self._fetch_genius_lyrics(artist, title)

        return LyricsResult(
            artist=artist,
            title=title,
            lyrics=lyrics,
            language=detected_language,
            confidence=confidence,
            source_note="genius.com",
        )

    # ==================================================================
    # Fallback: web_search → scrape URLs
    # ==================================================================

    async def _web_search_fallback(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None,
        title_hint: str | None,
    ) -> LyricsResult:
        """Use OpenAI web_search to find lyrics page URLs, then scrape."""
        user_parts = []
        if artist_hint or title_hint:
            user_parts.append(
                f"Song: {artist_hint or '?'} — {title_hint or '?'}"
            )
        user_parts.append(
            f"ASR transcript (may have errors): {asr_text[:500]}"
        )
        user_parts.append(f"Language: {detected_language}")

        payload = {
            "model": self._model,
            "tools": [{"type": "web_search_preview"}],
            "input": [
                {"role": "developer", "content": _WEB_SEARCH_PROMPT},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
            "temperature": 0.0,
        }

        headers = {
            "Authorization": f"Bearer {self._openai_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{self._openai_base_url}/v1/responses",
                json=payload,
                headers=headers,
            )

        if resp.status_code >= 400:
            raise LyricsAPIError(
                f"Web search API error {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        response_text = self._extract_responses_text(data)
        ws_result = self._parse_web_search_response(response_text)

        artist = ws_result["artist"]
        title = ws_result["title"]
        urls = ws_result.get("lyrics_urls", [])

        logger.info(
            "web_search_identified",
            artist=artist, title=title, urls_count=len(urls),
        )

        # Try Genius first if we got a new artist+title
        try:
            lyrics = await self._fetch_genius_lyrics(artist, title)
            return LyricsResult(
                artist=artist, title=title, lyrics=lyrics,
                language=detected_language, confidence="medium",
                source_note="web_search+genius",
            )
        except LyricsSearchError:
            logger.debug("genius_failed_after_web_search")

        # Try scraping the URLs web search returned
        for url in urls:
            try:
                lyrics = await self._scrape_generic_page(url)
                if lyrics and len(lyrics) >= 50:
                    return LyricsResult(
                        artist=artist, title=title, lyrics=lyrics,
                        language=detected_language, confidence="medium",
                        source_note=f"web_search+{_domain(url)}",
                    )
            except Exception as exc:
                logger.debug("url_scrape_failed", url=url, error=str(exc))

        raise LyricsNotFoundError(
            f"Web search found {artist} - {title} but no scrapeable lyrics"
        )

    @staticmethod
    def _extract_responses_text(data: dict) -> str:
        """Extract text content from OpenAI Responses API output."""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content["text"]
        raise LyricsAPIError("No text in web search response")

    @staticmethod
    def _parse_web_search_response(text: str) -> dict:
        """Parse web search JSON response."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    raise LyricsAPIError("Invalid JSON from web search")
            else:
                raise LyricsAPIError("Invalid JSON from web search")

        if not data.get("found", False):
            raise LyricsNotFoundError(
                data.get("reason", "Web search could not find song")
            )
        if not data.get("artist") or not data.get("title"):
            raise LyricsNotFoundError("Web search returned empty artist/title")
        return data

    # ==================================================================
    # LLM identification (Step 1 of primary path)
    # ==================================================================

    async def _identify_song(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None,
        title_hint: str | None,
    ) -> dict:
        """Use OpenAI to identify artist + title from ASR text."""
        user_msg = (
            f"Approximate transcript (may have errors): {asr_text}\n"
            f"Detected language: {detected_language}\n"
            f"Artist hint: {artist_hint or 'unknown'}\n"
            f"Title hint: {title_hint or 'unknown'}"
        )

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _IDENTIFY_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
        }

        headers = {
            "Authorization": f"Bearer {self._openai_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None

        for attempt in range(1 + self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._openai_base_url}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    )

                if resp.status_code == 429:
                    logger.warning("identify_rate_limited", attempt=attempt)
                    if attempt < self._max_retries:
                        await asyncio.sleep(5.0)
                        continue
                    raise LyricsAPIError(f"Rate limited after {attempt + 1} attempts")

                if resp.status_code >= 500:
                    logger.warning("identify_server_error", status=resp.status_code)
                    if attempt < self._max_retries:
                        await asyncio.sleep(2.0)
                        continue
                    raise LyricsAPIError(f"Server error {resp.status_code}")

                if resp.status_code >= 400:
                    raise LyricsAPIError(f"API error {resp.status_code}: {resp.text[:200]}")

                response_text = resp.json()["choices"][0]["message"]["content"]
                return self._parse_identification(response_text)

            except (LyricsNotFoundError, LyricsAPIError):
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("identify_http_error", error=str(exc), attempt=attempt)
                if attempt < self._max_retries:
                    await asyncio.sleep(2.0)
                    continue
            except (KeyError, IndexError) as exc:
                raise LyricsAPIError(f"Unexpected response format: {exc}") from exc

        raise LyricsAPIError(f"All retries exhausted: {last_error}")

    @staticmethod
    def _parse_identification(response_text: str) -> dict:
        """Parse LLM JSON response into identification dict."""
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    raise LyricsAPIError("Invalid JSON from identification LLM")
            else:
                raise LyricsAPIError("Invalid JSON from identification LLM")

        if not data.get("found", False):
            raise LyricsNotFoundError(
                data.get("not_found_reason", "Song not identified")
            )

        if not data.get("artist") or not data.get("title"):
            raise LyricsNotFoundError("LLM returned empty artist/title")

        return data

    # ==================================================================
    # Genius search + scrape
    # ==================================================================

    async def _fetch_genius_lyrics(self, artist: str, title: str) -> str:
        """Search Genius for the song and scrape lyrics from the page."""
        song_url = await self._genius_search(artist, title)
        lyrics = await self._scrape_genius_page(song_url)

        if not lyrics or len(lyrics) < 20:
            raise LyricsNotFoundError(
                f"Genius page has no lyrics for {artist} - {title}"
            )

        return lyrics

    async def _genius_search(self, artist: str, title: str) -> str:
        """Search Genius API and return the best matching song URL."""
        query = f"{artist} {title}"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                _GENIUS_SEARCH_URL,
                params={"q": query},
                headers={"Authorization": f"Bearer {self._genius_token}"},
            )

        if resp.status_code != 200:
            raise LyricsAPIError(f"Genius search failed: {resp.status_code}")

        data = resp.json()
        hits = data.get("response", {}).get("hits", [])

        if not hits:
            raise LyricsNotFoundError(
                f"No results on Genius for '{query}'"
            )

        song_url = hits[0]["result"]["url"]
        song_title = hits[0]["result"]["full_title"]
        logger.info("genius_hit", url=song_url, full_title=song_title)
        return song_url

    async def _scrape_genius_page(self, url: str) -> str:
        """Fetch a Genius lyrics page and extract clean lyrics text."""
        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers=_BROWSER_HEADERS)

        if resp.status_code != 200:
            raise LyricsAPIError(f"Genius page fetch failed: {resp.status_code}")

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")
        containers = soup.select('[data-lyrics-container="true"]')

        if not containers:
            raise LyricsNotFoundError(f"No lyrics containers on {url}")

        parts: list[str] = []
        for container in containers:
            for br in container.find_all("br"):
                br.replace_with("\n")
            parts.append(container.get_text())

        raw = "\n".join(parts).strip()
        return _clean_lyrics(raw)

    # ==================================================================
    # Generic page scraper (for fallback URLs)
    # ==================================================================

    async def _scrape_generic_page(self, url: str) -> str:
        """Scrape lyrics from an arbitrary lyrics page URL.

        Tries CSS selectors first (fast, free), then LLM extraction (slower).
        """
        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers=_BROWSER_HEADERS)

        if resp.status_code != 200:
            raise LyricsAPIError(f"Page fetch failed: {resp.status_code} for {url}")

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")

        # --- Fast path: known CSS selectors ---

        # Genius-style containers
        containers = soup.select('[data-lyrics-container="true"]')
        if containers:
            parts = []
            for c in containers:
                for br in c.find_all("br"):
                    br.replace_with("\n")
                parts.append(c.get_text())
            return _clean_lyrics("\n".join(parts).strip())

        # Elements with 'lyric' in class name
        lyric_els = soup.select('[class*=lyric]')
        if lyric_els:
            texts = []
            for el in lyric_els:
                for br in el.find_all("br"):
                    br.replace_with("\n")
                texts.append((len(el.get_text()), el.get_text()))
            texts.sort(reverse=True)
            if texts and texts[0][0] >= 50:
                return _clean_lyrics(texts[0][1].strip())

        # <pre> tags
        for pre in soup.find_all("pre"):
            text = pre.get_text().strip()
            if len(text) >= 100:
                return _clean_lyrics(text)

        # --- Slow path: LLM extraction ---
        return await self._llm_extract_lyrics(soup)

    async def _llm_extract_lyrics(self, soup) -> str:
        """Use gpt-4o-mini to extract lyrics from page text."""
        # Strip non-content tags and get visible text
        for tag in soup.find_all(["script", "style", "noscript", "svg"]):
            tag.decompose()
        page_text = soup.get_text(separator="\n", strip=True)
        page_text = re.sub(r"\n{3,}", "\n\n", page_text)

        # Truncate to fit context (~10k chars ≈ 3k tokens)
        page_text = page_text[:10000]

        if len(page_text) < 50:
            raise LyricsNotFoundError("Page has too little text for extraction")

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _EXTRACT_PROMPT},
                {"role": "user", "content": page_text},
            ],
            "temperature": 0.0,
            "max_tokens": 4096,
        }

        headers = {
            "Authorization": f"Bearer {self._openai_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._openai_base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            )

        if resp.status_code >= 400:
            raise LyricsAPIError(f"LLM extract API error: {resp.status_code}")

        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()

        if text == "NOT_FOUND" or len(text) < 30:
            raise LyricsNotFoundError("LLM could not extract lyrics from page")

        return _clean_lyrics(text)


# ======================================================================
# Helpers
# ======================================================================

def _clean_lyrics(raw: str) -> str:
    """Clean scraped lyrics text."""
    lines = raw.split("\n")

    # Skip Genius header noise — find first [Section] marker
    clean_lines: list[str] = []
    started = False
    for line in lines:
        if not started:
            if re.match(r"^\[", line):
                started = True
                clean_lines.append(line)
        else:
            clean_lines.append(line)

    # If no section markers, use all text
    lyrics = "\n".join(clean_lines).strip() if clean_lines else raw

    # Remove section markers like [Intro], [Verse 1], [Припев]
    lyrics = re.sub(r"\[.*?\]\n?", "", lyrics).strip()

    # Collapse 3+ blank lines into 2
    lyrics = re.sub(r"\n{3,}", "\n\n", lyrics)

    return lyrics


def _domain(url: str) -> str:
    """Extract domain from URL for logging."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return url[:50]
