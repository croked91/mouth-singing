# Журнал проекта: Караоке-приложение

## Статус проекта
**Текущая фаза:** 3 — Скаффолдинг проекта и инфраструктура
**Дата начала:** 2026-02-22

## Фаза 1: Анализ и проектирование
**Коммит:** 43bb7dc

### Задачи фазы:
- [x] Анализ мастер-промпта и experiments
- [x] Уточняющие вопросы пользователю
- [x] Архитектура (C4, диаграммы, модели данных)
- [x] Промпты для FigGPT (дизайн UI) — 8 файлов, все UI-тексты на русском
- [x] ADR: 9 решений (включая VPN для РФ и локальный дамп lrc-lib)
- [x] Согласование с пользователем
- [x] Коммит

### Хронология:
- **2026-02-22**: Начало проекта. Инициализация git. Запущен анализ experiments.
- **2026-02-22**: Анализ experiments завершён. Выявлены расхождения с мастер-промптом.
- **2026-02-22**: Согласованы ключевые решения с пользователем (Python, Sonoix+WhisperX, QDrant+SQLite)
- **2026-02-22**: Зафиксировано 9 ADR. Подагенты создали архитектуру (1720+ строк) и 8 промптов FigGPT.
- **2026-02-22**: Добавлены ADR-008 (VPN/РФ) и ADR-009 (lrc-lib дамп).
- **2026-02-22**: Все UI-тексты в промптах локализованы на русский.
- **2026-02-22**: Фаза 1 принята. Коммит 43bb7dc.

## Фаза 2: Пофазный план реализации

### Задачи фазы:
- [x] Составление плана (17 фаз реализации: 3-15, с подфазами a/b)
- [x] Ревью архитектором: зависимости, размеры фаз, риски
- [x] Согласование с пользователем
- [x] Коммит

### Хронология:
- **2026-02-22**: Подагент-архитектор провёл ревью плана. Ключевые правки:
  - Разбита Фаза 7 на 7a (UVR+worker) и 7b (Sonoix+VideoGen+SSE)
  - Добавлен shared/ пакет в Фазу 3 (исключение дублирования)
  - Добавлены start/finish эндпоинты в Фазу 5
  - Добавлено обновление portrait_vector в Фазу 8
  - VPN для Sonoix включён в Фазу 7b
- **2026-02-22**: По запросу пользователя разбиты крупные фазы: 4→4a/4b, 8→8a/8b, 10→10a/10b. Итого 17 фаз.
- **2026-02-23**: Переписаны описания фаз в формате мастер-промпта: входные артефакты, задачи по ролям, выходные артефакты. Фазы вынесены в journals/phases/.
- **2026-02-23**: Фаза 2 принята пользователем. Коммит 2694b95.

## Фаза 3: Скаффолдинг проекта и инфраструктура

### Задачи фазы:
- [x] docker-compose.yml (QDrant + backend)
- [x] shared/ Python-пакет (karaoke_shared)
- [x] backend/ FastAPI скелет (config, dependencies, lifespan, CORS)
- [x] SQLite init.sql (6 таблиц + FTS5 + триггеры + 11 индексов)
- [x] QDrant init (3 коллекции с payload индексами, graceful degradation)
- [x] structlog JSON логирование
- [x] backend/Dockerfile (Python 3.12, ffmpeg, curl)
- [x] .env.example, .gitignore
- [x] Smoke-тест (Docker Compose)
- [x] Согласование с пользователем
- [x] Коммит (36c6f44)

### Хронология:
- **2026-02-23**: python-developer создал скелет. Исправлен build-backend (setuptools.build_meta). Добавлен package-data для init.sql. QDrant init сделан graceful (degraded mode без QDrant).
- **2026-02-23**: Локальная проверка: backend стартует, SQLite инициализируется с 6 таблицами + FTS5, health endpoint возвращает корректный статус.
- **2026-02-23**: Docker Compose: исправлен healthcheck QDrant (curl/wget отсутствуют в образе → bash /dev/tcp). Оба контейнера healthy, GET /health → `{"status":"ok","sqlite":"ok","qdrant":"ok"}`.

## Фаза 4a: Pydantic-модели и репозитории

### Задачи фазы:
- [x] Pydantic-модели (6 файлов: session, track, queue, job, recommendation, play_history)
- [x] SQLiteRepository (25 async методов, CRUD для 6 таблиц)
- [x] QDrantRepository (upsert, search, delete, batch_upsert)
- [x] Обновление dependencies.py (get_sqlite_repo, get_qdrant_repo)
- [x] Архитектурное ревью (PASS WITH NOTES, все замечания исправлены)
- [x] Smoke-тест (CRUD, FTS, error handling)
- [x] Согласование с пользователем
- [x] Коммит (4639b3f)

### Хронология:
- **2026-02-23**: python-developer создал 6 файлов моделей и 2 репозитория. 17 реэкспортов в __init__.py. Все импорты работают.
- **2026-02-23**: software-architect провёл ревью. Вердикт: PASS WITH NOTES. Исправлено:
  - Race condition в create_queue_entry → атомарный INSERT с subquery
  - get_db/get_sqlite_repo: убраны ложные AsyncGenerator → plain функции
  - assert → RuntimeError (6 мест)
  - row_factory убран из конструктора (уже ставится в init_db)
  - fail_job: очистка locked_by/locked_at при retry
  - search_fts: try/except для невалидного FTS5 синтаксиса
  - Убран неиспользуемый _deserialize_json_fields
  - PointIdsList: import перенесён на уровень модуля
- **2026-02-23**: Smoke-тест пройден: Session, Participant, Track CRUD работает, FTS с невалидным запросом не падает.

## Фаза 4b: Unit-тесты слоя данных

### Задачи фазы:
- [x] conftest.py (in-memory SQLite + QDrant fixtures)
- [x] test_models.py (59 тестов, все Pydantic-модели)
- [x] test_sqlite_repo.py (47 тестов, CRUD для 6 таблиц)
- [x] test_qdrant_repo.py (13 тестов, upsert/search/delete/batch)
- [x] Баг-фиксы по результатам тестирования (2 бага)
- [x] 119/119 тестов пройдены
- [ ] Коммит

### Хронология:
- **2026-02-23**: polyglot-test-engineer написал 122 теста (111 pass, 11 xfail). Выявлено 2 бага:
  - Bug #1: отсутствующий _job_from_row() в SQLiteRepository
  - Bug #2: QDrant client.search() удалён в qdrant-client 1.7+, нужен query_points()
- **2026-02-23**: Оба бага исправлены. xfail-тесты переведены в обычные. 119/119 pass, 0 fail.
