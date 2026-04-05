"""Shared package for the karaoke application.

Domain models, repository implementations, and constants live here so they
can be imported by the backend, workers, and any future service without
creating circular dependencies.

Convenient top-level imports::

    from karaoke_shared import Track, TrackCreate, PgRepository, QDrantRepository
    from karaoke_shared import JobService
    from karaoke_shared.constants import TrackStatus, JobStatus
"""

from karaoke_shared.models import *  # noqa: F401, F403
from karaoke_shared.repositories import QDrantRepository, PgRepository
from karaoke_shared.services.job_service import JobService

__all__ = ["QDrantRepository", "PgRepository", "JobService"]
