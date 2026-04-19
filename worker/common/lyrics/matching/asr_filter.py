"""ASR-driven junk filter for candidate lyrics.

Candidate lyrics from web providers often carry non-sung metadata that
can't be caught by a finite set of regexes — section markers without
brackets (``ПРИПЕВ II:``), dates, credits, inline prefixes glued to a
real line (``Dzetta Кометы Дата: Сентрября г, I: Звёзды в небе...``).

Whisper and the MMS-CTC aligner both read the same vocals stem after
BS-Roformer + back-vocal separation. If Whisper did not hear a line, its
phonemes aren't present in the signal the aligner will receive — leaving
such a line in the text makes the aligner smear its phonemes over silence
and neighboring words. So "not in ASR → drop" is correctness-driven, not
cosmetic.

The filter:

1. Tokenizes the candidate line-by-line with character offsets so we can
   trim inside a line, not only at line boundaries.
2. For every cand-word checks whether a phonetically-similar word exists
   in the ASR (``_match_score`` from scorer: text / lemma / consonant
   skeleton / metaphone / Levenshtein ≥0.8).
3. Per line: ``coverage = matched_words / total_words``. Zones:
   - ``coverage ≥ HIGH`` AND line has enough words → KEEP (then try to
     trim an unmatched prefix / suffix if it looks like a metadata tag).
   - ``coverage < LOW`` → DROP.
   - otherwise → GREY, decided by an LLM in a batched call.
4. Safety guard: if more than ``1 - safety_bypass_ratio`` of lines would
   be dropped, return the original untouched — such a candidate should
   have been rejected by the matcher upstream.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass

import structlog
from openai import OpenAI

from worker.common.lyrics.matching.linguistics import (
    WordFeatures,
    make_word_featurizer,
)
from worker.common.lyrics.matching.scorer import _build_index, _match_score

logger = structlog.get_logger(__name__)


# ----------------------------------------------------------------------
# Local copies of normalizer regexes — we need to clean a single token
# while keeping its byte offsets in the original line.
# ----------------------------------------------------------------------

_SECTION_RE = re.compile(r"\[[^\[\]]*\]", re.UNICODE)
_SHORT_PARENS_RE = re.compile(r"\(([^()]{1,30})\)")
_DIGIT_RUN_RE = re.compile(r"\b\d+\b", re.UNICODE)
_PUNCT_RE = re.compile(r"[^\w'\s]+", re.UNICODE)

# Words that strongly signal a non-sung prefix: section labels, credits,
# remix/edit tags. Used only as a gate for inline prefix trimming — a
# prefix without any of these is never trimmed to avoid cutting real lyrics.
_TRIM_MARKER_WORDS = frozenset({
    "припев", "куплет", "бридж", "интро", "аутро",
    "chorus", "verse", "bridge", "intro", "outro",
    "дата", "date",
    "mix", "remix", "edit", "radio", "version",
    "текст", "слова", "музыка", "автор", "author", "lyrics",
    "feat", "ft",
})

# Suspicious characters inside the original prefix slice: digits or colons.
_SUSPICIOUS_CHAR_RE = re.compile(r"[:\d]")


@dataclass(frozen=True)
class _Token:
    """One whitespace-separated token from the original candidate line.

    ``features`` is ``None`` when the token carries no word content
    (pure punctuation, digits, [section marker]) — such tokens don't
    participate in coverage calculations but keep their place for trim
    offset math.
    """

    line_idx: int
    char_start: int
    char_end: int
    original: str
    features: WordFeatures | None


@dataclass
class ASRFilterResult:
    lyrics_clean: str
    lines_kept: int = 0
    lines_dropped: int = 0
    lines_trimmed: int = 0
    grey_zone_lines: int = 0
    sandwich_rescued: int = 0
    llm_called: bool = False
    safety_bypass: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "lines_kept": self.lines_kept,
            "lines_dropped": self.lines_dropped,
            "lines_trimmed": self.lines_trimmed,
            "grey_zone_lines": self.grey_zone_lines,
            "sandwich_rescued": self.sandwich_rescued,
            "llm_called": self.llm_called,
            "safety_bypass": self.safety_bypass,
        }


class ASRLyricsFilter:
    def __init__(
        self,
        high_thresh: float = 0.70,
        low_thresh: float = 0.15,
        min_line_words: int = 3,
        min_prefix_trim: int = 1,
        max_prefix_trim: int = 6,
        safety_bypass_ratio: float = 0.40,
        use_llm_grey: bool = True,
        deepseek_api_key: str | None = None,
        model: str = "deepseek-chat",
    ) -> None:
        self._high = high_thresh
        self._low = low_thresh
        self._min_words = min_line_words
        self._min_prefix = min_prefix_trim
        self._max_prefix = max_prefix_trim
        self._safety_ratio = safety_bypass_ratio
        self._use_llm = use_llm_grey
        self._api_key = deepseek_api_key
        self._model = model

    async def filter(
        self,
        asr_text: str,
        candidate_lyrics: str,
        language: str,
    ) -> ASRFilterResult:
        if not candidate_lyrics or not candidate_lyrics.strip():
            return ASRFilterResult(lyrics_clean=candidate_lyrics or "")
        if not asr_text or not asr_text.strip():
            # Nothing to match against — leave candidate as-is.
            return ASRFilterResult(lyrics_clean=candidate_lyrics)

        featurize = make_word_featurizer(language)
        lines = candidate_lyrics.splitlines()
        tokens_per_line = [
            self._tokenize_line(idx, line, featurize)
            for idx, line in enumerate(lines)
        ]

        # Build ASR index from normalized ASR words.
        asr_words = _features_from_text(asr_text, language)
        if not asr_words:
            return ASRFilterResult(lyrics_clean=candidate_lyrics)
        asr_idx = _build_index(asr_words)

        # Decide per line.
        decisions: list[_LineDecision] = []
        grey_indices: list[int] = []
        for line_idx, line in enumerate(lines):
            tokens = tokens_per_line[line_idx]
            word_tokens = [t for t in tokens if t.features is not None]
            has_marker = any(
                t.features is not None
                and t.features.text in _TRIM_MARKER_WORDS
                for t in tokens
            )

            if not word_tokens:
                # Empty / marker-only / punctuation-only — preserve as paragraph
                # break or structural whitespace. Doesn't count as drop/keep.
                decisions.append(_LineDecision(
                    line_idx=line_idx, action="empty", text=line,
                    has_marker_word=has_marker,
                ))
                continue

            matched = [
                _match_score(t.features, asr_idx) >= 2 for t in word_tokens
            ]
            coverage = sum(matched) / len(word_tokens)
            total_words = len(word_tokens)

            if total_words < self._min_words:
                grey_indices.append(line_idx)
                decisions.append(_LineDecision(
                    line_idx=line_idx, action="grey", text=line,
                    coverage=coverage, has_marker_word=has_marker,
                ))
                continue

            if coverage >= self._high:
                trimmed = self._try_trim_unmatched_edges(
                    tokens, matched, line,
                )
                if trimmed is not None:
                    decisions.append(_LineDecision(
                        line_idx=line_idx, action="trim", text=trimmed,
                        coverage=coverage, has_marker_word=has_marker,
                    ))
                else:
                    decisions.append(_LineDecision(
                        line_idx=line_idx, action="keep", text=line,
                        coverage=coverage, has_marker_word=has_marker,
                    ))
            elif coverage < self._low:
                decisions.append(_LineDecision(
                    line_idx=line_idx, action="drop", text=line,
                    coverage=coverage, has_marker_word=has_marker,
                ))
            else:
                grey_indices.append(line_idx)
                decisions.append(_LineDecision(
                    line_idx=line_idx, action="grey", text=line,
                    coverage=coverage, has_marker_word=has_marker,
                ))

        # LLM resolve grey lines.
        llm_called = False
        if grey_indices and self._use_llm and self._api_key:
            llm_decisions = await self._llm_decide_grey(
                asr_text=asr_text,
                grey_lines=[(i, lines[i]) for i in grey_indices],
                language=language,
            )
            if llm_decisions is not None:
                llm_called = True
                for idx in grey_indices:
                    action, new_text = llm_decisions.get(idx, ("keep", None))
                    old = decisions[idx]
                    if action == "drop":
                        decisions[idx] = _LineDecision(
                            line_idx=idx, action="drop", text=old.text,
                            coverage=old.coverage, from_llm=True,
                            has_marker_word=old.has_marker_word,
                        )
                    elif action == "trim" and new_text and new_text.strip():
                        decisions[idx] = _LineDecision(
                            line_idx=idx, action="trim", text=new_text.strip(),
                            coverage=old.coverage, from_llm=True,
                            has_marker_word=old.has_marker_word,
                        )
                    else:
                        decisions[idx] = _LineDecision(
                            line_idx=idx, action="keep", text=old.text,
                            coverage=old.coverage, from_llm=True,
                            has_marker_word=old.has_marker_word,
                        )
        if grey_indices and not llm_called:
            # Fail-safe: keep grey lines when LLM unavailable or failed.
            for idx in grey_indices:
                old = decisions[idx]
                if old.action == "grey":
                    decisions[idx] = _LineDecision(
                        line_idx=idx, action="keep", text=old.text,
                        coverage=old.coverage,
                        has_marker_word=old.has_marker_word,
                    )

        # Positional protection — a DROP line sandwiched between two KEEP
        # lines (ignoring blank separators) is almost certainly legitimate
        # content. Metadata like "Artist — «Album»" lives at the document
        # edges; ad-libs / spoken inserts / backing-vocal shouts appear
        # between verses and get transcribed poorly by Whisper, so their
        # line-coverage drops them into GREY → LLM → drop. Guard against
        # that false-positive by only allowing DROP when at least one
        # immediate neighbour is also DROP (i.e. metadata block) or the
        # line sits at the edge of the document.
        decisions, sandwich_rescued = self._apply_sandwich_protection(
            decisions,
        )

        # Safety bypass: if too many lines would be dropped, return original.
        content_lines = [d for d in decisions if d.action != "empty"]
        total_content = len(content_lines)
        kept_count = sum(
            1 for d in content_lines if d.action in ("keep", "trim")
        )
        safety_bypass = (
            total_content > 0
            and kept_count / total_content < self._safety_ratio
        )
        if safety_bypass:
            result = ASRFilterResult(
                lyrics_clean=candidate_lyrics,
                lines_kept=total_content,
                lines_dropped=0,
                lines_trimmed=0,
                grey_zone_lines=len(grey_indices),
                sandwich_rescued=sandwich_rescued,
                llm_called=llm_called,
                safety_bypass=True,
            )
            logger.info("asr_filter_safety_bypass", **result.as_dict())
            return result

        # Assemble output.
        out_lines: list[str] = []
        stats = ASRFilterResult(
            lyrics_clean="",
            llm_called=llm_called,
            grey_zone_lines=len(grey_indices),
            sandwich_rescued=sandwich_rescued,
        )
        for d in decisions:
            if d.action == "empty":
                out_lines.append(d.text)
            elif d.action == "drop":
                stats.lines_dropped += 1
                # Swallow the line entirely. Don't emit a blank in its place —
                # that would inflate paragraph spacing.
            elif d.action == "trim":
                out_lines.append(d.text)
                stats.lines_trimmed += 1
                stats.lines_kept += 1
            else:  # keep
                out_lines.append(d.text)
                stats.lines_kept += 1

        # Collapse 3+ consecutive blank lines to 2 (mirrors clean_lyrics).
        cleaned = "\n".join(out_lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n")
        stats.lyrics_clean = cleaned

        logger.info("asr_filter_result", **stats.as_dict())
        return stats

    # ------------------------------------------------------------------
    # Tokenization with offsets
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize_line(
        line_idx: int,
        line: str,
        featurize,
    ) -> list[_Token]:
        tokens: list[_Token] = []
        for m in re.finditer(r"\S+", line):
            original = m.group()
            cleaned = _clean_single_token(original)
            if not cleaned:
                tokens.append(_Token(
                    line_idx=line_idx,
                    char_start=m.start(), char_end=m.end(),
                    original=original, features=None,
                ))
                continue
            feats = featurize(cleaned)
            if not feats.text:
                tokens.append(_Token(
                    line_idx=line_idx,
                    char_start=m.start(), char_end=m.end(),
                    original=original, features=None,
                ))
                continue
            tokens.append(_Token(
                line_idx=line_idx,
                char_start=m.start(), char_end=m.end(),
                original=original, features=feats,
            ))
        return tokens

    # ------------------------------------------------------------------
    # Positional (sandwich) protection
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_sandwich_protection(
        decisions: list[_LineDecision],
    ) -> tuple[list[_LineDecision], int]:
        """Flip LLM-issued ``drop`` back to ``keep`` for any line whose
        closest non-empty neighbours on both sides are ``keep`` / ``trim``.

        Only LLM-driven drops are rescued — a drop caused by very low
        ASR coverage (``coverage < low_thresh``) is a hard signal that
        the words aren't in the audio, so CTC alignment wouldn't anchor
        them anyway. Lines containing section markers (``припев``,
        ``chorus``, etc.) are never rescued even from LLM drops.

        Two consecutive ``drop`` lines stay dropped (metadata block),
        and edges of the document aren't protected either.
        """
        result = list(decisions)
        rescued = 0
        for i, d in enumerate(result):
            if d.action != "drop":
                continue
            if not d.from_llm:
                continue
            if d.has_marker_word:
                continue
            prev_n = ASRLyricsFilter._find_content_neighbor(result, i, -1)
            next_n = ASRLyricsFilter._find_content_neighbor(result, i, +1)
            if (
                prev_n is not None
                and next_n is not None
                and prev_n.action in ("keep", "trim")
                and next_n.action in ("keep", "trim")
            ):
                result[i] = _LineDecision(
                    line_idx=d.line_idx,
                    action="keep",
                    text=d.text,
                    coverage=d.coverage,
                    from_llm=d.from_llm,
                    has_marker_word=d.has_marker_word,
                )
                rescued += 1
        return result, rescued

    @staticmethod
    def _find_content_neighbor(
        decisions: list[_LineDecision],
        start: int,
        direction: int,
    ) -> _LineDecision | None:
        """Closest non-``empty`` decision in the given direction, or None
        if the edge is reached."""
        i = start + direction
        while 0 <= i < len(decisions):
            if decisions[i].action != "empty":
                return decisions[i]
            i += direction
        return None

    # ------------------------------------------------------------------
    # Prefix / suffix trimming
    # ------------------------------------------------------------------

    def _try_trim_unmatched_edges(
        self,
        tokens: list[_Token],
        matched: list[bool],
        original_line: str,
    ) -> str | None:
        """Return trimmed line if an unmatched prefix or suffix of words
        looks like a metadata tag glued to a real line. None otherwise.
        """
        word_tokens = [t for t in tokens if t.features is not None]
        if len(word_tokens) != len(matched):
            return None

        first_match = next(
            (i for i, m in enumerate(matched) if m), None,
        )
        last_match = next(
            (
                len(matched) - 1 - i
                for i, m in enumerate(reversed(matched))
                if m
            ),
            None,
        )
        if first_match is None or last_match is None:
            return None

        prefix_len = first_match
        suffix_len = len(matched) - 1 - last_match

        trim_prefix = (
            prefix_len > 0
            and self._edge_is_trimmable(
                word_tokens[:prefix_len], original_line,
            )
        )
        trim_suffix = (
            suffix_len > 0
            and self._edge_is_trimmable(
                word_tokens[-suffix_len:], original_line,
            )
        )

        if not trim_prefix and not trim_suffix:
            return None

        cut_start = (
            word_tokens[first_match].char_start if trim_prefix else 0
        )
        cut_end = (
            word_tokens[last_match].char_end if trim_suffix else len(original_line)
        )
        new_line = original_line[cut_start:cut_end].strip()
        if not new_line:
            return None
        return new_line

    def _edge_is_trimmable(
        self,
        edge_tokens: list[_Token],
        original_line: str,
    ) -> bool:
        """Return True when the unmatched edge looks like a metadata tag
        (section label / remix/date marker / colon- or digit-bearing token).
        """
        if len(edge_tokens) < self._min_prefix or len(edge_tokens) > self._max_prefix:
            return False
        for t in edge_tokens:
            if t.features and t.features.text in _TRIM_MARKER_WORDS:
                return True
            original_slice = original_line[t.char_start:t.char_end]
            if _SUSPICIOUS_CHAR_RE.search(original_slice):
                return True
        return False

    # ------------------------------------------------------------------
    # LLM grey-zone resolver
    # ------------------------------------------------------------------

    async def _llm_decide_grey(
        self,
        asr_text: str,
        grey_lines: list[tuple[int, str]],
        language: str,
    ) -> dict[int, tuple[str, str | None]] | None:
        try:
            raw = await asyncio.to_thread(
                self._call_llm_grey, asr_text, grey_lines, language,
            )
        except Exception as exc:
            logger.warning("asr_filter_llm_failed", error=str(exc))
            return None
        decisions = _parse_llm_grey_response(raw, {i for i, _ in grey_lines})
        if decisions is None:
            logger.warning("asr_filter_llm_unparsed", raw=raw[:200])
        return decisions

    def _call_llm_grey(
        self,
        asr_text: str,
        grey_lines: list[tuple[int, str]],
        language: str,
    ) -> str:
        client = OpenAI(
            api_key=self._api_key,
            base_url="https://api.deepseek.com",
            timeout=30.0,
        )
        system = (
            "Ты верификатор строк текста песни. На вход: приблизительная ASR-"
            "расшифровка песни (Whisper делает ошибки, особенно в вокале) и "
            "массив сомнительных строк из предполагаемого текста песни. Для "
            "каждой строки выбери одно действие:\n"
            "- keep — строка реально поётся в песне (совпадает с ASR по смыслу, "
            "допускаются искажения Whisper);\n"
            "- drop — это метаданные, не поются (заголовок, дата, название "
            "трека, кредиты, маркер секции);\n"
            "- trim — в строке есть непоющийся префикс или суффикс; в поле "
            "text верни строку без мусора.\n"
            "При сомнении выбирай keep. Ответь ТОЛЬКО валидным JSON-массивом "
            "без комментариев: [{\"id\": 0, \"action\": \"drop\"}, "
            "{\"id\": 1, \"action\": \"trim\", \"text\": \"очищенная строка\"}]."
        )
        payload = {
            "language": language,
            "asr": asr_text,
            "lines": [{"id": i, "text": text} for i, text in grey_lines],
        }
        resp = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(
                    payload, ensure_ascii=False,
                )},
            ],
            temperature=0.0,
            max_tokens=1024 + 80 * len(grey_lines),
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""


@dataclass(frozen=True)
class _LineDecision:
    line_idx: int
    action: str  # keep | drop | trim | grey | empty
    text: str
    coverage: float = 0.0
    from_llm: bool = False  # True if action was set by the grey-zone LLM
    has_marker_word: bool = False  # True if tokens contain припев/chorus/etc.


def _clean_single_token(token: str) -> str:
    """Normalize a single whitespace-separated token. Mirrors the regexes
    used by ``normalizer._clean_text`` but on one token. Returns the
    cleaned form; empty string if token is pure junk (markers / digits /
    punctuation only).
    """
    if not token:
        return ""
    cleaned = unicodedata.normalize("NFKC", token).lower()
    cleaned = _SECTION_RE.sub(" ", cleaned)
    cleaned = _SHORT_PARENS_RE.sub(" ", cleaned)
    cleaned = _DIGIT_RUN_RE.sub(" ", cleaned)
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return ""
    # If the token contained punctuation that split it into parts (rare —
    # e.g. "hello/world"), take the longest remaining piece.
    parts = cleaned.split()
    if not parts:
        return ""
    return max(parts, key=len)


def _features_from_text(text: str, language: str) -> tuple[WordFeatures, ...]:
    """Normalized featurized words from ``text`` — same logic as
    ``normalizer.normalize_text`` but inlined to avoid the extra dataclass."""
    if not text:
        return ()
    cleaned = unicodedata.normalize("NFKC", text).lower()
    cleaned = _SECTION_RE.sub(" ", cleaned)
    cleaned = _SHORT_PARENS_RE.sub(" ", cleaned)
    cleaned = _DIGIT_RUN_RE.sub(" ", cleaned)
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    featurize = make_word_featurizer(language)
    words = tuple(featurize(t) for t in cleaned.split() if t)
    return tuple(w for w in words if w.text)


def _parse_llm_grey_response(
    raw: str,
    expected_ids: set[int],
) -> dict[int, tuple[str, str | None]] | None:
    """Parse the LLM JSON response into ``{line_idx: (action, text|None)}``.

    Accepts either a raw JSON array or a JSON object with a ``lines``
    array (DeepSeek's ``response_format={"type":"json_object"}`` sometimes
    wraps arrays in an object). Unknown ids are ignored; missing ids will
    default to ``keep`` upstream.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        # Find the first list value — DeepSeek commonly wraps under
        # keys like "lines", "results", "decisions".
        items = None
        for v in parsed.values():
            if isinstance(v, list):
                items = v
                break
        if items is None:
            return None
    elif isinstance(parsed, list):
        items = parsed
    else:
        return None
    out: dict[int, tuple[str, str | None]] = {}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        line_id = entry.get("id")
        action = entry.get("action")
        if not isinstance(line_id, int) or line_id not in expected_ids:
            continue
        if action not in ("keep", "drop", "trim"):
            continue
        new_text = entry.get("text") if action == "trim" else None
        if action == "trim" and not (isinstance(new_text, str) and new_text.strip()):
            # Treat malformed trim as keep — "when in doubt, keep".
            action = "keep"
            new_text = None
        out[line_id] = (action, new_text)
    return out
