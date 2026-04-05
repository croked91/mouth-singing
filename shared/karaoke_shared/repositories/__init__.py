"""Repository implementations for the karaoke application.

Re-exports the two concrete repository classes so callers can do:

    from karaoke_shared.repositories import PgRepository, QDrantRepository
"""

from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.repositories.pg_repository import PgRepository

__all__ = ["QDrantRepository", "PgRepository"]
