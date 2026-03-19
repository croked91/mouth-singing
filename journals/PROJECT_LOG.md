# Журнал проекта: Караоке-приложение

## Статус проекта
**Текущая фаза:** 18 — v3-rc1 Worker Pipeline (GPU-сервер) — Задеплоен
**Дата начала:** 2026-02-22
**Последний коммит:** (pending) v3-rc1 deployment fixes
**Структура:** Реализация в `v2/` (prod), `v3-rc1/` (new GPU pipeline), документация в корне

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
- [x] Согласование с пользователем
- [x] Коммит (cd654f2)

### Хронология:
- **2026-02-24**: ml-sota-expert создал FeatureExtractor (45-d, MFCC+Chroma+SpectralContrast+Tonnetz+scalars, L2-norm) и LyricEmbedder (384-d, chunking по 256 токенов, mean pooling).
- **2026-02-24**: Интеграция в AudioPipeline: шаги 4+5 через asyncio.gather, шаг 6 QDrant upsert для audio_features и lyrics_embeddings, обновление qdrant_synced=1.
- **2026-02-24**: Worker main.py: lazy loading с graceful degradation (если librosa/sentence-transformers недоступны — шаги пропускаются).
- **2026-02-24**: polyglot-test-engineer написал 66 тестов. Все 452 pass.
- **2026-02-24**: Фаза 8a принята. Коммит cd654f2.

## Фаза 8b: Рекомендательная система
**Коммит:** (pending)

### Задачи фазы:
- [x] RecommendationService с 4 стратегиями (popular, last, last_two_avg, session_avg)
- [x] Автоматический выбор стратегии по количеству исполненных треков
- [x] KNN-поиск по audio_features коллекции QDrant с фильтрацией played tracks
- [x] Portrait vector обновление при finish_playing (скользящее среднее)
- [x] Граф переходов (transitions collection в QDrant)
- [x] QDrantRepository.retrieve — получение вектора по ID точки
- [x] RecommendedTrackItem модель (id, artist, title, duration_sec, similarity_score)
- [x] API: GET /recommendations?participant_id=X&session_id=Y&limit=10
- [x] Интеграция в QueueService.finish_playing (portrait + transitions при наличии qdrant_repo)
- [x] Тесты: 57 новых (стратегии, fallbacks, portrait update, transitions, API, integration)
- [x] 509/509 тестов пройдены (452 старых + 57 новых)
- [ ] Согласование с пользователем
- [ ] Коммит

### Хронология:
- **2026-02-24**: RecommendationService реализован с 4 стратегиями. Автоматический выбор: 0→popular, 1→last, 2→last_two_avg, 3+→session_avg. Fallback на popular при отсутствии вектора.
- **2026-02-24**: Portrait vector обновляется при finish_playing: running average (old*(n-1)+current)/n. Transition граф записывается в QDrant transitions collection.
- **2026-02-24**: QDrantRepository расширен методом retrieve (получение вектора по point ID).
- **2026-02-24**: API endpoint GET /recommendations возвращает strategy + tracks с similarity_score.
- **2026-02-24**: QueueService.finish_playing интегрирован: при наличии qdrant_repo вызывает update_portrait и record_transition. При ошибке — логирует и продолжает.
- **2026-02-24**: polyglot-test-engineer написал 57 тестов. Все 509 pass.
- **2026-02-24**: Фаза 8b принята. Коммит 1c5246f.

## Фаза 9: Фронтенд — скаффолдинг, тема, Landing + Sessions
**Коммит:** (pending)

### Задачи фазы:
- [x] Vite + React 18 + TypeScript + MUI 5 скаффолдинг
- [x] Тёмная тема по дизайн-системе (glassmorphism, градиенты, glow-эффекты)
- [x] Zustand stores (session, queue, player)
- [x] API client (axios, типизированные методы для всех эндпоинтов)
- [x] SSE service (EventSource обёртка)
- [x] Роутинг (react-router-dom v6): /, /session/:id, /session/:id/queue, /session/:id/play/:entryId, /admin
- [x] CosmicBackground (градиенты, glow blobs, мерцающие звёзды, SVG noise)
- [x] WelcomePage: логотип, заголовок, кнопка «Начать сессию», feature pills, ссылка «Админ»
- [x] SessionPage: добавление участников, генерация никнеймов, gradient аватары, кнопка «Поехали!»
- [x] Placeholder страницы (QueuePage, PlayerPage, AdminPage)
- [x] Dockerfile (multi-stage node → nginx) + nginx.conf (proxy /api, SPA fallback)
- [x] `npm run build` — success, `tsc --noEmit` — 0 errors
- [x] `docker build` — success
- [x] E2E проверка: WelcomePage → создание сессии → SessionPage → добавление участников (всё через реальный API)
- [ ] Коммит

### Хронология:
- **2026-02-24**: frontend-web-client создал React-приложение. Vite 7 + React 18 + TS + MUI 5 + Zustand + react-router-dom v6 + axios.
- **2026-02-24**: Тёмная тема реализована по 00_design_system.md: glassmorphism карточки, gradient кнопки, фокус-glow на TextField, cosmic background с 55 мерцающими звёздами.
- **2026-02-24**: WelcomePage: полноэкранный лендинг с gradient CTA → POST /sessions → навигация к SessionPage.
- **2026-02-24**: SessionPage: glassmorphism карточка, chips участников с cycling gradient аватарами, генерация никнеймов через API, empty state с dashed border.
- **2026-02-24**: E2E flow проверен через Playwright: создание сессии, генерация «ДушевныйПингвин», добавление «Маша» вручную — всё работает с реальным бэкендом.
- **2026-02-24**: Docker build проходит (multi-stage node:20-alpine → nginx:alpine).
- **2026-02-24**: Фаза 9 принята. Коммит 571b2a5.

## Фаза 10a: Фронтенд — QueuePage + рекомендации
**Коммит:** (pending)

### Задачи фазы:
- [x] Исправлены TypeScript типы под реальный backend API (Session.status, Track.duration_sec, QueueEntry.order_position, RecommendedTrackItem)
- [x] QueuePage: двухпанельный layout (левая 480px + правая flex)
- [x] Top nav bar: логотип, «СЕЙЧАС ПОЁТ: NAME» с пульсирующим MicIcon, кнопка «ПРОПУСТИТЬ», admin lock
- [x] Left panel: Current Singer Card (glassmorphism, 88px avatar, glow ring, status dot) + Queue Strip (горизонтальный скролл аватаров с badge позиции)
- [x] Right panel: Табы (Поиск / Рекомендации / Загрузить) с кастомным MUI Tabs стилем
- [x] Таб «Рекомендации»: ParticipantSelector + strategy label + 2-column TrackCard grid
- [x] Компоненты: TrackCard, QueueItem, ParticipantSelector
- [x] Кнопка «ВЫБРАТЬ» → addToQueue → refresh очереди
- [x] Polling очереди каждые 5 сек
- [x] Placeholder табы «Поиск» и «Загрузить»
- [x] `npm run build` — success, `tsc --noEmit` — 0 errors
- [x] E2E: полный flow Landing → Session → Queue через Playwright с реальным API
- [ ] Коммит

### Хронология:
- **2026-02-24**: frontend-web-client реализовал QueuePage. Двухпанельный layout с glassmorphism.
- **2026-02-24**: Типы исправлены: Session.status (string вместо boolean), Track.duration_sec, QueueEntry.order_position, RecommendedTrackItem с similarity_score.
- **2026-02-24**: TrackCard, QueueItem, ParticipantSelector — переиспользуемые компоненты по дизайн-системе.
- **2026-02-24**: E2E flow проверен через Playwright: Landing → создание сессии → добавление участника → QueuePage с табами и participant selector.
- **2026-02-24**: Фаза 10a принята. Коммит c8d6a0e.

## Фаза 10b: Фронтенд — Поиск + Загрузка
**Коммит:** (pending)

### Задачи фазы:
- [x] SearchTab: поисковая строка с debounced suggestions (300ms), результаты с карточками, skeleton loading, empty state
- [x] UploadTab: drag & drop зона (MP3/WAV/M4A до 50МБ), метаданные (artist, title), прогресс-оверлей
- [x] SSE-интеграция для отслеживания прогресса обработки (named events: status/completed/error)
- [x] Маппинг шагов в русские labels (separating→«Разделение вокала и музыки», transcribing→«Распознавание текста» и т.д.)
- [x] Типы: TrackSearchItem, SearchResult, UploadResponse, JobStatusEvent
- [x] API: searchTracks, suggestTracks, uploadTrack (multipart/form-data)
- [x] sseService.ts переписан для named SSE events
- [x] Tabs в QueuePage: SearchTab (таб 0) и UploadTab (таб 2) подключены
- [x] `npm run build` — success, `tsc --noEmit` — 0 errors
- [ ] Коммит

### Хронология:
- **2026-02-24**: frontend-web-client реализовал SearchTab и UploadTab.
- **2026-02-24**: SearchTab: InputBase с debounce → suggestions dropdown (Paper с List) → SearchResultCard (index, album art placeholder, title/artist, duration, ВЫБРАТЬ кнопка). Empty state и initial state.
- **2026-02-24**: UploadTab: drag & drop зона с прогресс-оверлеем (4 фазы: idle→uploading→processing→done/error). SSE подписка на job progress. Step labels на русском.
- **2026-02-24**: sseService.ts переписан: подписка на named events (status, completed, error) через addEventListener + fallback onmessage.
- **2026-02-24**: API расширен: searchTracks (GET /tracks/search), suggestTracks (GET /tracks/search/suggest), uploadTrack (POST /tracks/upload, multipart/form-data).
- **2026-02-24**: Сборка и TypeScript проверка пройдены.
- **2026-02-24**: Фаза 10b принята. Коммит f25f53d.

## Фаза 11: Фронтенд — Караоке-плеер
**Коммит:** (pending)

### Задачи фазы:
- [x] PlayerPage: полноэкранный режим (position: fixed, inset: 0) с near-black фоном #050508
- [x] Два анимированных gradient blob на краях (deep violet + deep navy, blur + drift animation)
- [x] Top bar (64px): название трека · исполнитель + аватар певца + кнопка «ЗАВЕРШИТЬ» (красная pill)
- [x] LyricHighlight — ключевой компонент послоговой подсветки:
  - Группировка слогов в строки (порог 1.0с между слогами)
  - 3 состояния: sung (dim white 0.3), active (neon pink #F0ABFC с triple glow), upcoming (white 0.9)
  - ref-driven rAF loop — прямая DOM-мутация span.style для 60fps без React re-renders
  - Progress bar под активной строкой (gradient #F0ABFC → #7C3AED с glow)
  - Автоматическая ресинхронизация при перемотке (читает audio.currentTime каждый кадр)
- [x] Bottom controls (80px): время · -15с · Play/Pause · +15с · Progress slider (gradient fill) · время · Volume
- [x] При входе: POST /queue/{entry_id}/start → получение syllable_timings + clip_url + duration
- [x] Audio: `<audio>` элемент с preload="auto", src = /api/v1/tracks/{track_id}/stream
- [x] Finish flow: кнопка «ЗАВЕРШИТЬ» или event 'ended' → POST /queue/{entry_id}/finish → navigate to QueuePage
- [x] Типы: SyllableTiming, StartPlayingResponse, FinishPlayingResponse
- [x] API: startPlaying, finishPlaying (обновлённые return types)
- [x] Handle null syllable_timings — «Субтитры недоступны»
- [x] `npm run build` — success, `tsc --noEmit` — 0 errors
- [ ] Коммит

### Хронология:
- **2026-02-24**: frontend-web-client реализовал караоке-плеер — самый сложный UI-компонент.
- **2026-02-24**: LyricHighlight: ref-driven rAF loop с прямой DOM-мутацией (span.style.color, fontWeight, textShadow) для 60fps. Key-based ремонтирование ActiveLine при смене строки.
- **2026-02-24**: PlayerPage: полноэкранный overlay, два анимированных blob, top info bar, bottom controls с MUI Slider (gradient track + glow).
- **2026-02-24**: API types обновлены: startPlaying → StartPlayingResponse, finishPlaying → FinishPlayingResponse.
- **2026-02-24**: Сборка и TypeScript проверка пройдены.
- **2026-02-24**: Фаза 11 принята. Коммит 7878463.

## Фаза 12: Фронтенд — Админка и UX polish
**Коммит:** (pending)

### Задачи фазы:
- [x] AdminModal с 4 состояниями:
  - PIN Entry: 4 dot-индикатора (empty/filled/active/pulse) + виртуальный numpad (3x4 grid)
  - Wrong PIN: красные dots, shake-анимация (CSS @keyframes), «Неверный PIN», auto-reset через 1.5с
  - Unlocked: зелёные dots, «ЗАВЕРШИТЬ СЕССИЮ» + «Отмена»
  - Confirmation: WarningAmberIcon, описание, «Отмена» / «Да, завершить» (solid red)
- [x] PIN → X-Admin-Secret header: DELETE /sessions/{id} → 403=wrong PIN → State B, 204=success → navigate('/')
- [x] AdminPage: CosmicBackground + AdminModal immediately open, onClose → navigate(-1)
- [x] API: terminateSession(sessionId, adminSecret) method
- [x] Улучшенная обработка ошибок в axios interceptor:
  - Network error → «Нет подключения к серверу»
  - Timeout → «Сервер не отвечает»
  - 403 → «Доступ запрещён»
  - 5xx → «Что-то пошло не так, попробуйте позже»
- [x] `npm run build` — success, `tsc --noEmit` — 0 errors
- [ ] Коммит

### Хронология:
- **2026-02-24**: frontend-web-client реализовал AdminModal с 4 состояниями по спеке 07_admin_modal.md.
- **2026-02-24**: Glassmorphism card (460px, blur 32px), PIN dots с gradient fill и pulse-анимацией, numpad с hover/active стилями.
- **2026-02-24**: Shake-анимация при неверном PIN, авто-сброс через setTimeout. Confirm key активна только при 4 цифрах.
- **2026-02-24**: Axios error interceptor переписан: осмысленные русскоязычные сообщения по типу ошибки.
- **2026-02-24**: Сборка и TypeScript проверка пройдены.
- **2026-02-24**: Фаза 12 принята. Коммит 087941d.

## Фаза 13: Bootstrap CLI
**Коммит:** (pending)

### Задачи фазы:
- [x] CLI (typer): --input-dir, --workers, --lrclib-dump, --language, --output-dir, --db-path, --qdrant-host/port, --skip-existing
- [x] LRCLibDump: JSON-lines → in-memory SQLite, fuzzy search (normalize + LIKE fallback), LRC парсинг
- [x] WhisperXTranscriber: lazy import, transcribe + force_align, CPU-only, модель medium
- [x] BootstrapRunner: multiprocessing.Pool с imap_unordered, tqdm прогресс-бар
- [x] Pipeline per track: UVR → LRC search/WhisperX → Syllabifier → VideoGenerator → FeatureExtractor + LyricEmbedder → SQLite + QDrant
- [x] Batch QDrant upsert (каждые 100 треков)
- [x] Error resilience: ошибки логируются в файл, не останавливают процесс
- [x] Track ID: uuid5 от имени файла для детерминизма
- [x] Dockerfile: python:3.12-slim + ffmpeg + torch CPU + whisperx
- [x] pyproject.toml с optional deps [whisperx]
- [x] Python syntax check — все файлы компилируются
- [ ] Коммит

### Хронология:
- **2026-02-24**: python-developer создал Bootstrap CLI.
- **2026-02-24**: LRCLibDump: двухэтапный поиск (exact normalized → LIKE wildcard), парсинг LRC формата с регулярками.
- **2026-02-24**: WhisperXTranscriber: lazy import с HAS_WHISPERX флагом, force_align через pseudo-segment pattern.
- **2026-02-24**: BootstrapRunner: module-level _process_track() для pickling в multiprocessing, _WordToken dataclass для Syllabifier duck-typing.
- **2026-02-24**: Batch QDrant: векторы возвращаются из workers в main process, upsert каждые 100.
- **2026-02-24**: Dockerfile: 4 слоя для оптимального кэширования (PyTorch ~1GB отдельно).
- **2026-02-24**: Фаза 13 принята. Коммит 23bcba4.

## Фаза 14: Docker Compose + Nginx + Deploy
**Коммит:** (pending)

### Задачи фазы:
- [x] docker-compose.yml: 4 сервиса (qdrant, backend, worker, frontend) + karaoke_net сетевая изоляция
- [x] Frontend (nginx) как единая точка входа: /api/ proxy → backend, /health passthrough, SPA fallback
- [x] client_max_body_size 50M для загрузки MP3
- [x] SSE: proxy_buffering off, proxy_cache off, proxy_read_timeout 300s
- [x] Health checks: qdrant (TCP), backend (curl /health), frontend (curl /)
- [x] docker-compose.override.yml: dev overrides (exposed ports, DEBUG logging)
- [x] .env.example: полная документация (ADMIN_SECRET, SONOIX_API_KEY, LOG_LEVEL, WORKER_*, APP_PORT, HTTP_PROXY)
- [x] Worker env: VPN proxy forwarding (HTTP_PROXY/HTTPS_PROXY)
- [x] docker compose config — valid
- [ ] Коммит

### Хронология:
- **2026-02-24**: Финализирован docker-compose.yml: добавлен frontend сервис, karaoke_net network, container_name для всех сервисов.
- **2026-02-24**: nginx.conf обновлён: client_max_body_size 50M, /health passthrough.
- **2026-02-24**: docker-compose.override.yml: dev ports (6333, 8000, 3000), DEBUG logging.
- **2026-02-24**: .env.example: полный набор переменных с комментариями.
- **2026-02-24**: Фаза 14 принята. Коммит 0f44b8e.

## Фаза 15: E2E тестирование и hardening
**Коммит:** d586571 (unit/integration + hardening), pending (browser E2E fixes)

### Задачи фазы:
- [x] E2E сценарные тесты (31 новый тест в test_e2e_scenarios.py)
- [x] Полный user journey: создание сессии → участники → поиск → очередь → старт → финиш → skip → рекомендации → admin terminate
- [x] Edge cases (26 тестов): невалидные файлы, пустой каталог, double-finish, double-start, 404/403/409 ошибки
- [x] Стресс-тест (5 тестов): 5 участников, 20 треков, round-robin, play-through, skip rotation
- [x] Архитектурное ревью (software-architect): PASS на всех 6 чекпоинтах
- [x] Критический баг #1 исправлен: bootstrap QDrant collection name `lyric_embeddings` → `lyrics_embeddings`
- [x] Критический баг #2 исправлен: transition point_id `f"{from}_{to}"` → `uuid5(NAMESPACE_URL, f"{from}_{to}")` (QDrant требует валидный UUID)
- [x] Предупреждение #3 исправлено: SSE clip_url `/media/clips/` → `/api/v1/tracks/{id}/stream`
- [x] Предупреждение #4 исправлено: `_get_queue_entry()` → `get_queue_entry()` (публичный API)
- [x] Предупреждение #5 исправлено: LIKE wildcard injection в suggest_tracks (escape `%` и `_`)
- [x] Предупреждение #7 исправлено: timing-safe admin secret comparison (`hmac.compare_digest`)
- [x] 540/540 тестов пройдены (509 старых + 31 новых)
- [x] Frontend build — success, tsc --noEmit — 0 errors
- [x] Docker compose config — valid
- [x] Browser E2E (Playwright MCP): полный прогон через Docker
- [x] Баг Player: useEffect deps `[]` → `[isLoading]` — audio listeners не привязывались к `<audio>` элементу
- [x] Баг QueuePage: re-click на выбранного участника сбрасывал рекомендации без перезагрузки
- [x] Коммит

### Хронология:
- **2026-02-24**: polyglot-test-engineer написал 31 E2E тест: 1 полный journey, 26 edge cases, 5 стресс-тестов. Все pass.
- **2026-02-24**: software-architect провёл финальное ревью. Чеклист: code duplication PASS, resource leaks PASS, error handling PASS, ADR compliance PASS, security PASS, Docker PASS. Найдено 2 критических бага и 5 предупреждений.
- **2026-02-24**: Критический баг #1: имя QDrant-коллекции в bootstrap (`lyric_embeddings` без 's') не совпадало с backend/worker (`lyrics_embeddings`). Семантический поиск по каталогу не работал бы. Исправлено.
- **2026-02-24**: Критический баг #2: transition_id формата `uuid_uuid` не является валидным UUID — QDrant отклоняет в продакшене (in-memory клиент в тестах допускает). Collaborative filtering был бы мёртв. Исправлено через uuid5.
- **2026-02-24**: Исправлены предупреждения: SSE clip_url, публичный get_queue_entry, LIKE wildcard escape, timing-safe admin secret comparison.
- **2026-02-24**: Все 540 тестов pass. Frontend build clean. Docker compose config valid.
- **2026-02-24**: Browser E2E через Playwright MCP (Docker): Welcome → Session → Participants → Queue → Recommendations → Search → Upload (полный pipeline: UVR+Soniox+FFmpeg) → Player → Admin terminate. Все потоки работают.
- **2026-02-24**: Найден и исправлен баг PlayerPage: audio event listeners (timeupdate, play, pause и т.д.) привязывались в useEffect с `[]` deps, но `<audio>` элемент рендерится только после isLoading=false. Таймер и слайдер не обновлялись. Fix: deps `[isLoading]`.
- **2026-02-24**: Найден и исправлен баг QueuePage: handleParticipantSelect сбрасывал recommendations в null при re-click на того же участника, useEffect не перезапускался (тот же dependency value). Fix: `if (id === selectedParticipantId) return;`.

## Фаза 16: Bootstrap pipeline v2 + массовый импорт треков

### Задачи фазы:
- [x] Собрать библиотеку из 4820 MP3 (lrclib + hitmotop.com грабер)
- [x] Удалить VideoGenerator — мёртвый код (ADR-011)
- [x] Feature Extraction на оригинальном MP3 с голосом (ADR-011)
- [x] Новый syllabify-then-align flow для точных слоговых таймстемпов (ADR-012)
- [x] LRCLib SQLite адаптер для 78GB дампа на VPS (ADR-012)
- [x] HTTP адаптер для lrclib (`--lrclib-url`)
- [x] Тестовый запуск бутстрапа на 5 треках (5/5 ok)
- [x] Fix GPU memory leak (cleanup() для WhisperX и UVR)
- [x] Remote mode: pull MP3 → process local GPU → push results → delete source
- [x] Multi-worker claiming: atomic `mv` для параллельной работы на нескольких GPU
- [x] Setup/run scripts для быстрого старта на новой машине
- [x] BS-Roformer (SDR 12.9) вместо MDX-NET (SDR ~8-9) для бутстрапа (ADR-013)
- [x] MVSEP API тест (15 треков, sep_type=49 Karaoke, ~3 мин/трек, ~$0.15/трек)
- [x] Local GPU bootstrap: 48 треков BS-Roformer на RTX 4060 (~107-148 сек/трек)
- [x] Multi-GPU local mode (`--gpu-id N`): atomic file claiming, per-track QDrant flush, preemptible safety
- [x] GPU сервер Selectel: 4×RTX 4090, миграция диска, настройка окружения
- [x] Баг-фикс: torchaudio 2.8.0→2.10.0 (ABI mismatch с torch 2.10.0)
- [x] Баг-фикс: infinite retry loop на failed tracks (добавлен `failed_ids: set`)
- [x] Оптимизация: 2-3 воркера на GPU (8→12 воркеров, GPU util 0-20% → 93-100%)
- [x] Полный бутстрап 4725/4727 треков (12 воркерами на 4×RTX 4090, ~10ч)

### Хронология:
- **2026-02-25**: Собрано 4820 уникальных MP3 из 6 источников: bootstrap (1770), batch2 (1187), ru_from_db (1067), batch3 (654), missing_batch3 (38), russian_manual (107). Грабер `grab_mp3_links.py` + `download_mp3s.py`.
- **2026-02-25**: Удалён VideoGenerator — `video_generator.py`, `test_video_generator.py`, все импорты/ссылки в worker, backend, bootstrap, frontend. clip_path оставлен nullable в БД.
- **2026-02-25**: Feature Extraction переключен с instrumental на оригинальный MP3 (bootstrap_runner.py, audio_pipeline.py).
- **2026-02-25**: Новый `LRCLibSQLiteAdapter` — read-only адаптер для 78GB SQLite дампа lrclib. CLI: `--lrclib-sqlite`.
- **2026-02-25**: Новый syllabify-then-align flow: pyphen split → WhisperX force_align → точные слоговые таймстемпы из аудио. Метод `Syllabifier.split_text_to_syllables()` + `_map_syllable_timestamps()`.
- **2026-02-26**: Тестовый прогон на "Виктор Цой — Малыш" (RTX 4060, 1:57). Результат: 29 строк, 236 слогов, идеальное совпадение с LRC.
- **2026-02-26**: Fix force_align: per-line LRC segments с start/end из LRC таймстемпов → точное выравнивание. Lazy ASR loading (force_align не грузит тяжёлую модель).
- **2026-02-26**: `\n` маркеры строк в syllable_timings из LRC: `is_line_start` флаги → `_map_syllable_timestamps()` инжектит `\n` prefix вместо пробела на границах строк.
- **2026-02-26**: Фронтенд: `groupIntoLines()` в LyricHighlight.tsx обрабатывает `\n` маркеры — разбивает строки по бэкенд-маркерам вместо эвристик (gap/punctuation).
- **2026-02-26**: Коммит 9dcbc96: bootstrap pipeline \n markers, lazy ASR, force_align per-line segments.
- **2026-02-26**: Добавлен `LRCLibHTTPAdapter` — HTTP-клиент для lrclib сервера на VPS. CLI: `--lrclib-url`.
- **2026-02-26**: lrclib HTTP сервер запущен на VPS (`http://130.49.170.186:9876`) поверх 78GB SQLite дампа.
- **2026-02-26**: Тестовый прогон на 5 треках (Adele, Metallica, Ария, Валерия, Виктор Цой). Результат первого прогона: 3/5 ok, 1 CUDA OOM (Валерия — ASR fallback), 1 killed (Цой — UVR crawl). Причина: GPU memory leak — модели WhisperX и UVR ONNX не освобождали VRAM между треками.
- **2026-02-26**: Fix GPU memory leak: добавлены `cleanup()` методы в `WhisperXTranscriber` (del models + gc.collect + torch.cuda.empty_cache) и `UVRSeparator` (del separator + gc.collect + empty_cache). Вызываются после каждого шага в `_process_track`.
- **2026-02-26**: Повторный прогон: **5/5 ok, 0 failed, 5:25 total** (было >45 min с 2 failures). Валерия обработана через ASR fallback (589 слогов, без `\n`). Все LRC-треки с `\n` маркерами.
- **2026-02-26**: Коммит c898bab: Fix GPU memory leak between bootstrap tracks.
- **2026-02-26**: Remote mode: `--remote-host` флаг — pull MP3 с VPS → process local GPU → push instrumental + DB insert → delete source MP3. SSH ControlMaster для единого TCP-соединения.
- **2026-02-26**: Тест remote mode на 20 треках: 20/20 ok, ~1.5 мин/трек (MDX-NET), 4820→4800 MP3 на сервере.
- **2026-02-26**: Multi-worker claiming: atomic `mv -n` в `.processing/` subdir. Два GPU-воркера работают параллельно без дубликатов. Unclaim-on-failure возвращает файл при ошибке. Коммит 4b4a08d.
- **2026-02-26**: Setup scripts: `tools/setup-worker.sh` (conda env, PyTorch+CUDA, packages), `tools/run-bootstrap.sh` (one-liner с defaults для VPS).
- **2026-02-26**: A/B/C сравнение моделей vocal separation: MDX-NET-Voc_FT (SDR ~8-9, 16-19s), BS-Roformer-1297 (SDR 12.9, 59-65s), Mel-Roformer-Karaoke (SDR 10.2, 28s). Тест на мужском (5sta Family) и женском (Adele) вокале.
- **2026-02-26**: Переключение бутстрапа на BS-Roformer (SDR 12.9, SOTA). `UVRSeparator` параметризован (`model_name`), CLI: `--uvr-model`. Дефолт для бутстрапа: BS-Roformer. Продакшн-воркер: MDX-NET (обратная совместимость).
- **2026-02-26**: Тест BS-Roformer на 5 треках: 5/5 ok, ~63-66 сек/трек UVR (vs 16-19 на MDX-NET). Качество значительно лучше — минимум вокального bleed в инструментале.
- **2026-02-26**: MVSEP API тест: 15 треков через sep_type=49 (Karaoke), ~3 мин credits/трек, ~$0.15/трек. Результат хороший, но для бутстрапа 4800 треков слишком дорого (~$720). Решение: MVSEP для прода (on-demand), BS-Roformer для бутстрапа.
- **2026-02-26**: Запуск массового BS-Roformer бутстрапа на RTX 4060 (WSL2). 48 треков обработано за ~1.5ч (~107-148 сек/трек). Остановлен для переноса на GPU сервер.
- **2026-02-28**: Планирование GPU-сервера: `--gpu-id N` флаг, `_run_local_gpu()` с atomic file claiming (Path.rename), per-track QDrant flush (preemptible safety), SQLite timeout=30s, `run-gpu-server.sh` с auto-detect GPU count.
- **2026-02-28**: ADR-014: Multi-GPU bootstrap с preemptible-safe design.
- **2026-02-28**: GPU сервер арендован на Selectel: root@195.225.111.241 (philomena), 4×RTX 4090, 24 vCPU, 235GB RAM, CUDA 12.2 (driver 535). Диск мигрирован с lainey (130.49.170.186).
- **2026-02-28**: Настройка окружения: miniconda на local disk, conda env на data disk (`/mnt/data/conda_envs/bootstrap`, 394GB). PyTorch cu128 (forward compat с driver 535). QDrant v1.8.0 binary (matching Docker image version) с existing data.
- **2026-02-28**: Проблемы и фиксы:
  - `uvr_separator.py` — symlink на локальный путь → rsync `--copy-links`
  - torchaudio 2.8.0 ABI mismatch с torch 2.10.0 → обновлён до 2.10.0+cu128
  - Диск `/` (20GB) 100% заполнен → удалён неиспользуемый conda env (7GB), pip cache (6.8GB), huggingface cache перенесён на data disk через symlink
  - QDrant v1.17.0 incompatible с данными → использован v1.8.0 (из docker-compose)
- **2026-03-01**: Баг-фикс: infinite retry loop — при ошибке трек возвращался в очередь и подхватывался тем же воркером бесконечно. Добавлен `failed_ids: set` для пропуска ранее упавших треков.
- **2026-03-01**: Оптимизация: GPU utilization 0-20% при 4 воркерах (CPU-bound librosa/ffmpeg). Запуск 2-3 воркеров на GPU (8→12 total). GPU util выросла до 93-100%, скорость 2.8→8.0 треков/мин (×2.9).
- **2026-03-01**: Прогресс бутстрапа: ~3337/4726 треков обработано (~71%), 12 воркеров на 4×RTX 4090, ETA ~5ч.
- **2026-03-01**: **Бутстрап завершён: 4725/4727 треков** в SQLite + QDrant. 2 трека не обработались (1 проблемный — QDrant timeout, 1 скит — тишина). Общее время ~10 часов на 4×RTX 4090 (12 воркеров). GPU сервер остановлен, диск сохранён для подключения к дешёвому серверу.
- **2026-03-01**: ML-аудит рекомендательной системы (совместно с ml-sota-expert): выявлено 9 проблем (scale dominance, transition weight=1, N+1 SQL, no recency bias, portrait drift, popularity feedback loop и др.).
- **2026-03-01**: Полное исправление рекомендательной системы (9 из 9 проблем) за один проход:
  - Post-hoc z-score нормализация фичей (скрипт `reindex_audio_features.py`, $0 cost)
  - FeatureExtractor: z-score трансформация для новых треков через сохранённые stats
  - EMA (alpha=0.3) вместо running average для portrait vector + L2-renorm
  - Transition weight: read-modify-write (retrieve_payload + upsert)
  - Transition candidates в LAST стратегии (scroll_filtered + sort by weight)
  - Batch SQLite запросы (get_tracks_by_ids) вместо N+1
  - Popular стратегия: 70% top + 30% random (breaks feedback loop)
  - QDrant payload index from_track_id для transitions
  - tracks_played из participant (не len(history))
  - Тесты: 85/85 pass (27 feature extractor + 58 recommendation service)
- **2026-03-01**: Weighted fusion рекомендаций: audio (0.7) + lyrics embeddings (0.3):
  - Два параллельных KNN запроса (audio_features + lyrics_embeddings) через asyncio.gather
  - Merge по track_id: fused_score = 0.7 * audio_score + 0.3 * lyrics_score
  - Dual EMA portrait: отдельные audio и lyrics портреты участника
  - DB migration: lyrics_portrait_vector TEXT в participants
  - Fallback: tracks без текста → чистый audio KNN
  - Тесты: 98/98 pass (80 recommendation + 18 feature extractor, +13 новых для fusion)
- **2026-03-01**: Глубокий аудит рекомендательной системы — mental trace всех флоу. Выявлено 3 бага:
  1. **Critical**: Worker создаёт FeatureExtractor без normalization_stats_path → user-uploaded треки в другом нормализационном пространстве vs каталог (z-scored). Каскадно портит portrait при смешивании.
  2. **Medium**: update_portrait стирает lyrics_portrait_vector при игре трека без лирики (NULL overwrite).
  3. **Medium**: Fallback в get_recommendations при portrait=None и len(history)<2 → IndexError (500).
- **2026-03-01**: Все 3 бага исправлены:
  - Worker config: добавлен `NORMALIZATION_STATS_PATH` → передаётся в FeatureExtractor
  - SQLite update_portrait: если lyrics=None — не трогает столбец (оставляет старый)
  - Fallback: каскадная деградация history≥2→LAST_TWO, ==1→LAST, 0→POPULAR
  - Docker Compose: env var `NORMALIZATION_STATS_PATH` для worker
  - ADR-015: Нормализация фичей при пользовательских загрузках
  - Тесты: 85+48 pass (5 новых: 3 fallback guard + 1 lyrics preservation + 1 SQLite portrait)
- **2026-03-01**: Повторный аудит рекомендательной системы (mental trace всех flow). 3 фикса:
  1. Zero vector guard в AudioPipeline: нулевые векторы (от сбоя librosa/sentence-transformers) больше не попадают в QDrant (cosine distance undefined для нулевого вектора). Логирование warning при пропуске.
  2. Defensive guards в get_recommendations: при рассинхроне tracks_played и history (len(history) < tracks_played) — безопасная деградация к более простой стратегии вместо IndexError.
  3. Cold start diversity: `list_popular` ORDER BY `play_count DESC, RANDOM()` — треки с одинаковым play_count перемешиваются, ломая positive feedback loop при бутстрапе (все play_count=0).
  - Тесты: 132/132 pass (0 новых — существующие тесты покрывают все изменённые пути)

## Фаза 17: Ре-бутстрап каталога (17 409 треков)

### Задачи фазы:
- [x] Подготовка GPU-сервера: 8×RTX 4090, conda env, CUDA, зависимости
- [x] Pre-init SQLite (полная схема из init.sql с FTS-триггерами)
- [x] Pre-init QDrant (init-qdrant.py — 3 коллекции + payload indexes)
- [x] Запуск 24 воркеров (3/GPU × 8 GPU) с BS-Roformer
- [x] Баг-фикс FTS: `track_id` → `id` в tracks_fts (content-sync column mismatch)
- [x] Баг-фикс qdrant_synced: `_flush_qdrant()` returns bool + 3 retries
- [x] Баг-фикс QDrant ulimit: `--ulimit nofile=65535:65535`
- [x] Баг-фикс HuggingFace 429: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`
- [x] Бутстрап 17 315/17 409 треков (~19ч)
- [x] z-score reindex (post-bootstrap)
- [x] Деплой Docker Compose (prod)
- [x] Фикс QDrant версии (v1.8.0 → v1.13.6)
- [x] Верификация рекомендаций (все стратегии)

### Хронология:
- **2026-03-02**: Подготовка GPU-сервера (155.212.182.210): 8×RTX 4090, 48 vCPU, 188GB RAM. Conda env `bootstrap` (Python 3.12, torch 2.8.0+cu128, audio-separator --no-deps). MP3 библиотека: 17 409 треков, 141GB.
- **2026-03-02**: Pre-init: `sqlite3 karaoke.db < init.sql` + `python init-qdrant.py`. QDrant Docker с `--ulimit nofile=65535:65535`.
- **2026-03-02**: Первый запуск: 8 воркеров (1/GPU), ~7.4 треков/мин. Переключено на 24 (3/GPU).
- **2026-03-02**: Обнаружены баги при проверке данных:
  1. **FTS content-sync**: столбец `track_id` в tracks_fts не совпадал с `id` в tracks → FTS пустой. Фикс: переименование + rebuild.
  2. **qdrant_synced всегда 0**: не было кода для обновления после flush. Фикс: новый `_mark_qdrant_synced()`.
  3. **qdrant_synced=1 при failed flush**: `_flush_qdrant()` ловил исключение молча. 185 в SQLite vs 147 в QDrant. Фикс: return bool + retry.
  4. **QDrant "too many open files"**: 24 воркера исчерпали ulimit 1024. Фикс: `--ulimit nofile=65535:65535`.
  5. **HuggingFace 429 rate limit**: 24 воркера одновременно проверяли версии моделей. 371 ошибка за один прогон. Фикс: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.
- **2026-03-02**: Все баги исправлены. Данные очищены (46 orphan tracks удалены, sync reset+rebuild). Перезапуск с 24 воркерами.
- **2026-03-02**: Стабильная работа: ~14 треков/мин, 0 ошибок.
- **2026-03-03**: Сервер прерван (preemptible). Перезапущен пользователем. QDrant recovery ~30 сек (17k+ points). Бутстрап завершён: 17 315 треков в SQLite + QDrant (audio + lyrics) + FTS. 94 трека — инструменталы/скиты без вокала.
- **2026-03-03**: Код закоммичен: FTS fix (init.sql), QDrant flush retry (bootstrap_runner.py), multi-worker launcher (run-gpu-server.sh).
- **2026-03-03**: z-score reindex: 17 315 векторов нормализованы за ~30 сек. Фикс скрипта: `timeout=300`, `check_compatibility=False` (qdrant-client 1.17 vs server 1.8). Stats JSON → `/root/models/feature_normalization_stats.json`.
- **2026-03-03**: Деплой Docker Compose на 155.212.182.210. Фиксы: QDrant bind mount (`/root/qdrant_storage`), worker models `:ro`→`:rw`, QDrant image v1.8.0→v1.13.6 (фикс 404 на `/points/query` — qdrant-client 1.17 использует новый API). Все 4 контейнера healthy.
- **2026-03-03**: Верификация рекомендаций (E2E): все 3 стратегии (last, last_two_avg, session_avg) возвращают результаты. Граф переходов наполняется (11 transitions после тестирования). Бета-тест запланирован на 2026-03-04.

## Исследование M3: Оптимизация слоговой разметки

### Контекст
Бета-тестирование выявило, что качество слоговой разметки сильно варьируется между треками. Причина — два пути в бутстрапе: Path 1 (LRC найден → syllabify → WhisperX force_align → точные тайминги) vs Path 2 (LRC не найден → WhisperX ASR → pyphen proportional split → плохие тайминги). Задача: найти способ получать качественные тайминги без LRC.

### Тестовая выборка
5 треков: Слава КПСС, Григорий Лепс, RHCP, Дима Билан, Король и Шут. Эталон — syllable_timings из БД (Path 1).

### Протестированные методы (7 вариантов)
1. **Baseline** — WhisperX ASR → pyphen proportional split
2. **V2** — WhisperX ASR → difflib fuzzy match с известным текстом → force_align слогов
3. **V3b** — WhisperX ASR → difflib alignment → pyphen (без force_align)
4. **V3 Sonoix+LLM** — Sonoix API → BPE-токены → GPT-4o-mini коррекция текста
5. **CTC Word** — CTC Forced Aligner (MMS-300m ONNX) word-level → pyphen
6. **CTC Char** — CTC char-level на весь трек
7. **CTC Hybrid** — CTC word-level → char-level CTC внутри каждого слова → слоги из char-таймингов

### Результаты (средние по русским трекам 1,2,4,5)

| Метод | MAE | Hit rate (<0.1с) | ASR? | Время/трек |
|-------|-----|-------------------|------|------------|
| Baseline | 4.467с | 56.4% | да | ~15с (GPU) |
| V3b (difflib) | 0.351с | 57.0% | да | ~15с (GPU) |
| CTC Word | 0.276с | 53.2% | нет | ~22с (CPU) |
| V2 (WhisperX) | 0.341с | 72.2% | да (×2) | ~30с (GPU) |
| **CTC Hybrid** | **0.240с** | **71.0%** | **нет** | **~22с (CPU)** |

### Провалы
- **V3 Sonoix+LLM**: MAE 13-54с — GPT-4o-mini не умеет переписывать BPE-токены с сохранением таймингов
- **CTC Char (full track)**: MAE 2-24с — накапливающийся дрейф на 1700+ символах
- **RHCP (трек 3)**: провал у всех методов — в БД обработан как language="ru"

### faster-whisper CPU benchmark (трек 1, 2:34)
tiny=30с, base=57с, small=94с, medium=372с — все медленнее CTC Hybrid (22с).

### Выводы
1. **CTC Hybrid — лучший подход**: лучший MAE (0.240с), hit rate на уровне WhisperX (71% vs 72%)
2. **Не требует ASR** — текст подаётся напрямую, нет зависимости от качества распознавания
3. **Один проход** лёгкой ONNX-модели (MMS-300m, ~300MB), работает на CPU за ~22с/трек
4. **Pyphen proportional split** — главный bottleneck всех word-level методов
5. **LLM-коррекция BPE-токенов не работает** — задача слишком сложна для языковой модели

### Предложенный новый флоу загрузки треков
1. Пользователь присылает трек
2. UVR → извлечение аудиофичей (параллельно)
3. librosa VAD на вокале (удаление тишины)
4. Whisper tiny (ASR) — только для идентификации песни (пусть с ошибками)
5. LLM (gpt-4o-mini / deepseek-v3) — поиск правильного текста в интернете
6. CTC force-align word-level с найденным текстом
7. CTC force-align char-level внутри каждого слова (>1 слога)
8. Эмбеддинги, z-score, строки — как прежде

### Подводные камни и решения
- **LLM не найдёт текст** → fallback: показать пользователю текст для ручной вставки; AcoustID/Shazam fingerprinting как альтернатива
- **Текст не совпадает с аудио** (другая версия) → проверка CTC confidence score + difflib WER ASR vs найденный текст
- **CTC падает на коротких словах** (1-2 символа) → пропуск char-level для односложных слов (pre-check emissions frames vs tokens)

### Файлы экспериментов
- `m3_test/RESULTS.md` — полная сводка
- `m3_test/variant2/` — V2 (WhisperX force_align)
- `m3_test/variant3/` — V3, V3b (Sonoix+LLM, difflib)
- `m3_test/variant_ctc/` — CTC Word, Char, Hybrid

### Библиотека
- `ctc-forced-aligner` v1.0.2 (pip)
- Модель: MMS-300m (ONNX, ~300MB)
- API: `AlignmentSingleton`, `generate_emissions`, `get_alignments`, `preprocess_text`

### Хронология
- **2026-03-06**: Анализ двух путей бутстрапа, выявление причин разброса качества
- **2026-03-06**: Подготовка тестовых данных (5 треков, эталонные тайминги из БД)
- **2026-03-06**: Эксперименты V2 (WhisperX fuzzy+force_align), V3b (difflib+pyphen), V3 (Sonoix+LLM)
- **2026-03-06**: Открытие CTC Forced Aligner (MMS-300m ONNX). Эксперименты CTC Word, Char, Hybrid
- **2026-03-06**: Фикс ONNX Runtime crash: emissions считаются 1 раз на весь трек, слайсятся по словам
- **2026-03-06**: Фикс CTC short words: pre-check `emissions.shape[0] < n_tokens * 2` → fallback
- **2026-03-07**: Benchmark faster-whisper CPU (tiny→medium). Итоговая сводка результатов
- **2026-03-07**: Проектирование нового флоу загрузки треков
- **2026-03-07**: Оценка двух вариантов деплоя: rc1 (GPU-сервер i3-14100 + T4 16GB) vs rc2 (дешёвый VPS + API)
- **2026-03-07**: Подготовка детальных планов реализации: v3-rc1/PLAN.md (~1540 строк) и v3-rc2/PLAN.md (~1850 строк)
- **2026-03-07**: Решение: реализация rc1 первым (меньше внешних зависимостей, тестируется на RTX 4060)

## Фаза 18: v3-rc1 — Новый worker pipeline (GPU-сервер)

### Контекст
Полная переработка worker-пайплайна для загрузки пользовательских треков. Замена Sonoix ASR (BPE-токены) на CTC Hybrid alignment с поиском текста через LLM. Результат исследования M3 (ADR-017).

### Целевое железо
Intel Core i3-14100, 32 GB DDR5, Tesla T4 16 GB, 2 TB NVMe

### Задачи фазы
- [x] Скопировать shared/, backend/, frontend/ из v2 в v3-rc1/
- [x] VADProcessor — librosa VAD на вокале
- [x] WhisperTranscriber — faster-whisper (tiny/base) на GPU для идентификации
- [x] LyricsSearcher — OpenAI gpt-4o-mini для поиска текста + Genius scraping
- [x] CTCAligner — гибридный word+char CTC alignment (из experiment_hybrid.py)
- [x] Новый AudioPipeline — оркестрация 10 шагов
- [x] UVRSeparator — BS-Roformer на GPU (torch_device через env)
- [x] Новый config.py — env vars для всех компонентов
- [x] Новый main.py — wire-up компонентов
- [x] Dockerfile (CUDA 12.1 + cuDNN 8 + Python 3.11 + все зависимости)
- [x] docker-compose.yml + docker-compose.prod.yml с GPU passthrough
- [x] download_models.py — предзагрузка 4 моделей при старте
- [x] Unit-тесты (44 passed)
- [x] Деплой на выделенный GPU-сервер (212.41.1.108)
- [x] Исправление runtime-ошибок при деплое (6+ итераций Dockerfile)

### Ключевые решения
- Единственный внешний API — OpenAI (gpt-4o-mini) для идентификации + поиска текста (~$0.0005/трек)
- Genius API — скрейпинг текстов песен (бесплатно, fallback: web search + CSS/LLM extract)
- CTC alignment на CPU (~22с/трек), не требует GPU
- UVR BS-Roformer на T4 GPU (~60-90с/трек, SDR 12.9)
- faster-whisper tiny на T4 (~5-10с, только для идентификации)
- QDrant v1.16.2 (совместимость с qdrant-client 1.17)
- Fallback при ненайденном тексте: трек получает status="error", user вводит текст вручную
- IMPORTANT: OpenAI content_filter блокирует тексты с ненормативной лексикой — CSS-селекторы как primary fallback

### Хронология
- **2026-03-07**: План написан (v3-rc1/PLAN.md, ~1540 строк), начало реализации
- **2026-03-07**: Реализация всех компонентов worker pipeline: VADProcessor, WhisperTranscriber, LyricsSearcher, CTCAligner, AudioPipeline, config, main
- **2026-03-07**: Скопированы shared/, backend/, frontend/ из v2, адаптированы для v3-rc1
- **2026-03-07**: Dockerfile с CUDA 12.1 + cuDNN 8, docker-compose.yml/prod.yml
- **2026-03-07**: 44 unit-теста passed
- **2026-03-08**: Подготовка к деплою на новый сервер root@212.41.1.108 (Xeon E-2236 + T4 16GB)
- **2026-03-08**: Установка NVIDIA driver 550 + Docker 29 + nvidia-container-toolkit на сервере
- **2026-03-08**: Проблема PCI BAR allocation для T4 — «can't assign; no space» в dmesg
- **2026-03-08**: Попытка `pci=realloc=on` — не помогло. Попытка `pci=nocrs` — сломала загрузку
- **2026-03-08**: Переустановка ОС, обращение к хостеру для включения "Above 4G Decoding" в BIOS
- **2026-03-08**: BIOS настроен, `pci=realloc=on` + nvidia-smi заработал, T4 видна
- **2026-03-08**: Полная установка: NVIDIA driver + Docker + nvidia-container-toolkit
- **2026-03-08**: rsync кода на сервер, создание .env с API ключами
- **2026-03-08**: Сборка Docker образов (4 контейнера), множественные исправления Dockerfile:
  - Добавлен g++ для сборки ctc-forced-aligner
  - Пиннинг sentence-transformers<3, transformers<5 (v5.x несовместим с torch 2.3.1)
  - Upgrade pip/setuptools/wheel перед установкой shared/ (old setuptools → UNKNOWN package)
  - COPY только pyproject.toml + karaoke_shared/ (без stale build/ артефактов)
  - Явные runtime deps (aiosqlite, structlog, httpx и др.) в отдельном шаге
  - Удалён torch_device из Separator() в download_models.py (API изменился)
- **2026-03-08**: Обновление QDrant v1.13.6 → v1.16.2 (совместимость с qdrant-client 1.17)
- **2026-03-08**: Все 4 контейнера запущены и healthy. Worker polling for jobs

## Экспертный аудит и фиксы рекомендательной системы

### Контекст
Перед финальной доработкой MVP проведён экспертный аудит системы рекомендаций: 7 подагентов-экспертов (пользователь-одиночка, малая группа 2-3, большая группа 4-10, эксперт по музыке, эксперт по рекомендательным системам, архитектор, UX-дизайнер) провели мысленную симуляцию всех сценариев использования. Результаты собраны в `journals/RECOMMENDATIONS_REVIEW.md`.

### Выявленные баги (исправлены)
1. **Fusion bias**: треки без лирики получали заниженный fused score (0.7*audio вместо audio) из-за penalty за отсутствующую модальность. Фикс: per-candidate нормализация по доступным весам.
2. **Transitions race condition**: read-modify-write weight в QDrant без блокировки. Два concurrent finish_entry могли потерять инкремент. Фикс: миграция transitions в SQLite с атомарным `INSERT ON CONFLICT DO UPDATE SET weight = weight + 1` (ADR-019).
3. **Transitions в QDrant без необходимости**: коллекция хранила 45-D вектор, который никогда не использовался для KNN (только payload-фильтрация). Перенесено в SQLite-таблицу.
4. **N+1 в semantic search**: цикл `get_track()` заменён на batch `get_tracks_by_ids()`.
5. **Sequential QDrant calls**: 3 метода (`_last_strategy`, `_last_two_avg_strategy`, `update_portrait`) делали 2-4 sequential retrieve — переведены на `asyncio.gather()`.
6. **Нет timeout у QdrantClient**: добавлен `timeout=10`.

### Хронология
- **2026-03-19**: Запуск 7 подагентов-экспертов для аудита системы рекомендаций
- **2026-03-19**: Сбор обратной связи, составление `RECOMMENDATIONS_REVIEW.md` (8 разделов, 6 предложенных фаз улучшений)
- **2026-03-19**: Исправление 6 багов: fusion bias, transitions → SQLite, N+1, parallel QDrant calls, timeout. 77/77 тестов pass
- **2026-03-19**: Мозговой штурм рекомендаций v2 (`RECOMMENDATIONS_V2_BRAINSTORM.md`): 10 вопросов обсуждены и решены. Ключевые решения: убрать участников, автокластеры сессии (макс. 3, порог 0.7), теги настроения (100-200 на 15-20 кластеров каталога), popularity scoring (5 категорий: вечный хит / ситуативный / бывший ситуативный / лучшее артиста / просто песня), MMR (λ=0.7), фото артистов (Spotify + Яндекс), тогл "только русский". План из 8 фаз (R0-R7).
- **2026-03-19**: Фаза R0 — ревизия и очистка: удалены EMA-портреты, 4 старые стратегии (last/last_two_avg/session_avg), transitions, update_portrait, record_transition. Упрощён QueueService (убрана зависимость от RecommendationService). API переведён на session_id (без participant_id). Лимит по умолчанию 5. Сохранены: _fused_knn_search, _knn_raw, _popular_strategy для будущих фаз. 398/398 тестов pass.
- **2026-03-19**: Фаза R1 — popularity scoring: новый enum PopularityCategory (eternal_hit/current_hit/former_hit/artist_best/regular). Поля popularity_category, chart_count, chart_last_seen в tracks. Миграция ALTER TABLE. Метод update_popularity в repository. Скрипт parse_karaoke_charts.py (парсинг караоке-списков + чартов + fuzzy matching + категоризация). Захардкожен fallback-список ~70 вечных хитов (рус+англ). 16 тестов popularity + 149 всего pass.
- **2026-03-19**: Фаза R2 — кластеризация каталога: таблица catalog_clusters (id, centroid_audio, centroid_lyrics, track_count). Поле catalog_cluster_id в tracks. Модель CatalogCluster. CRUD-методы в repository (create_catalog_cluster, get_all_clusters, clear_clusters, assign_cluster). Скрипт cluster_catalog.py (QDrant scroll → fused vectors → K-Means → SQLite). Масштабирование лирики sqrt(0.3/0.7) для корректного cosine distance. 8 тестов clustering + 157 всего pass.
- **2026-03-19**: Фаза R3 — теги настроения: таблица mood_tags (id, name, cluster_id). Модель MoodTag + MoodTagResponse. CRUD-методы (create_mood_tag, get_all_tags, get_tag, get_tags_excluding_clusters, clear_mood_tags). API endpoints: GET /tags (фильтрация покрытых кластеров), GET /recommendations?tag_id=X (KNN по центроиду кластера тега). Скрипт create_mood_tags.py (--show-clusters для ревью, --example для тестовых тегов). 10 тестов mood_tags + 94 regression pass.
- **2026-03-19**: Фаза R4 — новый алгоритм рекомендаций: автокластеры сессии (жадная кластеризация, порог 0.7, макс. 3, одиночки вес 0.5), распределение слотов (4 кластерных + 1 exploration), popularity re-ranking (category_weight), MMR diversity (λ=0.7), exploration (anti-KNN по популярным). Стратегия CLUSTER добавлена в enum. Параметр language для фильтрации. Чистые функции: auto_cluster_session, distribute_slots, popularity_rerank, mmr_select. 94 тестов pass.
- **2026-03-19**: Фаза R5 — фото артистов: таблица artists (name PK, image_path, source). Модель Artist. Repository (upsert_artist, get_artist, get_artists_without_images). Скрипт fetch_artist_images.py (Spotify API + fallback placeholder). Поле artist_image_url в RecommendedTrackItem. Recommendations API подставляет image_url из БД. 5 тестов artists + 41 всего pass.
- **2026-03-19**: Фаза R6 — фронтенд v2: убран SessionPage (WelcomePage → сразу QueuePage). Убран ParticipantSelector. Добавлены mood tags (горизонтальная полоска чипов). Добавлен тогл "Только на русском". Рекомендации: 5 карточек (1 колонка) вместо 12 (2 колонки). API обновлён: getRecommendations(sessionId, limit, tagId?, language?), getTags(sessionId). Типы обновлены: MoodTag, artist_image_url, strategy='cluster'.
