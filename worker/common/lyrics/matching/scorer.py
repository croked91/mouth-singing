"""Multi-feature scoring of candidate lyrics against an ASR transcription.

For each candidate we compute six independent features in [0..1] and combine
them into a single ``composite`` score:

* ``coverage_asr`` — fraction of ASR words that have a good match (lemma /
  consonant-skeleton / metaphone / Levenshtein ≥ 0.8) somewhere in the
  candidate. Main signal: «правильный кандидат должен покрывать почти все
  слова из ASR».
* ``coverage_cand`` — symmetric, candidate-side. Low value indicates that the
  candidate is much longer than the ASR — likely a remix / long version.
* ``phonetic_match_rate`` — mean per-ASR-word match score normalized to [0..1].
  Rewards candidates with stronger (exact / lemma) matches over fuzzy ones.
* ``ngram_jaccard`` — Jaccard similarity of consonant-skeleton 4-grams. Catches
  phrase-level overlap and is robust against vowel substitutions.
* ``rare_anchor_score`` — IDF-weighted count of 5-word lemma phrases shared
  between ASR and candidate but rare across the candidate pool. Strongest
  signal for distinguishing very-similar candidates (different versions).
* ``length_ratio_penalty`` — ``|log(cand_words / asr_words)|`` clamped to
  [0..1]. ASR is assumed to cover the full song, so big size mismatch is bad.

The composite combines the two coverage values via their **harmonic mean (F1)**
so a candidate must score high on BOTH directions to win — this is what stops
remix / long-mix candidates from winning purely on ``coverage_asr`` while
``coverage_cand`` is low.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz

from worker.common.lyrics.matching.linguistics import WordFeatures
from worker.common.lyrics.matching.normalizer import NormalizedText

_NGRAM_N = 4
_ANCHOR_N = 5
_LEV_RATIO_HIGH = 80.0
_LEV_RATIO_LOW = 60.0
_LEV_LEN_TOL = 2

# Composite weights — sum of positive weights = 1.0; penalty subtracts.
_W_COV_F1 = 0.55
_W_PHONETIC = 0.15
_W_NGRAM = 0.10
_W_ANCHOR = 0.20
_W_LENGTH_PEN = 0.10


@dataclass(frozen=True)
class MatchFeatures:
    coverage_asr: float
    coverage_cand: float
    phonetic_match_rate: float
    ngram_jaccard: float
    rare_anchor_score: float
    length_ratio_penalty: float
    composite: float

    def as_dict(self) -> dict[str, float]:
        return {
            "coverage_asr": round(self.coverage_asr, 3),
            "coverage_cand": round(self.coverage_cand, 3),
            "phonetic_match_rate": round(self.phonetic_match_rate, 3),
            "ngram_jaccard": round(self.ngram_jaccard, 3),
            "rare_anchor_score": round(self.rare_anchor_score, 3),
            "length_ratio_penalty": round(self.length_ratio_penalty, 3),
            "composite": round(self.composite, 3),
        }


def score_all(
    asr: NormalizedText,
    candidates: list[NormalizedText],
) -> list[MatchFeatures]:
    """Score every candidate against the ASR. Returns features in input order."""
    if not candidates:
        return []
    rare_scores = _rare_anchor_scores(asr, candidates, n=_ANCHOR_N)
    return [
        _score_one(asr, cand, rare_scores[i])
        for i, cand in enumerate(candidates)
    ]


def _score_one(
    asr: NormalizedText,
    cand: NormalizedText,
    rare_anchor_score: float,
) -> MatchFeatures:
    if not asr.words or not cand.words:
        return MatchFeatures(0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    asr_idx = _build_index(asr.words)
    cand_idx = _build_index(cand.words)

    asr_scores = [_match_score(w, cand_idx) for w in asr.words]
    cand_scores = [_match_score(w, asr_idx) for w in cand.words]

    coverage_asr = sum(1 for s in asr_scores if s >= 2) / len(asr_scores)
    coverage_cand = sum(1 for s in cand_scores if s >= 2) / len(cand_scores)
    phonetic_match_rate = sum(asr_scores) / (3.0 * len(asr_scores))

    ngram_jaccard = _ngram_jaccard(asr.words, cand.words, n=_NGRAM_N)

    length_ratio_penalty = min(
        1.0,
        abs(math.log(len(cand.words) / len(asr.words))),
    )

    coverage_f1 = _harmonic_mean(coverage_asr, coverage_cand)

    composite = (
        _W_COV_F1 * coverage_f1
        + _W_PHONETIC * phonetic_match_rate
        + _W_NGRAM * ngram_jaccard
        + _W_ANCHOR * rare_anchor_score
        - _W_LENGTH_PEN * length_ratio_penalty
    )
    composite = max(0.0, min(1.0, composite))

    return MatchFeatures(
        coverage_asr=coverage_asr,
        coverage_cand=coverage_cand,
        phonetic_match_rate=phonetic_match_rate,
        ngram_jaccard=ngram_jaccard,
        rare_anchor_score=rare_anchor_score,
        length_ratio_penalty=length_ratio_penalty,
        composite=composite,
    )


@dataclass(frozen=True)
class _Index:
    texts: frozenset[str]
    lemmas: frozenset[str]
    skeletons: frozenset[str]
    metaphones: frozenset[str]
    text_list: tuple[str, ...]


def _build_index(words: tuple[WordFeatures, ...]) -> _Index:
    texts: set[str] = set()
    lemmas: set[str] = set()
    skeletons: set[str] = set()
    metaphones: set[str] = set()
    text_list: list[str] = []
    for w in words:
        if not w.text:
            continue
        texts.add(w.text)
        text_list.append(w.text)
        if w.lemma:
            lemmas.add(w.lemma)
        if w.skeleton:
            skeletons.add(w.skeleton)
        if w.metaphone:
            metaphones.add(w.metaphone)
    return _Index(
        texts=frozenset(texts),
        lemmas=frozenset(lemmas),
        skeletons=frozenset(skeletons),
        metaphones=frozenset(metaphones),
        text_list=tuple(text_list),
    )


def _match_score(word: WordFeatures, idx: _Index) -> int:
    if not word.text:
        return 0
    if word.text in idx.texts:
        return 3
    score = 0
    if word.lemma and word.lemma in idx.lemmas:
        score = 2
    if score < 2 and word.skeleton and word.skeleton in idx.skeletons:
        score = 2
    if score < 2 and word.metaphone and word.metaphone in idx.metaphones:
        score = 2
    if score >= 2:
        return score
    # Fallback: Levenshtein against candidate words of similar length.
    target_len = len(word.text)
    best_ratio = 0.0
    for t in idx.text_list:
        if abs(len(t) - target_len) > _LEV_LEN_TOL:
            continue
        r = fuzz.ratio(word.text, t)
        if r > best_ratio:
            best_ratio = r
            if best_ratio >= _LEV_RATIO_HIGH:
                return 2
    if best_ratio >= _LEV_RATIO_LOW:
        return 1
    return 0


def _ngram_jaccard(
    asr_words: tuple[WordFeatures, ...],
    cand_words: tuple[WordFeatures, ...],
    n: int,
) -> float:
    asr_grams = _skeleton_ngrams(asr_words, n)
    cand_grams = _skeleton_ngrams(cand_words, n)
    if not asr_grams or not cand_grams:
        return 0.0
    inter = asr_grams & cand_grams
    union = asr_grams | cand_grams
    return len(inter) / len(union) if union else 0.0


def _skeleton_ngrams(
    words: tuple[WordFeatures, ...], n: int,
) -> set[tuple[str, ...]]:
    keys = [w.skeleton or "?" for w in words]
    if len(keys) < n:
        return set()
    return {tuple(keys[i : i + n]) for i in range(len(keys) - n + 1)}


def _rare_anchor_scores(
    asr: NormalizedText,
    candidates: list[NormalizedText],
    n: int,
) -> list[float]:
    """For each candidate, IDF-weighted count of n-word phrases shared with
    ASR but rare across the candidate pool. Normalized to [0..1] by the max."""
    if not candidates:
        return []
    asr_grams = _lemma_ngrams(asr.words, n)
    cand_grams_list = [_lemma_ngrams(c.words, n) for c in candidates]

    df: dict[tuple[str, ...], int] = defaultdict(int)
    for grams in cand_grams_list:
        for g in grams:
            df[g] += 1

    raw: list[float] = []
    for grams in cand_grams_list:
        s = 0.0
        for g in grams:
            if g in asr_grams and df[g] > 0:
                s += 1.0 / df[g]
        raw.append(s)

    max_raw = max(raw) if raw else 0.0
    if max_raw <= 0:
        return [0.0] * len(candidates)
    return [s / max_raw for s in raw]


def _lemma_ngrams(
    words: tuple[WordFeatures, ...], n: int,
) -> set[tuple[str, ...]]:
    keys = [w.lemma or w.text for w in words]
    if len(keys) < n:
        return set()
    return {tuple(keys[i : i + n]) for i in range(len(keys) - n + 1)}


def _harmonic_mean(a: float, b: float) -> float:
    if a + b <= 0:
        return 0.0
    return 2.0 * a * b / (a + b)
