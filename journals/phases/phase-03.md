## Фаза 3: Скаффолдинг проекта и инфраструктура

### Входные артефакты
- `journals/ARCHITECTURE.md` — раздел 8 «Структура проекта», раздел 9 «Docker Compose архитектура»
- `journals/ADR.md` — ADR-001 (Python+FastAPI), ADR-003 (QDrant+SQLite), ADR-006 (Docker Compose)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь подагенту `python-developer` задачу на создание скелета проекта. Контролируешь, что структура директорий соответствует разделу 8 ARCHITECTURE.md. После создания скелета запускаешь `polyglot-test-engineer` для smoke-теста health endpoint.

#### Подагент `python-developer`
Создаёт полную структуру проекта в соответствии с разделом 8 ARCHITECTURE.md:

1. **docker-compose.yml**: сервисы `qdrant` и `backend` (заглушка). QDrant v1.8, healthcheck. Backend зависит от QDrant.
2. **shared/ Python-пакет**: `shared/karaoke_shared/` с `pyproject.toml`, `__init__.py` — пустой пакет, который будет расти в следующих фазах.
3. **backend/**: FastAPI скелет:
   - `backend/app/main.py` — FastAPI app, middleware (CORS), подключение роутеров
   - `backend/app/config.py` — Pydantic Settings: `DATABASE_URL`, `QDRANT_HOST`, `QDRANT_PORT`, `MEDIA_ROOT`, `ADMIN_SECRET`, `LOG_LEVEL`
   - `backend/app/dependencies.py` — FastAPI Depends для db connection и qdrant client
   - `backend/app/api/v1/health.py` — `GET /health` → 200, проверка SQLite и QDrant
4. **backend/app/db/init.sql**: все 6 таблиц из раздела 4.1 ARCHITECTURE.md (`tracks`, `sessions`, `participants`, `queue_entries`, `play_history`, `job_queue`) + FTS5 виртуальная таблица `tracks_fts` + триггеры синхронизации FTS + все индексы.
5. **QDrant инициализация**: при старте backend создаёт 3 коллекции, если их нет: `audio_features` (dim=45, cosine), `lyrics_embeddings` (dim=384, cosine), `transitions` (dim=45, cosine). Payload индексы: `status`, `language`, `source`.
6. **Файлы конфигурации**: `.env.example` (все переменные с комментариями), `.gitignore` (data/, .env, __pycache__, *.pyc, .venv/), корневой `pyproject.toml`.
7. **Логирование**: structlog с JSON-форматом, настройка в `backend/app/logging_config.py`.
8. **backend/Dockerfile**: Python 3.12, установка ffmpeg, копирование кода, CMD uvicorn.

#### Подагент `polyglot-test-engineer`
Пишет smoke-тест:
- `docker compose up -d` поднимает QDrant + backend
- `GET /health` → 200, тело содержит `{"status": "ok", "sqlite": "ok", "qdrant": "ok"}`

#### Пользователь
Проверяет, что `docker compose up` работает, смотрит структуру директорий, подтверждает или вносит замечания.

### Выходные артефакты
- Полная структура директорий проекта (backend/, shared/, docker-compose.yml, .env.example и т.д.)
- SQLite база инициализируется с 6 таблицами при старте
- QDrant содержит 3 пустые коллекции
- `docker compose up` поднимает QDrant + backend
- `GET /health` → 200
- `docker build ./backend` проходит без ошибок
- Коммит

