"""MoodQueryExpander — expands a free-form mood/theme query into
4-6 lines of lyrics-like text for semantic vector search.

The generated text is then embedded with sentence-transformers and used
to search the QDrant lyrics_embeddings collection. This gives a
lyrics↔lyrics comparison (symmetric) instead of query↔lyrics (asymmetric),
which dramatically improves relevance for mood/theme queries.
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
Ты помощник для поиска музыки в каталоге песен.
Пользователь вводит свободный запрос — настроение, тему, ситуацию.
Твоя задача — написать 4-6 строк текста, которые максимально точно отражают \
этот запрос и будут использованы для семантического поиска похожих песен.
Правила:
- Если знаешь реальные строки из известных песен на эту тему — используй их
- Если нет — напиши в стиле типичных песен на эту тему
- Только текст, без заголовков, названий песен и пояснений
- Язык фрагмента должен совпадать с языком запроса
- 4-6 строк, не больше
"""


class MoodQueryExpander:
    """Expands a free-form mood/theme query into lyrics-like text via DeepSeek."""

    def __init__(self, api_key: str, model: str = "deepseek-chat") -> None:
        self._api_key = api_key
        self._model = model

    async def expand(self, query: str) -> str:
        """Return expanded lyrics-like text, or the original query on failure."""
        try:
            result = await asyncio.to_thread(self._call_llm, query)
            if result.strip():
                return result.strip()
        except Exception as exc:
            logger.warning("mood_expander_failed", error=str(exc))
        return query

    def _call_llm(self, query: str) -> str:
        from openai import OpenAI  # noqa: PLC0415

        client = OpenAI(
            api_key=self._api_key,
            base_url="https://api.deepseek.com",
            timeout=15.0,
        )
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            temperature=0.7,
            max_tokens=200,
        )
        return response.choices[0].message.content or ""
