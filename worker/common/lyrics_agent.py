"""Lyrics search agent — collects 1-3 raw lyrics candidates from the web.

Uses an agentic tool-calling loop: the LLM can invoke ``web_search`` (SearXNG
primary, Yandex fallback) and ``fetch_webpage`` (httpx + BeautifulSoup) to
find pages that look like the song's lyrics. It returns a JSON array of
candidates — selection between them is the matcher's job, not the agent's.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import xml.etree.ElementTree as ET

import httpx
import structlog
from bs4 import BeautifulSoup
from openai import OpenAI

from worker.common.lyrics.base_provider import LyricsCandidate
from worker.common.lyrics_searcher import (
    LyricsAPIError,
    LyricsSearchError,
)

logger = structlog.get_logger(__name__)

_YANDEX_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"

_SYSTEM_PROMPT = """\
Ты — бот для поиска текстов песен. На вход — приблизительная расшифровка \
от Whisper (с ошибками) и опциональные подсказки artist/title.

Твоя задача — найти в интернете 1-3 страницы с предполагаемым текстом этой \
песни и вернуть сырое содержимое. ВЫБИРАТЬ лучший вариант — НЕ твоя задача, \
это сделают позже.

ОБЯЗАТЕЛЬНЫЙ АЛГОРИТМ (нарушение → tool вернёт ошибку):
1. web_search с КОРОТКИМ запросом (2-4 слова: артист + название + "текст").
2. fetch_webpage самой релевантной ссылки из результатов поиска.
3. Анализ текста на странице.
4. Если страница содержит подходящий текст — добавь её в финальный JSON.
5. Если нет — повтори с шага 1 с другой формулировкой.

ЗАПРЕЩЕНО:
- Делать 2 web_search подряд без fetch_webpage между ними.
- Оборачивать в кавычки фразы длиннее 3 слов. Whisper искажает слова, \
exact match по длинной фразе почти никогда не сработает. Кавычки используй \
ТОЛЬКО для anchor'ов: имя артиста, название песни, отдельное редкое слово.

ПОДСКАЗКИ ПО ЗАПРОСАМ:
- Если в подсказках имя артиста выглядит транслитерированным или необычным, \
попробуй и оригинальное написание (как может быть в имени файла), и каноническое.
- Начинай с самого простого: `<artist> <title> текст песни` (для русских) \
или `<artist> <title> lyrics` (для английских).

Формат ответа — строго JSON-массив (НЕ объект):
[
  {"artist": "имя артиста", "title": "название", "lyrics": "сырой текст со страницы"},
  {"artist": "...", "title": "...", "lyrics": "..."}
]

Правила для каждого кандидата:
- "lyrics" — основной текст со страницы (можно с маркерами [Куплет] / [Chorus]).
- НЕ нужно чистить аккорды или маркеры — это сделают далее.
- Если ничего не нашёл — верни пустой массив [].
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете. Возвращает релевантные ссылки.",
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
                "Загрузить веб-страницу и извлечь текстовое содержимое."
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

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Yandex Search API enum mapping by ISO 639-1 detected language code.
# Unsupported languages fall back to the global SEARCH_TYPE_COM with
# English localisation, which is closest to a neutral default.
_YANDEX_SEARCH_TYPE = {
    "ru": "SEARCH_TYPE_RU",
    "tr": "SEARCH_TYPE_TR",
    "kk": "SEARCH_TYPE_KK",
}
_YANDEX_LOCALIZATION = {
    "ru": "LOCALIZATION_RU",
    "uk": "LOCALIZATION_UK",
    "be": "LOCALIZATION_BE",
    "kk": "LOCALIZATION_KK",
    "tr": "LOCALIZATION_TR",
    "en": "LOCALIZATION_EN",
}

# Default search language when Whisper did not detect anything (empty / None).
# English biases the search backend towards global content, which is a safer
# default than Russian for an agent serving multilingual songs.
_DEFAULT_SEARCH_LANGUAGE = "en"

# Max words allowed inside any "..." quoted fragment in a search query.
# Whisper-distorted long phrases almost never match in exact mode; short
# anchors (artist, title, rare keyword) is what works.
_MAX_WORDS_PER_QUOTED_PHRASE = 3

# Max consecutive web_search calls before agent is forced to fetch_webpage.
_MAX_CONSECUTIVE_SEARCHES = 2


def _quoted_phrase_too_long(query: str) -> str | None:
    """Return offending phrase if any "..." in query has > N words, else None."""
    for match in re.finditer(r'"([^"]*)"', query):
        phrase = match.group(1).strip()
        if len(phrase.split()) > _MAX_WORDS_PER_QUOTED_PHRASE:
            return phrase
    return None


# ======================================================================
# Tool implementations (synchronous — run via asyncio.to_thread)
# ======================================================================


def _searxng_search(
    query: str,
    base_url: str,
    timeout: float,
    language: str = _DEFAULT_SEARCH_LANGUAGE,
) -> list[dict] | None:
    try:
        response = httpx.get(
            f"{base_url}/search",
            params={
                "q": query,
                "format": "json",
                "categories": "general",
                "language": language,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("results", [])[:10]:
            results.append({
                "title": item.get("title", ""),
                "href": item.get("url", ""),
                "body": item.get("content", ""),
            })
        return results
    except Exception as exc:
        logger.warning("searxng_search_failed", error=str(exc))
        return None


def _yandex_search(
    query: str,
    api_key: str,
    folder_id: str,
    timeout: float,
    language: str = _DEFAULT_SEARCH_LANGUAGE,
) -> list[dict] | None:
    search_type = _YANDEX_SEARCH_TYPE.get(language, "SEARCH_TYPE_COM")
    l10n = _YANDEX_LOCALIZATION.get(language, "LOCALIZATION_EN")
    try:
        response = httpx.post(
            _YANDEX_SEARCH_URL,
            headers={
                "Authorization": f"Api-Key {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": {
                    "searchType": search_type,
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
                "l10n": l10n,
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
        return results if results else None
    except Exception as exc:
        logger.warning("yandex_search_failed", error=str(exc))
        return None


def _web_search(
    query: str,
    backend: str,
    language: str = _DEFAULT_SEARCH_LANGUAGE,
    api_key: str = "",
    folder_id: str = "",
    timeout: float = 15.0,
    searxng_url: str | None = None,
) -> str:
    """Query exactly one backend.

    The orchestrator (:class:`LyricsAgent.search`) drives backend choice
    sequentially: first pass uses SearXNG only; if the agent returns no
    candidates, a second pass is launched against Yandex. ``language`` is
    the Whisper-detected ISO code that biases each backend's regional
    relevance.
    """
    bad_phrase = _quoted_phrase_too_long(query)
    if bad_phrase is not None:
        logger.info("web_search_rejected_long_quote", phrase=bad_phrase[:80])
        return json.dumps(
            {
                "error": (
                    f"Кавычная фраза \"{bad_phrase}\" слишком длинная "
                    f"(max {_MAX_WORDS_PER_QUOTED_PHRASE} слова). "
                    "Используй короткие quotes из 1-3 ключевых слов "
                    "(имя артиста, название, отдельное редкое слово)."
                )
            },
            ensure_ascii=False,
        )

    if backend == "searxng":
        if not searxng_url:
            return json.dumps(
                {"error": "SearXNG backend not configured"}, ensure_ascii=False,
            )
        results = _searxng_search(query, searxng_url, timeout, language)
    elif backend == "yandex":
        if not (api_key and folder_id):
            return json.dumps(
                {"error": "Yandex Search backend not configured"}, ensure_ascii=False,
            )
        results = _yandex_search(query, api_key, folder_id, timeout, language)
    else:
        return json.dumps(
            {"error": f"Unknown backend: {backend}"}, ensure_ascii=False,
        )

    if not results:
        return json.dumps({"error": "Ничего не найдено"}, ensure_ascii=False)

    logger.debug("web_search_via", backend=backend, count=len(results))
    return json.dumps(results, ensure_ascii=False)


def _fetch_webpage(url: str, timeout: float) -> str:
    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _BROWSER_UA},
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        # BeautifulSoup will happily parse binary blobs (PDF, images, audio)
        # and emit token-eating garbage into the LLM context. Accept only
        # HTML/XHTML; treat anything else as an agent-visible error so the
        # LLM picks a different URL.
        if not (
            "text/html" in content_type or "application/xhtml" in content_type
        ):
            logger.info(
                "fetch_webpage_skipped_non_html",
                url=url,
                content_type=content_type,
            )
            return json.dumps(
                {"error": f"Unsupported content-type: {content_type or 'unknown'}"},
                ensure_ascii=False,
            )
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
    """Web-search agent that returns candidate lyrics for the matcher.

    Selection between candidates is delegated to ``LyricsMatcher``.
    """

    def __init__(
        self,
        deepseek_api_key: str,
        yandex_search_api_key: str = "",
        yandex_search_folder_id: str = "",
        model: str = "deepseek-chat",
        max_iterations: int = 20,
        timeout: float = 15.0,
        searxng_url: str | None = None,
    ) -> None:
        self._deepseek_api_key = deepseek_api_key
        self._yandex_api_key = yandex_search_api_key
        self._yandex_folder_id = yandex_search_folder_id
        self._model = model
        self._max_iterations = max_iterations
        self._timeout = timeout
        self._searxng_url = searxng_url

    async def search(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None = None,
        title_hint: str | None = None,
        artist_alts: list[str] | None = None,
        title_alts: list[str] | None = None,
    ) -> list[LyricsCandidate]:
        # Whisper occasionally returns "" for short / instrumental segments.
        # Falling back to a deterministic default keeps SearXNG/Yandex from
        # silently mis-localising into the previously hard-coded "ru".
        search_language = (detected_language or "").lower() or _DEFAULT_SEARCH_LANGUAGE
        """Return up to ~3 raw candidate lyrics from web search.

        ``artist_alts`` / ``title_alts`` provide alternative spellings (e.g.
        Latin transliteration alongside canonical Cyrillic) which help the
        agent retry with a different form when the primary hint doesn't
        find the song.

        Raises ``LyricsAPIError`` on API failures. Returns an empty list (not
        raises) if the agent finished but found nothing.
        """
        user_parts = [f"Расшифровка Whisper (с ошибками):\n{asr_text}"]
        if detected_language:
            user_parts.append(f"Язык: {detected_language}")
        if artist_hint:
            user_parts.append(f"Подсказка исполнителя: {artist_hint}")
        if artist_alts:
            user_parts.append(
                "Альтернативные написания исполнителя (пробуй ОБА варианта "
                f"в разных запросах): {', '.join(artist_alts)}"
            )
        if title_hint:
            user_parts.append(f"Подсказка названия: {title_hint}")
        if title_alts:
            user_parts.append(
                "Альтернативные написания названия (пробуй ОБА варианта в "
                f"разных запросах): {', '.join(title_alts)}"
            )
        user_message = "\n".join(user_parts)

        # Sequential two-pass: SearXNG first (free, broad), then Yandex
        # (paid, Russian-content advantage) only if the first pass returned
        # nothing usable. This conserves Yandex API quota.
        backends_to_try: list[str] = []
        if self._searxng_url:
            backends_to_try.append("searxng")
        if self._yandex_api_key and self._yandex_folder_id:
            backends_to_try.append("yandex")
        if not backends_to_try:
            logger.warning("lyrics_agent_no_backends_configured")
            return []

        logger.info("lyrics_agent_starting", backends=backends_to_try)
        t0 = time.monotonic()

        candidates: list[LyricsCandidate] = []
        for i, backend in enumerate(backends_to_try):
            pass_message = user_message
            if i > 0:
                prior = backends_to_try[i - 1]
                pass_message += (
                    f"\n\n[Системная подсказка: предыдущая попытка через "
                    f"{prior} НЕ нашла подходящего текста. Сейчас активен "
                    f"{backend} — попробуй другие формулировки запросов "
                    "(возможно, изменив транслитерацию или ключевые слова).]"
                )

            logger.info("lyrics_agent_pass_starting", backend=backend, pass_idx=i + 1)
            t_pass = time.monotonic()

            try:
                raw_response = await asyncio.to_thread(
                    self._run_agent, pass_message, backend, search_language,
                )
            except LyricsSearchError:
                raise
            except Exception as exc:
                raise LyricsAPIError(f"Lyrics agent error: {exc}") from exc

            candidates = self._parse_candidates(raw_response, backend)
            logger.info(
                "lyrics_agent_pass_completed",
                backend=backend,
                candidate_count=len(candidates),
                duration_sec=round(time.monotonic() - t_pass, 2),
            )
            if candidates:
                break

        logger.info(
            "lyrics_agent_completed",
            candidate_count=len(candidates),
            duration_sec=round(time.monotonic() - t0, 2),
        )
        return candidates

    # ------------------------------------------------------------------
    # Agent loop (synchronous — called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _run_agent(
        self,
        user_message: str,
        backend: str,
        language: str = _DEFAULT_SEARCH_LANGUAGE,
    ) -> str:
        client = OpenAI(
            api_key=self._deepseek_api_key,
            base_url="https://api.deepseek.com",
            timeout=120.0,
        )

        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        tool_functions = {
            "web_search": lambda query: _web_search(
                query=query,
                backend=backend,
                language=language,
                api_key=self._yandex_api_key,
                folder_id=self._yandex_folder_id,
                timeout=self._timeout,
                searxng_url=self._searxng_url,
            ),
            "fetch_webpage": lambda url: _fetch_webpage(url, self._timeout),
        }

        # Track consecutive web_search calls so we can force the agent to
        # fetch_webpage instead of looping in search-only mode (the failure
        # pattern observed for "Dzetta - Кометы.mp3").
        consecutive_searches = 0

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
                    "agent_tool_call", tool=fn_name, args=fn_args,
                )

                if (
                    fn_name == "web_search"
                    and consecutive_searches >= _MAX_CONSECUTIVE_SEARCHES
                ):
                    logger.info(
                        "agent_search_blocked_force_fetch",
                        consecutive_searches=consecutive_searches,
                    )
                    result = json.dumps(
                        {
                            "error": (
                                f"Ты уже сделал {consecutive_searches} "
                                "web_search подряд. Сейчас ОБЯЗАТЕЛЬНО загрузи "
                                "самую релевантную ссылку из предыдущих "
                                "результатов через fetch_webpage. Только "
                                "после fetch_webpage можно будет снова искать."
                            )
                        },
                        ensure_ascii=False,
                    )
                else:
                    fn = tool_functions.get(fn_name)
                    if fn:
                        result = fn(**fn_args)
                    else:
                        result = json.dumps(
                            {"error": f"Unknown tool: {fn_name}"},
                            ensure_ascii=False,
                        )
                    if fn_name == "web_search":
                        consecutive_searches += 1
                    elif fn_name == "fetch_webpage":
                        consecutive_searches = 0

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

        logger.warning(
            "agent_iterations_exhausted",
            iterations=self._max_iterations,
        )
        return "[]"

    # ------------------------------------------------------------------
    # Response parsing — JSON array of {artist, title, lyrics}
    # ------------------------------------------------------------------

    def _parse_candidates(self, raw: str, backend: str) -> list[LyricsCandidate]:
        """Parse the agent's JSON-array response, tagging each candidate
        with the backend (``searxng`` / ``yandex``) used in this pass.
        """
        if not raw:
            return []

        # Try direct parse.
        items = _try_parse_json_array(raw)
        if items is None:
            # Try to find a JSON array anywhere in the response.
            match = re.search(r"\[\s*\{.*?\}\s*\]", raw, re.DOTALL)
            if match:
                items = _try_parse_json_array(match.group())

        if not isinstance(items, list):
            logger.warning("agent_response_not_array", raw=raw[:200])
            return []

        candidates: list[LyricsCandidate] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            lyrics = (item.get("lyrics") or "").strip()
            if len(lyrics) < 20:
                continue
            artist = (item.get("artist") or "").strip() or "Unknown"
            title = (item.get("title") or "").strip() or "Unknown"
            candidates.append(
                LyricsCandidate(
                    artist=artist,
                    title=title,
                    lyrics=lyrics,
                    source=backend,
                )
            )
        return candidates


def _try_parse_json_array(text: str) -> list | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None
