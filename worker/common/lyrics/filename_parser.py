"""Extract artist/title variants from a filename via DeepSeek.

Returns up to two forms per field — the canonical name (in original script,
e.g. Cyrillic for Russian artists) plus the original literal form from the
filename when it differs (e.g. transliterated Latin). Downstream search
benefits from trying both: ``Dzetta`` matches sonichits/genius, while
``Джетта`` matches Russian-only sources.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

import structlog
from openai import OpenAI

logger = structlog.get_logger(__name__)


_PROMPT = """\
Извлеки имя исполнителя и название песни из имени файла.

Правила:
- Имя файла может содержать мусор: номера треков, битрейт, год, теги, \
скобки, названия сайтов — ИГНОРИРУЙ это.
- Имя может быть транслитерировано (например "Leps_Grigorij" → \
"Григорий Лепс", "Zemfira" → "Земфира"). Верни КАНОНИЧЕСКОЕ имя на \
ОРИГИНАЛЬНОМ языке (для русских артистов — кириллица).
- Если в имени файла оригинальное написание ОТЛИЧАЕТСЯ от канонического \
(например файл "Dzetta - Кометы.mp3" — канонически "Джетта" но в файле \
латиницей "Dzetta"), верни ОБА варианта в полях artist_original и/или \
title_original. Это нужно поисковику, который может не знать кириллицу.
- Если оригинальное написание совпадает с каноническим — НЕ заполняй поля \
artist_original/title_original (или оставь пустыми).
- Если несколько артистов — верни ГЛАВНОГО (наиболее известного). \
Например "Peha_Stas_Leps_Grigorij" → "Григорий Лепс".
- Имя артиста в правильном порядке (Имя Фамилия).

Верни ТОЛЬКО JSON:
{"artist": "...", "title": "...", "artist_original": "...", "title_original": "..."}

Поля artist_original / title_original — опциональные. Если не удаётся \
определить — верни пустые строки."""


@dataclass(frozen=True)
class ParsedFilename:
    """Variants of artist + title extracted from a filename.

    First element of each tuple is the canonical form. Optional second
    element is the original literal form from the filename when it differs.
    """

    artist_variants: tuple[str, ...]
    title_variants: tuple[str, ...]

    @property
    def artist(self) -> str | None:
        return self.artist_variants[0] if self.artist_variants else None

    @property
    def title(self) -> str | None:
        return self.title_variants[0] if self.title_variants else None

    @property
    def artist_alts(self) -> list[str]:
        return list(self.artist_variants[1:])

    @property
    def title_alts(self) -> list[str]:
        return list(self.title_variants[1:])

    @classmethod
    def empty(cls) -> "ParsedFilename":
        return cls(artist_variants=(), title_variants=())


class FilenameParser:
    def __init__(
        self,
        deepseek_api_key: str,
        model: str = "deepseek-chat",
    ) -> None:
        self._api_key = deepseek_api_key
        self._model = model

    async def parse(self, filename: str) -> ParsedFilename:
        """Parse ``filename`` into canonical + optional original variants."""
        try:
            raw = await asyncio.to_thread(
                self._call_llm, f"Имя файла: {filename}",
            )
        except Exception as exc:
            logger.warning("filename_parse_llm_failed", error=str(exc))
            return ParsedFilename.empty()

        data = _extract_json(raw)
        if data is None:
            return ParsedFilename.empty()

        artist_variants = _build_variants(
            canonical=data.get("artist"),
            original=data.get("artist_original"),
        )
        title_variants = _build_variants(
            canonical=data.get("title"),
            original=data.get("title_original"),
        )
        return ParsedFilename(
            artist_variants=artist_variants,
            title_variants=title_variants,
        )

    def _call_llm(self, user_message: str) -> str:
        client = OpenAI(
            api_key=self._api_key,
            base_url="https://api.deepseek.com",
            timeout=60.0,
        )
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            max_tokens=256,
        )
        return response.choices[0].message.content or ""


def _build_variants(canonical: str | None, original: str | None) -> tuple[str, ...]:
    can = (canonical or "").strip()
    orig = (original or "").strip()
    variants: list[str] = []
    if can:
        variants.append(can)
    if orig and orig.casefold() != can.casefold():
        variants.append(orig)
    return tuple(variants)


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return None
