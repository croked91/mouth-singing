"""Repository implementations for the karaoke application.

Re-exports the two concrete repository classes so callers can do:

    from karaoke_shared.repositories import SQLiteRepository, QDrantRepository
"""

from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.sqlite_repository import SQLiteRepository

__all__ = ["QDrantRepository", "SQLiteRepository"]
