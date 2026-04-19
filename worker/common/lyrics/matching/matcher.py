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

from worker.common.lyrics.base_provider import LyricsCandidate
from worker.common.lyrics.matching.asr_filter import ASRLyricsFilter
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
        asr_filter: ASRLyricsFilter | None = None,
        deepseek_api_key: str | None = None,
        model: str = "deepseek-chat",
        thresh_strong: float = 0.65,
        thresh_weak: float = 0.45,
        margin: float = 0.05,
    ) -> None:
        self._expander = expander
        self._asr_filter = asr_filter
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
    ) -> LyricsResult | None:
        if not candidates or not asr_text.strip():
            return None

        expanded = await self._expand_all(candidates)
        asr_norm = normalize_text(asr_text, language)
        cand_norms = [normalize_text(exp, language) for exp in expanded]
        feature_list = score_all(asr_norm, cand_norms)

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
                return await self._build_result(
                    top, asr_text, language, confidence="high",
                )
            picked = await self._tiebreak(asr_text, top, second, language)
            if picked is not None:
                logger.info(
                    "matcher_decision",
                    outcome="tiebreaker",
                    picked=picked.candidate.source,
                    **log,
                )
                return await self._build_result(
                    picked, asr_text, language, confidence="high",
                )
            logger.info("matcher_decision", outcome="strong_close_no_tb", **log)
            return await self._build_result(
                top, asr_text, language, confidence="medium",
            )

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
                picked = await self._tiebreak(asr_text, top, second, language)
                if picked is not None:
                    logger.info(
                        "matcher_decision",
                        outcome="weak_tiebreaker",
                        picked=picked.candidate.source,
                        **log,
                    )
                    return await self._build_result(
                        picked, asr_text, language, confidence="medium",
                    )
            logger.info("matcher_decision", outcome="weak_win", **log)
            return await self._build_result(
                top, asr_text, language, confidence="medium",
            )

        logger.info("matcher_decision", outcome="reject", **log)
        return None

    # ------------------------------------------------------------------

    async def _expand_all(self, candidates: list[LyricsCandidate]) -> list[str]:
        if not self._expander:
            return [c.lyrics for c in candidates]
        return await asyncio.gather(
            *(self._expander.expand(c.lyrics) for c in candidates)
        )

    async def _build_result(
        self,
        ranked: _Ranked,
        asr_text: str,
        language: str,
        confidence: str,
    ) -> LyricsResult:
        # Start from the EXPANDED lyrics (those reflect what's actually sung).
        # Run them through the ASR-driven filter (drops non-sung metadata that
        # would otherwise confuse the CTC aligner) before the final cleanup
        # of residual bracketed markers.
        lyrics = ranked.expanded_lyrics
        if self._asr_filter is not None and asr_text.strip():
            filtered = await self._asr_filter.filter(
                asr_text=asr_text,
                candidate_lyrics=lyrics,
                language=language,
            )
            lyrics = filtered.lyrics_clean
        lyrics = clean_lyrics(lyrics).strip()
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
    ) -> _Ranked | None:
        if not self._api_key:
            return None
        try:
            answer = await asyncio.to_thread(
                self._call_llm_tiebreak,
                asr_text,
                a.expanded_lyrics,
                b.expanded_lyrics,
                language,
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
        cand_a: str,
        cand_b: str,
        language: str,
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
            "Ответь строго одной цифрой: 1 или 2. Никаких пояснений."
        )
        user = (
            f'<asr language="{language}">\n{asr_text}\n</asr>\n\n'
            f'<candidate id="1">\n{cand_a}\n</candidate>\n\n'
            f'<candidate id="2">\n{cand_b}\n</candidate>'
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
