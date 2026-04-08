"""DeepSeek-based lyrics verifier.

Receives ASR text and a list of candidates from providers, asks DeepSeek
to pick the best match (or reject all). One LLM call instead of an
agent loop.
"""

from __future__ import annotations

import asyncio
import json
import re

import structlog
from openai import OpenAI

from worker.common.lyrics.base_provider import LyricsCandidate
from worker.common.lyrics_searcher import LyricsResult, clean_lyrics

logger = structlog.get_logger(__name__)

_FILENAME_PROMPT = """\
Извлеки имя исполнителя и название песни из имени файла.

Правила:
- Имя файла может содержать мусор: номера треков, битрейт, год, теги, \
скобки, названия сайтов — ИГНОРИРУЙ это.
- Имя может быть транслитерировано (например "Leps_Grigorij" → \
"Григорий Лепс", "Zemfira" → "Земфира"). Верни КАНОНИЧЕСКИЕ имена на \
ОРИГИНАЛЬНОМ языке.
- Если указано несколько артистов — верни ГЛАВНОГО (наиболее известного). \
Например "Peha_Stas_Leps_Grigorij" → "Григорий Лепс".
- Имя артиста должно быть в правильном порядке (Имя Фамилия).

Верни ТОЛЬКО JSON: {"artist": "...", "title": "..."}
Если не удаётся определить — верни пустые строки."""

_SYSTEM_PROMPT = """\
Ты верификатор текстов песен. Тебе даны:
1. Приблизительная расшифровка песни от Whisper (с ошибками).
2. Несколько кандидатов — текстов песен из разных источников.

Задача: выбери кандидата, текст которого лучше всего совпадает с расшифровкой.
Учитывай что Whisper искажает слова — ищи смысловое совпадение, а не точное.

Правила:
- Если хотя бы один кандидат явно соответствует расшифровке (даже с учётом \
ошибок Whisper) — выбери его.
- Если ни один кандидат не подходит — ответь "none".
- Выбирай кандидата, текст которого НАИБОЛЕЕ БЛИЗОК к расшифровке по содержанию \
и объёму. Кандидат должен покрывать расшифровку, но НЕ содержать много лишнего \
текста, которого нет в расшифровке. Если кандидат содержит дополнительные \
куплеты/строки, которых нет в расшифровке — это другая версия песни (ремикс, \
лонг микс), и его НЕ следует выбирать.
- Укажи каноническое имя исполнителя и название песни.
- В поле lyrics верни ПОЛНЫЙ ОЧИЩЕННЫЙ текст выбранного кандидата:
  - Без пометок [Куплет]/[Припев]/[Verse]/[Chorus] и любых тегов в квадратных \
скобках.
  - Без аккордов, заголовков, комментариев, описаний.
  - Куплеты разделяй пустой строкой.
  - Полный текст от первой до последней строки.

Ответ строго JSON:
{"choice": <номер кандидата или "none">, "artist": "...", "title": "...", \
"lyrics": "очищенный полный текст песни"}
"""


class LyricsVerifier:
    """Uses DeepSeek to pick the best lyrics candidate."""

    def __init__(
        self,
        deepseek_api_key: str,
        model: str = "deepseek-chat",
    ) -> None:
        self._api_key = deepseek_api_key
        self._model = model

    async def parse_filename(
        self,
        filename: str,
    ) -> tuple[str | None, str | None]:
        """Use DeepSeek to extract artist/title from a filename.

        Returns (artist, title) — either or both may be None.
        """
        try:
            raw = await asyncio.to_thread(
                self._call_llm,
                f"Имя файла: {filename}",
                system_prompt=_FILENAME_PROMPT,
            )
        except Exception as exc:
            logger.warning("filename_parse_llm_failed", error=str(exc))
            return None, None

        data = _extract_json(raw)
        if data is None:
            return None, None

        artist = data.get("artist", "").strip() or None
        title = data.get("title", "").strip() or None
        return artist, title

    async def verify(
        self,
        asr_text: str,
        candidates: list[LyricsCandidate],
        detected_language: str,
    ) -> LyricsResult | None:
        """Pick the best candidate or return ``None`` if none match."""
        if not candidates:
            return None

        for i, c in enumerate(candidates, 1):
            cleaned = clean_lyrics(c.lyrics)
            logger.info(
                "verifier_candidate",
                idx=i,
                artist=c.artist,
                title=c.title,
                source=c.source,
                chars=len(cleaned),
            )

        user_message = self._build_user_message(asr_text, candidates, detected_language)
        logger.info("verifier_prompt_size", chars=len(user_message))

        try:
            raw = await asyncio.to_thread(self._call_llm, user_message)
        except Exception as exc:
            logger.warning("verifier_llm_failed", error=str(exc))
            return None

        return self._parse_response(raw, candidates, detected_language)

    def _build_user_message(
        self,
        asr_text: str,
        candidates: list[LyricsCandidate],
        detected_language: str,
    ) -> str:
        parts = [
            f'<asr language="{detected_language}" chars="{len(asr_text)}">',
            asr_text,
            "</asr>",
            "",
        ]
        for i, c in enumerate(candidates, 1):
            cleaned = clean_lyrics(c.lyrics)
            n_lines = len([ln for ln in cleaned.splitlines() if ln.strip()])
            parts.append(
                f'<candidate id="{i}" source="{c.source}" '
                f'artist="{c.artist}" title="{c.title}" '
                f'lines="{n_lines}" chars="{len(cleaned)}">'
            )
            parts.append(cleaned)
            parts.append("</candidate>")
            parts.append("")
        return "\n".join(parts)

    def _parse_response(
        self,
        raw: str,
        candidates: list[LyricsCandidate],
        detected_language: str,
    ) -> LyricsResult | None:
        data = _extract_json(raw)
        if data is None:
            logger.warning("verifier_json_parse_failed", raw=raw[:200])
            return None

        choice = data.get("choice")
        if choice == "none" or choice is None:
            return None

        try:
            idx = int(choice) - 1
        except (ValueError, TypeError):
            return None

        if not (0 <= idx < len(candidates)):
            return None

        picked = candidates[idx]
        lyrics = data.get("lyrics") or clean_lyrics(picked.lyrics)
        return LyricsResult(
            artist=data.get("artist") or picked.artist,
            title=data.get("title") or picked.title,
            lyrics=lyrics.strip(),
            language=detected_language,
            confidence="high",
            source_note=f"verified:{picked.source}",
        )

    def _call_llm(
        self,
        user_message: str,
        system_prompt: str = _SYSTEM_PROMPT,
    ) -> str:
        client = OpenAI(
            api_key=self._api_key,
            base_url="https://api.deepseek.com",
            timeout=60.0,
        )
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            max_tokens=8192,
        )
        return response.choices[0].message.content or ""


def _lyrics_preview(text: str, max_chars: int = 2000) -> str:
    """Return text as-is if short, or head + tail if long."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n[...пропущена середина...]\n\n" + text[-half:]


def _extract_json(text: str) -> dict | None:
    """Try to parse JSON from the LLM response."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find first JSON object in text
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None
