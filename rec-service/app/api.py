"""Rec-service HTTP API (FastAPI).

Endpoints:
    POST /recommendations     — cluster KNN recommendations
    POST /recommendations/by-tag — tag-based KNN recommendations
    POST /tags                — mood tags excluding covered clusters
    GET  /health              — health check
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import FastAPI, Request
from qdrant_client import QdrantClient

from app.config import settings
from app.recommendation_engine import RecommendationEngine
from app.schemas import (
    RecommendationRequest,
    RecommendationResponse,
    RecTrackItem,
    TagRecommendationRequest,
    TagsRequest,
)
from karaoke_shared.constants import COLLECTION_AUDIO_FEATURES
from karaoke_shared.repositories.qdrant_repository import QDrantRepository

logger = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="Rec Service", docs_url=None, redoc_url=None)

    @app.on_event("startup")
    async def _startup() -> None:
        # QDrant client.
        qdrant = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port, timeout=30)
        app.state.qdrant = qdrant
        app.state.qdrant_repo = QDrantRepository(qdrant)

        # Load catalog data from JSON.
        catalog_data: dict = {"clusters": [], "tags": []}
        if settings.catalog_data_path:
            try:
                with open(settings.catalog_data_path) as f:
                    catalog_data = json.load(f)
                logger.info(
                    "catalog_data_loaded",
                    clusters=len(catalog_data.get("clusters", [])),
                    tags=len(catalog_data.get("tags", [])),
                )
            except FileNotFoundError:
                logger.warning("catalog_data_not_found", path=settings.catalog_data_path)

        app.state.catalog_data = catalog_data
        app.state.engine = RecommendationEngine(app.state.qdrant_repo, catalog_data)
        logger.info("rec_api.started")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if hasattr(app.state, "qdrant"):
            app.state.qdrant.close()

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/recommendations", response_model=RecommendationResponse)
    async def recommendations(req: RecommendationRequest, request: Request) -> RecommendationResponse:
        engine: RecommendationEngine = request.app.state.engine
        exclude = set(req.exclude_ids) if req.exclude_ids else None

        strategy, results = await engine.get_recommendations(
            played_track_ids=req.played_track_ids,
            limit=req.limit,
            language=req.language,
            exclude_ids=exclude,
        )

        tracks = [
            RecTrackItem(
                id=r.track.id,
                artist=r.track.artist,
                title=r.track.title,
                duration_sec=r.track.duration_sec,
                similarity_score=r.similarity_score,
            )
            for r in results
        ]
        return RecommendationResponse(strategy=strategy, tracks=tracks)

    @app.post("/recommendations/by-tag", response_model=RecommendationResponse)
    async def recommendations_by_tag(req: TagRecommendationRequest, request: Request) -> RecommendationResponse:
        engine: RecommendationEngine = request.app.state.engine

        strategy, results = await engine.get_tag_recommendations(
            tag_id=req.tag_id,
            played_track_ids=req.played_track_ids,
            limit=req.limit * 3,  # oversample for artist dedup
            language=req.language,
        )

        # Artist dedup.
        seen: set[str] = set()
        deduped: list[RecTrackItem] = []
        for r in results:
            if r.track.artist not in seen:
                seen.add(r.track.artist)
                deduped.append(RecTrackItem(
                    id=r.track.id, artist=r.track.artist, title=r.track.title,
                    duration_sec=r.track.duration_sec, similarity_score=r.similarity_score,
                ))
                if len(deduped) >= req.limit:
                    break

        return RecommendationResponse(strategy=strategy, tracks=deduped)

    @app.post("/tags")
    async def tags(req: TagsRequest, request: Request) -> list[dict]:
        engine: RecommendationEngine = request.app.state.engine
        qdrant_repo: QDrantRepository = request.app.state.qdrant_repo

        # Get catalog_cluster_id for each played track from QDrant payload.
        payloads: list[dict] = []
        for tid in req.played_track_ids:
            payload = await asyncio.to_thread(
                qdrant_repo.retrieve_payload, COLLECTION_AUDIO_FEATURES, tid,
            )
            if payload:
                payloads.append(payload)

        result = engine.get_tags(payloads, req.limit)
        return [{"id": t["id"], "name": t["name"]} for t in result]

    return app
