# Журнал проекта: Караоке-приложение

## Статус проекта
**Состояние:** Все фазы завершены, приложение задеплоено
**Дата начала:** 2026-02-22
**Структура:** Код в корне (`backend/`, `worker/`, `frontend/`, `shared/`, `scripts/`)

## Фазы 1-15: MVP (2026-02-22 — 2026-02-24)

Полный цикл от анализа до E2E тестирования. Сводка коммитов — в `PHASES.md`.

**Ключевые вехи:**
- Фаза 1: Архитектура, C4, ADR-001..009, промпты FigGPT (43bb7dc)
- Фаза 2: План 17 фаз реализации (2694b95)
- Фазы 3-8b: Backend (FastAPI + SQLite + QDrant), Worker (UVR + Sonoix + VideoGen), Shared, 509 тестов
- Фазы 9-12: Frontend (React + MUI), караоке-плеер с послоговой подсветкой (rAF + DOM mutation)
- Фаза 13: Bootstrap CLI (multiprocessing, WhisperX, lrc-lib)
- Фаза 14: Docker Compose + Nginx
- Фаза 15: E2E тестирование, 540 тестов, 2 критических бага найдены и исправлены

---

## Фаза 16: Bootstrap pipeline v2 + массовый импорт (2026-02-25 — 2026-03-01)

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
- **2026-03-19**: Фаза R7 — финальная очистка: удалены SessionPage, ParticipantSelector (мёртвый код после R6). ADR-020 (рекомендации v2).
