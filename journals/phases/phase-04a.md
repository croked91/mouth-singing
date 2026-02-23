## Фаза 4a: Pydantic-модели и репозитории

### Входные артефакты
- Результат Фазы 3 (работающий скелет проекта)
- `journals/ARCHITECTURE.md` — раздел 4 «Даталогическая модель» (таблицы SQLite, коллекции QDrant), раздел 3 «Модули» (API каждого сервиса)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `python-developer` задачу на создание слоя данных. Этот слой будет переиспользоваться backend, worker и bootstrap через пакет `shared/`. После реализации запускаешь `software-architect` для ревью кода.

#### Подагент `python-developer`
Реализует слой данных в пакете `shared/karaoke_shared/`:

1. **Pydantic-модели** (`shared/karaoke_shared/models/`):
   - `session.py`: `Session`, `SessionCreate`, `Participant`, `ParticipantCreate`
   - `track.py`: `Track`, `TrackCreate`, `TrackUpdate`, `SyllableTiming`
   - `queue.py`: `QueueEntry`, `QueueEntryCreate`
   - `job.py`: `Job`, `JobCreate`, `JobUpdate`
   - `recommendation.py`: `RecommendationResponse`, `RecommendationStrategy` (enum)
   - `play_history.py`: `PlayHistoryEntry`

   Все модели должны соответствовать схемам таблиц из ARCHITECTURE.md раздел 4.1. UUID генерируется через `uuid4()`. Даты — ISO8601 строки.

2. **SQLite-репозиторий** (`shared/karaoke_shared/repositories/sqlite_repository.py`):
   - Асинхронный через `aiosqlite`
   - CRUD-методы для каждой из 6 таблиц:
     - `tracks`: create, get_by_id, update, list_popular(limit), search_fts(query, limit, offset)
     - `sessions`: create, get_by_id, terminate, get_active_by_room
     - `participants`: create, get_by_session, get_by_id, update_portrait
     - `queue_entries`: create, get_by_session, update_status, delete, reorder, get_current
     - `play_history`: create, get_by_participant(limit), get_by_session
     - `job_queue`: create, poll_pending(limit), lock, complete, fail, get_by_id
   - Pessimistic locking для job_queue (SELECT ... UPDATE с locked_by)

3. **QDrant-репозиторий** (`shared/karaoke_shared/repositories/qdrant_repository.py`):
   - Через `qdrant-client` (async)
   - Для каждой из 3 коллекций (`audio_features`, `lyrics_embeddings`, `transitions`):
     - `upsert(collection, id, vector, payload)`
     - `search(collection, vector, limit, filters)` → list of (id, score, payload)
     - `delete(collection, id)`
     - `batch_upsert(collection, points)` — для bootstrap
   - Фильтрация по payload полям (status, language, source)

4. **Обновление `backend/app/dependencies.py`**: FastAPI Depends для SQLiteRepository и QDrantRepository. Connection pool / singleton pattern.

#### Подагент `software-architect`
Ревью кода: проверяет отсутствие дублирования, корректность абстракций, соответствие ARCHITECTURE.md.

#### Пользователь
Проверяет, что модели соответствуют ожиданиям, подтверждает или вносит замечания.

### Выходные артефакты
- `shared/karaoke_shared/models/` — 6 файлов с Pydantic-моделями
- `shared/karaoke_shared/repositories/sqlite_repository.py` — полный CRUD для 6 таблиц
- `shared/karaoke_shared/repositories/qdrant_repository.py` — upsert/search/delete для 3 коллекций
- `backend/app/dependencies.py` — обновлён с новыми Depends
- Код проходит линтер (ruff)
- Коммит

