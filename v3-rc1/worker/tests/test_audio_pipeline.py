"""Unit tests for AudioPipeline (v3-rc1) with all dependencies mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from karaoke_shared.models.job import Job
from karaoke_shared.models.track import SyllableTiming, Track

from app.pipeline.audio_pipeline import AudioPipeline
from app.pipeline.ctc_aligner import AlignmentStats
from app.pipeline.lyrics_searcher import (
    LyricsNotFoundError,
    LyricsResult,
)
from app.pipeline.whisper_transcriber import WhisperResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_track(**overrides) -> Track:
    defaults = dict(
        id="track-1",
        artist="Test Artist",
        title="Test Song",
        mp3_path="/data/media/test.mp3",
        source="user_upload",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return Track(**defaults)


def _make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        track_id="track-1",
        worker_id="worker-1",
        status="running",
        step="pending",
        progress=0,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return Job(**defaults)


@pytest.fixture
def mock_deps():
    """Create all mocked dependencies for AudioPipeline."""
    job_service = AsyncMock()
    repo = AsyncMock()
    uvr = MagicMock()
    whisper = MagicMock()
    vad = MagicMock()
    lyrics_searcher = AsyncMock()
    ctc_aligner = MagicMock()
    feature_extractor = MagicMock()
    lyric_embedder = MagicMock()
    qdrant_repo = MagicMock()

    repo.get_track = AsyncMock(return_value=_make_track())
    repo.update_track = AsyncMock()
    uvr.separate.return_value = ("/data/vocals.wav", "/data/instrumental.mp3")
    uvr.cleanup.return_value = None
    uvr.model_cache_dir = "/data/models"
    uvr.media_root = "/data/media"
    uvr._model_name = "test.ckpt"
    vad.process.return_value = "/data/cleaned_vocals.wav"
    whisper.transcribe.return_value = WhisperResult(
        text="some transcribed text", language="en", confidence=0.8
    )
    whisper.cleanup.return_value = None
    lyrics_searcher.search = AsyncMock(return_value=LyricsResult(
        artist="Found Artist",
        title="Found Song",
        lyrics="line one\nline two",
        language="en",
        confidence="high",
        source_note="test",
    ))
    ctc_aligner.align.return_value = (
        [
            SyllableTiming(syllable="line", start=0.0, end=0.5),
            SyllableTiming(syllable=" one", start=0.5, end=1.0),
        ],
        AlignmentStats(total_words=2, char_level_used=2, proportional_fallback=0),
    )
    feature_extractor.extract.return_value = [0.1] * 45
    lyric_embedder.embed.return_value = [0.1] * 384
    qdrant_repo.upsert = MagicMock()

    return {
        "job_service": job_service,
        "repo": repo,
        "uvr": uvr,
        "whisper": whisper,
        "vad": vad,
        "lyrics_searcher": lyrics_searcher,
        "ctc_aligner": ctc_aligner,
        "feature_extractor": feature_extractor,
        "lyric_embedder": lyric_embedder,
        "qdrant_repo": qdrant_repo,
    }


@pytest.fixture
def pipeline(mock_deps):
    return AudioPipeline(
        job_service=mock_deps["job_service"],
        uvr=mock_deps["uvr"],
        repo=mock_deps["repo"],
        whisper=mock_deps["whisper"],
        vad_processor=mock_deps["vad"],
        lyrics_searcher=mock_deps["lyrics_searcher"],
        ctc_aligner=mock_deps["ctc_aligner"],
        feature_extractor=mock_deps["feature_extractor"],
        lyric_embedder=mock_deps["lyric_embedder"],
        qdrant_repo=mock_deps["qdrant_repo"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineSuccess:
    """Test successful pipeline execution."""

    @pytest.mark.asyncio
    async def test_full_pipeline_success(self, pipeline, mock_deps):
        """Full pipeline produces status=ready and syncs QDrant."""
        job = _make_job()

        with patch("app.pipeline.audio_pipeline.Path"):
            with patch(
                "karaoke_shared.utils.line_breaker.detect_line_breaks",
                return_value=[SyllableTiming(syllable="test", start=0.0, end=1.0)],
            ):
                await pipeline.process(job)

        update_calls = mock_deps["repo"].update_track.call_args_list
        final_update = update_calls[-1]
        assert final_update[0][1].status == "ready"
        assert final_update[0][1].qdrant_synced == 1

        mock_deps["job_service"].mark_completed.assert_called_once()

    @pytest.mark.asyncio
    async def test_pipeline_calls_all_steps(self, pipeline, mock_deps):
        """Pipeline calls UVR, VAD, Whisper, lyrics search, CTC, embedding."""
        job = _make_job()

        with patch("app.pipeline.audio_pipeline.Path"):
            with patch(
                "karaoke_shared.utils.line_breaker.detect_line_breaks",
                return_value=[],
            ):
                await pipeline.process(job)

        mock_deps["uvr"].separate.assert_called_once()
        mock_deps["uvr"].cleanup.assert_called()
        mock_deps["vad"].process.assert_called_once()
        mock_deps["whisper"].transcribe.assert_called_once()
        mock_deps["whisper"].cleanup.assert_called()
        mock_deps["lyrics_searcher"].search.assert_called_once()
        mock_deps["ctc_aligner"].align.assert_called_once()
        mock_deps["lyric_embedder"].embed.assert_called_once()


class TestPipelineErrors:
    """Test error handling in pipeline."""

    @pytest.mark.asyncio
    async def test_track_not_found(self, pipeline, mock_deps):
        mock_deps["repo"].get_track = AsyncMock(return_value=None)
        job = _make_job()
        await pipeline.process(job)
        mock_deps["job_service"].mark_failed.assert_called_once()
        assert "not found" in mock_deps["job_service"].mark_failed.call_args[0][1]

    @pytest.mark.asyncio
    async def test_no_mp3_path(self, pipeline, mock_deps):
        mock_deps["repo"].get_track = AsyncMock(return_value=_make_track(mp3_path=None))
        job = _make_job()
        await pipeline.process(job)
        mock_deps["job_service"].mark_failed.assert_called_once()
        assert "mp3_path" in mock_deps["job_service"].mark_failed.call_args[0][1]

    @pytest.mark.asyncio
    async def test_lyrics_not_found_saves_audio_features(self, pipeline, mock_deps):
        mock_deps["lyrics_searcher"].search = AsyncMock(
            side_effect=LyricsNotFoundError("not found")
        )
        job = _make_job()

        with patch("app.pipeline.audio_pipeline.Path"):
            await pipeline.process(job)

        mock_deps["job_service"].mark_failed.assert_called_once()
        error_updates = [
            c for c in mock_deps["repo"].update_track.call_args_list
            if c[0][1].status == "error"
        ]
        assert len(error_updates) == 1

    @pytest.mark.asyncio
    async def test_no_lyrics_searcher(self, mock_deps):
        pipeline = AudioPipeline(
            job_service=mock_deps["job_service"],
            uvr=mock_deps["uvr"],
            repo=mock_deps["repo"],
            whisper=mock_deps["whisper"],
            vad_processor=mock_deps["vad"],
            lyrics_searcher=None,
            ctc_aligner=mock_deps["ctc_aligner"],
        )
        job = _make_job()
        await pipeline.process(job)
        mock_deps["job_service"].mark_failed.assert_called_once()
        assert "not configured" in mock_deps["job_service"].mark_failed.call_args[0][1]

    @pytest.mark.asyncio
    async def test_uvr_failure_marks_job_failed(self, pipeline, mock_deps):
        mock_deps["uvr"].separate.side_effect = RuntimeError("file not found")
        job = _make_job()
        await pipeline.process(job)
        mock_deps["job_service"].mark_failed.assert_called_once()


class TestPipelineHelpers:
    """Test helper methods."""

    def test_parse_hints_artist_title(self):
        artist, title = AudioPipeline._parse_hints_from_path(
            "/data/media/Земфира - Хочешь.mp3"
        )
        assert artist == "Земфира"
        assert title == "Хочешь"

    def test_parse_hints_no_separator(self):
        artist, title = AudioPipeline._parse_hints_from_path(
            "/data/media/some_track.mp3"
        )
        assert artist is None
        assert title is None

    def test_parse_hints_multiple_dashes(self):
        artist, title = AudioPipeline._parse_hints_from_path(
            "/data/media/A - B - C.mp3"
        )
        assert artist == "A"
        assert title == "B - C"
