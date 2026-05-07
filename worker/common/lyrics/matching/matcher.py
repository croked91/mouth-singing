"""Algorithmic lyrics matcher: pick the candidate that best matches the ASR.

Pipeline per ``match()`` call:

1. Expand repetition shortcuts in every candidate via :class:`LyricsExpander`.
2. Normalize ASR + each expanded candidate (NFKC, lowercase, strip markers).
3. Score every candidate with the multi-feature scorer (F1 coverage, phonetic
   match rate, n-gram Jaccard, rare anchor IDF, length-ratio penalty).
4. Pick a winner based on absolute thresholds and the margin to runner-up.
   Optionally call DeepSeek as a tiebreaker when top-2 are very close.

Returns ``None`` if no candidate is good enough — caller should escalate to
the fallback agent (web search).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
from openai import OpenAI
from rapidfuzz import fuzz

from worker.common.lyrics.base_provider import LyricsCandidate
from worker.common.lyrics.matching.expander import LyricsExpander
from worker.common.lyrics.matching.normalizer import normalize_text
from worker.common.lyrics.matching.scorer import MatchFeatures, score_all
from worker.common.lyrics_searcher import LyricsResult, clean_lyrics

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _Ranked:
    candidate: LyricsCandidate
    expanded_lyrics: str
    features: MatchFeatures


class LyricsMatcher:
    def __init__(
        self,
        expander: LyricsExpander | None = None,
        deepseek_api_key: str | None = None,
        model: str = "deepseek-chat",
        thresh_strong: float = 0.65,
        thresh_weak: float = 0.45,
        margin: float = 0.05,
    ) -> None:
        self._expander = expander
        self._api_key = deepseek_api_key
        self._model = model
        self._thresh_strong = thresh_strong
        self._thresh_weak = thresh_weak
        self._margin = margin

    async def match(
        self,
        asr_text: str,
        candidates: list[LyricsCandidate],
        language: str,
        artist_hints: list[str] | None = None,
        title_hints: list[str] | None = None,
    ) -> LyricsResult | None:
        if not candidates or not asr_text.strip():
            return None

        expanded = await self._expand_all(candidates)
        asr_norm = normalize_text(asr_text, language)
        cand_norms = [normalize_text(exp, language) for exp in expanded]
        hint_scores = [
            _hint_match_score(
                c.artist, c.title, artist_hints or [], title_hints or [],
            )
            for c in candidates
        ]
        feature_list = score_all(asr_norm, cand_norms, hint_scores=hint_scores)

        for cand, feats, exp_text in zip(candidates, feature_list, expanded):
            logger.info(
                "matcher_features",
                artist=cand.artist,
                title=cand.title,
                source=cand.source,
                cand_words=len(exp_text.split()),
                cand_lyrics=exp_text,
                **feats.as_dict(),
            )

        ranked = sorted(
            (
                _Ranked(cand, exp, feats)
                for cand, exp, feats in zip(candidates, expanded, feature_list)
            ),
            key=lambda r: r.features.composite,
            reverse=True,
        )
        top = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        gap = top.features.composite - (second.features.composite if second else 0.0)

        log = {
            "top_score": round(top.features.composite, 3),
            "second_score": round(second.features.composite, 3) if second else 0.0,
            "margin": round(gap, 3),
            "top_source": top.candidate.source,
            "top_artist": top.candidate.artist,
            "top_title": top.candidate.title,
        }

        if top.features.composite >= self._thresh_strong:
            if gap >= self._margin or second is None:
                logger.info("matcher_decision", outcome="strong_win", **log)
                return self._build_result(top, language, confidence="high")
            picked = await self._tiebreak(
                asr_text, top, second, language,
                artist_hints=artist_hints, title_hints=title_hints,
            )
            if picked is not None:
                logger.info(
                    "matcher_decision",
                    outcome="tiebreaker",
                    picked=picked.candidate.source,
                    **log,
                )
                return self._build_result(picked, language, confidence="high")
            logger.info("matcher_decision", outcome="strong_close_no_tb", **log)
            return self._build_result(top, language, confidence="medium")

        if top.features.composite >= self._thresh_weak:
            # In the weak band we always attempt an LLM tiebreak when a
            # runner-up exists — including the case where the two top
            # candidates are very close (small gap). A small gap between
            # two candidates that both clear ``thresh_weak`` usually means
            # they are the same song from different providers (e.g. two
            # SearxNG hits for "Slava KPSS - Культура G" with slightly
            # different artist spelling). Rejecting both and falling back
            # to raw ASR loses correct text for no good reason.
            if second is not None:
                picked = await self._tiebreak(
                    asr_text, top, second, language,
                    artist_hints=artist_hints, title_hints=title_hints,
                )
                if picked is not None:
                    logger.info(
                        "matcher_decision",
                        outcome="weak_tiebreaker",
                        picked=picked.candidate.source,
                        **log,
                    )
                    return self._build_result(
                        picked, language, confidence="medium",
                    )
            logger.info("matcher_decision", outcome="weak_win", **log)
            return self._build_result(top, language, confidence="medium")

        logger.info("matcher_decision", outcome="reject", **log)
        return None

    # ------------------------------------------------------------------

    async def _expand_all(self, candidates: list[LyricsCandidate]) -> list[str]:
        if not self._expander:
            return [c.lyrics for c in candidates]
        return await asyncio.gather(
            *(self._expander.expand(c.lyrics) for c in candidates)
        )

    def _build_result(
        self,
        ranked: _Ranked,
        language: str,
        confidence: str,
    ) -> LyricsResult:
        lyrics = clean_lyrics(ranked.expanded_lyrics).strip()
        return LyricsResult(
            artist=ranked.candidate.artist,
            title=ranked.candidate.title,
            lyrics=lyrics,
            language=language,
            confidence=confidence,
            source_note=ranked.candidate.source,
        )

    # ------------------------------------------------------------------
    # LLM tiebreaker — only used when top-2 are within ``margin`` and an
    # API key is configured. Asks for a single digit answer.
    # ------------------------------------------------------------------

    async def _tiebreak(
        self,
        asr_text: str,
        a: _Ranked,
        b: _Ranked,
        language: str,
        artist_hints: list[str] | None = None,
        title_hints: list[str] | None = None,
    ) -> _Ranked | None:
        if not self._api_key:
            return None
        try:
            answer = await asyncio.to_thread(
                self._call_llm_tiebreak,
                asr_text,
                a, b,
                language,
                artist_hints or [],
                title_hints or [],
            )
        except Exception as exc:
            logger.warning("matcher_tiebreak_failed", error=str(exc))
            return None
        cleaned = (answer or "").strip()
        if cleaned.startswith("1"):
            return a
        if cleaned.startswith("2"):
            return b
        logger.warning("matcher_tiebreak_unparsed", raw=cleaned[:40])
        return None

    def _call_llm_tiebreak(
        self,
        asr_text: str,
        a: _Ranked,
        b: _Ranked,
        language: str,
        artist_hints: list[str],
        title_hints: list[str],
    ) -> str:
        client = OpenAI(
            api_key=self._api_key,
            base_url="https://api.deepseek.com",
            timeout=30.0,
        )
        system = (
            "Ты выбираешь, какой из двух текстов песни ТОЧНЕЕ соответствует "
            "приблизительной расшифровке от Whisper (с ошибками). Учитывай "
            "что Whisper мог исказить слова — ищи смысловое совпадение. "
            "Если присутствует <filename_hint> — это артист/название из "
            "имени загруженного файла, СИЛЬНЫЙ приоритетный сигнал, "
            "особенно когда ASR содержит мало распознаваемых слов "
            "(инструменталки, скэт, повторы la-la / a-a). У каждого "
            "кандидата проверь его artist/title против hint и совпадение "
            "lyrics с ASR. Ответь строго одной цифрой: 1 или 2. Никаких "
            "пояснений."
        )
        hint_block = ""
        if artist_hints or title_hints:
            artist_str = " / ".join(h for h in artist_hints if h) or "—"
            title_str = " / ".join(h for h in title_hints if h) or "—"
            hint_block = (
                f"<filename_hint>\n"
                f"  artist: {artist_str}\n"
                f"  title: {title_str}\n"
                f"</filename_hint>\n\n"
            )
        user = (
            f'<asr language="{language}">\n{asr_text}\n</asr>\n\n'
            f"{hint_block}"
            f'<candidate id="1" artist="{a.candidate.artist}" '
            f'title="{a.candidate.title}">\n{a.expanded_lyrics}\n</candidate>'
            f'\n\n<candidate id="2" artist="{b.candidate.artist}" '
            f'title="{b.candidate.title}">\n{b.expanded_lyrics}\n</candidate>'
        )
        resp = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=4,
        )
        return resp.choices[0].message.content or ""


# Below this raw partial_ratio the match is noise (a 10-char query against
# any 30-char haystack averages ~0.5 just from coincidental letter overlap).
# Above it, scale linearly to [0..1] so that a 0.65 raw match contributes
# nothing while a perfect 1.0 match contributes the full bonus.
_HINT_NOISE_FLOOR = 0.65


def _hint_match_score(
    cand_artist: str,
    cand_title: str,
    artist_hints: list[str],
    title_hints: list[str],
) -> float:
    """Fuzzy-match candidate identity against filename-derived hints.

    Returns a value in [0..1]. Combines candidate ``artist + title`` into a
    single haystack so providers like Genius — where the canonical artist
    sometimes lives inside the title field (``artist="Genius English
    Translations"``, ``title="Eduard Khil — Я очень рад…"``) — still match.
    Each hint variant is checked with ``partial_ratio`` (substring-tolerant)
    so transliterations and embedded canonical names both score well.

    Below ``_HINT_NOISE_FLOOR`` the raw partial_ratio is treated as noise
    and the side score is 0. Above the floor, it is rescaled to [0..1]. The
    final score averages artist and title sides when both hint sets exist.
    """
    if not artist_hints and not title_hints:
        return 0.0

    haystack = f"{cand_artist} {cand_title}".casefold()
    if not haystack.strip():
        return 0.0

    def best(hints: list[str]) -> float | None:
        best_ratio = -1.0
        for h in hints:
            h = (h or "").strip().casefold()
            if not h:
                continue
            r = fuzz.partial_ratio(h, haystack) / 100.0
            if r > best_ratio:
                best_ratio = r
        if best_ratio < 0:
            return None
        if best_ratio < _HINT_NOISE_FLOOR:
            return 0.0
        return (best_ratio - _HINT_NOISE_FLOOR) / (1.0 - _HINT_NOISE_FLOOR)

    a = best(artist_hints) if artist_hints else None
    t = best(title_hints) if title_hints else None
    parts = [s for s in (a, t) if s is not None]
    return sum(parts) / len(parts) if parts else 0.0
