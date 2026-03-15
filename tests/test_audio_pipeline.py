# LEGACY: This file was written against the old v2 worker structure which had
# worker/app/pipeline/audio_pipeline.py and worker/app/pipeline/uvr_separator.py.
# After restructuring, those paths no longer exist. The v3 equivalent tests
# are in tests/worker/test_audio_pipeline.py (using worker.gpu.gpu_pipeline).
# This file is kept for historical reference only and will fail at import time
# because the importlib.util paths it loads no longer exist.
# TODO: Delete this file once confirmed the tests/worker/ suite covers the same ground.

"""Unit tests for AudioPipeline, UVRSeparator error handling, and JobPoller.

Strategy
--------
- AudioPipeline tests inject a ``MagicMock`` in place of ``UVRSeparator`` so
  the heavy ``audio_separator`` library (absent from the test environment) is
  never imported.
- The worker package (``worker/``) is added to sys.path inside a session-scoped
  autouse fixture so all imports resolve correctly without installing the package.
- All tests are async; asyncio_mode = "auto" (pytest.ini) removes the need
  for explicit @pytest.mark.asyncio decorators.
"""

from __future__ import annotations

import pathlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import worker modules without clobbering the ``app`` namespace.
#
# The conftest.py fixture ``app_db`` does ``from app.main import app`` which
# resolves to the *backend* FastAPI application (backend/app/main.py).
# Both the backend and the worker own a package called ``app``, so we must
# not let either bleed into the other's ``sys.modules`` slot.
#
# Strategy: use importlib.util to load each worker module under a private
# ``_worker_app.*`` namespace. The worker modules' internal relative imports
# (e.g. ``from app.pipeline.uvr_separator import UVRSeparator``) still use
# the ``app`` name, so we also register aliases that point to the
# ``_worker_app.*`` modules.  After all imports are done we remove the
# aliased ``app.*`` keys so conftest.py can register the backend's ``app``.
# ---------------------------------------------------------------------------

import importlib.util

_WORKER_ROOT = pathlib.Path(__file__).parent.parent / "worker"

# Stub out audio_separator *before* importing worker code so the lazy import
# inside UVRSeparator.separate() never tries to load the real heavy library.
_audio_separator_stub = types.ModuleType("audio_separator")
_audio_separator_separator_stub = types.ModuleType("audio_separator.separator")
_audio_separator_separator_stub.Separator = MagicMock()  # type: ignore[attr-defined]
sys.modules.setdefault("audio_separator", _audio_separator_stub)
sys.modules.setdefault("audio_separator.separator", _audio_separator_separator_stub)


def _load_worker_module(rel_path: str, private_name: str) -> types.ModuleType:
    """Load a worker source file under a private module name.

    Registers both ``private_name`` and the original ``app.*`` alias in
    sys.modules so that intra-package imports work during loading.
    """
    spec = importlib.util.spec_from_file_location(
        private_name,
        str(_WORKER_ROOT / rel_path),
        submodule_search_locations=[str(_WORKER_ROOT / rel_path.rsplit("/", 1)[0])]
        if "/" in rel_path
        else [],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[private_name] = mod
    # Also expose under the original ``app.*`` name so intra-package imports
    # (e.g. ``from app.pipeline.uvr_separator import UVRSeparator``) resolve.
    original_name = private_name.replace("_worker_", "", 1)
    sys.modules[original_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load in dependency order.
_w_app_pkg = types.ModuleType("_worker_app")
_w_app_pkg.__path__ = [str(_WORKER_ROOT / "app")]  # type: ignore[attr-defined]
sys.modules["_worker_app"] = _w_app_pkg
sys.modules["app"] = _w_app_pkg  # temporary alias so sub-imports resolve

# app.config is imported by app.main; load it first.
_load_worker_module("app/config.py", "_worker_app.config")

# app.pipeline package
_w_pipeline_pkg = types.ModuleType("_worker_app.pipeline")
_w_pipeline_pkg.__path__ = [str(_WORKER_ROOT / "app" / "pipeline")]  # type: ignore[attr-defined]
sys.modules["_worker_app.pipeline"] = _w_pipeline_pkg
sys.modules["app.pipeline"] = _w_pipeline_pkg

_uvr_mod = _load_worker_module("app/pipeline/uvr_separator.py", "_worker_app.pipeline.uvr_separator")
_pipeline_mod = _load_worker_module("app/pipeline/audio_pipeline.py", "_worker_app.pipeline.audio_pipeline")
_main_mod = _load_worker_module("app/main.py", "_worker_app.main")

UVRSeparator = _uvr_mod.UVRSeparator
AudioPipeline = _pipeline_mod.AudioPipeline
JobPoller = _main_mod.JobPoller

# Remove the temporary ``app.*`` aliases so conftest.py can register the
# backend application under ``app`` / ``app.main`` without collision.
for _key in list(sys.modules.keys()):
    if _key == "app" or _key.startswith("app."):
        del sys.modules[_key]

from karaoke_shared.models.track import TrackCreate  # noqa: E402
from karaoke_shared.repositories import SQLiteRepository  # noqa: E402
from karaoke_shared.services.job_service import JobService  # noqa: E402


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _track_create(mp3_path: str | None = "/media/song.mp3") -> TrackCreate:
    """Return a minimal TrackCreate; optionally without an mp3_path."""
    return TrackCreate(
        artist="Fixture Artist",
        title="Fixture Title",
        source="catalog",
        mp3_path=mp3_path,
    )


def _mock_uvr(
    vocals_path: str = "/media/vocals.wav",
    instrumental_path: str = "/media/inst.wav",
) -> MagicMock:
    """Return a MagicMock UVRSeparator whose separate() returns the given paths."""
    uvr = MagicMock(spec=UVRSeparator)
    uvr.separate.return_value = (vocals_path, instrumental_path)
    return uvr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def job_service(sqlite_repo: SQLiteRepository) -> JobService:
    return JobService(sqlite_repo)


@pytest.fixture
def mock_uvr() -> MagicMock:
    return _mock_uvr()


@pytest.fixture
def pipeline(
    job_service: JobService, mock_uvr: MagicMock, sqlite_repo: SQLiteRepository
) -> AudioPipeline:
    return AudioPipeline(job_service=job_service, uvr=mock_uvr, repo=sqlite_repo)


# ---------------------------------------------------------------------------
# AudioPipeline.process — happy path
# ---------------------------------------------------------------------------


class TestAudioPipelineSuccess:
    async def test_process_marks_job_completed(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """process() marks the job as 'completed' when UVR succeeds."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status == "completed"

    async def test_process_stores_result_with_paths(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
        mock_uvr: MagicMock,
    ) -> None:
        """process() stores the vocals and instrumental paths in the job result."""
        mock_uvr.separate.return_value = ("/out/vocals.wav", "/out/inst.wav")
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        assert updated_job.result is not None
        assert updated_job.result["vocals_path"] == "/out/vocals.wav"
        assert updated_job.result["instrumental_path"] == "/out/inst.wav"

    async def test_process_updates_track_instrumental_path(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
        mock_uvr: MagicMock,
    ) -> None:
        """process() persists the instrumental_path on the track record."""
        mock_uvr.separate.return_value = ("/out/vocals.wav", "/out/inst.wav")
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_track = await sqlite_repo.get_track(track.id)
        assert updated_track is not None
        assert updated_track.instrumental_path == "/out/inst.wav"

    async def test_process_calls_uvr_separate_with_mp3_path(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
        mock_uvr: MagicMock,
    ) -> None:
        """process() calls uvr.separate() with the track's mp3_path."""
        track = await sqlite_repo.create_track(_track_create(mp3_path="/songs/test.mp3"))
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        mock_uvr.separate.assert_called_once_with("/songs/test.mp3")

    async def test_process_records_separating_step(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """process() records 'separating' at progress=100 after UVR completes."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        # After completion the job status is 'completed'; the last mark_step
        # call set progress to 100.
        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        assert updated_job.progress == 100


# ---------------------------------------------------------------------------
# AudioPipeline.process — failure paths
# ---------------------------------------------------------------------------


class TestAudioPipelineFailures:
    async def test_process_fails_when_track_not_found(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """process() marks the job failed when the track does not exist."""
        # Create a job referencing a non-existent track ID.
        import uuid
        fake_track_id = str(uuid.uuid4())
        # Insert the job directly via the repo so we can use a phantom track_id.
        from karaoke_shared.models.job import JobCreate
        job = await sqlite_repo.create_job(JobCreate(track_id=fake_track_id))

        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        # With default max_attempts=3 the job is reset to 'pending' for retry.
        assert updated_job.status in ("failed", "pending")
        assert updated_job.error_message is not None
        assert fake_track_id in updated_job.error_message

    async def test_process_fails_when_track_has_no_mp3_path(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """process() marks the job failed when the track has no mp3_path."""
        track = await sqlite_repo.create_track(_track_create(mp3_path=None))
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status in ("failed", "pending")
        assert updated_job.error_message is not None
        assert "mp3_path" in updated_job.error_message

    async def test_process_fails_when_uvr_raises_exception(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
        mock_uvr: MagicMock,
    ) -> None:
        """process() marks the job failed when UVR raises an unexpected exception."""
        mock_uvr.separate.side_effect = RuntimeError("ONNX inference error")
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status in ("failed", "pending")
        assert updated_job.error_message is not None
        assert "ONNX inference error" in updated_job.error_message

    async def test_process_does_not_call_uvr_when_track_missing(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
        mock_uvr: MagicMock,
    ) -> None:
        """UVR is never invoked if the track lookup fails early."""
        import uuid
        from karaoke_shared.models.job import JobCreate
        job = await sqlite_repo.create_job(JobCreate(track_id=str(uuid.uuid4())))

        await pipeline.process(job)

        mock_uvr.separate.assert_not_called()

    async def test_process_does_not_call_uvr_when_no_mp3_path(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
        mock_uvr: MagicMock,
    ) -> None:
        """UVR is never invoked if the track has no mp3_path."""
        track = await sqlite_repo.create_track(_track_create(mp3_path=None))
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        mock_uvr.separate.assert_not_called()


# ---------------------------------------------------------------------------
# UVRSeparator — error handling (no real model required)
# ---------------------------------------------------------------------------


class TestUVRSeparatorErrorHandling:
    """Test UVRSeparator's RuntimeError guard without running the real UVR model.

    We patch the lazy ``audio_separator.separator.Separator`` import so we
    can control the list of files that the fake separator returns.
    """

    def _build_separator(self, tmp_path: pathlib.Path) -> UVRSeparator:
        return UVRSeparator(
            model_cache_dir=str(tmp_path / "models"),
            media_root=str(tmp_path / "media"),
        )

    def test_raises_runtime_error_when_output_files_missing_vocals(
        self, tmp_path: pathlib.Path
    ) -> None:
        """RuntimeError is raised when the separator returns no vocals file."""
        sep = self._build_separator(tmp_path)

        fake_separator_cls = MagicMock()
        fake_separator_instance = MagicMock()
        # Only an instrumental file — no "vocal" in the name.
        fake_separator_instance.separate.return_value = ["/out/song_(Instrumental).wav"]
        fake_separator_cls.return_value = fake_separator_instance

        with patch.dict(
            sys.modules,
            {"audio_separator.separator": types.SimpleNamespace(Separator=fake_separator_cls)},
        ):
            with pytest.raises(RuntimeError, match="UVR separation failed"):
                sep.separate("/media/song.mp3")

    def test_raises_runtime_error_when_output_files_missing_instrumental(
        self, tmp_path: pathlib.Path
    ) -> None:
        """RuntimeError is raised when the separator returns no instrumental file."""
        sep = self._build_separator(tmp_path)

        fake_separator_cls = MagicMock()
        fake_separator_instance = MagicMock()
        # Only a vocals file — no "instrumental" or "no_vocal" in the name.
        fake_separator_instance.separate.return_value = ["/out/song_(Vocals).wav"]
        fake_separator_cls.return_value = fake_separator_instance

        with patch.dict(
            sys.modules,
            {"audio_separator.separator": types.SimpleNamespace(Separator=fake_separator_cls)},
        ):
            with pytest.raises(RuntimeError, match="UVR separation failed"):
                sep.separate("/media/song.mp3")

    def test_raises_runtime_error_when_output_list_is_empty(
        self, tmp_path: pathlib.Path
    ) -> None:
        """RuntimeError is raised when the separator produces no output files at all."""
        sep = self._build_separator(tmp_path)

        fake_separator_cls = MagicMock()
        fake_separator_instance = MagicMock()
        fake_separator_instance.separate.return_value = []
        fake_separator_cls.return_value = fake_separator_instance

        with patch.dict(
            sys.modules,
            {"audio_separator.separator": types.SimpleNamespace(Separator=fake_separator_cls)},
        ):
            with pytest.raises(RuntimeError, match="UVR separation failed"):
                sep.separate("/media/song.mp3")

    def test_returns_tuple_when_both_files_present(
        self, tmp_path: pathlib.Path
    ) -> None:
        """separate() returns (vocals_path, instrumental_path) on success."""
        sep = self._build_separator(tmp_path)

        fake_separator_cls = MagicMock()
        fake_separator_instance = MagicMock()
        fake_separator_instance.separate.return_value = [
            "/out/song_(Vocals).wav",
            "/out/song_(Instrumental).wav",
        ]
        fake_separator_cls.return_value = fake_separator_instance

        with patch.dict(
            sys.modules,
            {"audio_separator.separator": types.SimpleNamespace(Separator=fake_separator_cls)},
        ):
            vocals, instrumental = sep.separate("/media/song.mp3")

        assert "vocal" in vocals.lower()
        assert "instrumental" in instrumental.lower()

    def test_recognises_no_vocal_as_instrumental(
        self, tmp_path: pathlib.Path
    ) -> None:
        """separate() accepts 'no_vocal' in the filename as the instrumental track."""
        sep = self._build_separator(tmp_path)

        fake_separator_cls = MagicMock()
        fake_separator_instance = MagicMock()
        fake_separator_instance.separate.return_value = [
            "/out/song_(Vocals).wav",
            "/out/song_(No_Vocal).wav",
        ]
        fake_separator_cls.return_value = fake_separator_instance

        with patch.dict(
            sys.modules,
            {"audio_separator.separator": types.SimpleNamespace(Separator=fake_separator_cls)},
        ):
            vocals, instrumental = sep.separate("/media/song.mp3")

        assert vocals == "/out/song_(Vocals).wav"
        assert instrumental == "/out/song_(No_Vocal).wav"


# ---------------------------------------------------------------------------
# JobPoller
# ---------------------------------------------------------------------------


class TestJobPoller:
    def _make_poller(self, poll_interval: float = 0.01) -> JobPoller:
        """Construct a JobPoller with MagicMock dependencies."""
        pipeline = MagicMock(spec=AudioPipeline)
        job_service = MagicMock()
        return JobPoller(
            pipeline=pipeline,
            job_service=job_service,
            worker_id="test-worker",
            poll_interval=poll_interval,
        )

    def test_initial_running_state_is_true(self) -> None:
        """JobPoller starts with _running=True."""
        poller = self._make_poller()

        assert poller._running is True

    def test_stop_sets_running_to_false(self) -> None:
        """stop() sets _running to False so the polling loop can exit."""
        poller = self._make_poller()

        poller.stop()

        assert poller._running is False

    def test_stop_is_idempotent(self) -> None:
        """Calling stop() multiple times does not raise and keeps _running=False."""
        poller = self._make_poller()

        poller.stop()
        poller.stop()

        assert poller._running is False

    async def test_run_processes_one_job_then_stops(self) -> None:
        """run() processes a job, then exits when stop() is called mid-loop.

        The job_service mock returns a fake job on the first call to
        poll_and_lock, then stop() sets _running=False so the loop exits
        after processing that single job.
        """
        import asyncio

        fake_job = MagicMock()
        fake_job.id = "job-1"
        fake_job.track_id = "track-1"

        pipeline = MagicMock(spec=AudioPipeline)
        pipeline.process = AsyncMock(return_value=None)

        job_service = MagicMock()
        # First call returns a job; second call (if the loop continues) would
        # block on sleep, but stop() prevents that.
        job_service.poll_and_lock = AsyncMock(return_value=fake_job)

        poller = JobPoller(
            pipeline=pipeline,
            job_service=job_service,
            worker_id="test-worker",
            poll_interval=0.01,
        )

        # Stop the poller after it processes the first job.
        async def _stop_after_process(*args, **kwargs):  # noqa: ANN002
            poller.stop()

        pipeline.process.side_effect = _stop_after_process

        await poller.run()

        pipeline.process.assert_called_once_with(fake_job)

    async def test_run_sleeps_when_queue_is_empty(self) -> None:
        """run() calls asyncio.sleep when poll_and_lock returns None."""
        import asyncio

        pipeline = MagicMock(spec=AudioPipeline)
        job_service = MagicMock()

        call_count = 0

        async def _poll_once(worker_id: str):  # noqa: ANN202
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                poller.stop()
            return None

        job_service.poll_and_lock = _poll_once

        poller = JobPoller(
            pipeline=pipeline,
            job_service=job_service,
            worker_id="test-worker",
            poll_interval=0.001,  # Keep test fast
        )

        await poller.run()

        # The pipeline should never have been called.
        pipeline.process.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 8a: Feature extraction, lyric embedding, and QDrant sync
# ---------------------------------------------------------------------------
#
# Strategy
# --------
# - feature_extractor.extract() and lyric_embedder.embed() are synchronous
#   functions called via asyncio.to_thread; mock them as plain MagicMock so
#   their return value is available once the thread wrapper resolves.
# - qdrant_repo.upsert() is also synchronous + called via asyncio.to_thread;
#   mock it as a plain MagicMock and assert on call_args_list.
# - To activate the lyric_embedder path, a mock sonoix client is injected so
#   that transcription is not None.  The transcription object only needs
#   .full_text, .tokens, and .language attributes.
# - The existing ``pipeline`` fixture (no ML components) is kept unchanged;
#   new fixtures build on top of the base setup.
# ---------------------------------------------------------------------------

import types as _types  # noqa: E402  (already imported above as ``types``)


def _fake_transcription(
    full_text: str = "These are the lyrics",
) -> _types.SimpleNamespace:
    """Return a minimal transcription-like namespace accepted by the pipeline."""
    # Syllabifier only checks t.text for BPE detection; empty list skips it.
    return _types.SimpleNamespace(
        full_text=full_text,
        tokens=[],
        language="en",
    )


def _mock_sonoix(full_text: str = "These are the lyrics") -> MagicMock:
    """Return an AsyncMock sonoix client whose transcribe() resolves quickly."""
    client = MagicMock()
    client.transcribe = AsyncMock(return_value=_fake_transcription(full_text))
    return client


def _mock_feature_extractor(
    vector: list[float] | None = None,
) -> MagicMock:
    """Return a MagicMock FeatureExtractor whose extract() returns a 45-d vector."""
    if vector is None:
        vector = [0.1] * 45
    fe = MagicMock()
    fe.extract.return_value = vector
    return fe


def _mock_lyric_embedder(
    vector: list[float] | None = None,
) -> MagicMock:
    """Return a MagicMock LyricEmbedder whose embed() returns a 384-d vector."""
    if vector is None:
        vector = [0.2] * 384
    le = MagicMock()
    le.embed.return_value = vector
    return le


def _mock_qdrant_repo() -> MagicMock:
    """Return a MagicMock QDrantRepository whose upsert() is a plain mock."""
    repo = MagicMock()
    repo.upsert = MagicMock()
    return repo


class TestAudioPipelinePhase8aFullML:
    """Both feature_extractor and lyric_embedder present — happy path."""

    async def test_both_vectors_extracted_and_qdrant_upserted(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """With both ML components, extract() and embed() are each called once."""
        feature_extractor = _mock_feature_extractor()
        lyric_embedder = _mock_lyric_embedder()
        qdrant = _mock_qdrant_repo()
        sonoix = _mock_sonoix()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=sonoix,
            feature_extractor=feature_extractor,
            lyric_embedder=lyric_embedder,
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        feature_extractor.extract.assert_called_once()
        lyric_embedder.embed.assert_called_once()

    async def test_feature_extractor_called_with_instrumental_path(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """feature_extractor.extract() receives the instrumental path from UVR."""
        instrumental = "/media/inst.wav"
        feature_extractor = _mock_feature_extractor()
        uvr = _mock_uvr(instrumental_path=instrumental)

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=uvr,
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=feature_extractor,
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        feature_extractor.extract.assert_called_once_with(instrumental)

    async def test_lyric_embedder_called_with_transcription_full_text(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """lyric_embedder.embed() receives the full_text from the transcription."""
        lyrics = "Never gonna give you up, never gonna let you down"
        lyric_embedder = _mock_lyric_embedder()
        sonoix = _mock_sonoix(full_text=lyrics)

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=sonoix,
            feature_extractor=_mock_feature_extractor(),
            lyric_embedder=lyric_embedder,
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        lyric_embedder.embed.assert_called_once_with(lyrics)

    async def test_qdrant_upserted_for_both_collections(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """qdrant_repo.upsert() is called for both audio_features and lyrics_embeddings."""
        qdrant = _mock_qdrant_repo()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=_mock_feature_extractor(),
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        assert qdrant.upsert.call_count == 2
        collection_names = [call.args[0] for call in qdrant.upsert.call_args_list]
        assert "audio_features" in collection_names
        assert "lyrics_embeddings" in collection_names

    async def test_qdrant_upsert_called_with_correct_track_id(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """qdrant_repo.upsert() is called with the job's track_id as point_id."""
        qdrant = _mock_qdrant_repo()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=_mock_feature_extractor(),
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        for call in qdrant.upsert.call_args_list:
            point_id = call.args[1]
            assert point_id == track.id

    async def test_qdrant_synced_flag_set_when_both_vectors_present(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """qdrant_synced=1 is persisted on the track when upsert succeeds."""
        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=_mock_feature_extractor(),
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        updated_track = await sqlite_repo.get_track(track.id)
        assert updated_track is not None
        assert updated_track.qdrant_synced == 1

    async def test_job_completed_when_both_ml_components_present(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """The job is marked 'completed' when both ML steps succeed."""
        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=_mock_feature_extractor(),
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status == "completed"

    async def test_steps_completed_includes_ml_steps(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """The job result's steps_completed includes the Phase 8a step names."""
        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=_mock_feature_extractor(),
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        steps = updated_job.result.get("steps_completed", [])
        assert "extracting_features" in steps
        assert "embedding_lyrics" in steps
        assert "syncing_qdrant" in steps


class TestAudioPipelinePhase8aFeatureExtractorOnly:
    """Only feature_extractor provided — only audio_features collection upserted."""

    async def test_only_audio_features_upserted(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """With only feature_extractor, upsert is called once for audio_features only."""
        qdrant = _mock_qdrant_repo()
        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            feature_extractor=_mock_feature_extractor(),
            # lyric_embedder intentionally absent
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        assert qdrant.upsert.call_count == 1
        collection = qdrant.upsert.call_args.args[0]
        assert collection == "audio_features"

    async def test_lyric_embedder_not_called_when_absent(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """lyric_embedder.embed() is never called when lyric_embedder is None."""
        feature_extractor = _mock_feature_extractor()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            feature_extractor=feature_extractor,
            lyric_embedder=None,
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        # No embed() call since lyric_embedder is None
        # (assert by checking the feature extractor was called but no second collection)
        feature_extractor.extract.assert_called_once()

    async def test_qdrant_synced_set_with_only_audio_features(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """qdrant_synced=1 when only audio_features upserted."""
        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            feature_extractor=_mock_feature_extractor(),
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        updated_track = await sqlite_repo.get_track(track.id)
        assert updated_track is not None
        assert updated_track.qdrant_synced == 1


class TestAudioPipelinePhase8aLyricEmbedderOnly:
    """Only lyric_embedder provided — only lyrics_embeddings collection upserted."""

    async def test_only_lyrics_embeddings_upserted(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """With only lyric_embedder (plus sonoix), upsert called once for lyrics_embeddings."""
        qdrant = _mock_qdrant_repo()
        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            # feature_extractor intentionally absent
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        assert qdrant.upsert.call_count == 1
        collection = qdrant.upsert.call_args.args[0]
        assert collection == "lyrics_embeddings"

    async def test_feature_extractor_not_called_when_absent(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """feature_extractor.extract() is never called when feature_extractor is None."""
        lyric_embedder = _mock_lyric_embedder()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=None,
            lyric_embedder=lyric_embedder,
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        lyric_embedder.embed.assert_called_once()

    async def test_lyric_embedder_skipped_when_no_transcription(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """lyric_embedder.embed() is not called when sonoix is absent (no transcription)."""
        lyric_embedder = _mock_lyric_embedder()
        qdrant = _mock_qdrant_repo()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=None,          # no transcription produced
            feature_extractor=None,
            lyric_embedder=lyric_embedder,
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        lyric_embedder.embed.assert_not_called()
        qdrant.upsert.assert_not_called()


class TestAudioPipelinePhase8aNoMLComponents:
    """No ML components — steps 4-6 are fully skipped; existing behaviour unchanged."""

    async def test_pipeline_completes_without_ml_components(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """The pipeline marks the job completed even when no ML components are present."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status == "completed"

    async def test_qdrant_not_synced_without_ml_components(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """qdrant_synced remains 0 when no ML components are present."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_track = await sqlite_repo.get_track(track.id)
        assert updated_track is not None
        # qdrant_synced is 0 (falsy) when no vectors were upserted
        assert not updated_track.qdrant_synced

    async def test_steps_completed_only_contains_separating_without_ml(
        self,
        pipeline: AudioPipeline,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """Without ML components, only 'separating' appears in steps_completed."""
        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)

        await pipeline.process(job)

        updated_job = await job_service.get_job(job.id)
        assert updated_job is not None
        steps = updated_job.result.get("steps_completed", [])
        assert steps == ["separating"]


class TestAudioPipelinePhase8aParallelExecution:
    """Steps 4 and 5 must run via asyncio.gather — both are invoked."""

    async def test_both_extract_and_embed_are_called_in_same_process(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """When both ML components are present, both extract and embed are called."""
        feature_extractor = _mock_feature_extractor()
        lyric_embedder = _mock_lyric_embedder()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=feature_extractor,
            lyric_embedder=lyric_embedder,
            qdrant_repo=_mock_qdrant_repo(),
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        # Both must have been called — this validates that asyncio.gather
        # ran both tasks to completion.
        feature_extractor.extract.assert_called_once()
        lyric_embedder.embed.assert_called_once()

    async def test_feature_vector_passed_to_audio_features_upsert(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """The exact vector returned by feature_extractor.extract() is passed to upsert."""
        audio_vector = [float(i) / 45 for i in range(45)]
        qdrant = _mock_qdrant_repo()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=_mock_feature_extractor(vector=audio_vector),
            lyric_embedder=_mock_lyric_embedder(),
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        # Find the audio_features call
        audio_call = next(
            c for c in qdrant.upsert.call_args_list if c.args[0] == "audio_features"
        )
        assert audio_call.args[2] == audio_vector

    async def test_lyric_vector_passed_to_lyrics_embeddings_upsert(
        self,
        job_service: JobService,
        sqlite_repo: SQLiteRepository,
    ) -> None:
        """The exact vector returned by lyric_embedder.embed() is passed to upsert."""
        lyric_vector = [float(i) / 384 for i in range(384)]
        qdrant = _mock_qdrant_repo()

        pipeline = AudioPipeline(
            job_service=job_service,
            uvr=_mock_uvr(),
            repo=sqlite_repo,
            sonoix=_mock_sonoix(),
            feature_extractor=_mock_feature_extractor(),
            lyric_embedder=_mock_lyric_embedder(vector=lyric_vector),
            qdrant_repo=qdrant,
        )

        track = await sqlite_repo.create_track(_track_create())
        job = await job_service.create_job(track.id)
        await pipeline.process(job)

        # Find the lyrics_embeddings call
        lyric_call = next(
            c for c in qdrant.upsert.call_args_list if c.args[0] == "lyrics_embeddings"
        )
        assert lyric_call.args[2] == lyric_vector
