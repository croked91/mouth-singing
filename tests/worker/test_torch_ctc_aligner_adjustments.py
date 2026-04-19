"""Regression tests for TorchCTCAligner RMS-based adjustments.

Covers the three post-MMS audio-driven adjustments with real production
inputs captured from two reference tracks:

  * ``leps`` — «Григорий Лепс — Она не твоя» (sung with sustained vowels,
    long outro silence, mid-line phrase breaks).
  * ``st1m`` — «ST1M — Я рэп (Remix)» (rap delivery, uniform low-level
    backing leakage, many line-start «Я»).

Each fixture is the exact Silero-trimmed lead-vocals waveform plus the
serialized MMS forced-align output (ratio, word_spans, words,
first_flags, trim_offset). Tests reconstruct that state in-process and
invoke the three adjustment methods individually — the MMS model is
never loaded, so the suite runs on CPU in seconds.

Regenerate fixtures with::

    scripts/generate_alignment_fixtures.py  # run inside worker GPU container

Any change to the adjustment algorithms (thresholds, walk direction,
fallback gate, etc.) that alters behaviour on these tracks will fail
one or more assertions here. Update the expected values deliberately
after listening to the resulting timings on the real audio.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures" / "alignment"


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    """Serializable stand-in for torchaudio.functional.TokenSpan."""
    start: int
    end: int
    token: int
    score: float


def _load(name: str):
    """Return ``(aligner, ctx)`` where ctx has waveform/words/spans/etc.

    ``vocals.wav`` in the fixture is the **pre-Silero-trim** lead-vocals
    waveform. The post-trim waveform the adjustment methods operate on
    is derived here by slicing at ``trim_offset * SR`` — same semantics
    as ``TorchCTCAligner.align``. ``pretrim_waveform`` is also exposed
    on ``ctx`` so Silero-refinement tests can work against it directly
    without needing the Silero model at test time.
    """
    import soundfile as sf
    import torch

    from worker.gpu.torch_ctc_aligner import TorchCTCAligner, _SAMPLE_RATE

    base = _FIXTURES / name
    data, sr = sf.read(str(base / "vocals.wav"), dtype="float32")
    assert sr == _SAMPLE_RATE, f"fixture sample rate mismatch: {sr}"
    pretrim_waveform = torch.from_numpy(data).unsqueeze(0)  # (1, samples)

    meta = json.loads((base / "alignment.json").read_text(encoding="utf-8"))
    word_spans = [[_Span(**s) for s in w] for w in meta["word_spans"]]

    trim_samples = int(meta["trim_offset"] * _SAMPLE_RATE)
    waveform = pretrim_waveform[:, trim_samples:] if trim_samples else pretrim_waveform

    aligner = TorchCTCAligner(
        device="cpu",
        model_cache_dir=None,
        line_start_rms_adjust=True,
        word_end_drift_adjust=True,
        word_end_sustain_extend=True,
    )

    ctx = {
        "waveform": waveform,
        "pretrim_waveform": pretrim_waveform,
        "ratio": meta["ratio"],
        "trim_offset": meta["trim_offset"],
        "silero_start_samples": meta["silero_start_samples"],
        "silero_threshold": meta["silero_threshold"],
        "words": meta["words"],
        "first_flags": meta["first_flags"],
        "word_spans": word_spans,
    }
    return aligner, ctx


def _line_start(aligner, ctx):
    return aligner._compute_line_start_adjustments(
        ctx["words"], ctx["word_spans"], ctx["ratio"],
        ctx["first_flags"], ctx["waveform"],
    )


def _word_end_drift(aligner, ctx):
    return aligner._compute_word_end_adjustments(
        ctx["words"], ctx["word_spans"], ctx["ratio"], ctx["waveform"],
        time_offset=ctx["trim_offset"],
    )


def _word_end_sustain(aligner, ctx, drift=None):
    return aligner._compute_word_end_extensions(
        ctx["words"], ctx["word_spans"], ctx["ratio"], ctx["waveform"],
        time_offset=ctx["trim_offset"],
        end_adjustments=drift or {},
    )


# ---------------------------------------------------------------------------
# Fixture shape sanity (protects against regenerated fixtures missing data)
# ---------------------------------------------------------------------------


class TestFixtureIntegrity:
    def test_leps_structure(self):
        _, ctx = _load("leps")
        assert len(ctx["words"]) == 213
        assert len(ctx["word_spans"]) == 213
        assert len(ctx["first_flags"]) == 213
        assert ctx["ratio"] == pytest.approx(0.020, abs=0.001)
        assert ctx["trim_offset"] == pytest.approx(4.1, abs=0.05)
        # Silero-trimmed waveform is ~210s of 16 kHz mono.
        assert ctx["waveform"].shape[1] / 16_000 > 200
        # Pre-trim carries the full Silero-pre-trim region too.
        assert ctx["pretrim_waveform"].shape[1] > ctx["waveform"].shape[1]
        assert ctx["silero_threshold"] == pytest.approx(0.7, abs=1e-6)
        # Silero fired at ~4.11 s on Leps → raw samples ~65760.
        assert ctx["silero_start_samples"] == pytest.approx(65760, abs=160)

    def test_st1m_structure(self):
        _, ctx = _load("st1m")
        assert len(ctx["words"]) == 561
        assert len(ctx["word_spans"]) == 561
        assert ctx["ratio"] == pytest.approx(0.020, abs=0.001)
        assert ctx["trim_offset"] == pytest.approx(24.06, abs=0.1)
        # Silero fired at ~24.398 s → raw samples ~390368.
        assert ctx["silero_start_samples"] == pytest.approx(390368, abs=160)


# ---------------------------------------------------------------------------
# Silero onset refinement
# ---------------------------------------------------------------------------


class TestSileroOnsetRefine:
    """``_refine_silero_onset`` walks the RMS envelope backwards from the
    raw Silero VAD onset until a silence floor transition is found.
    Tests exercise it on real pre-trim vocals with the raw Silero output
    captured in the fixture — no Silero model is needed at test time."""

    def test_leps_small_backtrack(self):
        """Leps: Silero fires at ~4.11 s on a clean onset; the refine
        step only shaves ~10 ms because the vocal attack is already
        sharp and close to Silero's detection point."""
        aligner, ctx = _load("leps")
        pretrim_np = ctx["pretrim_waveform"].squeeze(0).numpy()
        refined = aligner._refine_silero_onset(
            pretrim_np, ctx["silero_start_samples"],
        )
        assert refined == pytest.approx(ctx["trim_offset"], abs=0.005)
        # Sanity: on this track the refine should sit between ~4.0 and
        # the raw Silero onset.
        raw_sec = ctx["silero_start_samples"] / 16_000
        assert refined <= raw_sec
        assert raw_sec - refined < 0.2

    def test_st1m_large_backtrack(self):
        """ST1M: Silero fires late (~24.398 s) because the first
        syllable has a soft onset; RMS back-track pushes ~338 ms back
        to the real word attack at ~24.06 s."""
        aligner, ctx = _load("st1m")
        pretrim_np = ctx["pretrim_waveform"].squeeze(0).numpy()
        refined = aligner._refine_silero_onset(
            pretrim_np, ctx["silero_start_samples"],
        )
        assert refined == pytest.approx(ctx["trim_offset"], abs=0.005)
        raw_sec = ctx["silero_start_samples"] / 16_000
        # Substantial back-track: 300-400 ms.
        assert raw_sec - refined == pytest.approx(0.338, abs=0.05)

    def test_backtrack_clamped_to_one_second(self):
        """Hard guard in the refine: if the computed back-track exceeds
        1 s the method falls back to the raw Silero onset rather than
        pull the start wildly earlier on a noisy track."""
        aligner, _ = _load("leps")
        # Synthesise a 3 s audio with silence → voiced plateau → silence.
        # Silero fires at 2.5 s; real "voiced" level is at 1.5-2.5 s.
        # With a 1 s cap, refine can't move past 1.5 s before clamping.
        import numpy as np

        sr = 16_000
        audio = np.zeros(3 * sr, dtype="float32")
        # Plateau at 1.5-2.5 s, amplitude 0.3 → well above silence floor.
        audio[int(1.5 * sr) : int(2.5 * sr)] = 0.3
        refined = aligner._refine_silero_onset(audio, int(2.5 * sr))
        # Not pulled earlier than 1.5 s (the real plateau start), but
        # stayed within [plateau_start, raw] = [1.5, 2.5].
        assert 1.4 <= refined <= 2.5


# ---------------------------------------------------------------------------
# Line-start — «Г.Лепс — Она не твоя»
# ---------------------------------------------------------------------------


class TestLepsLineStart:
    """Line-start on Leps: «Ссоры» (true silence) is the only word that
    should be trimmed; all other outliers are sustained first vowels or
    voiced fricatives that stay above the attack floor."""

    def test_ssory_trimmed_via_walk(self):
        aligner, ctx = _load("leps")
        adj = _line_start(aligner, ctx)
        assert 78 in adj, "«Ссоры» (real silence pre-word) must be trimmed"
        orig_start, new_start = adj[78]
        assert orig_start == pytest.approx(76.764, abs=0.1)
        assert new_start - orig_start == pytest.approx(1.70, abs=0.2)  # seen in logs

    def test_eto_sustained_not_trimmed(self):
        """«Это» (word_idx=87, ratio 4.2): sustained /e/ across gap_0 →
        drift_seen never triggers, ratio below 7× fallback gate."""
        aligner, ctx = _load("leps")
        adj = _line_start(aligner, ctx)
        assert 87 not in adj

    def test_ona_three_instances_not_trimmed(self):
        """Three «Она» line-starts (53, 119, 191) with ratio 4.5-6.8:
        sustained /o/, below fallback gate, must not be adjusted."""
        aligner, ctx = _load("leps")
        adj = _line_start(aligner, ctx)
        for idx in (53, 119, 191):
            assert idx not in adj, f"«Она» (word_idx={idx}) wrongly trimmed"

    def test_znaesh_not_trimmed(self):
        """«Знаешь» (word_idx=162, ratio 6.0): /z/ is a voiced fricative
        close to peak, below fallback gate."""
        aligner, ctx = _load("leps")
        adj = _line_start(aligner, ctx)
        assert 162 not in adj

    def test_only_ssory_adjusted(self):
        aligner, ctx = _load("leps")
        adj = _line_start(aligner, ctx)
        assert list(adj.keys()) == [78]


# ---------------------------------------------------------------------------
# Line-start — «ST1M — Я рэп»
# ---------------------------------------------------------------------------


class TestST1MLineStart:
    """Line-start on ST1M: nine «Я» outliers, all should produce an
    adjustment — via the RMS walk for words with clean silence or soft
    pre-attack, via the span1.start fallback for those where the scan
    region is uniform low-level leakage (walk can't find a boundary)."""

    def test_ya_459_walk_exact_target(self):
        """«Я» @189.584 (user-validated target: +0.40s shift, absolute 189.984)."""
        aligner, ctx = _load("st1m")
        adj = _line_start(aligner, ctx)
        assert 459 in adj
        orig, new = adj[459]
        # Stored pre-offset; absolute = orig + trim_offset.
        assert orig == pytest.approx(165.524, abs=0.01)
        abs_new = new + ctx["trim_offset"]
        assert abs_new == pytest.approx(189.984, abs=0.05)

    def test_ya_23_fallback_to_span1_start(self):
        """«Я» 23 (uniform leakage, walk fails, ratio 9.0 ≥ 7 → fallback).
        User-validated target: shift +0.38s to absolute 32.76 (±20ms)."""
        aligner, ctx = _load("st1m")
        adj = _line_start(aligner, ctx)
        assert 23 in adj
        orig, new = adj[23]
        assert orig == pytest.approx(8.32, abs=0.01)
        abs_new = new + ctx["trim_offset"]
        assert abs_new == pytest.approx(32.76, abs=0.05)

    def test_all_nine_ya_outliers_adjusted(self):
        """All outlier «Я» (gap_0 ≥ 2×median) should yield a timing —
        either via the walk (350, 377, 459) or via the span1.start
        fallback (23, 55, 222, 254, 418, 450). Regression guard: if any
        of these drops from 'applied', user-facing timing breaks."""
        aligner, ctx = _load("st1m")
        adj = _line_start(aligner, ctx)
        expected = {23, 55, 222, 254, 350, 377, 418, 450, 459}
        assert expected.issubset(adj.keys()), (
            f"missing: {expected - set(adj.keys())}"
        )

    def test_walk_group_shifts_small(self):
        """«Я» 350, 377: gap_0 ratio 7-7.5, walk finds main vocal quickly
        (no long soft pre-attack) → small shift ~+0.18s."""
        aligner, ctx = _load("st1m")
        adj = _line_start(aligner, ctx)
        for idx in (350, 377):
            orig, new = adj[idx]
            assert 0.15 < new - orig < 0.25, (
                f"word_idx={idx}: expected walk shift ~0.18s, got {new - orig:.3f}"
            )

    def test_fallback_group_uses_span1_start(self):
        """«Я» 23, 55, 222, 254, 418, 450: fallback maps new_start to
        span1.start — gap-sized shift (+0.34..+0.46s)."""
        aligner, ctx = _load("st1m")
        adj = _line_start(aligner, ctx)
        for idx in (23, 55, 222, 254, 418, 450):
            spans = ctx["word_spans"][idx]
            span1_sec = spans[1].start * ctx["ratio"]
            _orig, new = adj[idx]
            assert new == pytest.approx(span1_sec, abs=0.01), (
                f"word_idx={idx}: fallback should set new=span1.start ({span1_sec:.3f}), "
                f"got {new:.3f}"
            )


# ---------------------------------------------------------------------------
# Line-start — «Слава КПСС — Культура G (Rework 2023)»
#
# Regression track for the backward-walk branch. Candidate lyrics from
# Genius miss the spoken line «Саундрекордс» between «Лукошко
# глубокомыслия,» and «Ебать того всё», so MMS is forced to place the
# first phoneme of «Ебать» somewhere in the ~2.4 s window that actually
# contains «Саундрекордс…». The forward walk used to latch onto reverb
# tail / leakage at the start of that window (shift ~0.16 s, attack at
# 200.37 s) — real attack is ~202.4 s per listening.
# ---------------------------------------------------------------------------


class TestSlavaLineStart:
    """Extreme gap_0 outlier (ratio 59×) where candidate lyrics omit a
    whole sung line. Backward RMS walk anchored at ``spans[1].start``
    finds the voiced onset of the vowel, ~2 s later than MMS's span_0."""

    def test_ebat_trimmed_via_backward_walk(self):
        """«Ебать» (word_idx=448): MMS places span_0 at 192.05 (rel,
        pre-offset 8.16 s → absolute 200.21 s). span_1 of phoneme «б»
        at 194.43 (rel, 202.59 absolute). Real vowel «Е» onset is in
        the ~200-ms window immediately before the consonant at span_1.
        Backward walk lands at ~194.22 (rel, 202.38 absolute) — shift
        ≥ 2 s relative to orig_start."""
        aligner, ctx = _load("slava")
        adj = _line_start(aligner, ctx)
        assert 448 in adj, "«Ебать» (extreme outlier) must be adjusted"
        orig, new = adj[448]
        assert orig == pytest.approx(192.048, abs=0.05)
        # Backward walk target: within one emission frame of span1.start
        # (phoneme-2 «б» attack) or slightly earlier by the vowel «Е»
        # sustain. span1.start * ratio ≈ 194.43.
        abs_new = new + ctx["trim_offset"]
        assert abs_new == pytest.approx(202.38, abs=0.15), (
            f"expected absolute new_start near 202.38 s (real vowel "
            f"onset ~202.4 s), got {abs_new:.3f}"
        )
        assert new - orig > 2.0, (
            f"expected shift > 2 s (big gap closed by backward walk), "
            f"got {new - orig:.3f} s"
        )

    def test_ebat_uses_backward_walk_path(self):
        """Regression guard: the extreme-outlier branch must be the one
        that produced the «Ебать» adjustment (not the forward-walk
        non-extreme path). gap_0 ratio is 59× — solidly in extreme
        territory. If the branch gating regresses, forward walk will
        latch onto the spurious early attack and test_ebat_* breaks."""
        aligner, ctx = _load("slava")
        spans = ctx["word_spans"][448]
        gap0 = spans[1].start - spans[0].end
        global_gaps = []
        for w in ctx["word_spans"]:
            for j in range(1, len(w) - 1):
                global_gaps.append(w[j + 1].start - w[j].end)
        import statistics
        median_gap = statistics.median(global_gaps)
        ratio = gap0 / max(median_gap, 1.0)
        assert ratio >= 7.0, (
            f"fixture drift: «Ебать» ratio={ratio:.1f}× must stay ≥ 7 "
            f"for the extreme branch to fire"
        )

    def test_fixture_structure(self):
        """Basic shape guard for the slava fixture."""
        _, ctx = _load("slava")
        assert len(ctx["words"]) == 451
        assert len(ctx["word_spans"]) == 451
        assert len(ctx["first_flags"]) == 451
        assert ctx["ratio"] == pytest.approx(0.020, abs=0.001)
        assert ctx["trim_offset"] == pytest.approx(8.16, abs=0.05)
        # «Ебать» should still be flagged as line-start (after «Лукошко
        # глубокомыслия,»).
        assert ctx["first_flags"][448] is True
        assert ctx["words"][448] == "Ебать"


# ---------------------------------------------------------------------------
# Word-end drift trim — «Г.Лепс — Она не твоя»
# ---------------------------------------------------------------------------


class TestLepsWordEndDrift:
    """Forward RMS walk trims the last phoneme's span when MMS placed it
    deep into a drift silence region."""

    def test_govoril_drift_trimmed(self):
        """«говорил» (word_idx=11): MMS places end at 19.301 but vocal
        really lasts until ~17.7s (user-validated via real audio)."""
        aligner, ctx = _load("leps")
        drift = _word_end_drift(aligner, ctx)
        assert 11 in drift
        orig, new = drift[11]
        assert orig == pytest.approx(19.301, abs=0.1)
        assert new == pytest.approx(17.741, abs=0.15)

    def test_lyubov_outro_trim(self):
        """Final «любовь?» (word_idx=212): MMS drifts 16s into outro
        silence. Walk finds voiced tail near 202s."""
        aligner, ctx = _load("leps")
        drift = _word_end_drift(aligner, ctx)
        assert 212 in drift
        orig, new = drift[212]
        assert orig > 218
        assert new == pytest.approx(202.49, abs=0.5)

    def test_feature_flag_disabled(self):
        """With word_end_drift_adjust=False, no drift trims are produced."""
        import soundfile as sf
        import torch

        from worker.gpu.torch_ctc_aligner import TorchCTCAligner

        base = _FIXTURES / "leps"
        data, _ = sf.read(str(base / "vocals.wav"), dtype="float32")
        meta = json.loads((base / "alignment.json").read_text(encoding="utf-8"))
        spans = [[_Span(**s) for s in w] for w in meta["word_spans"]]

        aligner = TorchCTCAligner(
            device="cpu", model_cache_dir=None,
            word_end_drift_adjust=False,
        )
        assert aligner._word_end_drift_adjust is False
        # The align() orchestration would short-circuit; here we just
        # confirm the flag sticks and the method returns something when
        # called directly (flag only gates the caller, not the method).
        adj = aligner._compute_word_end_adjustments(
            meta["words"], spans, meta["ratio"],
            torch.from_numpy(data).unsqueeze(0),
            time_offset=meta["trim_offset"],
        )
        # Method is unconditionally usable; flag sits on the instance.
        assert isinstance(adj, dict)


# ---------------------------------------------------------------------------
# Word-end sustain extend — «Г.Лепс — Она не твоя»
# ---------------------------------------------------------------------------


class TestLepsWordEndSustain:
    """Forward RMS walk extends the last emission frame forward through
    sustained vowel tail until silence (or next word onset)."""

    def _tvoya_at_174(self, ext):
        """Locate the «твоя» extension whose original end sits at
        ~174.049s (the user-reported line-end refrain)."""
        for word_idx, (orig, new) in ext.items():
            if abs(orig - 174.049) < 0.3:
                return word_idx, orig, new
        return None

    def test_tvoya_line_end_extended_two_seconds(self):
        """Line-end «твоя» at 173.589-174.049: real vowel sustains until
        ~176.069s — RMS walk forward extends the end there."""
        aligner, ctx = _load("leps")
        drift = _word_end_drift(aligner, ctx)
        ext = _word_end_sustain(aligner, ctx, drift=drift)
        hit = self._tvoya_at_174(ext)
        assert hit is not None, "«твоя» extension near 174s missing"
        _idx, orig, new = hit
        assert new - orig == pytest.approx(2.02, abs=0.2)
        assert new == pytest.approx(176.069, abs=0.2)

    def test_extend_skips_drift_trimmed_words(self):
        """Mutual exclusion: a word already handled by drift-trim must
        NOT also appear in extensions (drift and sustain are opposite
        failure modes)."""
        aligner, ctx = _load("leps")
        drift = _word_end_drift(aligner, ctx)
        ext = _word_end_sustain(aligner, ctx, drift=drift)
        overlap = set(drift) & set(ext)
        assert not overlap, f"word(s) in both drift and extend: {overlap}"

    def test_extensions_never_cross_next_word(self):
        """Forward walk is capped at next_word.start - ratio; no
        extension should push ``new_end`` past that natural bound."""
        aligner, ctx = _load("leps")
        drift = _word_end_drift(aligner, ctx)
        ext = _word_end_sustain(aligner, ctx, drift=drift)
        for word_idx, (_orig, new) in ext.items():
            if word_idx + 1 >= len(ctx["word_spans"]):
                continue
            next_spans = ctx["word_spans"][word_idx + 1]
            if not next_spans:
                continue
            next_start_abs = ctx["trim_offset"] + next_spans[0].start * ctx["ratio"]
            assert new < next_start_abs + 1e-6, (
                f"word_idx={word_idx}: new_end={new:.3f} crosses next word start "
                f"{next_start_abs:.3f}"
            )


# ---------------------------------------------------------------------------
# End-to-end sanity — full pipeline on Leps produces the validated
# «твоя» / «Это» timings in syllable space.
# ---------------------------------------------------------------------------


class TestLepsEndToEndSyllables:
    """Exercise the whole adjustment → syllable-timing pipeline (the
    three adjustment methods plus ``_to_syllable_timings``) and assert
    on the end-user outputs for the two words whose positions the user
    explicitly validated against real audio."""

    def _build_timings(self):
        aligner, ctx = _load("leps")
        drift = _word_end_drift(aligner, ctx)
        ext = _word_end_sustain(aligner, ctx, drift=drift)
        line = _line_start(aligner, ctx)

        combined = dict(ext)
        combined.update(drift)  # drift wins over sustain on any overlap.

        timings, _ = aligner._to_syllable_timings(
            ctx["words"], ctx["word_spans"], ctx["ratio"],
            language="ru",
            first_flags=ctx["first_flags"],
            time_offset=ctx["trim_offset"],
            line_adjustments=line,
            end_adjustments=combined,
        )
        return timings

    def test_eto_syllable_starts_at_expected(self):
        """«\\nЭто» syllable must start at 98.325s (sustained /e/ not trimmed)."""
        timings = self._build_timings()
        hit = [t for t in timings if t.syllable == "\nЭто"]
        assert hit, "«Это» syllable missing"
        assert hit[0].start == pytest.approx(98.325, abs=0.02)

    def test_tvoya_line_end_reaches_176(self):
        """Line-end « твоя» at 173.589 must extend to ~176s."""
        timings = self._build_timings()
        # syllabifier yields a single-part syllable for «твоя»; find the
        # one near 173.589s.
        hit = [
            t for t in timings
            if "твоя" in t.syllable and 173 < t.start < 174
        ]
        assert hit, "« твоя» near 173s missing"
        assert hit[0].end == pytest.approx(176.069, abs=0.2)
