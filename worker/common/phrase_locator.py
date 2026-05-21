"""ASR + phonetic phrase locator for rough line windows.

The locator does not replace forced alignment.  It finds likely phrase
locations in the vocal stem, then auto-repair runs the existing syllable
alignment pipeline on those candidate windows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import structlog
from rapidfuzz import fuzz

from karaoke_shared.storage import S3Storage

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AsrWord:
    word: str
    start: float
    end: float
    probability: float | None = None


@dataclass(frozen=True)
class PhraseLocatorCandidate:
    start: float
    end: float
    confidence: float
    matched_text: str
    method: str
    phoneme_score: float
    text_score: float
    duration_score: float
    position_score: float


class PhraseLocator:
    """Find rough audio windows for text using ASR words and phonetic fuzzy match."""

    def __init__(
        self,
        storage: S3Storage,
        track_id: str,
        audio_key: str,
        model_size: str = "tiny",
        device: str = "cuda",
        compute_type: str = "float16",
        model_cache_dir: str | None = None,
    ) -> None:
        self.storage = storage
        self.track_id = track_id
        self.audio_key = audio_key
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.model_cache_dir = model_cache_dir
        self._model = None
        self._phoneme_cache: dict[tuple[str, str], str] = {}

    async def get_word_stream(self, audio_path: str, language: str) -> list[AsrWord]:
        cache_key = self._cache_key(language)
        if await self.storage.exists(cache_key):
            try:
                payload = json.loads((await self.storage.download(cache_key)).decode("utf-8"))
                words = self._parse_cached_words(payload)
                if words:
                    logger.info("phrase_locator_asr_cache_hit", key=cache_key, words=len(words))
                    return words
            except Exception as exc:  # noqa: BLE001
                logger.warning("phrase_locator_asr_cache_invalid", key=cache_key, error=str(exc))

        words = await asyncio.to_thread(self._transcribe_words, audio_path, language)
        payload = {
            "version": 1,
            "audio_key": self.audio_key,
            "model": f"faster-whisper:{self.model_size}",
            "language": self._language_code(language),
            "words": [
                {
                    "word": word.word,
                    "start": word.start,
                    "end": word.end,
                    "probability": word.probability,
                }
                for word in words
            ],
        }
        await self.storage.upload(
            cache_key,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        logger.info("phrase_locator_asr_cache_written", key=cache_key, words=len(words))
        return words

    def locate(
        self,
        query_text: str,
        words: list[AsrWord],
        language: str,
        old_start: float,
        old_end: float,
        track_duration: float,
        limit: int = 8,
        threshold: float = 0.55,
        keep_overlapping: bool = False,
    ) -> list[PhraseLocatorCandidate]:
        query_tokens = self._tokenize(query_text, language)
        if not query_tokens or not words:
            return []

        query_variants = self._query_variants(query_text, language)
        query_phonemes = [
            self._phonemize_text(variant, language)
            for variant in query_variants
            if variant.strip()
        ]
        query_norms = [self._normalize_text(variant, language) for variant in query_variants]
        query_len = len(query_tokens)
        min_len = max(1, query_len - 2)
        max_len = min(len(words), query_len + 3)

        candidates: list[PhraseLocatorCandidate] = []
        for size in range(min_len, max_len + 1):
            for start_idx in range(0, len(words) - size + 1):
                window = words[start_idx : start_idx + size]
                candidate_text = " ".join(word.word for word in window)
                candidate_norm = self._normalize_text(candidate_text, language)
                if not candidate_norm:
                    continue

                candidate_phonemes = self._phonemize_text(candidate_text, language)
                phoneme_score = max(
                    (fuzz.ratio(query_phoneme, candidate_phonemes) / 100.0 for query_phoneme in query_phonemes),
                    default=0.0,
                )
                text_score = max(
                    (fuzz.token_sort_ratio(query_norm, candidate_norm) / 100.0 for query_norm in query_norms),
                    default=0.0,
                )
                duration = max(0.001, window[-1].end - window[0].start)
                duration_score = self._duration_score(query_len, duration)
                position_score = self._position_score(
                    window[0].start,
                    window[-1].end,
                    old_start,
                    old_end,
                    track_duration,
                )
                score = (
                    phoneme_score * 0.55
                    + text_score * 0.25
                    + duration_score * 0.10
                    + position_score * 0.10
                )
                if score < threshold:
                    continue
                candidates.append(
                    PhraseLocatorCandidate(
                        start=round(max(0.0, window[0].start), 3),
                        end=round(min(track_duration, window[-1].end), 3),
                        confidence=round(score, 4),
                        matched_text=candidate_text.strip(),
                        method="asr_phonetic",
                        phoneme_score=round(phoneme_score, 4),
                        text_score=round(text_score, 4),
                        duration_score=round(duration_score, 4),
                        position_score=round(position_score, 4),
                    )
                )

        candidates.sort(key=lambda item: item.confidence, reverse=True)
        if keep_overlapping:
            return candidates[:limit]
        return self._dedupe_overlaps(candidates, limit)

    def cleanup(self) -> None:
        self._model = None

    def _cache_key(self, language: str) -> str:
        raw = f"{self.audio_key}|{self.model_size}|{self.compute_type}|{self._language_code(language)}|v1"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"alignment-cache/{self.track_id}/asr-wordstream-v1/{digest}.json"

    @staticmethod
    def _parse_cached_words(payload: dict[str, Any]) -> list[AsrWord]:
        if payload.get("version") != 1:
            return []
        result: list[AsrWord] = []
        for item in payload.get("words") or []:
            word = str(item.get("word") or "").strip()
            start = item.get("start")
            end = item.get("end")
            if not word or start is None or end is None:
                continue
            result.append(
                AsrWord(
                    word=word,
                    start=round(float(start), 3),
                    end=round(float(end), 3),
                    probability=(
                        float(item["probability"])
                        if item.get("probability") is not None
                        else None
                    ),
                )
            )
        return result

    def _transcribe_words(self, audio_path: str, language: str) -> list[AsrWord]:
        from faster_whisper import WhisperModel

        if self._model is None:
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.model_cache_dir,
            )
        segments, info = self._model.transcribe(
            audio_path,
            language=self._language_code(language),
            beam_size=5,
            word_timestamps=True,
            vad_filter=False,
        )
        result: list[AsrWord] = []
        for segment in segments:
            for word in segment.words or []:
                if word.start is None or word.end is None:
                    continue
                text = (word.word or "").strip()
                if not text:
                    continue
                result.append(
                    AsrWord(
                        word=text,
                        start=round(float(word.start), 3),
                        end=round(float(word.end), 3),
                        probability=(
                            float(word.probability)
                            if word.probability is not None
                            else None
                        ),
                    )
                )
        logger.info(
            "phrase_locator_asr_completed",
            language=getattr(info, "language", None),
            words=len(result),
        )
        return result

    def _phonemize_text(self, text: str, language: str) -> str:
        normalized = self._normalize_text(text, language)
        if not normalized:
            return ""
        cache_key = (self._language_code(language), normalized)
        cached = self._phoneme_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            from phonemizer import phonemize

            phonemes = phonemize(
                normalized,
                language=self._phonemizer_language(language),
                backend="espeak",
                strip=True,
                preserve_punctuation=False,
                with_stress=False,
                njobs=1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("phrase_locator_phonemizer_failed", language=language, error=str(exc))
            phonemes = normalized
        if isinstance(phonemes, list):
            phonemes = " ".join(str(item) for item in phonemes)
        phonemes = self._normalize_phonemes(phonemes)
        self._phoneme_cache[cache_key] = phonemes
        return phonemes

    def _query_variants(self, text: str, language: str) -> list[str]:
        normalized = self._normalize_text(text, language)
        variants = [normalized]
        if self._language_code(language) == "en":
            expanded = self._expand_english_contractions(normalized)
            compact = normalized.replace("'", "")
            variants.extend([expanded, compact])
        if self._language_code(language) == "ru":
            variants.extend([normalized.replace("ё", "е"), normalized.replace("е", "ё")])
        return list(dict.fromkeys(variant for variant in variants if variant.strip()))

    @staticmethod
    def _tokenize(text: str, language: str) -> list[str]:
        normalized = PhraseLocator._normalize_text(text, language)
        return normalized.split()

    @staticmethod
    def _normalize_text(text: str, language: str) -> str:
        del language
        text = unicodedata.normalize("NFKC", text)
        text = text.lower().replace("’", "'").replace("`", "'")
        text = re.sub(r"[^0-9a-zа-яё'\s-]+", " ", text, flags=re.IGNORECASE)
        text = text.replace("-", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _normalize_phonemes(text: str) -> str:
        text = unicodedata.normalize("NFKC", text).lower()
        text = re.sub(r"[ˈˌ.ːˑ]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _expand_english_contractions(text: str) -> str:
        replacements = {
            "don't": "do not",
            "dont": "do not",
            "won't": "will not",
            "can't": "can not",
            "i'm": "i am",
            "i'll": "i will",
            "i've": "i have",
            "you're": "you are",
            "we're": "we are",
            "they're": "they are",
            "it's": "it is",
            "that's": "that is",
            "there's": "there is",
            "what's": "what is",
            "n't": " not",
            "'re": " are",
            "'ve": " have",
            "'ll": " will",
            "'d": " would",
            "'m": " am",
            "'s": " is",
        }
        result = text
        for source, target in replacements.items():
            result = result.replace(source, target)
        return re.sub(r"\s+", " ", result).strip()

    @staticmethod
    def _duration_score(query_words: int, duration: float) -> float:
        if duration <= 0:
            return 0.0
        words_per_sec = query_words / duration
        if 1.0 <= words_per_sec <= 5.2:
            return 1.0
        distance = min(abs(words_per_sec - 1.0), abs(words_per_sec - 5.2))
        return max(0.0, 1.0 - distance / 3.0)

    @staticmethod
    def _position_score(
        start: float,
        end: float,
        old_start: float,
        old_end: float,
        track_duration: float,
    ) -> float:
        old_center = (old_start + old_end) / 2.0
        center = (start + end) / 2.0
        tolerance = max(8.0, min(30.0, track_duration * 0.15))
        return max(0.0, 1.0 - abs(center - old_center) / tolerance)

    @staticmethod
    def _dedupe_overlaps(
        candidates: list[PhraseLocatorCandidate],
        limit: int,
    ) -> list[PhraseLocatorCandidate]:
        selected: list[PhraseLocatorCandidate] = []
        for candidate in candidates:
            if all(PhraseLocator._overlap_ratio(candidate, existing) < 0.65 for existing in selected):
                selected.append(candidate)
            if len(selected) >= limit:
                break
        return selected

    @staticmethod
    def _overlap_ratio(a: PhraseLocatorCandidate, b: PhraseLocatorCandidate) -> float:
        overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
        shortest = max(0.001, min(a.end - a.start, b.end - b.start))
        return overlap / shortest

    @staticmethod
    def _language_code(language: str) -> str:
        code = (language or "en").lower().split("-")[0]
        return "ru" if code in {"ru", "rus"} else "en"

    @staticmethod
    def _phonemizer_language(language: str) -> str:
        return "ru" if PhraseLocator._language_code(language) == "ru" else "en-us"
