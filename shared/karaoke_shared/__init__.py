"""Shared package for the karaoke application.

Domain models and repository implementations live here so they can be
imported by the backend, workers, and any future service without
creating circular dependencies.

Convenient top-level imports::

    from karaoke_shared import Track, TrackCreate, SQLiteRepository, QDrantRepository
"""

from karaoke_shared.models import *  # noqa: F401, F403
from karaoke_shared.repositories import QDrantRepository, SQLiteRepository

__all__ = ["QDrantRepository", "SQLiteRepository"]
