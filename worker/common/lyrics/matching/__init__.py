"""Algorithmic matcher for picking the best lyrics candidate against ASR."""

from worker.common.lyrics.matching.expander import LyricsExpander
from worker.common.lyrics.matching.matcher import LyricsMatcher
from worker.common.lyrics.matching.normalizer import NormalizedText, normalize_text
from worker.common.lyrics.matching.scorer import MatchFeatures, score_all

__all__ = [
    "LyricsExpander",
    "LyricsMatcher",
    "MatchFeatures",
    "NormalizedText",
    "normalize_text",
    "score_all",
]
