"""Lyrics search agent using DeepSeek LLM with Yandex web search.

Uses an agentic tool-calling loop: the LLM can invoke web_search (Yandex)
and fetch_webpage (httpx + BeautifulSoup) to find original lyrics online,
then returns a structured JSON with artist, title, and lyrics.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import xml.etree.ElementTree as ET

import httpx
import structlog
from bs4 import BeautifulSoup
from openai import OpenAI

from worker.common.lyrics_searcher import (
    LyricsAPIError,
    LyricsNotFoundError,
    LyricsResult,
    LyricsSearchError,
    clean_lyrics,
)

logger = structlog.get_logger(__name__)

_YANDEX_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"

_SYSTEM_PROMPT = """\
Ты — бот для поиска текстов песен. На вход приходит приблизительная расшифровка \
от Whisper (с ошибками). Найди оригинал в интернете через web_search и \
fetch_webpage.

Правила поиска:
- Whisper искажает слова. ИСПРАВЬ очевидные ошибки перед поиском.
- Начинай с ПРОСТОГО запроса: 2-3 ключевых слова + "текст песни" (или "lyrics" \
  для английских песен).
- НЕ ставь больше одной фразы в кавычках. Длинные фразы в кавычках НЕ работают.
- Загружай 2-3 страницы с текстами, сравни с входным — выбери ту, где \
  совпадают фразы.
- Если текст на странице не совпадает — пробуй другие ссылки.
- Если после 3-4 попыток ничего не нашёл — ответь ровно: текст не найден

Формат ответа:
Верни JSON-объект с тремя полями:
{"artist": "каноническое имя исполнителя", "title": "каноническое название \
песни", "lyrics": "полный текст песни"}

Правила для текста:
- Полный текст от первой до последней строки.
- Куплеты разделяй пустой строкой.
- Без аккордов и пометок [Куплет]/[Припев]/[Verse]/[Chorus].
- Без комментариев, заголовков или пояснений ВНЕ JSON.

## Пример

ВХОД:
белые розы белые розы беззащитный шипы сердца колючие что с ними \
сделаешь дни пролетая словно стрелой поп отчего так бывает

ВЫХОД:
{"artist": "Ласковый май", "title": "Белые розы", "lyrics": "Белые розы, \
белые розы\\nБеззащитны шипы\\nЧто с ними сделаешь\\nЧто с ними сделаешь\\n\\n\
Дни пролетали, словно стрелой\\nНо отчего так бывает\\nПадает вниз белый, \
белый цветок\\nИ на землю тихо ложатся\\nЛепестки увядших цветов"}
"""

_METADATA_PROMPT = """\
Определи исполнителя и название песни по тексту. \
Верни ТОЛЬКО JSON: {"artist": "...", "title": "..."}"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете. Используй для поиска текстов песен.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": (
                "Загрузить веб-страницу и извлечь текстовое содержимое. "
                "Используй после web_search, чтобы получить полный текст со страницы."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL страницы для загрузки",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

_NOT_FOUND_MARKERS = ("текст не найден", "lyrics not found", "not found")

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ======================================================================
# Tool implementations (synchronous — run via asyncio.to_thread)
# ======================================================================


def _web_search(
    query: str,
    api_key: str,
    folder_id: str,
    timeout: float,
) -> str:
    """Search via Yandex Search API."""
    try:
        response = httpx.post(
            _YANDEX_SEARCH_URL,
            headers={
                "Authorization": f"Api-Key {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": {
                    "searchType": "SEARCH_TYPE_RU",
                    "queryText": query,
                    "familyMode": "FAMILY_MODE_NONE",
                    "page": 0,
                },
                "folderId": folder_id,
                "groupSpec": {
                    "groupMode": "GROUP_MODE_FLAT",
                    "groupsOnPage": 10,
                    "docsInGroup": 1,
                },
                "maxPassages": 2,
                "l10n": "LOCALIZATION_RU",
                "responseFormat": "FORMAT_XML",
            },
            timeout=timeout,
        )
        response.raise_for_status()

        raw_xml = base64.b64decode(response.json()["rawData"]).decode("utf-8")
        root = ET.fromstring(raw_xml)

        results = []
        for doc in root.findall(".//{*}doc"):
            url_el = doc.find("{*}url")
            title_el = doc.find("{*}title")
            passage_el = doc.find("{*}passages/{*}passage")
            if url_el is not None:
                results.append({
                    "title": (
                        "".join(title_el.itertext()).strip()
                        if title_el is not None
                        else ""
                    ),
                    "href": url_el.text.strip(),
                    "body": (
                        "".join(passage_el.itertext()).strip()
                        if passage_el is not None
                        else ""
                    ),
                })

        if not results:
            return json.dumps({"error": "Ничего не найдено"}, ensure_ascii=False)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _fetch_webpage(url: str, timeout: float) -> str:
    """Fetch a web page and extract text content."""
    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _BROWSER_UA},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup(
            ["script", "style", "nav", "header", "footer", "aside", "iframe", "noscript"]
        ):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        if len(text) > 12000:
            text = text[:12000] + "\n...[обрезано]"

        return text
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ======================================================================
# LyricsAgent
# ======================================================================


class LyricsAgent:
    """Agent-based lyrics searcher using DeepSeek LLM + Yandex Search.

    Drop-in replacement for LyricsSearcher — same async ``search()``
    interface returning ``LyricsResult``.
    """

    def __init__(
        self,
        deepseek_api_key: str,
        yandex_search_api_key: str,
        yandex_search_folder_id: str,
        model: str = "deepseek-chat",
        max_iterations: int = 15,
        timeout: float = 15.0,
    ) -> None:
        self._deepseek_api_key = deepseek_api_key
        self._yandex_api_key = yandex_search_api_key
        self._yandex_folder_id = yandex_search_folder_id
        self._model = model
        self._max_iterations = max_iterations
        self._timeout = timeout

    async def search(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None = None,
        title_hint: str | None = None,
    ) -> LyricsResult:
        """Search for song lyrics using an agentic tool-calling loop.

        Raises:
            LyricsNotFoundError: If the agent could not find lyrics.
            LyricsAPIError: If API requests fail.
        """
        user_parts = [f"Расшифровка Whisper (с ошибками):\n{asr_text}"]
        if detected_language:
            user_parts.append(f"Язык: {detected_language}")
        if artist_hint:
            user_parts.append(f"Подсказка исполнителя: {artist_hint}")
        if title_hint:
            user_parts.append(f"Подсказка названия: {title_hint}")

        user_message = "\n".join(user_parts)

        try:
            raw_response = await asyncio.to_thread(
                self._run_agent, user_message,
            )
        except LyricsSearchError:
            raise
        except Exception as exc:
            raise LyricsAPIError(f"Lyrics agent error: {exc}") from exc

        if not raw_response or raw_response.strip().lower() in _NOT_FOUND_MARKERS:
            raise LyricsNotFoundError("Agent could not find lyrics")

        # Try parsing structured JSON response
        artist, title, lyrics = self._parse_agent_response(
            raw_response, artist_hint, title_hint,
        )

        if not lyrics or len(lyrics) < 20:
            raise LyricsNotFoundError("Agent returned empty or very short lyrics")

        # Fallback metadata extraction if agent didn't return artist/title
        if not artist or not title:
            try:
                artist, title = await asyncio.to_thread(
                    self._extract_metadata,
                    lyrics,
                    asr_text,
                    artist_hint,
                    title_hint,
                )
            except Exception:
                logger.warning("metadata_extraction_failed")
                artist = artist or artist_hint or "Unknown"
                title = title or title_hint or "Unknown"

        lyrics = clean_lyrics(lyrics)

        return LyricsResult(
            artist=artist,
            title=title,
            lyrics=lyrics,
            language=detected_language,
            confidence="medium",
            source_note="deepseek+yandex",
        )

    # ------------------------------------------------------------------
    # Agent loop (synchronous — called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _run_agent(self, user_message: str) -> str:
        """Run the tool-calling agent loop. Returns the final text response."""
        client = OpenAI(
            api_key=self._deepseek_api_key,
            base_url="https://api.deepseek.com",
        )

        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        tool_functions = {
            "web_search": lambda query: _web_search(
                query, self._yandex_api_key, self._yandex_folder_id, self._timeout,
            ),
            "fetch_webpage": lambda url: _fetch_webpage(url, self._timeout),
        }

        for _ in range(self._max_iterations):
            response = client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=_TOOLS,
                max_tokens=8192,
            )

            message = response.choices[0].message
            messages.append(message)

            if not message.tool_calls:
                return message.content or ""

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)

                logger.debug(
                    "agent_tool_call",
                    tool=fn_name,
                    args=fn_args,
                )

                fn = tool_functions.get(fn_name)
                if fn:
                    result = fn(**fn_args)
                else:
                    result = json.dumps({"error": f"Unknown tool: {fn_name}"})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

        raise LyricsNotFoundError(
            "Agent exhausted max iterations without returning lyrics"
        )

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_agent_response(
        raw: str,
        artist_hint: str | None,
        title_hint: str | None,
    ) -> tuple[str, str, str]:
        """Parse agent response. Returns (artist, title, lyrics).

        Tries JSON first, falls back to treating the whole response as lyrics.
        """
        # Try full JSON parse
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "lyrics" in data:
                return (
                    data.get("artist", "") or "",
                    data.get("title", "") or "",
                    data["lyrics"],
                )
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from within text
        match = re.search(r"\{[^{}]*\"lyrics\"\s*:", raw, re.DOTALL)
        if match:
            # Find the matching closing brace
            start = match.start()
            brace_count = 0
            for i, ch in enumerate(raw[start:], start=start):
                if ch == "{":
                    brace_count += 1
                elif ch == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        try:
                            data = json.loads(raw[start : i + 1])
                            return (
                                data.get("artist", "") or "",
                                data.get("title", "") or "",
                                data.get("lyrics", ""),
                            )
                        except json.JSONDecodeError:
                            break

        # Plain text — treat entire response as lyrics
        return ("", "", raw.strip())

    # ------------------------------------------------------------------
    # Metadata extraction fallback
    # ------------------------------------------------------------------

    def _extract_metadata(
        self,
        lyrics: str,
        asr_text: str,
        artist_hint: str | None,
        title_hint: str | None,
    ) -> tuple[str, str]:
        """Extract artist and title from lyrics via a separate LLM call."""
        client = OpenAI(
            api_key=self._deepseek_api_key,
            base_url="https://api.deepseek.com",
        )

        user_parts = [f"Текст песни:\n{lyrics[:2000]}"]
        if artist_hint:
            user_parts.append(f"Подсказка исполнителя: {artist_hint}")
        if title_hint:
            user_parts.append(f"Подсказка названия: {title_hint}")
        user_parts.append(f"Расшифровка Whisper: {asr_text[:500]}")

        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _METADATA_PROMPT},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
            temperature=0.0,
            max_tokens=256,
        )

        text = response.choices[0].message.content or ""

        try:
            data = json.loads(text)
            return (
                data.get("artist", "") or artist_hint or "Unknown",
                data.get("title", "") or title_hint or "Unknown",
            )
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                    return (
                        data.get("artist", "") or artist_hint or "Unknown",
                        data.get("title", "") or title_hint or "Unknown",
                    )
                except json.JSONDecodeError:
                    pass

        return (artist_hint or "Unknown", title_hint or "Unknown")
