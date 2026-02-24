"""Unit tests for VideoGenerator ASS generation logic.

Strategy
--------
- Imports VideoGenerator via importlib to avoid clashing with the backend
  ``app`` namespace already registered by conftest.py.
- Only tests the pure Python logic (ASS generation, grouping, time conversion).
  FFmpeg execution is NOT tested — no real audio files or external processes.
- Table-driven tests for the time conversion function.
- All tests are synchronous since the functions under test are sync.

Import trick
------------
The worker package also owns a top-level ``app`` package.  conftest.py
(and test_audio_pipeline.py before it) temporarily put the worker's ``app``
into sys.modules then clean it up.  By the time this module is collected,
``sys.modules['app']`` points to the *backend* FastAPI application, which is
correct.  We load the video_generator module under a private name to avoid
any collision.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

import pytest

from karaoke_shared.models.track import SyllableTiming

# ---------------------------------------------------------------------------
# Load worker module under a private namespace to avoid sys.modules collision
# ---------------------------------------------------------------------------

_WORKER_PIPELINE_DIR = (
    pathlib.Path(__file__).parent.parent / "worker" / "app" / "pipeline"
)

_spec = importlib.util.spec_from_file_location(
    "_vg_module",
    str(_WORKER_PIPELINE_DIR / "video_generator.py"),
    submodule_search_locations=[],
)
assert _spec is not None and _spec.loader is not None
_vg_mod = importlib.util.module_from_spec(_spec)
sys.modules["_vg_module"] = _vg_mod
_spec.loader.exec_module(_vg_mod)

VideoGenerator = _vg_mod.VideoGenerator
_seconds_to_ass_time = _vg_mod._seconds_to_ass_time
_MAX_LINE_CHARS = _vg_mod._MAX_LINE_CHARS
_LINE_BREAK_PAUSE_SEC = _vg_mod._LINE_BREAK_PAUSE_SEC
_LINE_END_PADDING_CS = _vg_mod._LINE_END_PADDING_CS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timing(syllable: str, start: float, end: float) -> SyllableTiming:
    return SyllableTiming(syllable=syllable, start=start, end=end)


def _gen(tmp_path: pathlib.Path) -> VideoGenerator:
    return VideoGenerator(media_root=str(tmp_path))


def _build_timings_with_gap(gap: float = 1.0) -> list[SyllableTiming]:
    """Two groups separated by a gap longer than _LINE_BREAK_PAUSE_SEC."""
    group1 = [
        _timing("hel", 0.0, 0.3),
        _timing("lo", 0.3, 0.6),
    ]
    group2 = [
        _timing("world", 0.6 + gap, 1.2 + gap),
    ]
    return group1 + group2


# ---------------------------------------------------------------------------
# _seconds_to_ass_time
# ---------------------------------------------------------------------------


class TestSecondsToAssTime:
    """Verify H:MM:SS.cc formatting across a range of inputs."""

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0.0, "0:00:00.00"),
            (1.0, "0:00:01.00"),
            (60.0, "0:01:00.00"),
            (3600.0, "1:00:00.00"),
            (3661.5, "1:01:01.50"),
            (0.5, "0:00:00.50"),
            (0.01, "0:00:00.01"),
            (0.99, "0:00:00.99"),
            (1.505, "0:00:01.50"),  # rounding: 150.5 cs → 151 → 1.51 ... but
            # int(round(1.505 * 100)) = int(round(150.5)) = 150 in Python3
            # re-check: round(150.5) == 150 (banker's rounding)
            # so 0:00:01.50
            (3.14, "0:00:03.14"),
            (100.0, "0:01:40.00"),
        ],
    )
    def test_formatting(self, seconds: float, expected: str) -> None:
        assert _seconds_to_ass_time(seconds) == expected

    def test_large_hours(self) -> None:
        result = _seconds_to_ass_time(7322.0)  # 2h 2m 2s
        assert result == "2:02:02.00"

    def test_centiseconds_zero_padded(self) -> None:
        result = _seconds_to_ass_time(0.05)
        assert result.endswith(".05")

    def test_seconds_zero_padded(self) -> None:
        result = _seconds_to_ass_time(65.0)  # 1m 5s
        assert "01:05" in result


# ---------------------------------------------------------------------------
# _group_into_lines
# ---------------------------------------------------------------------------


class TestGroupIntoLines:
    """Tests for the line-grouping logic inside VideoGenerator."""

    def test_empty_input_returns_empty(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        result = gen._group_into_lines([])
        assert result == []

    def test_single_syllable_produces_one_group(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        timings = [_timing("hello", 0.0, 0.5)]

        groups = gen._group_into_lines(timings)

        assert len(groups) == 1
        assert len(groups[0]) == 1
        assert groups[0][0].syllable == "hello"

    def test_all_syllables_in_one_short_line(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        # Total chars: 3+3+3 = 9 — well under _MAX_LINE_CHARS (45)
        timings = [
            _timing("cat", 0.0, 0.3),
            _timing("sat", 0.3, 0.6),
            _timing("mat", 0.6, 0.9),
        ]

        groups = gen._group_into_lines(timings)

        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_long_line_split_at_char_limit(self, tmp_path: pathlib.Path) -> None:
        """A run of syllables exceeding _MAX_LINE_CHARS starts a new group."""
        gen = _gen(tmp_path)
        # Build syllables summing to just over _MAX_LINE_CHARS = 45 chars
        # Each "abcde" is 5 chars; 10 of them = 50 chars total
        timings = [
            _timing("abcde", i * 0.5, (i + 1) * 0.5) for i in range(10)
        ]

        groups = gen._group_into_lines(timings)

        # Must be split into at least 2 groups
        assert len(groups) >= 2
        # Each individual group should not exceed the limit
        for group in groups:
            total = sum(len(t.syllable) for t in group)
            assert total <= _MAX_LINE_CHARS + 5  # one syllable grace

    def test_pause_triggers_new_group(self, tmp_path: pathlib.Path) -> None:
        """A gap > _LINE_BREAK_PAUSE_SEC creates a new group."""
        gen = _gen(tmp_path)
        # Gap of 1.0 second between the two syllables
        timings = [
            _timing("hel", 0.0, 0.3),
            _timing("lo", 0.3, 0.6),
            _timing("world", 2.0, 2.5),  # 1.4 s gap — exceeds 0.5 s threshold
        ]

        groups = gen._group_into_lines(timings)

        assert len(groups) == 2
        assert len(groups[0]) == 2
        assert len(groups[1]) == 1
        assert groups[1][0].syllable == "world"

    def test_small_gap_does_not_split(self, tmp_path: pathlib.Path) -> None:
        """A gap below the threshold keeps syllables in the same group."""
        gen = _gen(tmp_path)
        timings = [
            _timing("hel", 0.0, 0.3),
            _timing("lo", 0.3, 0.6),
            _timing("world", 0.9, 1.4),  # 0.3 s gap — under 0.5 s threshold
        ]

        groups = gen._group_into_lines(timings)

        assert len(groups) == 1

    def test_exact_pause_threshold_does_not_split(self, tmp_path: pathlib.Path) -> None:
        """A gap exactly equal to the threshold does NOT trigger a new group
        (the check is strictly greater-than)."""
        gen = _gen(tmp_path)
        timings = [
            _timing("a", 0.0, 0.1),
            _timing("b", 0.6, 0.7),  # gap = 0.5 s exactly
        ]

        groups = gen._group_into_lines(timings)

        # gap == threshold → no new group
        assert len(groups) == 1

    def test_groups_preserve_all_syllables(self, tmp_path: pathlib.Path) -> None:
        """No syllable should be lost during grouping."""
        gen = _gen(tmp_path)
        timings = [_timing(f"s{i}", float(i), float(i) + 0.4) for i in range(20)]

        groups = gen._group_into_lines(timings)

        flat = [t for g in groups for t in g]
        assert len(flat) == 20


# ---------------------------------------------------------------------------
# _build_dialogue_lines
# ---------------------------------------------------------------------------


class TestBuildDialogueLines:
    def test_empty_timings_returns_empty(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        result = gen._build_dialogue_lines([])
        assert result == []

    def test_produces_dialogue_line_strings(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        timings = [
            _timing("hel", 0.0, 0.3),
            _timing("lo", 0.3, 0.6),
        ]

        lines = gen._build_dialogue_lines(timings)

        assert len(lines) >= 1
        for line in lines:
            assert line.startswith("Dialogue:")

    def test_k_tags_present(self, tmp_path: pathlib.Path) -> None:
        """Each syllable must have a \\k tag with a positive centisecond count."""
        gen = _gen(tmp_path)
        timings = [
            _timing("hel", 0.0, 0.3),
            _timing("lo", 0.3, 0.6),
        ]

        lines = gen._build_dialogue_lines(timings)
        combined = "".join(lines)

        assert r"{\k" in combined or "{\\k" in combined or "\\k" in combined

    def test_k_tag_duration_correct_centiseconds(self, tmp_path: pathlib.Path) -> None:
        r"""Verify \k duration is computed as round((end - start) * 100)."""
        gen = _gen(tmp_path)
        # 0.3 s = 30 cs, 0.2 s = 20 cs
        timings = [
            _timing("hel", 0.0, 0.30),
            _timing("lo", 0.30, 0.50),
        ]

        lines = gen._build_dialogue_lines(timings)
        combined = "".join(lines)

        assert "\\k30" in combined
        assert "\\k20" in combined

    def test_k_tag_minimum_duration_is_one(self, tmp_path: pathlib.Path) -> None:
        r"""A zero-duration syllable must still get \k1 (minimum 1 cs)."""
        gen = _gen(tmp_path)
        timings = [
            _timing("a", 0.0, 0.0),  # zero duration
        ]

        lines = gen._build_dialogue_lines(timings)
        combined = "".join(lines)

        assert "\\k1" in combined

    def test_dialogue_line_uses_karaoke_style(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        timings = [_timing("hello", 0.0, 0.5)]

        lines = gen._build_dialogue_lines(timings)

        assert any("Karaoke" in line for line in lines)

    def test_line_start_time_equals_first_syllable_start(
        self, tmp_path: pathlib.Path
    ) -> None:
        gen = _gen(tmp_path)
        timings = [
            _timing("hel", 1.5, 1.8),
            _timing("lo", 1.8, 2.0),
        ]

        lines = gen._build_dialogue_lines(timings)

        assert len(lines) == 1
        # Start time should be 0:00:01.50
        assert "0:00:01.50" in lines[0]

    def test_line_end_includes_padding(self, tmp_path: pathlib.Path) -> None:
        """The line end time should be last syllable end + _LINE_END_PADDING_CS/100."""
        gen = _gen(tmp_path)
        timings = [_timing("hello", 0.0, 1.0)]

        lines = gen._build_dialogue_lines(timings)

        # End should be 1.0 + 0.5 = 1.5 s → 0:00:01.50
        assert "0:00:01.50" in lines[0]

    def test_multiple_groups_produce_multiple_lines(self, tmp_path: pathlib.Path) -> None:
        """A large pause creates two groups → two Dialogue lines."""
        gen = _gen(tmp_path)
        timings = [
            _timing("hel", 0.0, 0.3),
            _timing("lo", 0.3, 0.6),
            _timing("world", 2.0, 2.5),  # 1.4 s gap
        ]

        lines = gen._build_dialogue_lines(timings)

        assert len(lines) == 2


# ---------------------------------------------------------------------------
# _generate_ass (full file output)
# ---------------------------------------------------------------------------


class TestGenerateAss:
    def test_output_file_created(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        timings = [_timing("hello", 0.0, 0.5)]

        gen._generate_ass(timings, "Artist", "Title", ass_path)

        assert pathlib.Path(ass_path).exists()

    def test_script_info_header_present(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "[Script Info]" in content
        assert "ScriptType: v4.00+" in content

    def test_play_res_x_present(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "PlayResX: 1920" in content
        assert "PlayResY: 1080" in content

    def test_styles_section_present(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "[V4+ Styles]" in content
        assert "Style: Karaoke" in content
        assert "Style: Title" in content

    def test_events_section_present(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "[Events]" in content

    def test_title_card_contains_artist_and_title(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Queen", "Bohemian Rhapsody", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "Queen" in content
        assert "Bohemian Rhapsody" in content

    def test_title_card_uses_title_style(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        # The title card dialogue line must use the "Title" style
        lines = content.splitlines()
        title_dialogue = [l for l in lines if "Title" in l and l.startswith("Dialogue")]
        assert len(title_dialogue) >= 1

    def test_title_card_starts_at_zero(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "0:00:00.00" in content

    def test_empty_timings_no_karaoke_dialogue_lines(
        self, tmp_path: pathlib.Path
    ) -> None:
        """With no syllable timings there should be no Karaoke Dialogue lines."""
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")
        karaoke_dialogues = [
            l for l in content.splitlines()
            if l.startswith("Dialogue") and "Karaoke" in l
        ]

        assert karaoke_dialogues == []

    def test_syllable_timings_produce_karaoke_dialogue(
        self, tmp_path: pathlib.Path
    ) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        timings = [
            _timing("hel", 1.0, 1.3),
            _timing("lo", 1.3, 1.6),
        ]
        gen._generate_ass(timings, "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")
        karaoke_dialogues = [
            l for l in content.splitlines()
            if l.startswith("Dialogue") and "Karaoke" in l
        ]

        assert len(karaoke_dialogues) >= 1

    def test_syllable_text_in_ass_output(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        timings = [
            _timing("world", 0.0, 0.5),
        ]
        gen._generate_ass(timings, "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "world" in content

    def test_k_tags_in_ass_output(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        timings = [_timing("hello", 0.0, 0.5)]
        gen._generate_ass(timings, "Artist", "Title", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "\\k" in content

    def test_ass_file_is_utf8(self, tmp_path: pathlib.Path) -> None:
        """File must be written as UTF-8 (critical for Russian/Unicode lyrics)."""
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        timings = [_timing("привет", 0.0, 0.6)]
        gen._generate_ass(timings, "Исполнитель", "Название", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "привет" in content
        assert "Исполнитель" in content

    def test_special_chars_in_artist_title(self, tmp_path: pathlib.Path) -> None:
        gen = _gen(tmp_path)
        ass_path = str(tmp_path / "test.ass")
        gen._generate_ass([], "AC/DC", "Rock & Roll", ass_path)

        content = pathlib.Path(ass_path).read_text(encoding="utf-8")

        assert "AC/DC" in content
        assert "Rock & Roll" in content


# ---------------------------------------------------------------------------
# VideoGenerator construction
# ---------------------------------------------------------------------------


class TestVideoGeneratorConstruction:
    def test_instantiation_with_media_root(self, tmp_path: pathlib.Path) -> None:
        gen = VideoGenerator(media_root=str(tmp_path))
        assert gen.media_root == str(tmp_path)
