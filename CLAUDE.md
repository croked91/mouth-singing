# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Karaoke web application for club rooms. Kiosk-style (no user registration). ~17,000 tracks in catalog. Supports user MP3 uploads with auto-generated karaoke (vocal separation, transcription, syllable-level alignment).

GPU mode only: Local UVR (BS-Roformer) + Whisper (PyTorch Transformers) + CTC alignment (requires NVIDIA GPU).

## Commands

```bash
# Docker — development (named volumes)
make up-gpu          # Start all services
make down            # Stop containers
make down-v          # Stop + delete all volumes (full reset)

# Docker — production / local (bind mounts)
make up-gpu-prod     # Production mode (bind mounts to /root paths)
make up-gpu-local    # Local mode (bind mounts to backup directory)

# Docker — diagnostics
make logs-worker     # Tail worker logs
make logs-backend    # Tail backend logs
make logs            # Tail all services
make ps              # Show running containers
make health          # Check all service health
make clean           # Stop containers and prune images
make build-gpu       # Build images without starting

# Migrations
make migrate-qdrant-clusters  # Backfill rec_cluster_id into QDrant (run before deploying new backend)

# Tests (requires local venv with deps installed)
python -m pytest tests/ -v              # All tests
make test-quick                         # Fast subset (skips tests/worker/)
python -m pytest tests/test_api_tracks.py -v          # Single file
python -m pytest tests/test_api_tracks.py::test_name  # Single test

# Lint (ruff: line-length=88, rules E/F/I/UP, configured in backend/pyproject.toml)
ruff check backend/ worker/ shared/ rec-service/
ruff format backend/ worker/ shared/ rec-service/

# Frontend
cd frontend && npm run dev      # Dev server (Vite)
cd frontend && npm run build    # Production build
cd frontend && npm run preview  # Preview production build locally
cd frontend && npm run lint     # ESLint
```

## Architecture

Five main components, each in its own directory:

```
backend/      → FastAPI server (port 8000). Routes in app/api/v1/, services in app/services/
worker/       → GPU audio processing pipeline. Pipeline in worker/gpu/. RabbitMQ consumer in worker/app/consumer.py
rec-service/  → Recommendation indexing microservice. Consumer in rec-service/app/consumer.py, indexing logic in app/indexer.py
shared/       → karaoke_shared package: Pydantic models, PgRepository, QDrant repo, S3 storage, RabbitMQ messaging, ML utils, constants
frontend/     → React 19 + TypeScript + MUI + Zustand. Vite build. Pages in src/pages/, state in src/store/
```

### Data flow

1. Frontend creates sessions, manages FIFO queue, searches tracks
2. Backend serves REST API + SSE for job progress; redirects audio playback to S3 presigned URLs
3. On MP3 upload (deferred track creation — no track record until pipeline completes):
   - Backend uploads to S3 → creates `job_queue` record (mp3_key, artist_hint, title_hint) → publishes to RabbitMQ "jobs" exchange
   - Worker consumes from "jobs.process" queue → downloads from S3 → runs pipeline → stores intermediate data in job_queue.data JSONB → at finalization: INSERT INTO tracks (status=ready, qdrant_synced=0) → publishes {track_id, mp3_key, lyrics} to "rec" exchange
   - Rec Service consumes from "rec.index" queue → extracts features → embeds lyrics → assigns rec_cluster → QDrant upsert → UPDATE tracks SET qdrant_synced=1
4. SSE progress delivered via RabbitMQ "job.progress" fanout exchange (fallback: DB polling)
5. Player page renders syllable-by-syllable highlighting synced to audio playback

### Infrastructure

- **PostgreSQL**: sessions, participants, queue_entries, tracks (with syllable_timings JSONB), job_queue (with data JSONB), mood_tags, catalog_clusters, artists. Full-text search via tsvector + GIN index
- **MinIO (S3-compatible)**: `uploads/{job_id}.mp3` (temporary), `instrumentals/{job_id}.mp3` (permanent)
- **RabbitMQ**: 3 exchanges — `jobs` (direct), `job.progress` (fanout), `rec` (direct). DLQ: `jobs.dlq`, `rec.dlq`
- **QDrant**: `audio_features` (45-d librosa vectors), `lyrics_embeddings` (384-d sentence-transformer vectors)
- **Nginx**: Reverse proxy, SSE passthrough, frontend static files

### Worker pipeline steps

Processing order (defined in `PipelineStep` enum):
SEPARATING → VAD → TRANSCRIBING → SEARCHING_LYRICS → ALIGNING → LINE_BREAKING

Worker creates track at finalization (deferred track creation — no track record until pipeline completes). Then publishes to Rec Service.

### Key patterns

- All Python config via pydantic-settings, loaded from env vars (no prefix). Defaults in `backend/app/config.py`, `worker/app/config.py`, `rec-service/app/config.py`
- Each component has its own `pyproject.toml` with dependencies (backend/, worker/, shared/, rec-service/). Install in editable mode: `pip install -e shared/ -e backend/` etc.
- Structured logging via structlog (JSON output). Use `structlog.get_logger(__name__)`
- Root `conftest.py` adds worker/, shared/, backend/ to sys.path for test imports
- pytest uses `asyncio_mode = auto` (no need for `@pytest.mark.asyncio`)
- Shared constants (status enums, collection names, pipeline steps) live in `shared/karaoke_shared/constants.py`
- DB schema in `backend/app/db/init_pg.sql`, applied at startup via `init_pg()`. No migration tool
- No foreign keys in PostgreSQL (denormalized by design, see ADR-03)
- Docker Compose: base `docker-compose.yml` + `docker-compose.gpu.yml`, optionally + `docker-compose.prod.yml` or `docker-compose.local.yml`
- Docker services: `postgres`, `minio`, `rabbitmq`, `qdrant`, `backend`, `rec-service`, `frontend`, `worker`
- Container names prefixed `karaoke_`. Network: `karaoke_net`
- Resource limits: backend 512M, rec-service 4G, GPU worker 24G + 1 GPU reservation
- Repository injection via FastAPI `Depends()` for PgRepository and QDrantRepository
- S3 storage via `karaoke_shared.storage.S3Storage` (boto3-based, works with MinIO/AWS/Yandex)
- RabbitMQ messaging via `karaoke_shared.messaging.RabbitMQClient` (aio-pika)
- Job progress: worker publishes to RabbitMQ → SSE endpoint consumes from fanout exchange

### Recommendations (KNN-within-cluster)

- Rec Service assigns `rec_cluster_id` via `RecClusterAssigner` (fused audio+lyrics vectors, cosine similarity to pre-computed centroids)
- Backend `recommendation_service.py`: `_cluster_strategy()` groups played tracks by cluster → computes per-cluster centroids → KNN search filtered by `rec_cluster_id`
- `hit_priority_sort()`: hits (eternal/current/artist_best) with fusion score >= 0.5 get priority over regular tracks
- QDrant payloads include `rec_cluster_id` for efficient filtered KNN

### Utility scripts

`scripts/` contains bulk operations: `seed_catalog.py`, `cluster_catalog.py`, `create_mood_tags.py`, `reindex_audio_features.py`, `fetch_artist_images.py`, `bootstrap_worker.py` (pre-download ML models).

## Environment

Required env vars (see `.env.example`):
- `DEEPSEEK_API_KEY`, `YANDEX_SEARCH_API_KEY` — lyrics search
- `PG_PASSWORD` — PostgreSQL password
- `S3_ACCESS_KEY`, `S3_SECRET_KEY` — MinIO/S3 credentials
- `RMQ_USER`, `RMQ_PASS` — RabbitMQ credentials
- `ADMIN_SECRET` — PIN for admin panel

## Documentation

`journals/` contains detailed docs:
- `ARCHITECTURE.md` — full architecture description
- `ADR.md` — Architecture Decision Records (e.g. ADR-03: no foreign keys)
- `DEPLOYMENT_GUIDE.md` — deployment procedures
- `WORKER_FLOW.md` — worker pipeline flow
- `PROJECT_LOG.md`, `PHASES.md` — project history and development phases
- `upload-sequence*.md` — upload flow sequence diagrams (target architecture)
