# Журнал проекта: Караоке-приложение

## Статус проекта
**Текущая фаза:** 8a — Извлечение фичей и эмбеддингов (ожидает начала)
**Дата начала:** 2026-02-22
**Последний коммит:** (реструктуризация v2/)
**Структура:** Реализация в `v2/`, документация в корне

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
- [x] Коммит (cf1f02c)

### Хронология:
- **2026-02-23**: polyglot-test-engineer написал 122 теста (111 pass, 11 xfail). Выявлено 2 бага:
  - Bug #1: отсутствующий _job_from_row() в SQLiteRepository
  - Bug #2: QDrant client.search() удалён в qdrant-client 1.7+, нужен query_points()
- **2026-02-23**: Оба бага исправлены. xfail-тесты переведены в обычные. 119/119 pass, 0 fail.

## Фаза 5: Сессии, участники, очередь

### Задачи фазы:
- [x] SessionService (create, get, terminate, add_participant)
- [x] QueueService (add, remove, skip, start, finish)
- [x] Генератор никнеймов (50×50=2500 комбинаций, русскоязычные)
- [x] API-роутеры sessions и queue (10 эндпоинтов)
- [x] Admin-авторизация (X-Admin-Secret)
- [x] Интеграционные тесты API (41 тест)
- [x] 160/160 тестов пройдены (119 старых + 41 новых)
- [x] Согласование с пользователем
- [x] Коммит (b6e09bd)

### Хронология:
- **2026-02-23**: python-developer создал SessionService, QueueService, генератор никнеймов, роутеры sessions и queue. Lint clean, app starts.
- **2026-02-23**: polyglot-test-engineer написал 41 интеграционный тест (17 sessions, 24 queue). Lifespan bypass pattern для in-memory тестирования. 160/160 pass.
- **2026-02-23**: Фаза 5 принята пользователем. Коммит b6e09bd.

## Фаза 6: Каталог треков, поиск и стриминг
**Коммит:** 725f1d3

### Задачи фазы:
- [x] TrackService (upload MP3, get, list_popular, enqueue_processing)
- [x] SearchService (гибридный FTS5 + semantic fallback, suggest autocomplete)
- [x] Playback-роутер (HTTP Range Request стриминг с path confinement)
- [x] API tracks (5 эндпоинтов)
- [x] Embedder (опциональная загрузка sentence-transformers при старте)
- [x] Дополнительные методы SQLiteRepository (28→ расширен)
- [x] Интеграционные тесты API tracks (24 теста)
- [x] 184/184 тестов пройдены (160 старых + 24 новых)
- [x] Коммит (725f1d3)

### Хронология:
- **2026-02-23**: python-developer создал TrackService, SearchService, playback router, tracks API. Embedder загружает sentence-transformers опционально.
- **2026-02-23**: polyglot-test-engineer написал 24 интеграционных теста для tracks API. 184/184 pass.
- **2026-02-23**: Фаза 6 принята. Коммит 725f1d3.

## Фаза 7a: Audio Worker — JobService + UVR сепаратор
**Коммит:** 1026165

### Задачи фазы:
- [x] JobService в shared/ (используется и backend, и worker)
- [x] Worker process с asyncio JobPoller и graceful SIGTERM shutdown
- [x] UVRSeparator (обёртка audio-separator с lazy model loading)
- [x] AudioPipeline (шаг 1 реальный, шаги 2-6 заглушки для 7b/8a)
- [x] Worker Dockerfile + entrypoint с auto-download модели UVR
- [x] docker-compose worker service
- [x] Фиксы из ревью: busy_timeout=5000, ALTER TABLE миграция, UVR no_vocal classifier bug, cached separator instance
- [x] Unit-тесты (22 JobService + 20 AudioPipeline)
- [x] 226/226 тестов пройдены (184 старых + 42 новых)
- [x] Коммит (1026165)

### Хронология:
- **2026-02-23**: python-developer создал JobService, worker process, UVRSeparator, AudioPipeline. Worker Dockerfile с entrypoint.sh для авто-скачивания UVR модели.
- **2026-02-23**: software-architect провёл ревью. Фиксы: busy_timeout для обоих DB connections, ALTER TABLE миграция для существующих БД, UVR no_vocal classifier bug, cached separator instance.
- **2026-02-23**: polyglot-test-engineer написал 42 теста (22 JobService + 20 AudioPipeline). 226/226 pass.
- **2026-02-23**: Фаза 7a принята. Коммит 1026165.

## Фаза 7b: Soniox транскрипция, видеогенерация, SSE стриминг
**Коммит:** 38abab3

### Задачи фазы:
- [x] SonoixClient (загрузка vocals → Soniox API → word-level транскрипция)
- [x] Syllabifier (pyphen, разбиение слов на слоги с пропорциональным распределением таймингов)
- [x] VideoGenerator (FFmpeg + ASS субтитры с \k тегами для послоговой подсветки)
- [x] SSE endpoint (стриминг прогресса job → фронтенд)
- [x] AudioPipeline шаги 2-3 реализованы (транскрипция + видеогенерация)
- [x] docker-compose: добавлены SONOIX_API_KEY, SONOIX_API_URL
- [x] Тесты (608 SonoixClient + 436 SSE + 579 Syllabifier + 521 VideoGenerator)
- [x] 386 тестов всего пройдены
- [x] Коммит (38abab3)

### Хронология:
- **2026-02-23**: ml-sota-expert и python-developer создали SonoixClient, Syllabifier, VideoGenerator. SSE endpoint для стриминга прогресса.
- **2026-02-23**: AudioPipeline шаги 2-3 подключены: vocal → Soniox API → syllabify → FFmpeg ASS video.
- **2026-02-23**: polyglot-test-engineer написал обширные тесты для всех новых компонентов. 386 pass.
- **2026-02-23**: Фаза 7b принята. Коммит 38abab3.

## E2E фиксы
**Коммит:** 370e34f

### Задачи:
- [x] Syllabifier: поддержка BPE (byte-pair encoding) токенов
- [x] Worker Dockerfile: исправления сборки (gcc, libc6-dev, pip timeout)
- [x] AudioPipeline: robustness (graceful handling missing steps)
- [x] Playback router: мелкий фикс
- [x] SQLiteRepository: дополнительные методы
- [x] Коммит (370e34f)

### Хронология:
- **2026-02-23**: E2E тестирование выявило проблемы: BPE-токены в syllabifier падали, Docker worker не собирался (отсутствовали gcc, libc6-dev), pipeline не обрабатывал edge cases.
- **2026-02-23**: Все проблемы исправлены. Syllabifier теперь поддерживает BPE. Worker Dockerfile добавлены зависимости сборки. Pipeline более устойчив к ошибкам отдельных шагов.
- **2026-02-23**: Коммит 370e34f.

## Реструктуризация: перенос в v2/, удаление experiments/

### Задачи:
- [x] Обновление PROJECT_LOG.md (записи фаз 6, 7a, 7b, E2E fixes)
- [x] Перенос реализации в v2/ (backend, worker, shared, tests, docker-compose, .env.example)
- [x] E2E верификация: 386/386 тестов pass, docker compose config valid, docker build backend ok, docker build worker ok
- [x] Удаление experiments/ (референсная директория, проанализирована в Фазе 1)
- [x] Обновление master-promt.md (ссылки на experiments → v2/)
- [x] ADR-010: обоснование реструктуризации
- [x] Коммит

### Хронология:
- **2026-02-24**: Обновлён PROJECT_LOG.md — добавлены записи для фаз 6, 7a, 7b, E2E fixes.
- **2026-02-24**: Реализация перенесена в v2/. Все внутренние относительные пути остались рабочими без правок (docker-compose context, conftest.py paths, Dockerfile COPY).
- **2026-02-24**: E2E: 386/386 тестов pass (3.02s), docker compose config valid, оба Docker-образа собираются.
- **2026-02-24**: experiments/ удалена. Ссылки в master-promt.md обновлены.
- **2026-02-24**: ADR-010 зафиксировано.

## Фаза 8a: Извлечение фичей и эмбеддингов
**Коммит:** (pending)

### Задачи фазы:
- [x] FeatureExtractor (librosa → 45-d L2-нормализованный вектор аудиофичей)
- [x] LyricEmbedder (sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 → 384-d вектор)
- [x] Интеграция в AudioPipeline (шаги 4+5 параллельно через asyncio.gather, шаг 6 QDrant upsert)
- [x] Worker main.py: lazy loading ML-компонентов + QDrant client
- [x] Worker config: добавлены QDRANT_HOST, QDRANT_PORT
- [x] docker-compose: QDRANT_HOST/PORT для worker
- [x] Worker Dockerfile: karaoke-shared[ml] вместо karaoke-shared
- [x] shared/pyproject.toml: optional deps [ml] (librosa, numpy, sentence-transformers), structlog в основные
- [x] Тесты: 66 новых (20 FeatureExtractor + 26 LyricEmbedder + 20 AudioPipeline phase 8a)
- [x] 452/452 тестов пройдены (386 старых + 66 новых)
- [ ] Согласование с пользователем
- [ ] Коммит

### Хронология:
- **2026-02-24**: ml-sota-expert создал FeatureExtractor (45-d, MFCC+Chroma+SpectralContrast+Tonnetz+scalars, L2-norm) и LyricEmbedder (384-d, chunking по 256 токенов, mean pooling).
- **2026-02-24**: Интеграция в AudioPipeline: шаги 4+5 через asyncio.gather, шаг 6 QDrant upsert для audio_features и lyrics_embeddings, обновление qdrant_synced=1.
- **2026-02-24**: Worker main.py: lazy loading с graceful degradation (если librosa/sentence-transformers недоступны — шаги пропускаются).
- **2026-02-24**: polyglot-test-engineer написал 66 тестов. Все 452 pass.
