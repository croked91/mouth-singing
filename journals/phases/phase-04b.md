## Фаза 4b: Unit-тесты слоя данных

### Входные артефакты
- Результат Фазы 4a (модели и репозитории в shared/)
- `journals/ARCHITECTURE.md` — раздел 4 «Даталогическая модель» (для проверки корректности CRUD)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `polyglot-test-engineer` задачу на написание unit-тестов для всего слоя данных из Фазы 4a. Тесты должны быть автономными, без внешних зависимостей.

#### Подагент `polyglot-test-engineer`
Пишет исчерпывающие тесты:

1. **Настройка pytest**: `pytest` + `pytest-asyncio`, conftest.py с фикстурами:
   - In-memory SQLite (`aiosqlite` с `:memory:`)
   - Инициализация таблиц из `backend/app/db/init.sql`
   - Mock QDrant (через `qdrant_client.QdrantClient(":memory:"`) или `MagicMock`)

2. **Тесты SQLite CRUD** для каждой таблицы:
   - `tracks`: create → get_by_id → update → list_popular → search_fts
   - `sessions`: create → get_by_id → terminate → get_active_by_room
   - `participants`: create → get_by_session → update_portrait
   - `queue_entries`: create → get_by_session → update_status → delete → reorder → get_current
   - `play_history`: create → get_by_participant → get_by_session
   - `job_queue`: create → poll_pending → lock → complete → fail (retry logic)

3. **Тесты FTS5**: insert track → поиск по artist → поиск по title → поиск по lyrics → проверка триггеров (update → re-indexing)

4. **Тесты QDrant-репозитория**: upsert → search (top-k, фильтры) → delete → batch_upsert. Проверка что фильтрация по payload работает.

5. **Тесты Pydantic-моделей**: сериализация/десериализация, валидация обязательных полей, дефолтные значения.

#### Пользователь
Проверяет покрытие тестов, подтверждает или вносит замечания.

### Выходные артефакты
- `tests/` директория с конфигурацией pytest
- Тесты CRUD для каждой таблицы SQLite
- Тесты FTS5
- Тесты QDrant-репозитория
- Тесты Pydantic-моделей
- `pytest` проходит: 100% покрытие CRUD-операций
- Коммит

