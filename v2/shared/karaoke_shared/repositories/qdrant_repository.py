"""Synchronous QDrant repository.

Uses the sync ``QdrantClient`` because the qdrant-client library's async
support is limited. In FastAPI async handlers, wrap calls in
``asyncio.to_thread()``::

    results = await asyncio.to_thread(
        qdrant_repo.search, "audio_features", vector, limit=10
    )

Usage::

    repo = QDrantRepository(client)
    repo.upsert("audio_features", track_id, feature_vector, {"status": "ready"})
"""

from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
)

# Maximum number of points sent in a single upsert call during batch operations.
_BATCH_SIZE = 100


class QDrantRepository:
    """Repository for QDrant vector operations.

    All methods are synchronous. The ``QdrantClient`` is injected so it can
    be shared across the application (stored on ``app.state.qdrant``).
    """

    def __init__(self, client: QdrantClient) -> None:
        self.client = client

    def upsert(
        self,
        collection: str,
        point_id: str,
        vector: list[float],
        payload: dict,
    ) -> None:
        """Insert or update a single point in a collection.

        Args:
            collection: Target QDrant collection name.
            point_id: UUID string used as the point ID.
            vector: Dense embedding vector.
            payload: Metadata dictionary stored alongside the vector.
        """
        point = PointStruct(id=point_id, vector=vector, payload=payload)
        self.client.upsert(collection_name=collection, points=[point])

    def search(
        self,
        collection: str,
        vector: list[float],
        limit: int = 10,
        filters: dict | None = None,
    ) -> list[tuple[str, float, dict]]:
        """Search for the nearest neighbours to *vector*.

        Args:
            collection: Collection to search in.
            vector: Query vector.
            limit: Maximum number of results to return.
            filters: Optional equality filters as ``{field_name: value}``.
                     All conditions are combined with AND (must).

        Returns:
            A list of ``(id, score, payload)`` tuples ordered by score
            descending (closest first).
        """
        qdrant_filter: Filter | None = None
        if filters:
            conditions = [
                FieldCondition(key=key, match=MatchValue(value=value))
                for key, value in filters.items()
            ]
            qdrant_filter = Filter(must=conditions)

        response = self.client.query_points(
            collection_name=collection,
            query=vector,
            query_filter=qdrant_filter,
            limit=limit,
        )

        return [
            (str(hit.id), hit.score, hit.payload or {})
            for hit in response.points
        ]

    def delete(self, collection: str, point_id: str) -> None:
        """Remove a single point from a collection.

        Args:
            collection: Collection to delete from.
            point_id: UUID string identifying the point to remove.
        """
        self.client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=[point_id]),
        )

    def batch_upsert(
        self,
        collection: str,
        points: list[tuple[str, list[float], dict]],
    ) -> None:
        """Upsert a large number of points in batches of 100.

        This is intended for the bootstrap importer where the full catalog
        is indexed at once. Batching avoids hitting QDrant's per-request
        payload limits.

        Args:
            collection: Target collection name.
            points: A list of ``(id, vector, payload)`` tuples.
        """
        for batch_start in range(0, len(points), _BATCH_SIZE):
            batch = points[batch_start : batch_start + _BATCH_SIZE]
            point_structs = [
                PointStruct(id=point_id, vector=vector, payload=payload)
                for point_id, vector, payload in batch
            ]
            self.client.upsert(collection_name=collection, points=point_structs)
