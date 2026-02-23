## Фаза 14: Docker Compose + Nginx + Deploy

### Входные артефакты
- Результат всех предыдущих фаз (backend, worker, frontend, bootstrap — каждый со своим Dockerfile)
- `journals/ARCHITECTURE.md` — раздел 9 «Docker Compose архитектура», nginx.conf
- `journals/ADR.md` — ADR-006 (Docker Compose), ADR-008 (VPN)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `python-developer` задачу на финализацию Docker Compose — объединение всех сервисов в одну команду запуска. Плюс nginx как reverse proxy. Всё должно подниматься через `cp .env.example .env && docker compose up -d`.

#### Подагент `python-developer`
Собирает всё вместе:

1. **docker-compose.yml** (финальный):
   - Сервисы: `qdrant`, `backend`, `worker`, `frontend`, `nginx`
   - Health checks для каждого сервиса
   - Зависимости: qdrant ← backend ← worker, backend ← frontend ← nginx
   - Volumes: `sqlite_data`, `qdrant_data`, `media_data`, `models_data`
   - Networks: единая `karaoke_net`

2. **docker-compose.override.yml** (для разработки):
   - Hot-reload volumes для backend и frontend
   - Debug logging level
   - Открытые порты для всех сервисов

3. **nginx/nginx.conf**:
   - Reverse proxy: `/api/*` → backend:8000
   - SSE: `/api/jobs/*` с отключённой буферизацией (proxy_buffering off)
   - Статика фронтенда: `/` → frontend static files
   - Медиафайлы: `/media/*` через X-Accel-Redirect (nginx отдаёт напрямую)
   - `client_max_body_size 50M` для загрузки MP3
   - SPA fallback: все неизвестные пути → `index.html`

4. **VPN (опционально)** (`docker-compose.override.vpn.yml`):
   - WireGuard-контейнер для роутинга трафика worker → Sonoix API
   - Или документация для настройки HTTP_PROXY на уровне хоста

5. **.env.example** с подробными комментариями для всех переменных:
   - `ADMIN_SECRET`, `SONOIX_API_KEY`, `SONOIX_API_URL`
   - `DATABASE_URL`, `QDRANT_HOST`, `QDRANT_PORT`
   - `MEDIA_ROOT`, `MODEL_CACHE_DIR`
   - `LOG_LEVEL`, `WORKER_POLL_INTERVAL`, `WORKER_MAX_CONCURRENT`
   - `HTTP_PROXY` / `HTTPS_PROXY` (для VPN)

#### Подагент `polyglot-test-engineer`
Smoke-тест полного деплоя:
- `cp .env.example .env && docker compose up -d` поднимает всё
- `GET http://localhost/` → фронтенд загружается
- `GET http://localhost/api/v1/health` → 200 OK
- SSE работает через nginx
- Медиафайлы отдаются через nginx
- Все сервисы healthy (`docker compose ps`)

#### Пользователь
Проверяет полный деплой на чистой машине. `docker compose up -d` → приложение доступно. Подтверждает или вносит замечания.

### Выходные артефакты
- Финальный `docker-compose.yml` со всеми сервисами
- `docker-compose.override.yml` для dev
- `nginx/nginx.conf` (reverse proxy, SSE, медиа, SPA)
- `.env.example` с документацией
- Опциональный VPN-конфиг
- `docker compose up -d` поднимает всё с нуля
- Приложение доступно на http://localhost
- Коммит

