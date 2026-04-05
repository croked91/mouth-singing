"""Rec indexer: extracts features, embeds lyrics, assigns cluster, upserts to QDrant."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import structlog

from karaoke_shared.ml.feature_extractor import FeatureExtractor
from karaoke_shared.ml.lyric_embedder import LyricEmbedder
from karaoke_shared.ml.rec_cluster_assigner import RecClusterAssigner
from karaoke_shared.repositories.pg_repository import PgRepository
from karaoke_shared.repositories.qdrant_repository import QDrantRepository
from karaoke_shared.models.track import TrackUpdate
from karaoke_shared.storage import S3Storage
from karaoke_shared.constants import COLLECTION_AUDIO_FEATURES, COLLECTION_LYRICS_EMBEDDINGS

logger = structlog.get_logger(__name__)


class RecIndexer:
    """Downloads MP3 from S3, extracts features, embeds lyrics, upserts to QDrant."""

    def __init__(
        self,
        pg_repo: PgRepository,
        qdrant_repo: QDrantRepository,
        s3_storage: S3Storage,
        feature_extractor: FeatureExtractor,
        lyric_embedder: LyricEmbedder,
        cluster_assigner: RecClusterAssigner,
    ) -> None:
        self._pg = pg_repo
        self._qdrant = qdrant_repo
        self._s3 = s3_storage
        self._feature_extractor = feature_extractor
        self._lyric_embedder = lyric_embedder
        self._cluster_assigner = cluster_assigner

    async def index(self, track_id: str, mp3_key: str, lyrics: str) -> None:
        """Run the full indexing pipeline for a single track.

        1. Download MP3 from S3 to /tmp
        2. Extract 45-d audio feature vector
        3. Embed lyrics into 384-d vector
        4. Assign rec cluster
        5. Upsert both vectors to QDrant
        6. Update track in PostgreSQL
        7. Delete original MP3 from S3
        8. Clean up /tmp file
        """
        log = logger.bind(track_id=track_id, mp3_key=mp3_key)
        tmp_path: str | None = None

        try:
            # 1. Download MP3 to /tmp
            suffix = os.path.splitext(mp3_key)[1] or ".mp3"
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="rec_")
            os.close(tmp_fd)

            log.info("rec_indexer.downloading")
            await self._s3.download_to_file(mp3_key, tmp_path)

            # 2. Extract audio features (CPU-bound)
            log.info("rec_indexer.extracting_features")
            audio_vector = await asyncio.to_thread(
                self._feature_extractor.extract, tmp_path
            )

            # 3. Embed lyrics (CPU-bound)
            log.info("rec_indexer.embedding_lyrics")
            lyrics_vector = await asyncio.to_thread(
                self._lyric_embedder.embed, lyrics or ""
            )

            # 4. Assign rec cluster
            rec_cluster_id = self._cluster_assigner.assign(audio_vector, lyrics_vector)
            log.info("rec_indexer.cluster_assigned", rec_cluster_id=rec_cluster_id)

            # 5. Upsert to QDrant
            payload = {"track_id": track_id}
            if rec_cluster_id is not None:
                payload["rec_cluster_id"] = rec_cluster_id

            await asyncio.to_thread(
                self._qdrant.upsert,
                COLLECTION_AUDIO_FEATURES,
                track_id,
                audio_vector,
                payload,
            )
            await asyncio.to_thread(
                self._qdrant.upsert,
                COLLECTION_LYRICS_EMBEDDINGS,
                track_id,
                lyrics_vector,
                payload,
            )
            log.info("rec_indexer.qdrant_upserted")

            # 6. Update track in PostgreSQL
            await self._pg.update_track(
                track_id,
                TrackUpdate(qdrant_synced=1, rec_cluster_id=rec_cluster_id),
            )
            log.info("rec_indexer.pg_updated")

            # 7. Delete original MP3 from S3
            await self._s3.delete(mp3_key)
            log.info("rec_indexer.s3_deleted")

        except Exception:
            log.exception("rec_indexer.failed")
            raise

        finally:
            # 8. Clean up /tmp
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
                log.debug("rec_indexer.tmp_cleaned", path=tmp_path)
