"""Expand repetition shortcuts in candidate lyrics.

Online lyrics commonly compress repeated sections via three patterns:

1. **Counted section header**: ``[Chorus x2]`` / ``[Припев 2 раза]`` — repeat
   the following body block N times.
2. **Section reference**: a ``[Chorus]`` header with no body, expecting the
   reader to copy the body of an earlier ``[Chorus]``.
3. **Inline repeat**: ``oh oh oh (2 раза)`` / ``я тебя люблю ×3`` at the end
   of a line — repeat that line N times.

Without expansion the candidate looks shorter than what's actually sung in
the audio (Whisper ASR contains every actual repetition), inflating
length-ratio penalty for the correct candidate and letting remix versions
win.

Two-pass implementation:

- **Algorithmic pass** handles the three patterns above with regex + section
  parsing. Deterministic, free, covers most real candidates.
- **LLM pass** triggers only when the algorithmic pass leaves residual
  meta-instructions like ``repeat chorus`` / ``повторить припев``. Asks
  DeepSeek to rewrite the text fully expanded.

Results are cached by SHA-256 of the raw input so identical candidates from
multiple providers cost only one LLM call.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass

import structlog
from openai import OpenAI

logger = structlog.get_logger(__name__)


# A section header is a line whose entire content is [whatever].
_SECTION_HEADER_RE = re.compile(r"^\s*\[\s*([^\[\]\n]+?)\s*\]\s*$")

# Repeat-count fragments inside a section label: "x2", "×3", "2 раза", "2 times".
_COUNT_FRAGMENT_RE = re.compile(
    r"(?:[xх×]\s*(\d+)"           # x2 / х2 / ×2  (note: latin x, cyrillic х, multiplication sign)
    r"|(\d+)\s*[xх×]"               # 2x / 2х / 2×
    r"|(\d+)\s*раз[аовы]*"          # 2 раза / 2 раз / 2 разов
    r"|(\d+)\s*times?)",            # 2 times / 2 time
    re.IGNORECASE,
)

# Inline repeat marker at end of a line: " (2 раза)", " [x3]", " (2x)"
_INLINE_REPEAT_BRACKETED_RE = re.compile(
    r"\s*[\(\[]\s*"
    r"(?:[xх×]\s*(\d+)"
    r"|(\d+)\s*[xх×]"
    r"|(\d+)\s*раз[аовы]*"
    r"|(\d+)\s*times?)"
    r"\s*[\)\]]\s*$",
    re.IGNORECASE,
)

# Bare repeat marker at end of a line, restricted to multiplication sign
# to avoid false positives ("formula x2" in lyrics): " ×3", " 3×"
_INLINE_REPEAT_BARE_RE = re.compile(r"\s+(?:×\s*(\d+)|(\d+)\s*×)\s*$")

# Meta-instruction phrases that indicate the algorithmic pass might have
# missed something — trigger LLM expansion.
_META_INSTRUCTION_RE = re.compile(
    r"\b(?:repeat|повтор\w*|снова)\s+(?:chorus|verse|bridge|припев|куплет|бридж)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Section:
    label: str | None
    count: int
    body: str


class LyricsExpander:
    def __init__(
        self,
        deepseek_api_key: str | None = None,
        model: str = "deepseek-chat",
    ) -> None:
        self._api_key = deepseek_api_key
        self._model = model
        self._cache: dict[str, str] = {}

    async def expand(self, raw_lyrics: str) -> str:
        """Return ``raw_lyrics`` with repetition shortcuts expanded."""
        if not raw_lyrics or not raw_lyrics.strip():
            return raw_lyrics or ""

        key = hashlib.sha256(raw_lyrics.encode("utf-8")).hexdigest()
        if key in self._cache:
            return self._cache[key]

        algo_result = self._expand_algorithmic(raw_lyrics)
        before = len(raw_lyrics)
        after = len(algo_result)
        if after != before:
            logger.info(
                "expander_algorithmic_applied",
                before_chars=before,
                after_chars=after,
            )

        result = algo_result
        if self._api_key and _META_INSTRUCTION_RE.search(algo_result):
            llm_result = await self._expand_llm(algo_result)
            if llm_result and llm_result.strip():
                logger.info(
                    "expander_llm_applied",
                    before_chars=after,
                    after_chars=len(llm_result),
                )
                result = llm_result
            else:
                logger.info("expander_llm_skipped", reason="empty_or_failed")
        elif _META_INSTRUCTION_RE.search(algo_result):
            logger.info("expander_llm_skipped", reason="no_api_key")

        self._cache[key] = result
        return result

    # ------------------------------------------------------------------
    # Algorithmic pass
    # ------------------------------------------------------------------

    def _expand_algorithmic(self, text: str) -> str:
        sections = self._parse_sections(text)
        return self._render_sections(sections)

    def _parse_sections(self, text: str) -> list[_Section]:
        sections: list[_Section] = []
        current_label: str | None = None
        current_count = 1
        body_lines: list[str] = []

        for line in text.splitlines():
            m = _SECTION_HEADER_RE.match(line)
            if m:
                # Flush previous section.
                if body_lines or current_label:
                    sections.append(
                        _Section(
                            label=current_label,
                            count=current_count,
                            body="\n".join(body_lines).strip("\n"),
                        )
                    )
                count, cleaned_label = self._extract_count(m.group(1))
                current_label = cleaned_label.lower().strip() or None
                current_count = count
                body_lines = []
            else:
                body_lines.append(line)

        if body_lines or current_label:
            sections.append(
                _Section(
                    label=current_label,
                    count=current_count,
                    body="\n".join(body_lines).strip("\n"),
                )
            )

        return sections

    def _extract_count(self, label: str) -> tuple[int, str]:
        count = 1
        cleaned = label
        for m in _COUNT_FRAGMENT_RE.finditer(label):
            for g in m.groups():
                if g and g.isdigit():
                    count = max(count, int(g))
            cleaned = cleaned.replace(m.group(0), "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" :;,-")
        return count, cleaned

    def _render_sections(self, sections: list[_Section]) -> str:
        registry: dict[str, str] = {}
        rendered_blocks: list[str] = []
        for sec in sections:
            body = sec.body.strip()
            if body:
                if sec.label:
                    registry[sec.label] = body
                block = body
            elif sec.label and sec.label in registry:
                block = registry[sec.label]
            else:
                # Header-only section with no known body — drop.
                continue
            block = self._expand_inline_repeats(block)
            for _ in range(max(1, sec.count)):
                rendered_blocks.append(block)
        return "\n\n".join(b for b in rendered_blocks if b.strip())

    def _expand_inline_repeats(self, block: str) -> str:
        out_lines: list[str] = []
        for line in block.splitlines():
            m = _INLINE_REPEAT_BRACKETED_RE.search(line)
            sub_re = _INLINE_REPEAT_BRACKETED_RE
            if not m:
                m = _INLINE_REPEAT_BARE_RE.search(line)
                sub_re = _INLINE_REPEAT_BARE_RE
            if not m:
                out_lines.append(line)
                continue
            n = 1
            for g in m.groups():
                if g and g.isdigit():
                    n = max(n, int(g))
            base = sub_re.sub("", line).rstrip()
            if not base.strip():
                continue
            for _ in range(max(1, n)):
                out_lines.append(base)
        return "\n".join(out_lines)

    # ------------------------------------------------------------------
    # LLM pass (only if api key set and meta-instructions present)
    # ------------------------------------------------------------------

    async def _expand_llm(self, text: str) -> str | None:
        try:
            raw = await asyncio.to_thread(self._call_llm, text)
        except Exception as exc:
            logger.warning("expander_llm_error", error=str(exc))
            return None
        return raw.strip() or None

    def _call_llm(self, text: str) -> str:
        client = OpenAI(
            api_key=self._api_key,
            base_url="https://api.deepseek.com",
            timeout=60.0,
        )
        system = (
            "Ты помощник по обработке текстов песен. На вход — текст песни, "
            "содержащий инструкции о повторах секций (например \"повторить "
            "припев\", \"снова куплет\", \"repeat chorus\"). Разверни эти "
            "инструкции так, чтобы получился полный текст, который реально "
            "поётся. Не меняй слова, не сокращай, не добавляй комментариев. "
            "Верни только развёрнутый текст."
        )
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=8192,
        )
        return response.choices[0].message.content or ""
