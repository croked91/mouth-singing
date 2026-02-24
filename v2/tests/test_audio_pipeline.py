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
