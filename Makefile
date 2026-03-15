.PHONY: help up-gpu up-api down logs ps build-gpu build-api test clean

# Default compose files
BASE   := docker-compose.yml
GPU    := docker-compose.gpu.yml
API    := docker-compose.api.yml
PROD   := docker-compose.prod.yml

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
	docker compose -f $(BASE) -f $(GPU) -f $(API) down 2>/dev/null; true

down-v: ## Stop, remove containers AND volumes (full reset)
	docker compose -f $(BASE) -f $(GPU) -f $(API) down -v 2>/dev/null; true

logs: ## Tail logs from all services
	docker compose -f $(BASE) logs -f --tail=50

logs-worker: ## Tail worker logs only
	docker compose -f $(BASE) logs -f --tail=100 worker

logs-backend: ## Tail backend logs only
	docker compose -f $(BASE) logs -f --tail=100 backend

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
