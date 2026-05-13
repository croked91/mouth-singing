# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Karaoke web application for club rooms. Kiosk-style (no user registration). ~17,000 tracks in catalog. Supports user MP3 uploads with auto-generated karaoke (vocal separation, transcription, syllable-level alignment).

GPU mode only: Direct PyTorch BS-Roformer (vocal separation) + Whisper (PyTorch Transformers) + CTC alignment (requires NVIDIA GPU).

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
make test-alignment                     # TorchCTCAligner CPU regression (~2s)
make test-alignment-regen               # Regenerate alignment fixtures via worker container (needs `make up-gpu`)
python -m pytest tests/test_api_tracks.py -v          # Single file
python -m pytest tests/test_api_tracks.py::test_name  # Single test
# tests/worker/ uses its own conftest — for that subdir add: --confcutdir=tests/worker

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
- **RabbitMQ**: 4 exchanges — `jobs` (direct), `job.progress` (fanout, non-durable), `rec` (direct), `dlq` (direct). Main queues: `jobs.process` (with `x-max-priority=10`), `rec.index`, `rec.indexed` (rec-service publishes after QDrant upsert with routing_key=`indexed`; backend consumes to set `tracks.qdrant_synced=1`). DLQ: `jobs.dlq`, `rec.dlq` (bound via `x-dead-letter-exchange`). SSE clients open exclusive auto-delete queues on `job.progress`. Connection uses `aio_pika.connect_robust` (auto-reconnect)
- **QDrant**: `audio_features` (45-d librosa vectors), `lyrics_embeddings` (384-d sentence-transformer vectors)
- **Nginx**: Reverse proxy, SSE passthrough, frontend static files

### Worker pipeline steps

Processing order (defined in `PipelineStep` enum):
SEPARATING → VAD → TRANSCRIBING → SEARCHING_LYRICS → ALIGNING → LINE_BREAKING

- **SEPARATING**: Direct PyTorch BS-Roformer inference (`worker/gpu/uvr_separator.py`). Model loaded in FP16, batched chunk processing with overlap-add on GPU, autocast enabled. Vocals output as 16kHz mono WAV (ready for VAD/Whisper). Instrumental WAV→MP3 conversion (ffmpeg, matching original bitrate via ffprobe) and S3 upload run as background asyncio task parallel to VAD+Whisper.
- **Back-vocal split** (sub-step inside SEPARATING, no separate `PipelineStep`): `BackVocalSeparator` (`worker/gpu/back_vocal_separator.py`) runs a second Mel-Band RoFormer pass (`mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956`) that splits the UVR vocals into **lead** and **backing** stems. **VAD and TRANSCRIBING run on the full vocals** (backing harmonies actually help Whisper identify the track — a lead-only experiment produced ~44% shorter transcripts and broke lyrics matching). **Only ALIGNING and LINE_BREAKING consume the lead stem** (see `worker/gpu/gpu_pipeline.py:158-237`). Falls back to full vocals for the lead stem if the separator fails.
- **VAD**: RMS energy detection via PyTorch CPU (`worker/common/vad_processor.py`). No librosa dependency — uses `torch.unfold` + threshold. Audio loaded via soundfile, resampled via `torchaudio.functional.resample` if needed.
- **TRANSCRIBING**: HuggingFace Transformers Whisper (PyTorch-native, not CTranslate2). Default model `openai/whisper-tiny`. `WhisperTranscriber.cleanup()` is invoked from the pipeline after every job (`del model` + `torch.cuda.empty_cache()`), so the next job re-loads weights from the local HF cache. A `warmup()` method exists but is never called — CUDA-kernel JIT happens on the first real job. The `whisper_compute_type="float16"` setting in `worker/app/config.py` is **not** a CTranslate2 flag — it just selects `torch.float16` vs `torch.float32`; the stale "faster-whisper" comments in `config.py` and `gpu_pipeline.py` docstring are leftovers from a prior backend.
- **SEARCHING_LYRICS**: Provider chain in `worker/common/lyrics/` — actually wired in `worker/app/main.py:88-100` are **only three providers**: `GeniusProvider` (if `GENIUS_TOKEN` is set), `LRCLibProvider`, `LyricsOvhProvider`. `ChartLyricsProvider` and `SimpMusicProvider` classes exist in `worker/common/lyrics/providers/` but are not imported and not instantiated (dead code). When the provider chain fails, the pipeline falls back to `LyricsAgent` (`worker/common/lyrics_agent.py`) — an agentic loop using DeepSeek + SearXNG (local instance in `searxng/`) / Yandex Search; if even the agent fails, raw ASR text is used (`lyrics_source="asr_fallback"`). Chain logic in `provider_chain.py`, candidate scoring/matching in `worker/common/lyrics/matching/`.
- **ALIGNING**: MMS-300M CTC forced aligner (`MahmoudAshraf/mms-300m-1130-forced-aligner` via HuggingFace transformers `Wav2Vec2ForCTC`) using `torchaudio.functional.forced_align` + `merge_tokens` on GPU. Includes Silero VAD pre-trim of intro noise plus three optional post-pass RMS adjustments for line-start anchoring, word-end drift trim, and word-end sustain extension (all toggleable in `TorchCTCAligner.__init__`).
- **LINE_BREAKING**: Injects `\n` markers into the syllable stream (`shared/karaoke_shared/utils/line_breaker.py`, called from `worker/gpu/gpu_pipeline.py`). Auto-selects between *gap mode* (break at inter-syllable gaps above a track-adaptive threshold) and *beat mode* (`librosa.beat.beat_track` on the vocal audio — used when too few large gaps, typical for rap). Skipped when timings already carry `\n` from LRC.

Worker creates track at finalization (deferred track creation — no track record until pipeline completes). Then publishes to Rec Service.

### Key patterns

- All Python config via pydantic-settings, loaded from env vars (no prefix). Defaults in `backend/app/config.py`, `worker/app/config.py`, `rec-service/app/config.py`
- Each component has its own `pyproject.toml` with dependencies (backend/, worker/, shared/, rec-service/). Install in editable mode: `pip install -e shared/ -e backend/` etc.
- Structured logging via structlog (JSON output). Use `structlog.get_logger(__name__)`
- Root `conftest.py` adds worker/, shared/, backend/ to sys.path for test imports
- pytest uses `asyncio_mode = auto` (no need for `@pytest.mark.asyncio`)
- Shared constants (status enums, collection names, pipeline steps) live in `shared/karaoke_shared/constants.py`
- DB schema in `backend/app/db/init_pg.sql`, applied at startup via `init_pg()`. No migration tool
- No foreign keys in PostgreSQL (denormalized by design)
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

`scripts/` contains bulk operations: catalog seeding/clustering, QDrant migrations, audio feature reindexing, artist image fetching, ML model pre-download (`bootstrap_worker.py`), MinIO import, alignment fixture generation, etc. `ls scripts/` for the full list.

### A/B alignment

`a-b-alignment/` holds reference MP3s used for manual alignment quality comparisons (not part of the test suite).

## Environment

Required env vars (see `.env.example`):
- `DEEPSEEK_API_KEY`, `YANDEX_SEARCH_API_KEY` — lyrics search
- `PG_PASSWORD` — PostgreSQL password
- `S3_ACCESS_KEY`, `S3_SECRET_KEY` — MinIO/S3 credentials
- `RMQ_USER`, `RMQ_PASS` — RabbitMQ credentials
- `ADMIN_SECRET` — PIN for admin panel

## Documentation

`journals/` (only these files are current — prior journals were deleted because they contained inaccurate claims; do not resurrect them from git history):
- `WORKER_FACTS.md` — exhaustive map of the worker pipeline with `file:line` citations from the live code. Source of truth for any worker-related fact.
- `THESIS_PLAN.md` — HSE master-thesis formatting requirements.
- `BUSINESS_CUSTOMER.md` — business-customer notes.
