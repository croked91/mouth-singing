# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Karaoke web application for club rooms. Kiosk-style (no user registration). ~17,000 tracks in catalog. Supports user MP3 uploads with auto-generated karaoke (vocal separation, transcription, syllable-level alignment).

Two deployment modes:
- **GPU mode**: Local UVR (BS-Roformer) + faster-whisper + CTC alignment (requires NVIDIA GPU)
- **API mode**: MVSEP API + OpenAI Whisper API + CTC alignment (CPU-only)

## Commands

```bash
# Docker
make up-gpu          # Start all services (GPU mode)
make up-api          # Start all services (API mode)
make down            # Stop containers
make down-v          # Stop + delete all volumes (full reset)
make logs-worker     # Tail worker logs
make logs-backend    # Tail backend logs
make health          # Check all service health
make build-gpu       # Build images without starting
make build-api

# Tests (requires local venv with deps installed)
python -m pytest tests/ -v              # All tests
make test-quick                         # Fast subset (skips tests/worker/)
python -m pytest tests/test_api_tracks.py -v          # Single file
python -m pytest tests/test_api_tracks.py::test_name  # Single test

# Lint (ruff configured in backend/pyproject.toml)
ruff check backend/ worker/ shared/
ruff format backend/ worker/ shared/

# Frontend
cd frontend && npm run dev      # Dev server (Vite)
cd frontend && npm run build    # Production build
cd frontend && npx eslint src/  # Lint
```

## Architecture

Four main components, each in its own directory:

```
backend/    → FastAPI server (port 8000). Routes in app/api/v1/, services in app/services/
worker/     → Audio processing pipelines. GPU pipeline in worker/gpu/, API pipeline in worker/api/
shared/     → karaoke_shared package: Pydantic models, SQLite/QDrant repositories, ML utils, constants
frontend/   → React 19 + TypeScript + MUI + Zustand. Vite build. Pages in src/pages/, state in src/store/
```

### Data flow

1. Frontend creates sessions, manages FIFO queue, searches tracks
2. Backend serves REST API + SSE for job progress; streams audio via HTTP 206 range requests
3. On MP3 upload: backend creates a job in `job_queue` table → worker polls and processes:
   vocal separation → VAD → ASR → lyrics search (OpenAI+Genius) → CTC alignment → feature extraction → QDrant indexing
4. Player page renders syllable-by-syllable highlighting synced to audio playback

### Storage

- **SQLite** (WAL mode): sessions, participants, queue_entries, tracks (with syllable_timings JSON), job_queue, mood_tags, catalog_clusters, artists
- **QDrant**: `audio_features` (45-d librosa vectors), `lyrics_embeddings` (384-d sentence-transformer vectors)
- **Filesystem** (`/data/media/`): MP3 files, instrumental tracks

### Worker pipeline steps

Processing order (defined in `PipelineStep` enum):
SEPARATING → EXTRACTING_FEATURES → VAD → TRANSCRIBING → SEARCHING_LYRICS → ALIGNING → LINE_BREAKING → EMBEDDING_LYRICS → SYNCING_QDRANT → DONE

Both GPU and API pipelines share common components from `worker/common/` (VAD, CTC aligner, lyrics agent).

### Key patterns

- All Python config via pydantic-settings, loaded from env vars (no prefix). Defaults in `backend/app/config.py` and `worker/app/config.py`
- Structured logging via structlog (JSON output). Use `structlog.get_logger(__name__)`
- Root `conftest.py` adds worker/, shared/, backend/ to sys.path for test imports
- pytest uses `asyncio_mode = auto` (no need for `@pytest.mark.asyncio`)
- Shared constants (status enums, collection names, pipeline steps) live in `shared/karaoke_shared/constants.py`
- DB schema is applied at startup in `backend/app/db/__init__.py` (idempotent). No migration tool
- No foreign keys in SQLite (denormalized by design, see ADR-03)
- Docker Compose uses overlay pattern: base `docker-compose.yml` + mode-specific overlay (`docker-compose.gpu.yml` or `docker-compose.api.yml`)
- Repository injection via FastAPI `Depends()` for SQLiteRepository and QDrantRepository

### Utility scripts

`scripts/` contains bulk operations: `seed_catalog.py`, `cluster_catalog.py`, `create_mood_tags.py`, `reindex_audio_features.py`, `fetch_artist_images.py`, `bootstrap_worker.py` (pre-download ML models).

## Environment

Required env vars (see `.env.example`):
- `OPENAI_API_KEY`, `GENIUS_TOKEN` — both modes
- `MVSEP_API_KEY` — API mode only
- `ADMIN_SECRET` — PIN for admin panel

## Documentation

Detailed architecture docs, ADRs, and project history are in `journals/`.
