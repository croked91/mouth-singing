"""Integration tests for the SSE job status endpoint.

Tests cover:
    GET /api/v1/jobs/{job_id}/status  (StreamingResponse / SSE)

Strategy
--------
- Uses the ``client`` and ``app_db`` fixtures from conftest.py so all tests
  run against an in-memory SQLite database with no external services.
- For terminal states (not_found, completed, failed) the generator terminates
  immediately, so ``client.get()`` returns the complete body.
- SSE format: each message is ``event: <name>\\ndata: <json>\\n\\n``.
- asyncio.sleep inside the generator is patched to avoid real delays while
  still testing the status-change polling behaviour.
"""

from __future__ import annotations

import json

import pytest

from karaoke_shared.models.job import JobCreate
from karaoke_shared.models.track import TrackCreate
from karaoke_shared.repositories import SQLiteRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse_events(body: str) -> list[dict]:
    """Parse a raw SSE body into a list of dicts with 'event' and 'data' keys.

    Each SSE message block looks like::

        event: status
        data: {"key": "value"}

    Blank lines separate messages.
    """
    events: list[dict] = []
    current_event: dict = {}

    for line in body.splitlines():
        line = line.strip()
        if not line:
            if current_event:
                events.append(current_event)
                current_event = {}
            continue

        if line.startswith("event:"):
            current_event["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            raw = line[len("data:"):].strip()
            try:
                current_event["data"] = json.loads(raw)
            except json.JSONDecodeError:
                current_event["data"] = raw

    # Flush any trailing event (no trailing blank line)
    if current_event:
        events.append(current_event)

    return events


async def _create_track_and_job(db, **track_kwargs) -> tuple:
    """Create a track and a job, return (track, job)."""
    repo = SQLiteRepository(db)
    track = await repo.create_track(
        TrackCreate(
            artist=track_kwargs.get("artist", "Test Artist"),
            title=track_kwargs.get("title", "Test Title"),
            source="catalog",
        )
    )
    job = await repo.create_job(JobCreate(track_id=track.id))
    return track, job


# ---------------------------------------------------------------------------
# Job not found
# ---------------------------------------------------------------------------


class TestSSEJobNotFound:
    async def test_not_found_returns_200(self, client) -> None:
        """The SSE endpoint always returns HTTP 200 (errors are in the stream)."""
        response = await client.get("/api/v1/jobs/nonexistent-job-id/status")

        assert response.status_code == 200

    async def test_not_found_emits_error_event(self, client) -> None:
        """A missing job_id emits an error SSE event."""
        response = await client.get("/api/v1/jobs/nonexistent-job-id/status")
        events = _parse_sse_events(response.text)

        assert len(events) >= 1
        assert events[0]["event"] == "error"

    async def test_not_found_error_message_contains_job_not_found(
        self, client
    ) -> None:
        response = await client.get("/api/v1/jobs/ghost-id/status")
        events = _parse_sse_events(response.text)

        assert events[0]["event"] == "error"
        assert "Job not found" in events[0]["data"]["error"]

    async def test_not_found_data_includes_job_id(self, client) -> None:
        response = await client.get("/api/v1/jobs/my-fake-id/status")
        events = _parse_sse_events(response.text)

        assert events[0]["data"]["job_id"] == "my-fake-id"

    async def test_not_found_stream_terminates(self, client) -> None:
        """The stream must terminate (not hang) for a missing job."""
        response = await client.get("/api/v1/jobs/nonexistent/status")

        # If the response was received at all, the stream terminated.
        assert response.text is not None

    async def test_content_type_is_event_stream(self, client) -> None:
        response = await client.get("/api/v1/jobs/nonexistent/status")

        assert "text/event-stream" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Job already completed
# ---------------------------------------------------------------------------


class TestSSEJobCompleted:
    async def test_completed_emits_completed_event(
        self, client, app_db
    ) -> None:
        """A job with status='completed' immediately emits a 'completed' event."""
        from karaoke_shared.models.track import TrackUpdate

        repo = SQLiteRepository(app_db)
        track, job = await _create_track_and_job(app_db)

        # Mark the job as completed.
        await repo.complete_job(job.id, {"vocals_path": "/v.wav"})

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        assert any(e["event"] == "completed" for e in events)

    async def test_completed_event_contains_track_id(
        self, client, app_db
    ) -> None:
        repo = SQLiteRepository(app_db)
        track, job = await _create_track_and_job(app_db)
        await repo.complete_job(job.id, {})

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        completed = next(e for e in events if e["event"] == "completed")
        assert completed["data"]["track_id"] == track.id

    async def test_completed_event_status_field(
        self, client, app_db
    ) -> None:
        repo = SQLiteRepository(app_db)
        _, job = await _create_track_and_job(app_db)
        await repo.complete_job(job.id, {})

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        completed = next(e for e in events if e["event"] == "completed")
        assert completed["data"]["status"] == "completed"

    async def test_completed_event_includes_clip_url_when_present(
        self, client, app_db
    ) -> None:
        """clip_url is derived from the track's clip_path."""
        from karaoke_shared.models.track import TrackUpdate

        repo = SQLiteRepository(app_db)
        track, job = await _create_track_and_job(app_db)

        # Set a clip_path on the track
        await repo.update_track(
            track.id, TrackUpdate(clip_path=f"/media/clips/{track.id}.mp4")
        )
        await repo.complete_job(job.id, {})

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        completed = next(e for e in events if e["event"] == "completed")
        assert completed["data"]["clip_url"] is not None
        assert track.id in completed["data"]["clip_url"]

    async def test_completed_event_clip_url_none_when_no_clip_path(
        self, client, app_db
    ) -> None:
        """clip_url is None when the track has no clip_path set."""
        repo = SQLiteRepository(app_db)
        _, job = await _create_track_and_job(app_db)
        await repo.complete_job(job.id, {})

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        completed = next(e for e in events if e["event"] == "completed")
        assert completed["data"]["clip_url"] is None


# ---------------------------------------------------------------------------
# Job failed
# ---------------------------------------------------------------------------


class TestSSEJobFailed:
    async def test_failed_emits_error_event(
        self, client, app_db
    ) -> None:
        """A job exhausted all attempts (status='failed') emits an error event."""
        repo = SQLiteRepository(app_db)
        # Create a job with max_attempts=1 so one failure → status='failed'
        track = await repo.create_track(
            TrackCreate(artist="A", title="T", source="catalog")
        )
        job = await repo.create_job(JobCreate(track_id=track.id, max_attempts=1))
        await repo.fail_job(job.id, "Processing error: out of memory")

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        assert any(e["event"] == "error" for e in events)

    async def test_failed_error_message_in_event(
        self, client, app_db
    ) -> None:
        repo = SQLiteRepository(app_db)
        track = await repo.create_track(
            TrackCreate(artist="A", title="T", source="catalog")
        )
        job = await repo.create_job(JobCreate(track_id=track.id, max_attempts=1))
        await repo.fail_job(job.id, "out of memory")

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        error_event = next(e for e in events if e["event"] == "error")
        assert "out of memory" in error_event["data"]["error"]

    async def test_failed_event_status_field(
        self, client, app_db
    ) -> None:
        repo = SQLiteRepository(app_db)
        track = await repo.create_track(
            TrackCreate(artist="A", title="T", source="catalog")
        )
        job = await repo.create_job(JobCreate(track_id=track.id, max_attempts=1))
        await repo.fail_job(job.id, "some error")

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        error_event = next(e for e in events if e["event"] == "error")
        assert error_event["data"]["status"] == "failed"

    async def test_failed_event_includes_job_id(
        self, client, app_db
    ) -> None:
        repo = SQLiteRepository(app_db)
        track = await repo.create_track(
            TrackCreate(artist="A", title="T", source="catalog")
        )
        job = await repo.create_job(JobCreate(track_id=track.id, max_attempts=1))
        await repo.fail_job(job.id, "some error")

        response = await client.get(f"/api/v1/jobs/{job.id}/status")
        events = _parse_sse_events(response.text)

        error_event = next(e for e in events if e["event"] == "error")
        assert error_event["data"]["job_id"] == job.id

    async def test_failed_stream_terminates(
        self, client, app_db
    ) -> None:
        """The stream must close as soon as a failed status is detected."""
        repo = SQLiteRepository(app_db)
        track = await repo.create_track(
            TrackCreate(artist="A", title="T", source="catalog")
        )
        job = await repo.create_job(JobCreate(track_id=track.id, max_attempts=1))
        await repo.fail_job(job.id, "error")

        response = await client.get(f"/api/v1/jobs/{job.id}/status")

        assert response.text is not None


# ---------------------------------------------------------------------------
# Job running: emits status events when step/progress changes
# ---------------------------------------------------------------------------


class TestSSEJobRunning:
    async def test_running_job_emits_status_event(
        self, client, app_db
    ) -> None:
        """A job in 'running' state with a current_step emits a status event.

        We mock asyncio.sleep in the SSE generator to prevent an infinite loop:
        after the first iteration we mark the job as 'completed'.
        """
        from unittest.mock import AsyncMock, patch

        repo = SQLiteRepository(app_db)
        track, job = await _create_track_and_job(app_db)

        # Put job in running state with a current step
        await repo.lock_job(job.id, "worker-1")
        await repo.mark_step(job.id, "separating", 50)

        call_count = 0

        async def _mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            # After the first sleep, mark the job completed so the
            # generator exits on its next iteration.
            await repo.complete_job(job.id, {})

        with patch("app.api.v1.sse.asyncio.sleep", side_effect=_mock_sleep):
            response = await client.get(f"/api/v1/jobs/{job.id}/status")

        events = _parse_sse_events(response.text)

        # Should have at least one status event followed by the completed event
        assert any(e["event"] == "status" for e in events)

    async def test_running_job_status_event_contains_step(
        self, client, app_db
    ) -> None:
        from unittest.mock import patch

        repo = SQLiteRepository(app_db)
        track, job = await _create_track_and_job(app_db)

        await repo.lock_job(job.id, "worker-1")
        await repo.mark_step(job.id, "transcribing", 30)

        async def _mock_sleep(seconds):
            await repo.complete_job(job.id, {})

        with patch("app.api.v1.sse.asyncio.sleep", side_effect=_mock_sleep):
            response = await client.get(f"/api/v1/jobs/{job.id}/status")

        events = _parse_sse_events(response.text)
        status_events = [e for e in events if e["event"] == "status"]

        assert len(status_events) >= 1
        assert status_events[0]["data"]["step"] == "transcribing"

    async def test_running_job_status_event_contains_progress(
        self, client, app_db
    ) -> None:
        from unittest.mock import patch

        repo = SQLiteRepository(app_db)
        track, job = await _create_track_and_job(app_db)

        await repo.lock_job(job.id, "worker-1")
        await repo.mark_step(job.id, "separating", 75)

        async def _mock_sleep(seconds):
            await repo.complete_job(job.id, {})

        with patch("app.api.v1.sse.asyncio.sleep", side_effect=_mock_sleep):
            response = await client.get(f"/api/v1/jobs/{job.id}/status")

        events = _parse_sse_events(response.text)
        status_events = [e for e in events if e["event"] == "status"]

        assert status_events[0]["data"]["progress"] == 75

    async def test_running_job_no_duplicate_status_events_when_unchanged(
        self, client, app_db
    ) -> None:
        """If step and progress do not change, no new status event is emitted."""
        from unittest.mock import patch

        repo = SQLiteRepository(app_db)
        track, job = await _create_track_and_job(app_db)

        await repo.lock_job(job.id, "worker-1")
        await repo.mark_step(job.id, "separating", 50)

        sleep_count = 0

        async def _mock_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                # Complete job after second sleep (two poll iterations)
                await repo.complete_job(job.id, {})

        with patch("app.api.v1.sse.asyncio.sleep", side_effect=_mock_sleep):
            response = await client.get(f"/api/v1/jobs/{job.id}/status")

        events = _parse_sse_events(response.text)
        status_events = [e for e in events if e["event"] == "status"]

        # Since step/progress didn't change between iterations, only ONE
        # status event should have been emitted.
        assert len(status_events) == 1


# ---------------------------------------------------------------------------
# Cache-Control and streaming headers
# ---------------------------------------------------------------------------


class TestSSEHeaders:
    async def test_cache_control_no_cache(self, client) -> None:
        response = await client.get("/api/v1/jobs/any-id/status")

        assert response.headers.get("cache-control") == "no-cache"

    async def test_x_accel_buffering_no(self, client) -> None:
        response = await client.get("/api/v1/jobs/any-id/status")

        assert response.headers.get("x-accel-buffering") == "no"
