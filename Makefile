.PHONY: help up-gpu up-api down logs ps build-gpu build-api test clean

# Default compose files
BASE   := docker-compose.yml
GPU    := docker-compose.gpu.yml
API    := docker-compose.api.yml
PROD   := docker-compose.prod.yml
LOCAL  := docker-compose.local.yml

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Local development (named volumes, no GPU required for up-api)
# ---------------------------------------------------------------------------

up-gpu: .env ## Start all services with GPU worker (requires NVIDIA GPU)
	docker compose -f $(BASE) -f $(GPU) up -d --build
	@echo ""
	@echo "=== Karaoke (GPU mode) ==="
	@echo "Frontend: http://localhost:$${APP_PORT:-80}"
	@echo "Backend:  http://localhost:8000/health (internal)"
	@echo "QDrant:   http://localhost:6333/dashboard"
	@echo ""
	@echo "NOTE: First start downloads ~1.5 GB of ML models. Watch: make logs-worker"

up-api: .env ## Start all services with API worker (CPU-only, requires API keys)
	docker compose -f $(BASE) -f $(API) up -d --build
	@echo ""
	@echo "=== Karaoke (API mode) ==="
	@echo "Frontend: http://localhost:$${APP_PORT:-80}"
	@echo ""
	@echo "NOTE: First start downloads ~1.5 GB of ML models. Watch: make logs-worker"

.env:
	@echo "ERROR: .env file not found. Copy and fill in:" && \
	echo "  cp .env.example .env" && \
	echo "  # then edit .env and set OPENAI_API_KEY, GENIUS_TOKEN, etc." && \
	exit 1

# ---------------------------------------------------------------------------
# Production (bind mounts, server paths)
# ---------------------------------------------------------------------------

up-gpu-prod: ## Start GPU mode with production bind mounts
	docker compose -f $(BASE) -f $(GPU) -f $(PROD) up -d --build

up-api-prod: ## Start API mode with production bind mounts
	docker compose -f $(BASE) -f $(API) -f $(PROD) up -d --build

# ---------------------------------------------------------------------------
# Local data (bind mounts to backup volumes)
# ---------------------------------------------------------------------------

up-gpu-local: .env ## Start GPU mode with local backup data
	docker compose -f $(BASE) -f $(GPU) -f $(LOCAL) up -d --build

up-api-local: .env ## Start API mode with local backup data
	docker compose -f $(BASE) -f $(API) -f $(LOCAL) up -d --build

# ---------------------------------------------------------------------------
# Build only (no start)
# ---------------------------------------------------------------------------

build-gpu: ## Build all images for GPU mode
	docker compose -f $(BASE) -f $(GPU) build

build-api: ## Build all images for API mode
	docker compose -f $(BASE) -f $(API) build

# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

down: ## Stop and remove all containers
	docker compose -f $(BASE) -f $(GPU) -f $(LOCAL) down 2>/dev/null; \
	docker compose -f $(BASE) -f $(GPU) down 2>/dev/null; \
	docker compose -f $(BASE) -f $(API) down 2>/dev/null; true

down-v: ## Stop, remove containers AND volumes (full reset)
	docker compose -f $(BASE) -f $(GPU) down -v 2>/dev/null; \
	docker compose -f $(BASE) -f $(API) down -v 2>/dev/null; true

logs: ## Tail logs from all services
	docker compose -f $(BASE) -f $(GPU) -f $(API) logs -f --tail=50 2>/dev/null || docker compose -f $(BASE) logs -f --tail=50

logs-worker: ## Tail worker logs only
	docker compose -f $(BASE) -f $(GPU) logs -f --tail=100 worker 2>/dev/null || docker compose -f $(BASE) -f $(API) logs -f --tail=100 worker

logs-backend: ## Tail backend logs only
	docker logs -f --tail=100 karaoke_backend

ps: ## Show running containers
	docker compose -f $(BASE) ps

health: ## Check health of all services
	@echo "--- QDrant ---"
	@curl -sf http://localhost:6333/healthz && echo " OK" || echo " FAIL"
	@echo "--- Backend ---"
	@curl -sf http://localhost:8000/health | python3 -m json.tool 2>/dev/null || echo " FAIL"
	@echo "--- Frontend ---"
	@curl -sf -o /dev/null http://localhost:80/ && echo " OK" || echo " FAIL"

# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

migrate-qdrant-clusters: ## Backfill rec_cluster_id into QDrant payloads (run BEFORE deploying new backend)
	@echo "=== Migrating QDrant: adding rec_cluster_id to payloads ==="
	docker exec karaoke_backend python -c "\
	import sqlite3; \
	from collections import defaultdict; \
	from qdrant_client import QdrantClient; \
	conn = sqlite3.connect('/data/sqlite/karaoke.db'); \
	conn.row_factory = sqlite3.Row; \
	rows = conn.execute(\"SELECT id, rec_cluster_id FROM tracks WHERE rec_cluster_id IS NOT NULL AND status = 'ready'\").fetchall(); \
	conn.close(); \
	by_cluster = defaultdict(list); \
	[by_cluster[r['rec_cluster_id']].append(r['id']) for r in rows]; \
	print(f'{len(rows)} tracks, {len(by_cluster)} clusters'); \
	client = QdrantClient(host='qdrant', port=6333, timeout=300, check_compatibility=False); \
	[client.set_payload(collection_name=coll, payload={'rec_cluster_id': cid}, points=tids[i:i+100]) for cid, tids in by_cluster.items() for coll in ['audio_features','lyrics_embeddings'] for i in range(0, len(tids), 100)]; \
	[client.create_payload_index(collection_name=coll, field_name='rec_cluster_id', field_schema='integer') for coll in ['audio_features','lyrics_embeddings']]; \
	print('Done.')"
	@echo ""
	@echo "Migration complete. Now rebuild backend: make up-gpu or make up-api"

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test: ## Run all unit tests
	python -m pytest tests/ -v

test-quick: ## Run tests (fast, skip slow)
	python -m pytest tests/ -x -q --ignore=tests/worker

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean: down ## Stop containers and prune images
	docker image prune -f --filter "label=com.docker.compose.project=karaoke"
