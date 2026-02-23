## Фаза 5: Сессии, участники, очередь

### Входные артефакты
- Результат Фаз 3, 4a, 4b (скелет + слой данных + тесты)
- `journals/ARCHITECTURE.md` — раздел 3.1 «SessionService», раздел 3.2 «QueueService», раздел 6.1 «Создание сессии», раздел 10 «API-контракт» (Sessions, Queue)
- `journals/ADR.md` — ADR-005 (сессионная модель без регистрации)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `python-developer` задачу на реализацию SessionService и QueueService с API-роутерами. Это ядро бизнес-логики приложения — управление сессиями кабинки и очередью исполнения. После реализации запускаешь `polyglot-test-engineer` для тестирования API.

#### Подагент `python-developer`
Реализует бизнес-логику сессий и очереди:

1. **SessionService** (`backend/app/services/session_service.py`):
   - `create_session(room_id: str) -> Session` — создаёт сессию кабинки
   - `get_session(session_id: str) -> Session` — с участниками
   - `terminate_session(session_id: str)` — admin action, ставит status=terminated, очищает очередь
   - `add_participant(session_id, name: str | None) -> Participant` — если name=None, генерирует никнейм
   - `get_participants(session_id) -> list[Participant]`

2. **Генератор никнеймов** (`backend/app/utils/nicknames.py`):
   - Комбинации «прилагательное + существительное» на русском
   - Примеры: «ЛихойКотяра», «ВесёлыйЁжик», «ДерзкийПингвин»
   - ~50 прилагательных + ~50 существительных = 2500 комбинаций
   - Не обидные, смешные, подходящие для караоке-атмосферы
   - Проверка уникальности внутри сессии

3. **QueueService** (`backend/app/services/queue_service.py`):
   - `add_to_queue(session_id, participant_id, track_id) -> QueueEntry` — добавляет в конец
   - `remove_from_queue(entry_id)` — удаляет запись
   - `skip_turn(entry_id) -> QueueEntry` — перемещает в конец очереди (НЕ удаляет, рекомендации сохраняются)
   - `get_current(session_id) -> QueueEntry | None` — текущий исполнитель
   - `get_queue(session_id) -> list[QueueEntry]` — текущий + upcoming
   - `start_playing(entry_id)` — ставит status=playing, started_at=now
   - `finish_playing(entry_id)` — ставит status=done, finished_at=now, создаёт play_history, инкрементирует play_count трека, инкрементирует tracks_played участника, продвигает очередь к следующему

4. **API-роутеры** (в соответствии с API-контрактом из ARCHITECTURE.md раздел 10):
   - `backend/app/api/v1/sessions.py`:
     - `POST /sessions` → 201
     - `GET /sessions/{session_id}` → 200
     - `POST /sessions/{session_id}/participants` → 201
     - `DELETE /sessions/{session_id}` → 204 (требует `X-Admin-Secret`)
   - `backend/app/api/v1/queue.py`:
     - `GET /sessions/{session_id}/queue` → 200
     - `POST /queue` → 201
     - `POST /queue/{entry_id}/skip` → 200
     - `POST /queue/{entry_id}/start` → 200
     - `POST /queue/{entry_id}/finish` → 200
     - `DELETE /queue/{entry_id}` → 204
   - `backend/app/api/router.py` — обновить, подключить новые роутеры

5. **Middleware для admin**: проверка заголовка `X-Admin-Secret` для admin-эндпоинтов.

#### Подагент `polyglot-test-engineer`
Интеграционные тесты API:
- Создание сессии → добавление 3 участников (имя + авто-никнейм) → проверка списка
- Управление очередью: добавить 3 трека → get_current → skip → проверка нового порядка
- `POST /queue/{entry_id}/start` → проверка status=playing
- `POST /queue/{entry_id}/finish` → проверка play_history, play_count, следующий участник
- Пропуск очереди: participant перемещается в конец, не удаляется
- Admin: `DELETE /sessions/{id}` с правильным/неправильным X-Admin-Secret

#### Пользователь
Тестирует через curl или HTTP-клиент: создание сессии, добавление участников, управление очередью. Подтверждает или вносит замечания.

### Выходные артефакты
- `SessionService` и `QueueService` с полной бизнес-логикой
- API-роутеры для `/sessions` и `/queue`
- Генератор русскоязычных никнеймов
- Интеграционные тесты API
- curl: полный цикл сессия → участники → очередь → start → finish
- Коммит

