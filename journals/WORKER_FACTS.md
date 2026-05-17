# WORKER_FACTS — карта подсистемы воркера, собранная только из кода

Файл собирается строго из исходного кода и `CLAUDE.md`. Любые расхождения с `CLAUDE.md` фиксируются в разделе «Несоответствия». Каждый факт сопровождается ссылкой `file:line`. Никаких выводов, обобщений или сравнений со «стандартными подходами» в этом файле быть не должно — только то, что прямо есть в коде.

> **Статус:** в работе. Прочитанные файлы перечислены в разделе «История чтения» в конце.

> **⚠️ Cleanup 2026-05-13/14.** Часть описанного ниже мёртвого кода была удалена из репозитория. Конкретные `file:line` ссылки на удалённые места могут не существовать в текущем `HEAD` — детальная история удалений в `git log`.

---

## 1. Точка входа воркера

**Файл:** `worker/app/main.py`

- Только GPU-режим. API-режим (MVSEP + OpenAI Whisper) удалён — `worker/app/main.py:1-5` (комментарий в шапке).
- Логирование через `structlog`, процессоры: `add_log_level`, `TimeStamper(fmt="iso")`, `JSONRenderer` — `worker/app/main.py:184-190`.
- Подключение к PostgreSQL: `asyncpg.create_pool(dsn, min_size=2, max_size=10)` — `worker/app/main.py:27-30`.
- Подключение к RabbitMQ: `RabbitMQClient(settings.rabbitmq_url)` → `connect()` → `declare_topology()` — `worker/app/main.py:197-199`.
- Подключение к S3: `S3Storage(bucket, endpoint_url, access_key, secret_key)` — `worker/app/main.py:202-207`.
- Перед запуском консьюмера вызывается `repo.reset_stale_running_jobs(worker_id)` — задачи, висящие в RUNNING с прошлого запуска, сбрасываются — `worker/app/main.py:219-221`.
- Прогресс публикуется через `ProgressPublisher(rmq)`, передаётся в `JobService(repo, publisher=publisher)` — `worker/app/main.py:215-216`.
- Обработка сигналов: `SIGTERM`, `SIGINT` → `consumer.stop()` — `worker/app/main.py:233-235`.
- В `finally` вызывается `pipeline.cleanup()` (если есть), `rmq.close()`, `pool.close()` — `worker/app/main.py:240-243`.

### 1.1. Состав GpuPipeline (как собирается)

`_build_gpu_pipeline(...)` — `worker/app/main.py:33-179`.

Импортируются и создаются компоненты в таком порядке:

| Компонент | Класс | Источник модели / провайдера | Условие |
|---|---|---|---|
| Источниковое разделение | `UVRSeparator` | модель из `settings.uvr_model_name`, см. п. 2 | всегда |
| Доп. разделение лидер/бэк-вокал | `BackVocalSeparator` | `settings.back_vocal_model_name` | если `back_vocal_enabled` |
| ASR | `WhisperTranscriber` | `whisper_model_size`, `whisper_device`, `whisper_compute_type` | всегда |
| VAD | `VADProcessor(top_db=settings.vad_top_db)` | — | всегда |
| Поиск текстов: text-провайдеры | `GeniusProvider` | `settings.genius_token` | если задан токен |
| Поиск текстов: metadata-провайдеры | `LRCLibProvider`, `LyricsOvhProvider` | — | всегда |
| Парсер имени файла | `FilenameParser` | `deepseek_api_key`, `deepseek_model` | если задан DeepSeek-ключ |
| Расширение запросов | `LyricsExpander` | DeepSeek (опц.) | всегда (LLM опц.) |
| Сопоставление кандидатов | `LyricsMatcher(expander, deepseek_api_key, model)` | DeepSeek (опц.) | всегда |
| Резервный агент | `LyricsAgent` | DeepSeek + Yandex Search и/или SearXNG | если есть DeepSeek-ключ И один из бэкендов поиска |
| Цепочка провайдеров | `LyricsProviderChain` | — | всегда |
| Послоговое выравнивание | `TorchCTCAligner(device="cuda", …)` | `model_cache_dir`, флаги MMS из конфига | всегда |

Финальная сборка — `GpuPipeline(job_service, uvr, back_vocal_separator, repo, whisper, vad_processor, lyrics_searcher, ctc_aligner, storage, rmq, settings)` — `worker/app/main.py:167-179`.

> **Замечание:** `TorchCTCAligner` создаётся с **`device="cuda"`** прямо хардкодом в `worker/app/main.py:156`. Параметр `settings.ctc_device` (по умолчанию `"cpu"`) сюда не передаётся.

---

## 2. Конфигурация воркера

**Файл:** `worker/app/config.py`. Класс `WorkerSettings(BaseSettings)`, `pydantic-settings`, `env_prefix=""` — `worker/app/config.py:14, 134`.

### 2.1. Инфраструктура — `worker/app/config.py:18-35`

| Параметр | Значение по умолчанию |
|---|---|
| `pg_dsn` | `postgresql://karaoke:karaoke@postgres:5432/karaoke` |
| `media_root` | `/data/media` |
| `s3_bucket` | `karaoke` |
| `s3_endpoint_url` | `http://minio:9000` |
| `s3_access_key`, `s3_secret_key` | `minioadmin` |
| `rabbitmq_url` | `amqp://karaoke:karaoke@rabbitmq:5672/` |
| `model_cache_dir` | `/data/models` |
| `worker_id` | `f"{socket.gethostname()}-{os.getpid()}"` |
| `poll_interval_sec` | `2.0` |
| `log_level` | `INFO` |

### 2.2. Поиск текстов — `worker/app/config.py:38-52`

| Параметр | Значение по умолчанию |
|---|---|
| `deepseek_api_key` | `""` |
| `deepseek_model` | `"deepseek-chat"` |
| `searxng_url` | `"http://searxng:8080"` |
| `yandex_search_api_key`, `yandex_search_folder_id` | `""` |
| `lyrics_agent_max_iterations` | `15` |
| `lyrics_agent_timeout` | `15.0` сек |
| `genius_token` | `""` |
| `lyrics_provider_timeout` | `10.0` сек |
| `lyrics_search_fragments` | `2` |

### 2.3. CTC-выравнивание — `worker/app/config.py:55-94`

| Параметр | Значение по умолчанию | Замечание из кода |
|---|---|---|
| `ctc_min_frames_for_char` | `10` | — |
| `ctc_device` | `"cpu"` | Комментарий `worker/app/config.py:60-63`: ONNX-граф wav2vec2 имеет 24 op'а, не поддерживаемых CUDA EP, что приводит к постоянному CPU↔GPU memcpy. **На `TorchCTCAligner` не подаётся.** |
| `ctc_batch_size` | `16` | — |
| `mms_pre_trim_enabled` | `True` | предобрезка интро через Silero VAD |
| `mms_pre_trim_threshold` | `0.7` | — |
| `mms_pre_trim_min_speech_ms` | `300` | — |
| `mms_pre_trim_lead_in_ms` | `100` | **deprecated** (см. `worker/app/config.py:73-75`) — заменён `_refine_silero_onset` через RMS back-tracking |
| `mms_line_start_rms_adjust` | `True` | пост-пасс «sandwich-RMS-dip» для коррекции начала строк |
| `mms_word_end_drift_adjust` | `True` | пост-пасс коррекции хвоста слов через RMS back-track |
| `mms_word_end_sustain_extend` | `True` | пост-пасс forward RMS walk для «удержания» хвостовой гласной |

### 2.4. VAD — `worker/app/config.py:97-100`

- `vad_top_db = 16`.

### 2.5. UVR (источниковое разделение) — `worker/app/config.py:103-113`

- Модель: **`model_bs_roformer_ep_317_sdr_12.9755.ckpt`** (BS-Roformer ViperX ep_317).
- `uvr_torch_device = "cuda"`.
- `uvr_chunk_batch_size = 2`.
- `uvr_use_autocast = True`.
- `uvr_overlap = 8.0`.
- Комментарий `worker/app/config.py:104-107`: «Revive 2 был оценён, но отклонён — слишком агрессивно чистит вокал, ломает Whisper и матчер выбирает не ту версию песни».

### 2.6. Back-vocal (разделение лидер/бэк) — `worker/app/config.py:117-124`

- `back_vocal_enabled = True`.
- Модель: **`mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt`**.
- `back_vocal_torch_device = "cuda"`.
- `back_vocal_chunk_batch_size = 2`.
- `back_vocal_use_autocast = True`.
- `back_vocal_overlap = 4.0`.

### 2.7. Whisper — `worker/app/config.py:128-132`

- `whisper_model_size = "medium"`.
- `whisper_device = "cuda"`.
- `whisper_compute_type = "float16"`.
- Комментарий-заголовок секции `worker/app/config.py:127`: **«GPU mode: faster-whisper local ASR»** — указывает на faster-whisper / CTranslate2.

---

## 3. Консьюмер задач (RabbitMQ)

**Файл:** `worker/app/consumer.py`. Класс `JobConsumer`.

- Имя очереди: **`"jobs.process"`** — `worker/app/consumer.py:53`.
- Семантика доставки: **`prefetch_count=1`** — `worker/app/consumer.py:55`.
- Тело сообщения: JSON, ожидается ключ `job_id`. Комментарий-шапка `worker/app/consumer.py:1-6` упоминает `mp3_key`, но в коде используется только `job_id` — `worker/app/consumer.py:73-74`.
- Алгоритм обработки сообщения (`_on_message`, `worker/app/consumer.py:68-103`):
  1. `repo.lock_job(job_id, worker_id)` — если не удалось залочить, **`nack(requeue=True)`**.
  2. `repo.get_job(job_id)` — если запись отсутствует, **`ack()`** без обработки.
  3. `pipeline.process(job)`.
  4. При успехе — **`ack()`**.
  5. При исключении — **`nack(requeue=False)`** (то есть в DLQ через DLX-настройки топологии RabbitMQ).
- Класс хранит флаг `_running`, цикл `while self._running: await asyncio.sleep(1)` — `worker/app/consumer.py:60-61`.
- `stop()` устанавливает `_running = False` — `worker/app/consumer.py:63-66`.
- Шапка-комментарий: «RabbitMQ-based job consumer — replaces DB-polling JobPoller» — то есть до этого был DB-poll, теперь RabbitMQ — `worker/app/consumer.py:1`.

---

## 4. Базовый класс пайплайна

**Файл:** `worker/common/base_pipeline.py`.

- Абстрактный класс `BasePipeline` с двумя методами: `process(job: Job) -> None` и `cleanup() -> None` — `worker/common/base_pipeline.py:8-25`.
- Единственная реализация — `worker.gpu.gpu_pipeline.GpuPipeline` (отмечено в docstring, `worker/common/base_pipeline.py:11-13`).

---

## 5. Константы (`shared/karaoke_shared/constants.py`)

- `PipelineStep` (StrEnum, `shared/karaoke_shared/constants.py:94-107`):
  - `SEPARATING = "separating"`
  - `BACK_VOCAL_SEPARATING = "back_vocal_separating"`
  - `VAD = "vad"`
  - `TRANSCRIBING = "transcribing"`
  - `SEARCHING_LYRICS = "searching_lyrics"`
  - `ALIGNING = "aligning"`
  - `LINE_BREAKING = "line_breaking"`
- Комментарий `shared/karaoke_shared/constants.py:96-99`: «Feature extraction, lyric embedding, and QDrant sync have moved to the Rec Service and are no longer part of the worker pipeline» — раньше эти шаги были в воркере.
- `JobStatus` (StrEnum): `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`.
- `TrackStatus` (StrEnum): `PENDING`, `PROCESSING`, `READY`, `ERROR`.
- `TrackSource` (StrEnum): `CATALOG`, `USER_UPLOAD`.
- `QueueEntryStatus`, `SessionStatus`, `PopularityCategory` — для смежных подсистем (кроме воркера).
- QDrant-коллекции: `COLLECTION_AUDIO_FEATURES = "audio_features"`, `COLLECTION_LYRICS_EMBEDDINGS = "lyrics_embeddings"`. Размерности — 45 и 384. Это для рекомендаций (вне ВКР).

---

## 6. Главный пайплайн `GpuPipeline`

**Файл:** `worker/gpu/gpu_pipeline.py`.

Шапка-документация перечисляет 7 шагов (`worker/gpu/gpu_pipeline.py:1-14`). Все семь идут последовательно; параллельно запущена только фоновая задача кодирования + загрузки инструментала, она перекрывает шаги 2..7, но в нумерацию не входит.

1. **separating** — UVR separation (BS-Roformer), вокал/инструментал.
2. **back_vocal_separating** — Mel-Band RoFormer aufr33, лидер/бэк.
3. **VAD на FULL vocals** (CPU) — «backing vocals help Whisper recognise the track».
4. **transcribing** — Whisper ASR на VAD-cleaned FULL vocals.
5. **searching_lyrics** — LLM lyrics search / провайдеры.
6. **CTC alignment на LEAD vocals**.
7. **Line break detection** (CPU).

Шапка явно говорит про `faster-whisper ASR transcriber` в docstring класса (`worker/gpu/gpu_pipeline.py:53`).

### 6.1. Реальная последовательность процесса (`process(job)`, `worker/gpu/gpu_pipeline.py:88-326`)

1. Проверка `job.mp3_key` — если пусто, `mark_failed` и выход.
2. `pipeline_t0 = time.monotonic()` — фиксация времени старта.
3. `storage.download_to_file(job.mp3_key, "/tmp/{job.id}.mp3")` — скачивание оригинала.
4. **STEP 1 (`mark_step("separating", 0)`)**: `_separate_with_fallback(local_mp3)` → `(vocals_path, instrumental_path)`. См. п. 6.3.
5. `asyncio.to_thread(self.uvr.cleanup)` — освобождение VRAM UVR.
6. `mark_step("separating", 100)` сразу после очистки UVR — STEP 1 закрыт.
7. **Запуск фоновой задачи** `_encode_and_upload_instrumental(...)` — конвертация инструментала и заливка в S3 идёт **параллельно** со STEP 2..7. См. п. 6.2.
8. **STEP 2 (`back_vocal_separating`)** — выполняется только если `back_vocal_separator` не None:
   - `mark_step("back_vocal_separating", 0)`.
   - `back_vocal_separator.separate(vocals_path)` → `(lead_vocals_path, _backing_path)`. На исключении логируется `back_vocal_separation_failed_falling_back_to_full_vocals`, `lead_vocals_path = vocals_path`. В `finally` — `cleanup` бэк-вокального сепаратора.
   - `mark_step("back_vocal_separating", 100)`.
9. **STEP 3 (`_vad`)** на полных вокалах:
   - `mark_step("vad", 0)`.
   - `vad_processor.process(vocals_path)` → `cleaned_path` (`VADResult.cleaned_path`).
   - `mark_step("vad", 100)`.
10. **STEP 4 (`_transcribe`)** на VAD-cleaned полных вокалах:
    - `mark_step("transcribing", 0)`.
    - `whisper.transcribe(cleaned_path)` → `WhisperResult{text, language}`.
    - `mark_step("transcribing", 100)`.
11. `whisper.cleanup()` — освобождение VRAM.
12. **STEP 5 (`searching_lyrics`)**:
    - `mark_step("searching_lyrics", 0)`.
    - Если `lyrics_searcher is None` → `mark_permanently_failed("Lyrics agent not configured…")`.
    - `lyrics_searcher.search(asr_text=whisper_result.text, detected_language=whisper_result.language, artist_hint=job.artist_hint, title_hint=job.title_hint, filename=(job.data or {}).get("filename"))` → `lyrics_result`.
    - На `LyricsSearchError` → `mark_permanently_failed`.
    - `mark_step("searching_lyrics", 100)`.
    - `repo.update_job_data(job.id, {artist, title, lyrics, language})`.
13. **STEP 6 (`aligning`)** на LEAD vocals:
    - `mark_step("aligning", 0)`.
    - `ctc_aligner.align(lead_vocals_path, lyrics_result.lyrics, lyrics_result.language)` → `(syllable_timings, align_stats)`. `align_stats` содержит `total_words`, `char_level_used`, `proportional_fallback`.
    - `mark_step("aligning", 100)`.
    - `cleanup` ctc_aligner.
14. **STEP 7 (`line_breaking`)**:
    - `mark_step("line_breaking", 0)`.
    - Ленивый `from karaoke_shared.utils.line_breaker import detect_line_breaks`.
    - `detect_line_breaks(syllable_timings, lead_vocals_path)` → обновлённый `syllable_timings`.
    - `mark_step("line_breaking", 100)`.
15. Удаление временных файлов:
    - `vocals_path` (полный вокал)
    - `lead_vocals_path` (если отличается)
    - `…_(Backing).wav` (рассчитывается из имени lead-файла)
    - `cleaned_vocals_{id}.wav` (от VAD)
16. **Финализация**:
    - `await instrumental_upload_task` — ждём завершения фоновой загрузки инструментала.
    - `repo.get_job(job.id)` → читаем актуальный `job.data` (там уже лежит `instrumental_key`).
    - `repo.create_track(TrackCreate(artist, title, source="user_upload", instrumental_key, lyrics_text, lyrics_source=lyrics_result.source_note, syllable_timings, language, status="ready"))` → `track` (поле `qdrant_synced` берёт дефолт `0` из модели).
    - `repo.set_job_track_id(job.id, track.id)`.
    - `job_service.mark_completed(job.id, {track_id, instrumental_key, language})`.
17. **Публикация события для рекомендаций**: `rmq.publish("rec", "", {"track_id": track_id, "mp3_key": job.mp3_key, "lyrics": lyrics_result.lyrics})` — `worker/gpu/gpu_pipeline.py:296-304`.
18. `logger.info("pipeline_completed", ..., total_duration_sec=…)`.
19. Удаление `/tmp/{job.id}.mp3` и `instrumental_path`.
20. **На исключении (`except Exception`)** — `worker/gpu/gpu_pipeline.py:317-326`:
    - `logger.error("pipeline_failed", ..., exc_info=True)`.
    - `asyncio.to_thread(self.cleanup)` — общий cleanup всех моделей (под защитой try/except).
    - `job_service.mark_permanently_failed(job.id, str(exc))`.

### 6.2. Параллелизация I/O и вычислений

- `_encode_and_upload_instrumental` запускается через `asyncio.create_task(...)` сразу после UVR (`worker/gpu/gpu_pipeline.py:115-122`) и **не ожидается** до самой финализации. Внутри: `ffprobe` (детектирование битрейта оригинала; default `192k`) → `ffmpeg -codec:a libmp3lame -b:a {bitrate}` → `storage.upload(key, bytes)` → удаление tmp → `repo.update_job_data(..., {"instrumental_key": ...})` — `worker/gpu/gpu_pipeline.py:341-389`.
- Все «тяжёлые» вычисления (UVR, back-vocal, VAD, Whisper, CTC, line breaker, ffmpeg-конвертация) обёрнуты в `asyncio.to_thread(...)` или запускаются как асинхронные подпроцессы.

### 6.3. Поведение при OOM на GPU (`_separate_with_fallback`)

**Файл:** `worker/gpu/gpu_pipeline.py:391-409`.

- Try: `asyncio.to_thread(self.uvr.separate, mp3_path)`.
- Catch `RuntimeError`: если в сообщении встречается `"out of memory"` или `"cuda"` (case-insensitive) — `cleanup` старого UVR и **пересоздание `UVRSeparator` с `torch_device="cpu", chunk_batch_size=1, use_autocast=False`** (другие параметры берутся через приватные поля `_model_name`, `_overlap`).
- Иные `RuntimeError` пробрасываются дальше.

### 6.4. Освобождение VRAM (`cleanup`)

**Файл:** `worker/gpu/gpu_pipeline.py:328-335`.

- Метод `cleanup` вызывает `cleanup` у `uvr`, `back_vocal_separator` (если не None), `whisper`, `ctc_aligner` (если у того есть метод).
- Также `cleanup` у каждого компонента вызывается **точечно** после соответствующего шага (UVR — после separating; back_vocal — после back-vocal split; whisper — после ASR; ctc_aligner — после alignment), чтобы освободить VRAM сразу.

### 6.5. Прогресс через `mark_step`

В коде `process(...)` (и helper'ах `_vad`, `_transcribe`) явно вызывается `job_service.mark_step` для **всех** константных шагов `PipelineStep`:

- `separating` — 0% и 100%.
- `back_vocal_separating` — 0% и 100% (только если `back_vocal_separator` не None; иначе шаг отсутствует целиком).
- `vad` — 0% и 100% (внутри `_vad`).
- `transcribing` — 0% и 100% (внутри `_transcribe`).
- `searching_lyrics` — 0% и 100%.
- `aligning` — 0% и 100%.
- `line_breaking` — 0% и 100%.

Прогресс-колбэков **внутри** конкретных моделей (UVR, BackVocal, VAD, Whisper, CTC, line breaker) нет — каждый шаг репортит только начало и конец, без промежуточных значений. То есть SSE-канал обновляется ровно 12 (или 14, если включён back-vocal) раз за задачу.

### 6.6. Финализация и интеграция со смежной подсистемой

- Создание `Track` с `status="ready"` (значение `qdrant_synced=0` приходит дефолтом из модели `TrackCreate`).
- Сообщение в exchange `"rec"` (routing key — пустая строка) для рекомендательной подсистемы. Поля: `track_id`, `mp3_key`, `lyrics`.

### 6.7. Типизация

- В `worker/gpu/gpu_pipeline.py:33` импортируется `from worker.common.ctc_aligner import CTCAligner` — используется как тайп-хинт `ctc_aligner: CTCAligner`. Фактически передаётся `TorchCTCAligner` (см. п. 1.1). Похоже, что `CTCAligner` — общий протокол/абстрактный класс. Прочитать на следующем шаге.
- `_parse_hints_from_path` (статический метод, `worker/gpu/gpu_pipeline.py:440-447`) парсит «Artist - Title.mp3» — **в `process()` не вызывается**, хинты берутся напрямую из `job.artist_hint`, `job.title_hint`.

---

## 7. Несоответствия и открытые вопросы

Несоответствия реализации воркера промышленным стандартам разработки (надёжность, observability, тестируемость, управление ресурсами) вынесены в отдельный документ — [`journals/WORKER_AUDIT.md`](./WORKER_AUDIT.md). Там же — приоритезация и сводная таблица.

Этот файл (`WORKER_FACTS.md`) остаётся строго фактологическим: только то, что прямо есть в коде, без оценочных суждений (см. шапку).

Последняя сверка — 2026-05-14. Открытых расхождений с `CLAUDE.md` (помимо отмеченных ✅/⚠️/🆕 в разделах 8.7, 9.7, 10.7, 11.18, 12.7, 15.9, 17.8, 21.7, 23.7, 24.10, 25.7, 26.7) на этот момент нет. Детальная история — в `git log`.

---

## 8. Источниковое разделение — `UVRSeparator` (BS-Roformer)

**Файл:** `worker/gpu/uvr_separator.py`. Класс `UVRSeparator`.

### 8.1. Назначение и общий подход

- Прямая PyTorch-инференция BS-Roformer **без** обёртки `audio-separator` — `worker/gpu/uvr_separator.py:1-8`. В docstring перечислены целевые отличия от обёртки:
  - `torch.inference_mode()` вместо `torch.no_grad()`.
  - Batched chunk processing (несколько чанков за один forward).
  - Overlap-add **на GPU**, без CPU↔GPU-перетаскивания после каждого чанка.
  - Native autocast FP16.

### 8.2. Модель

- Класс модели импортируется из стороннего пакета: `from audio_separator.separator.uvr_lib_v5.roformer.bs_roformer import BSRoformer` — `worker/gpu/uvr_separator.py:93-95`. То есть код модели — внешний, в проекте используется только её инференц-обёртка.
- Имя чекпоинта по умолчанию: `MODEL_NAME = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"` — `worker/gpu/uvr_separator.py:65`.
- Архитектурный конфиг — `_MODEL_CONFIG`, `worker/gpu/uvr_separator.py:20-43`:

  | Параметр | Значение |
  |---|---|
  | `dim` | `512` |
  | `depth` | `12` |
  | `stereo` | `True` |
  | `num_stems` | `1` |
  | `time_transformer_depth`, `freq_transformer_depth` | `1`, `1` |
  | `dim_head`, `heads` | `64`, `8` |
  | `attn_dropout`, `ff_dropout` | `0.1`, `0.1` |
  | `flash_attn` | `True` |
  | `mask_estimator_depth` | `2` |
  | STFT: `n_fft`, `hop_length`, `win_length`, `normalized` | `2048`, `441`, `2048`, `False` |
  | `freqs_per_bands` | кастомный кортеж из 62 элементов |

- Загрузка чекпоинта (`_ensure_model`, `worker/gpu/uvr_separator.py:87-117`):
  - Ленивая (при первом вызове `separate`).
  - `torch.load(model_path, map_location="cpu")`; если в файле есть ключ `"state_dict"` — разворачивается.
  - `.to(device).half().eval()` — то есть **FP16 на устройстве**, eval-режим.
  - Логируется `uvr_model_loaded` с числом параметров в миллионах.

### 8.3. Аудио-параметры (модульные константы)

- `_SAMPLE_RATE = 44100` — `worker/gpu/uvr_separator.py:46`.
- `_STFT_HOP = 441`.
- `_DIM_T = 801` (комментарий: «model's inference.dim_t»).
- `_CHUNK_SIZE = _STFT_HOP * (_DIM_T - 1) = 352_800` — комментарий «~8 sec at 44.1kHz».

### 8.4. Алгоритм `separate(mp3_path)` — `worker/gpu/uvr_separator.py:119-262`

1. Импорты внутри метода: `soundfile`, `torch`, `torchaudio.functional as F`, `scipy.signal.windows`.
2. **Загрузка аудио**: `sf.read(mp3_path, dtype="float32")` → `(samples, channels)`. Преобразуется в `(channels, samples)` через `torch.from_numpy(data.T)`.
3. **Ресэмплинг**: если `sr != 44100` — `torchaudio.functional.resample(mix, sr, 44100)`.
4. **Принудительное стерео**: 1 канал → `repeat(2,1)`; >2 каналов → `mix[:2]`.
5. **Нормализация по амплитуде**: `peak = mix.abs().max()`; если `peak > 0` → `mix = mix * (0.9 / peak)` — пиковая нормализация к 0.9.
6. **Чанкование**:
   - `chunk_size = 352_800`.
   - `desired_step = int(self._overlap * 44100)`; `step = min(desired_step, chunk_size)` — параметр `overlap` интерпретируется как шаг между чанками **в секундах** (а не как доля overlap, как может звучать имя).
   - Окно: `scipy.signal.windows.hamming(chunk_size)` на устройстве.
   - Аккумуляторы `result` (2 канала) и `weight` (моно) — оба `torch.zeros(...)` на GPU.
   - Список стартов чанков; если последний `start + chunk_size < num_samples`, добавляется `num_samples - chunk_size` чтобы покрыть хвост.
7. **Forward в `torch.inference_mode()`**:
   - Цикл `for batch_start in range(0, len(starts), self._chunk_batch_size)`.
   - Сбор `chunks` с padding нулями для последнего короткого чанка.
   - `batch = torch.stack(chunks).to(device)`.
   - Если `use_autocast and torch_device == "cuda"` → `with torch.amp.autocast("cuda")`.
   - Иначе — без autocast.
   - **Overlap-add на GPU**: для каждого индекса `windowed = vocals_batch[i, :, :length] * window[:length]`; `result[:, idx:idx+length] += windowed`; `weight[idx:idx+length] += window[:length]`.
8. **Нормализация по weight**: `weight.clamp(min=1e-8)`; `vocals = result / weight.unsqueeze(0)`.
9. **Инструментал**: `instrumental = mix_gpu - vocals` (т. е. residual — оригинальный микс минус извлечённый вокал).
10. **Де-нормализация**: `vocals` и `instrumental` умножаются на `peak / 0.9` для восстановления исходной амплитуды.
11. **Сохранение WAV**:
    - **Vocals: ресэмплируется до 16 kHz и сводится в моно** через `mean(dim=0, keepdim=True)`. Запись `sf.write(vocals_path, vocals_16k.numpy().T, 16000, subtype="PCM_16")`. Файл: `{job_id}_(Vocals).wav`.
    - **Instrumental: остаётся в 44.1 kHz стерео**. Файл: `{job_id}_(Instrumental).wav`.
12. Директория вывода: `{media_root}/instrumental` (создаётся в `_ensure_model`).
13. Логи: `uvr_starting`, `uvr_chunking` (debug), `uvr_completed` с `duration_sec`.

### 8.5. `cleanup()` — `worker/gpu/uvr_separator.py:264-282`

- `del self._model; self._model = None`.
- `gc.collect()`.
- `torch.cuda.empty_cache()` (если CUDA доступна; ловится `ImportError`).
- Лог `uvr_cleanup_done`.

### 8.6. Дефолты класса vs дефолты `WorkerSettings`

В `__init__` (`worker/gpu/uvr_separator.py:67-76`) дефолты `chunk_batch_size=4`, `overlap=4`. В `WorkerSettings` (`worker/app/config.py`) фактические значения, передаваемые из `_build_gpu_pipeline`, — `chunk_batch_size=2`, `overlap=8.0`. Несовпадение дефолтов класса и конфига — норма (приоритет у конфига).

### 8.7. Соответствие CLAUDE.md

- ✅ «Direct PyTorch BS-Roformer inference. Model loaded in FP16, batched chunk processing with overlap-add on GPU, autocast enabled.» — соответствует.
- ✅ «Vocals output as 16kHz mono WAV (ready for VAD/Whisper).» — соответствует (`worker/gpu/uvr_separator.py:243`).
- ✅ «Instrumental WAV→MP3 conversion (ffmpeg, matching original bitrate via ffprobe) and S3 upload run as background asyncio task parallel to VAD+Whisper.» — реализуется не здесь, а в `gpu_pipeline.py:341-389` (см. п. 6.2).

---

## 9. Разделение лидер/бэк-вокал — `BackVocalSeparator` (Mel-Band RoFormer aufr33)

**Файл:** `worker/gpu/back_vocal_separator.py`. Класс `BackVocalSeparator`.

### 9.1. Назначение

- Делит выходной вокал из `UVRSeparator` (16 kHz mono) на «лидер» (`Lead`) и «бэк-вокал» (`Backing`) — `worker/gpu/back_vocal_separator.py:1-10`.
- Внутри: апсэмплинг до 44.1 kHz стерео (нативный SR модели) → инференция → даунсэмплинг обратно до 16 kHz моно — `worker/gpu/back_vocal_separator.py:5-6, 142-152, 230-232`.

### 9.2. Модель

- Класс: `MelBandRoformer` из `audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer` — `worker/gpu/back_vocal_separator.py:95-97`. Внешний пакет, как и для UVR.
- Чекпоинт: `MODEL_NAME = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"` — `worker/gpu/back_vocal_separator.py:67`.
- Архитектурный конфиг — `_MODEL_CONFIG`, `worker/gpu/back_vocal_separator.py:22-46`:

  | Параметр | Значение |
  |---|---|
  | `dim` | `384` |
  | `depth` | `6` |
  | `stereo` | `True` |
  | `num_stems` | `1` |
  | `num_bands` | `60` |
  | `time_transformer_depth`, `freq_transformer_depth` | `1`, `1` |
  | `dim_head`, `heads` | `64`, `8` |
  | `attn_dropout`, `ff_dropout` | `0.0`, `0.0` |
  | `flash_attn` | `True` |
  | `dim_freqs_in` | `1025` |
  | `sample_rate` | `44100` |
  | STFT: `n_fft`, `hop_length`, `win_length`, `normalized` | `2048`, `441`, `2048`, `False` |
  | `mask_estimator_depth` | `2` |
  | `multi_stft_resolution_loss_weight` | `1.0` |
  | `multi_stft_resolutions_window_sizes` | `(4096, 2048, 1024, 512, 256)` |
  | `multi_stft_hop_size` | `147` |
  | `multi_stft_normalized` | `False` |

- По сравнению с BS-Roformer: у MelBand `dim` меньше (384 vs 512) и `depth` меньше (6 vs 12) → модель легче. Параметры `multi_stft_*` относятся к multi-resolution STFT loss (только тренировка, в инференсе игнорируются).
- Загрузка модели — структурно идентична `UVRSeparator`: lazy `_ensure_model`, `torch.load(map_location="cpu")`, разворачивание `state_dict`, `.to(device).half().eval()`, лог `back_vocal_model_loaded` с `params_m`.

### 9.3. Аудио-параметры

- `_SAMPLE_RATE = 44100`, `_STFT_HOP = 441`, `_DIM_T = 801`, `_CHUNK_SIZE = 352_800` (~8 с) — те же, что у UVR.

### 9.4. Алгоритм `separate(vocals_path)` — `worker/gpu/back_vocal_separator.py:121-252`

Структурно идентичен UVR (см. п. 8.4). Отличия по существу:

1. **Вход**: `vocals_path` (16 kHz моно от `UVRSeparator`). Если `sr != 44100` → ресэмплинг до 44.1 kHz; если 1 канал → `repeat(2,1)`.
2. **Выход модели**: `lead`. **Backing** считается как `mix - lead` (residual) — `worker/gpu/back_vocal_separator.py:222-223`.
3. **Оба выхода ресэмплируются обратно в 16 kHz моно** через `mean(dim=0, keepdim=True)` — `worker/gpu/back_vocal_separator.py:230-232`.
4. **Имена файлов**: stem от vocals (`"{job_id}_(Vocals)"`) → base_id без суффикса. На выходе: `{base_id}_(Lead).wav` и `{base_id}_(Backing).wav`. Директория — та же `{media_root}/instrumental`. Все WAV — `subtype="PCM_16"`, 16 kHz моно.
5. Логи: `back_vocal_starting`, `back_vocal_chunking` (debug), `back_vocal_completed` с `duration_sec`.

### 9.5. Параметр `overlap`

- В docstring `__init__` явно сказано: «Chunk step in seconds (smaller = more overlap, slower, better quality)» — `worker/gpu/back_vocal_separator.py:64`. То есть значение **в секундах**, и это шаг (step), а не доля overlap.
- Дефолт класса и `WorkerSettings` совпадают: `overlap = 4.0` сек. При `chunk_size ≈ 8` сек это даёт ~50 % overlap между соседними чанками.
- Для сравнения: у `UVRSeparator` дефолт `WorkerSettings` — `overlap = 8.0` сек, что при таком же `chunk_size = 8` сек даёт **0 % overlap (чанки встык)**. То есть осознанно: тяжёлая модель UVR обрабатывается без overlap (быстрее), а более лёгкая Mel-Band — с 50 % overlap (для качества).

### 9.6. `cleanup()` — `worker/gpu/back_vocal_separator.py:254-272`

Идентичен UVR: `del model`, `gc.collect()`, `torch.cuda.empty_cache()`. Лог `back_vocal_cleanup_done`.

### 9.7. Соответствие CLAUDE.md

- ✅ «BACK_VOCAL_SEPARATING: BackVocalSeparator … splits the UVR vocals into lead and backing stems» — соответствует. Шаг выделен в отдельную константу `PipelineStep.BACK_VOCAL_SEPARATING` и сопровождается своей парой `mark_step(..., 0/100)` в `gpu_pipeline.py` (только если `back_vocal_separator` не None).
- ✅ «Falls back to full vocals if the separator fails» — это поведение реализовано не здесь, а в `gpu_pipeline.py` (try/except → `lead_vocals_path = vocals_path`).
- ⚠️ Шапка-комментарий самого файла (`worker/gpu/back_vocal_separator.py:8-9`) утверждает: «Downstream VAD/Whisper/CTC steps consume the lead_vocals output» — это **устаревшая документация**, повторяющая ошибочное утверждение из CLAUDE.md. Реальное поведение в `gpu_pipeline.py`: VAD и Whisper работают на full vocals, lead — только для CTC и line breaker. Формулировка в CLAUDE.md уже исправлена в cleanup'е 2026-05-13 (см. `git log`).

---

## 10. ASR — `WhisperTranscriber`

**Файл:** `worker/gpu/whisper_transcriber.py`. Класс `WhisperTranscriber`, dataclass `WhisperResult`.

### 10.1. Backend: HuggingFace Transformers (PyTorch-native)

- Шапка-комментарий `worker/gpu/whisper_transcriber.py:1-6`: «Uses HuggingFace Transformers (PyTorch-native) for local speech-to-text».
- Docstring класса `worker/gpu/whisper_transcriber.py:36-39`: «PyTorch-native Whisper transcriber via HuggingFace Transformers. **No CTranslate2** — avoids ~28 s CUDA kernel JIT on first inference».
- Импорт: `from transformers import WhisperForConditionalGeneration, WhisperProcessor` — `worker/gpu/whisper_transcriber.py:67`.
- Лог при загрузке: `logger.info("whisper_loaded", ..., backend="transformers")` — `worker/gpu/whisper_transcriber.py:89-94`.

Реальный backend — Transformers, как и утверждает `CLAUDE.md`. Шапка `worker/app/config.py:127` («GPU mode: faster-whisper local ASR») и docstring `GpuPipeline` («faster-whisper ASR transcriber», `worker/gpu/gpu_pipeline.py:53`) были **устаревшими** комментариями (после миграции с faster-whisper их не обновили) — исправлены в cleanup'е 2026-05-13 (см. `git log`).

### 10.2. Параметры конструктора и MODEL_ID_MAP

- Defaults: `model_size="medium"`, `device="cuda"`, `compute_type="float16"`, `model_cache_dir=None` — `worker/gpu/whisper_transcriber.py:49-55`.
- `MODEL_ID_MAP` — `worker/gpu/whisper_transcriber.py:18-23`: `tiny → openai/whisper-tiny`, `base → openai/whisper-base`, `small → openai/whisper-small`, `medium → openai/whisper-medium`. Для других значений — fallback `f"openai/whisper-{model_size}"`. **`large`/`large-v2`/`large-v3` явно не в карте**, но через fallback тоже сработает.
- При фактических `WorkerSettings`: загружается **`openai/whisper-medium`**.
- `compute_type="float16"` в этом коде — **не CTranslate2-параметр**, а простой флаг для выбора `torch.float16` против `torch.float32` (`worker/gpu/whisper_transcriber.py:73-77`): `torch.float16` если `device == "cuda"` и в `compute_type` есть подстрока `"16"`, иначе — `torch.float32`. Имя случайно совпадает с CT2-параметром, что и сбило с толку при чтении конфига.

### 10.3. Загрузка модели — `_load_model`, `worker/gpu/whisper_transcriber.py:65-94`

- **Загружается в конструкторе** (вызов `self._load_model()` в `__init__`, `worker/gpu/whisper_transcriber.py:63`).
- `WhisperProcessor.from_pretrained(model_id, cache_dir=…)`.
- `WhisperForConditionalGeneration.from_pretrained(model_id, cache_dir=…, dtype=self._torch_dtype).to(device)`.
- Локальная директория кеша HuggingFace — `settings.model_cache_dir = "/data/models"`.

### 10.4. `warmup()` — `worker/gpu/whisper_transcriber.py:96-116`

- Прогоняет инференцию на **30 с тишины** (один полный Whisper-чанк) с `max_new_tokens=440` для прогрева CUDA-ядер.
- **Нигде не вызывается** (проверено grep'ом по worker/ и shared/) — определение метода есть, но в фактическом потоке инициализации воркера (`worker/app/main.py`, сборка пайплайна) этот метод не дёргается.
- Следствие: JIT-компиляция CUDA-ядер случается **на первой реальной задаче** воркера.

### 10.5. Алгоритм `transcribe(audio_path)` — `worker/gpu/whisper_transcriber.py:118-236`

1. Импорты внутри метода: `soundfile`, `torch`, `torchaudio.functional as F`.
2. Если `self._model is None` → перезагрузка через `_load_model()`. То есть после `cleanup()` следующая задача снова загрузит модель из кеша.
3. **Загрузка аудио**: `sf.read(audio_path, dtype="float32")`. Если многоканально — `mean(axis=1)` → моно. Если `sr != 16000` — ресэмплинг через `torchaudio.functional.resample`.
4. **Чанкование 30-секундными окнами**: `chunk_samples = 30 * 16000 = 480_000`. Цикл `for chunk_start in range(0, len(audio), chunk_samples)`.
5. Для каждого чанка:
   - `processor(chunk, sampling_rate=16000, return_tensors="pt")` → `inputs`.
   - `input_features` переносятся на `device` с `dtype = self._torch_dtype`.
   - `with torch.no_grad():` (а не `inference_mode()`, как у UVR/BackVocal).
   - `model.generate(input_features, return_dict_in_generate=True, output_scores=True, max_new_tokens=440)`.
   - `chunk_text = processor.decode(token_ids, skip_special_tokens=True).strip()`.
6. **Детекция языка** (только на первом чанке, `worker/gpu/whisper_transcriber.py:179-208`):
   - Декодирование первых 4 токенов с `skip_special_tokens=False`.
   - Поиск подстроки `<|{lang_code}|>` в декодированной строке.
   - Закрытый список из 20 языков: `ru, en, es, fr, de, it, pt, zh, ja, ko, uk, pl, cs, tr, ar, hi, th, vi, nl, sv`.
   - Если не нашли — остаётся дефолт `language = "en"`.
7. **Confidence** (`worker/gpu/whisper_transcriber.py:211-223`):
   - Для каждого `score` из `output.scores` → `log_softmax(score[0], dim=-1)[tok].item()` → накопление `all_log_probs`.
   - `confidence = clamp(exp(avg_logprob), 0, 1)`. Дефолт `0.5` если log_probs пусты.
8. Финальный `text = " ".join(all_text_parts)`.
9. Лог `whisper_completed` с `language`, `confidence`, `text_length` и **полным текстом** (`text=text`), `duration_sec`.
10. Возврат `WhisperResult(text, language, confidence)`.

### 10.6. `WhisperResult` — `worker/gpu/whisper_transcriber.py:26-32`

```python
@dataclass
class WhisperResult:
    text: str         # full text, segments joined by ' '
    language: str     # two-letter code ('ru', 'en', ...)
    confidence: float # average log-prob → prob (0..1)
```

В `gpu_pipeline.py` используется только `text` и `language` (`worker/gpu/gpu_pipeline.py:184-185`), `confidence` нигде не читается — поле собирается, но ни на что не влияет.

### 10.7. `cleanup()` — `worker/gpu/whisper_transcriber.py:238-256`

- `del self._model; del self._processor`; обнуление полей; `gc.collect()`; `torch.cuda.empty_cache()`.
- Вызывается из `gpu_pipeline.py:164` **после каждой задачи** (`asyncio.to_thread(self.whisper.cleanup)`).
- Поскольку в `transcribe()` модель загружается заново при `self._model is None` — после cleanup следующая задача **повторно загружает модель из кеша HuggingFace** (без сетевого запроса, но с CPU→GPU переносом и autoload весов).
- Это **противоречило утверждению CLAUDE.md** «Model stays in VRAM between tracks (no per-job cleanup). First job on cold worker ~9s (CUDA JIT), subsequent ~1.8s.» — фактически модель выгружается между задачами и каждый раз загружается обратно. Формулировка в CLAUDE.md (секция TRANSCRIBING) исправлена в cleanup'е 2026-05-13 (см. `git log`).

---

## 11. CTC-выравнивание — `TorchCTCAligner`

**Файл:** `worker/gpu/torch_ctc_aligner.py` (1297 строк). Класс `TorchCTCAligner`, dataclass `AlignmentStats`. Это самый большой и содержательный модуль воркера.

### 11.1. Назначение, backend и изоляция

- Шапка-комментарий `worker/gpu/torch_ctc_aligner.py:1-6`: «GPU-accelerated CTC forced alignment via torchaudio. Uses MMS-300M forced aligner (315M params, 1130 languages) with native CUDA forced_align() kernel. **Runs in-process — no subprocess isolation needed (PyTorch doesn't have ONNX's heap corruption issues).**»
- Модель: `_HF_MODEL_ID = "MahmoudAshraf/mms-300m-1130-forced-aligner"` — `worker/gpu/torch_ctc_aligner.py:24`. Загружается через **HuggingFace transformers** (`Wav2Vec2ForCTC`, `Wav2Vec2Processor`), сам же **forced_align — через `torchaudio.functional.forced_align` + `merge_tokens`**. То есть веса берутся из HF, инференция — стандартный forward, дальше — нативные torchaudio-функции для CTC-декодирования.
- **Subprocess-изоляция отсутствует.** Файлы `worker/common/ctc_aligner.py` и `worker/common/ctc_subprocess.py` (старая ONNX-реализация) в актуальном пути runtime не вызываются — нужно подтвердить при чтении этих файлов (планируется).

### 11.2. Параметры конструктора — `worker/gpu/torch_ctc_aligner.py:47-100`

| Параметр | Default | Назначение |
|---|---|---|
| `device` | `"cuda"` | torch device |
| `model_cache_dir` | `None` | HF cache |
| `pre_trim_enabled` | `True` | Silero VAD pre-trim интро |
| `pre_trim_threshold` | `0.7` | Silero confidence threshold |
| `pre_trim_min_speech_ms` | `300` | Silero min speech segment |
| `pre_trim_lead_in_ms` | `100` | **deprecated** (заменён `_refine_silero_onset`) |
| `line_start_rms_adjust` | `True` | Пост-пасс коррекции начала строк |
| `word_end_drift_adjust` | `True` | Пост-пасс коррекции хвоста слов (drift) |
| `word_end_sustain_extend` | `True` | Пост-пасс расширения хвоста слов (sustain) |

Лог `torch_ctc_aligner_created` со всеми флагами при создании.

### 11.3. Загрузка модели — `_ensure_model`, `worker/gpu/torch_ctc_aligner.py:106-145`

- Lazy: модель не грузится в `__init__`, только при первом `align()`.
- `Wav2Vec2ForCTC.from_pretrained(_HF_MODEL_ID, torch_dtype=torch.float16, cache_dir=…)` → `.to(device).eval()`. **FP16 инференция.**
- `Wav2Vec2Processor.from_pretrained(_HF_MODEL_ID)` для словаря.
- Vocab (комментарий 126): `<blank>=0, <pad>=1, </s>=2, <unk>=3, a=4, …, x=30`.
- Словарь фильтруется до однобуквенных алфавитных + апостроф: `{k: v for k, v in vocab.items() if len(k) == 1 and (k.isalpha() or k == "'")}`.
- `self._blank_idx = 0`.
- Лог `torch_ctc_model_loaded` с `params_m`, `vocab_size`, `duration_sec`.

### 11.4. Алгоритм `align(vocals_path, lyrics_text, language)` — `worker/gpu/torch_ctc_aligner.py:151-252`

1. `ValueError` если `lyrics_text` пуст.
2. `_ensure_model()`.
3. `waveform = self._load_audio(vocals_path)` (16 kHz моно, через soundfile + torchaudio).
4. **Silero VAD pre-trim** (если `pre_trim_enabled`): `trim_offset = self._silero_trim_start(waveform)`. Если `> 0` — режется начало waveform; абсолютные тайминги затем восстанавливаются через `time_offset = trim_offset`.
5. **Forward pass**: `emission, ratio = self._forward_pass(waveform)`.
6. **Tokenize lyrics**: `words, transcript, first_flags = self._tokenize_lyrics(lyrics_text, language)`.
7. **Forced alignment**: `word_spans = self._align_tokens(emission, transcript)` — возвращает per-word списки фонемных span'ов.
8. **Per-line RMS-dip adjustment** (если `line_start_rms_adjust`) → `line_adjustments`.
9. **Word-end drift adjustment** (если `word_end_drift_adjust`) → `end_adjustments`.
10. **Word-end sustain extension** (если `word_end_sustain_extend`) → `end_extensions`. **Принимает `end_adjustments`** для взаимной исключительности (если drift сработал, extend не запускается).
11. **Combined end adjustments**: `combined = dict(end_extensions); combined.update(end_adjustments)` — drift приоритетнее.
12. `timings, stats = self._to_syllable_timings(words, word_spans, ratio, language, first_flags, time_offset, line_adjustments, combined_end_adjustments)`.
13. Лог `alignment_complete` с `total_words, char_level, fallback, syllables, duration_sec`.

### 11.5. Forward pass — `_forward_pass`, `worker/gpu/torch_ctc_aligner.py:269-280`

```python
with torch.inference_mode():
    output = self._model(waveform.to(device, dtype=torch.float16))
    emission = torch.log_softmax(output.logits.float(), dim=-1)
ratio = waveform.size(1) / 16000 / emission.size(1)  # sec/frame
```

- `inference_mode` (легче `no_grad`).
- `ratio` = секунды на эмишн-фрейм; нужна для перевода фонемных span-индексов в секунды.

### 11.6. Forced alignment — `_align_tokens`, `worker/gpu/torch_ctc_aligner.py:282-310`

- Плоская токенизация: `tokenized = [dict[c] for word in transcript for c in word if c in dict and dict[c] != 0]`.
- `targets = torch.tensor([tokenized], dtype=torch.int64).to(emission.device)`.
- `aligned_tokens, scores = torchaudio.functional.forced_align(emission, targets, blank=0)`.
- `token_spans = torchaudio.functional.merge_tokens(aligned_tokens[0], scores[0])`.
- `_unflatten` группирует плоский список span'ов обратно по словам через `word_lengths = [len(word) for word in transcript]`.

### 11.7. Silero VAD pre-trim + RMS back-tracking

#### 11.7.1. `_ensure_silero` / `_silero_trim_start` — `worker/gpu/torch_ctc_aligner.py:316-347`

- Lazy load: `torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)`.
- `get_speech_timestamps(audio, model, threshold=pre_trim_threshold, sampling_rate=16000, min_speech_duration_ms=pre_trim_min_speech_ms, min_silence_duration_ms=500, speech_pad_ms=50)`.
- Берётся `ts[0]["start"]` — начало первого confident сегмента речи; затем уточняется через `_refine_silero_onset`.

#### 11.7.2. `_refine_silero_onset` — `worker/gpu/torch_ctc_aligner.py:349-417`

- **Мотивация (комментарий 354-360):** Silero с `threshold=0.7` срабатывает после того, как форманты вокала «развились» — реальный attack слова на 100–400 мс раньше.
- **Алгоритм:**
  - 20-мс RMS-фреймы по окну `[silero_start, silero_start + 500ms]`.
  - `voiced_level = median(rms[silero_frame:])`.
  - `silence_floor = voiced_level / 10` → **−20 дБ SNR** (стандартный порог тишины).
  - Walk **назад**: ищем непрерывный silent_run ≥ 2 фреймов; найденная граница = начало вокального onset'а.
  - **Guard**: если backtrack > 1 сек — откат к `silero_start` (не доверяем).
- Лог `silero_onset_refine` с `silero_start_sec, refined_onset_sec, backtrack_ms, voiced_level_db, silence_floor_db`.

### 11.8. Per-line RMS-dip adjustment — `_compute_line_start_adjustments`, `worker/gpu/torch_ctc_aligner.py:423-728`

**Назначение:** найти первое слово каждой строки (или слово с `i==0`), у которого первая фонема была якорена в предшествующую тишину/back-vocal-leakage, и сдвинуть начало к реальному attack'у.

**Структурный фильтр:**
- `global_median_gap` = медиана **всех** межфонемных gap'ов в треке (исключая `gap_0`, чтобы не было self-bias).
- Для каждого word `i` с `first_flags[i]` или `i == 0`:
  - `gap0 = spans[1].start - spans[0].end`.
  - Если `gap0 ≤ 2 × global_median_gap` → пропуск.
- **`outlier_factor = 2.0`** — порог «гэп — выброс».

**Воксельная референсная громкость:**
- `voiced_level = max(RMS)` по окну `[spans[0].start, next_word.start - ratio]` (чтобы захватить пики и в `span0`, и в sustain после `spans[-1].end`).
- **`attack_floor = voiced_level / 5`** → **−14 дБ** (более строгий порог чем стандартные −20 дБ; именно для line-start, чтобы отличать «main vocal ONSET» от continuous низкоуровневых артефактов).
- `silent_run_threshold = max(2, int(round(2 × ref)))`.

**Две ветки в зависимости от `gap0/ref`:**

- **Экстремальная (`gap0/ref ≥ 7`)**: backward RMS walk от `spans[1].start` (надёжный фонема-2 anchor) к `spans[0].start`. См. п. 11.9.
- **Не-экстремальная (`2 ≤ ratio < 7`)**: forward RMS walk по `[spans[0].start, spans[1].start]`. Ищется первый above-floor фрейм после drift-sized silent run (≥ `silent_run_threshold` ниже floor). Sustained-фонемы остаются над floor → adjustment не фиксируется.

`adjustments[i] = (orig_start_sec, new_start_sec)` сохраняется только если новый старт сдвигается **более чем на 1 ratio-фрейм**.

Лог `ctc_first_phoneme_trim` с `applied_count, outlier_count, considered_count, global_median_gap_frames, outliers (топ-15), considered (топ-20)`.

### 11.9. Backward walk — `_backward_walk_voiced_onset`, `worker/gpu/torch_ctc_aligner.py:734-803`

- Walk назад от `start_sample` (anchor) к `limit_sample` (граница).
- Tracking `last_voiced_local_idx` (последний above-floor фрейм).
- На confirmed silent run (`silent_run ≥ silent_run_threshold`) и непустом `last_voiced` — возврат `(region_start_frame + last_voiced_local_idx) * frame_len_samples / 16000`.
- Возврат `None` если регион однородный (все above floor — sustained, или все below — leakage).

### 11.10. Word-end drift adjustment — `_compute_word_end_adjustments`, `worker/gpu/torch_ctc_aligner.py:809-985`

**Назначение:** обнаружить слова, у которых последняя фонема якорена поздно в тишину/инструментал.

**Структурный фильтр:**
- `median_gap` = медиана внутрисловных межфонемных gap'ов (исключая `gap_0` и `gap_last`).
- Для каждого слова `last_gap = spans[-1].start - spans[-2].end`.
- Если `last_gap ≤ 2 × median_gap` → пропуск.

**RMS forward walk:**
- `voiced_level = max(RMS)` по `[spans[0].start, spans[-1].end]`.
- **`silence_floor = voiced_level / 10`** → **−20 дБ SNR**.
- Walk вперёд по `[prev_end_sec, orig_end_sec]`, tracking `last_voiced_idx`. На silent_run ≥ `silent_run_threshold (= max(2, round(2 × ref)))` — break.
- `new_end = (последний voiced + 1) frame boundary`.
- **Защиты:** требуется минимум 1 ratio-фрейм реального trim'а; нельзя пересечь `prev_end + ratio`.

Лог `ctc_word_end_trim` с `adjusted_count, considered_count, median_gap_frames, adjusted (топ-10), considered (топ-20)`.

### 11.11. Word-end sustain extension — `_compute_word_end_extensions`, `worker/gpu/torch_ctc_aligner.py:991-1127`

**Назначение:** MMS даёт ~1-фреймовые emission-only span'ы; sustained финальная гласная (типично в конце строки) закрывается на attack-фрейме фонемы. Этот пасс расширяет конец слова вперёд по RMS, пока сигнал «звучит».

**Алгоритм:**
- **Mutual exclusion:** если `i in end_adjustments` (drift сработал) → пропуск (иначе extend отменит trim).
- `forward_end_sec = next_word.start - ratio` (или `audio_end` для последнего слова).
- `voiced_level = max(RMS)` по `[spans[0].start, spans[-1].end]`.
- **`silence_floor = voiced_level / 10`** → **−20 дБ SNR**.
- Forward scan по `[orig_end_sec, forward_end_sec]`, tracking `last_voiced_idx`. На `silent_run ≥ 2` — break (`capped_by = "silence"`); иначе `capped_by = "next_word"`.
- `new_end = (last_voiced + 1) * frame` если сдвиг > 1 ratio-фрейм.

Лог `ctc_word_end_extend` с `extended_count, considered_count, extended (топ-10)`.

### 11.12. Tokenize lyrics — `_tokenize_lyrics`, `worker/gpu/torch_ctc_aligner.py:1150-1194`

- **Романизация** не-латиницы через `from unidecode import unidecode` (`romanized = unidecode(cleaned).lower()`).
- Фильтрация: только символы из словаря с non-blank index.
- `first_flags[i] = True` для первого слова **каждой строки** (по `lyrics_text.splitlines()`).
- Возврат: `(words_out, transcript_out, first_flags)`.

### 11.13. Syllable timings — `_to_syllable_timings`, `worker/gpu/torch_ctc_aligner.py:1216-1297`

- `Syllabifier()` создаётся в `__init__` (`worker/gpu/torch_ctc_aligner.py:64`); вызывается приватный метод `self._syllabifier._split_word(word, language)` — нужно прочитать `shared/karaoke_shared/utils/syllabifier.py`.
- Применение `line_adjustments` и `end_adjustments` к границам слова: новые start/end замещают `spans[0].start * ratio + time_offset` / `spans[-1].end * ratio + time_offset`.
- `prefix`:
  - `""` — для самого первого слова.
  - `"\n"` — для первого слова любой следующей строки (по `first_flags`).
  - `" "` — иначе.
- **Однослоговые слова**: один `SyllableTiming(syllable=prefix+parts[0], start=ws, end=wend)`.
- **Многослоговые**: пропорциональное распределение по длинам слогов: `cl[pi] = max(len(part.strip()), 1)`, `frac = cl[pi] / sum(cl)`. **`stats.proportional_fallback += 1` для каждого многослогового слова** (см. 11.14).
- Округление `start`/`end` до **3 знаков** (миллисекунды).

### 11.14. AlignmentStats — поля и их фактическое заполнение

- `total_words = match_count` (число выровненных слов).
- **`char_level_used` — мёртвое поле**: ни в `torch_ctc_aligner.py`, ни в `gpu_pipeline.py` нигде не инкрементируется (проверено grep'ом). Только определяется как `int = 0` и читается в логах `gpu_pipeline.py:227`. Реликт от старой реализации (см. `worker/common/ctc_aligner.py` для legacy ONNX-варианта).
- **`proportional_fallback` — мисслидинговое имя**: инкрементируется в `_to_syllable_timings:1294` для каждого слова с >1 слогом (то есть это **счётчик многослоговых слов**, а не «число fallback'ов»). Однослоговые слова не инкрементируют.

### 11.15. Загрузка аудио — `_load_audio`, `worker/gpu/torch_ctc_aligner.py:1133-1144`

- `soundfile.read(path, dtype="float32")`; если многоканально — `mean(axis=1)` → моно.
- Если `sr != 16000` → `torchaudio.functional.resample`.
- Возврат `(1, samples)` тензор.

### 11.16. cleanup — `worker/gpu/torch_ctc_aligner.py:254-263`

- `del self._model; self._model = None; self._dictionary = {}`.
- Silero-модель (если была лениво загружена через `_ensure_silero`) тоже выгружается: `del self._silero_model; self._silero_model = None; self._silero_get_ts = None`.
- `gc.collect(); torch.cuda.empty_cache()`.
- Вызывается из `gpu_pipeline.py:222` после каждой задачи.

### 11.17. Подтверждённые наблюдения по mark_step

`grep -rn 'mark_step'` по `worker/`:
- В `worker/gpu/gpu_pipeline.py` (включая helper'ы `_vad`, `_transcribe`) вызывается `separating(0/100)`, `back_vocal_separating(0/100)` (опционально, только если `back_vocal_separator` не None), `vad(0/100)`, `transcribing(0/100)`, `searching_lyrics(0/100)`, `aligning(0/100)`, `line_breaking(0/100)` — итого **все 7 константных шагов `PipelineStep`** (или 6, если back-vocal отключён).
- Промежуточных значений прогресса (1..99 %) ни один из шагов не репортит — модели прогресс-колбэков не имеют. SSE-канал получает по 2 события на шаг (старт/конец), плюс события `mark_completed` / `mark_permanently_failed`.
- Бутстрап-скрипт (`worker/bootstrap/`) удалён в cleanup'е 2026-05-13 — отдельных вызовов `mark_step` для каталога больше нет.

### 11.18. Соответствие CLAUDE.md

- ✅ «MMS-300M CTC forced aligner (`MahmoudAshraf/mms-300m-1130-forced-aligner` via HuggingFace transformers `Wav2Vec2ForCTC`) using `torchaudio.functional.forced_align` + `merge_tokens` on GPU» — точно соответствует.
- ✅ «Includes Silero VAD pre-trim of intro noise» — соответствует.
- ✅ «Three optional post-pass RMS adjustments for line-start anchoring, word-end drift trim, and word-end sustain extension (all toggleable in `TorchCTCAligner.__init__`)» — соответствует (флаги `line_start_rms_adjust`, `word_end_drift_adjust`, `word_end_sustain_extend`).
- ⚠️ `ctc_device="cpu"` из `worker/app/config.py` НЕ применяется к `TorchCTCAligner` — этот параметр относится к старой ONNX-реализации, которая в актуальном пайплайне не используется (требует подтверждения чтением `worker/common/ctc_aligner.py` и `ctc_subprocess.py`).

---

## 12. VAD — `VADProcessor`

**Файл:** `worker/common/vad_processor.py`. Класс `VADProcessor`, dataclass `VADResult`.

### 12.1. Назначение и backend

- Удаляет тишину из вокального WAV перед ASR — `worker/common/vad_processor.py:1-6`.
- Backend: **RMS energy detection через PyTorch (CPU)**. Никакой librosa, никакого нейросетевого VAD (Silero VAD используется отдельно — внутри `TorchCTCAligner` для pre-trim, но не здесь).
- Также возвращает `segments` — список voiced-интервалов в секундах (для опционального segmented CTC alignment).

### 12.2. Параметры

- Сэмпл-рейт: `_SR = 16_000` — `worker/common/vad_processor.py:18`.
- Конструктор: `top_db: int = 35` (default класса). **В `WorkerSettings.vad_top_db = 16`** — то есть в реальном воркере применяется значительно более строгий порог (16 дБ vs 35 дБ в default'е класса). Чем меньше `top_db`, тем больше срезается тишины.
- Docstring: «35 dB works well for vocals; lower = stricter» — `worker/common/vad_processor.py:33-34`.

### 12.3. `VADResult` dataclass — `worker/common/vad_processor.py:21-26`

```python
@dataclass
class VADResult:
    cleaned_path: str
    segments: list[tuple[float, float]] = field(default_factory=list)
```

### 12.4. Алгоритм `process(vocals_path)` — `worker/common/vad_processor.py:40-120`

1. **Загрузка**: `sf.read(vocals_path, dtype="float32")`. Многоканально → `mean(axis=1)` → моно. Если `sr != 16000` → `torchaudio.functional.resample`.
2. На исключении при загрузке: `vad_load_failed` warn-лог, возврат `VADResult(cleaned_path=vocals_path)` (т. е. оригинал без VAD).
3. **RMS-фреймы**:
   - `frame_length = 2048`
   - `hop_length = 512`
   - `frames = yt.unfold(0, frame_length, hop_length)` — `torch.unfold` (без librosa).
   - `rms = frames.pow(2).mean(dim=1).sqrt()`.
4. **Порог**:
   - `threshold = rms.max() * 10 ** (-top_db / 20)`.
   - При `top_db=16`: `threshold = peak_rms / 10^(0.8) ≈ peak_rms / 6.31`, то есть **−16 дБ от пика**.
5. **`is_voiced = rms > threshold`** → булева маска по фреймам.
6. **Маска → интервалы** (numpy):
   - `diff = np.diff(voiced.astype(int8), prepend=0, append=0)` — переходы 0→1 и 1→0.
   - `starts = np.where(diff == 1)[0]`, `ends = np.where(diff == -1)[0]`.
   - Если `starts` пусто: `vad_no_voiced_segments` warn, возврат `VADResult(cleaned_path=vocals_path)` (оригинал без cleaned-файла).
   - **Конвертация в samples**: `(s * hop_length, min(e * hop_length + frame_length, len(y)))`.
   - **Конвертация в секунды для `segments`**: `(s/SR, e/SR)`.
7. **Конкатенация**: `cleaned = np.concatenate([y[s:e] for s, e in intervals])`.
8. **Защита от слишком короткого результата**: если `len(cleaned)/SR < 1.0` — возврат `VADResult(cleaned_path=vocals_path, segments=segments)` (оригинал, но с уже посчитанными segments).
9. **Сохранение**:
   - `track_id = Path(vocals_path).stem.split("_")[0]` — первая часть stem (предполагается UUID без подчёркиваний; имя `{job_id}_(Vocals).wav` → `track_id = job_id`).
   - `out_path = parent / f"cleaned_vocals_{track_id}.wav"`.
   - `sf.write(out_path, cleaned, 16000, subtype="PCM_16")`.
10. Лог `vad_completed` с `original_sec`, `cleaned_sec`, `reduction_pct`, `segments` (count), `duration_sec`.
11. Возврат `VADResult(cleaned_path=out_path, segments=segments)`.

### 12.5. `map_cleaned_to_original(cleaned_time, segments)` — `worker/common/vad_processor.py:122-142`

- Статический метод. Маппит timestamp из cleaned-аудио обратно в время оригинала (идёт по сегментам, аккумулирует длительности).
- **Нигде в коде не вызывается** (проверено grep'ом по worker/ и shared/) — мёртвый метод. Возможно, был задуман для маппинга Whisper-таймингов обратно в оригинальное время (если бы Whisper выдавал тайминги по словам), но не используется.

### 12.6. Особенности

- **Без модели и без cleanup**: класс stateless (только параметр `top_db`), нет VRAM-ресурсов для освобождения.
- **Где используется в пайплайне**: вызывается из `_vad_and_transcribe(vocals_path, …)` — на **полных** vocals (см. п. 6.1). Cleaned audio передаётся в `Whisper.transcribe`. **CTC и line breaker работают НЕ на cleaned, а на lead vocals** (см. п. 6).
- **Файл `cleaned_vocals_{track_id}.wav`** удаляется в `gpu_pipeline.py:249-253` после line-break detection.
- `segments` (поле `VADResult.segments`) собирается, но в `gpu_pipeline.py` тоже не используется ни в каких дальнейших шагах — `_vad_and_transcribe` возвращает `(whisper_result, vad_result.segments)`, но `vad_result.segments` далее не читается. То есть сегменты собираются «впрок», для возможной segmented-alignment, которая в актуальном коде не реализована.

### 12.7. Соответствие CLAUDE.md

- ✅ «VAD: RMS energy detection via PyTorch CPU (`worker/common/vad_processor.py`). No librosa dependency — uses `torch.unfold` + threshold. Audio loaded via soundfile, resampled via `torchaudio.functional.resample` if needed.» — точно соответствует.

---

## 13. Legacy CTC — `CTCAligner` (ONNX + subprocess) *(файл удалён 2026-05-13)*

**Файл:** `worker/common/ctc_aligner.py`. Класс `CTCAligner`, dataclass `AlignmentStats` (отдельный от такого же в `torch_ctc_aligner.py`).

### 13.1. Назначение и статус

- ONNX-based forced alignment с **реальной subprocess-изоляцией**.
- Шапка `worker/common/ctc_aligner.py:1-6`: «All ONNX work runs in a separate process via `subprocess.Popen` to fully isolate heap corruption that ONNX Runtime can cause. If the child crashes, the main worker survives and reports a `RuntimeError`.»
- Делегирует всю ONNX-работу подпроцессу `worker.common.ctc_subprocess`.

### 13.2. Где реально используется (по grep'у `CTCAligner`)

| Место | Импорт / использование |
|---|---|
| `worker/app/main.py:45,155` | импортирует **`TorchCTCAligner`**, конструирует его. **`CTCAligner` не используется.** |
| `worker/gpu/gpu_pipeline.py:33,70` | `from worker.common.ctc_aligner import CTCAligner` **только для type hint** (`ctc_aligner: CTCAligner`). Фактически передаётся `TorchCTCAligner` (см. п. 6.7). Импорт класса не вызывает его `__init__` → `AlignmentSingleton()` тоже не вызывается. |
| `worker/bootstrap/pipeline.py:23,127` | импорт + type hint **+ реальное использование** в bootstrap-пайплайне массового импорта. |

**Итог:** `CTCAligner` фактически используется **только** в массовом бутстрапе каталога (`worker/bootstrap/pipeline.py`). В runtime-пайплайне (приём пользовательских задач через RabbitMQ) — `TorchCTCAligner`. То есть в воркере **сосуществуют две CTC-реализации** для разных назначений.

### 13.3. Параметры конструктора — `worker/common/ctc_aligner.py:52-70`

| Параметр | Default | Назначение |
|---|---|---|
| `syllabifier` | `None` | **«kept for API compat»** — игнорируется. |
| `model_cache_dir` | `None` | **«kept for API compat»** — игнорируется. |
| `min_frames_for_char` | `MIN_FRAMES_FOR_CHAR = 10` | передаётся в подпроцесс. Соответствует `settings.ctc_min_frames_for_char = 10`. |
| `device` | `"cpu"` | **«Unused (subprocess always uses CPU to avoid VRAM contention)»**. То есть `settings.ctc_device = "cpu"` относится именно сюда. |
| `batch_size` | `16` | передаётся в подпроцесс как `--batch-size`. Соответствует `settings.ctc_batch_size = 16`. |

- При создании выполняется **eager prefetch** ONNX-модели: `from ctc_forced_aligner import AlignmentSingleton; AlignmentSingleton()` — `worker/common/ctc_aligner.py:63-64`. Это скачивает / кеширует модель сразу при инстанциации (при старте bootstrap-процесса).
- Лог `ctc_aligner_loaded` с флагом `subprocess=True`.

### 13.4. Алгоритм `align(vocals_path, lyrics_text, language)` — `worker/common/ctc_aligner.py:72-164`

1. Валидация: `ValueError` если `lyrics_text` пуст.
2. Создаётся `tempfile.TemporaryDirectory()` для обмена с подпроцессом:
   - `lyrics.txt` — записывается lyrics_text (UTF-8).
   - `result.json` — читается выходной JSON.
3. **Запуск subprocess'а**:
   ```python
   cmd = [
       "python3", "-m", "worker.common.ctc_subprocess",
       "--vocals", vocals_path,
       "--lyrics-file", str(lyrics_path),
       "--language", language,
       "--batch-size", str(self._batch_size),
       "--output", str(output_path),
   ]
   subprocess.run(cmd, timeout=300, capture_output=True, text=True)
   ```
4. **Обработка результата:**
   - `TimeoutExpired` → `RuntimeError("CTC alignment subprocess timed out after 300s")`. Константа `_SUBPROCESS_TIMEOUT = 300` (5 минут).
   - `returncode != 0` → проверка наличия `result.json` с полем `error`; если есть — пробрасывается; иначе `RuntimeError` с последними 500 символами stderr.
   - Если `result.json` не появился → `RuntimeError("CTC alignment produced no output")`.
   - Парсинг JSON: `data["timings"]` → `[SyllableTiming(...)]`; `data["stats"]` → `AlignmentStats(...)`.
5. Лог `alignment_complete` с `total_words`, `char_level`, `fallback`, `syllables`, `subprocess=True`, `duration_sec`.

### 13.5. Внешняя зависимость `ctc_forced_aligner`

- Импортируется в `worker/common/ctc_subprocess.py:39-40` и `worker/common/ctc_aligner.py:63`.
- Это сторонняя библиотека (не наш код), даёт `AlignmentSingleton` для CTC.
- В `docker-compose.gpu.yml:69` смонтирован volume `ctc_model:/root/ctc_forced_aligner` — то есть кеш ONNX-модели есть, но в runtime-пайплайне не используется.
- `tests/worker/test_ctc_aligner.py:16` мокирует `ctc_forced_aligner.AlignmentSingleton` — то есть тест существует и активно мокает.

### 13.6. Соответствие моим прежним утверждениям

- ⚠️ Поле `char_level_used` я ранее предположил «инкрементируется в подпроцессе» — **неверно** (см. п. 14.7 для коррекции). В `ctc_subprocess.py` оно тоже всегда `0` (char-level CTC отключён из-за heap corruption ONNX Runtime). Поле мёртвое во всех актуальных реализациях.
- ✅ Subprocess-изоляция реально существует, но **только для legacy ONNX-варианта в bootstrap**. В runtime — in-process PyTorch.
- ⚠️ Для ВКР это означает: если описывать «изоляцию CTC в подпроцессе» как архитектурное решение, нужно явно указать, что это касается bootstrap-сценария, а не основного runtime-пайплайна обработки пользовательских задач.

---

## 14. Подпроцесс legacy CTC — `worker/common/ctc_subprocess.py` *(файл удалён 2026-05-13)*

**Файл:** `worker/common/ctc_subprocess.py` (191 строка). Standalone-скрипт, запускается как `python -m worker.common.ctc_subprocess`.

### 14.1. Назначение и интерфейс

- Самостоятельный исполняемый скрипт (`if __name__ == "__main__": main()`).
- Запускается из `CTCAligner.align` через `subprocess.run` (см. п. 13.4).
- Обмен с родительским процессом — **через файлы**: `--lyrics-file` (вход) и `--output` (выход в JSON).
- Шапка `worker/common/ctc_subprocess.py:1-13` описывает интерфейс CLI с примером usage.

### 14.2. Аргументы CLI и формат вывода

**CLI:**

| Флаг | Тип | Default | Назначение |
|---|---|---|---|
| `--vocals` | str | required | Путь к WAV для выравнивания |
| `--lyrics-file` | str | required | Путь к UTF-8 файлу с lyrics |
| `--language` | str | required | Код языка (`ru`, `en`, ...) |
| `--batch-size` | int | 16 | Передаётся в `generate_emissions` |
| `--output` | str | required | Путь к выходному JSON |

**Формат `--output`:**

- Успех: `{"timings": [{"syllable": str, "start": float, "end": float}, ...], "stats": {"total_words": int, "char_level_used": int, "proportional_fallback": int}}`.
- Ошибка: `{"error": "message"}`, `sys.exit(1)`.

### 14.3. Импорты и внешняя библиотека

- `onnxruntime` — для инференции.
- `ctc_forced_aligner` (внешний пакет): `AlignmentSingleton`, `Tokenizer`, `generate_emissions`, `get_alignments`, `get_spans`, `load_audio`, `postprocess_results`, `preprocess_text` — вся работа с CTC делается через эту библиотеку, своего кода forced_align нет.
- `karaoke_shared.models.track.SyllableTiming` — **импортируется, но не используется** (timings собираются как `dict`, не как `SyllableTiming`); ещё один мёртвый импорт.
- `karaoke_shared.utils.syllabifier.Syllabifier` — для разбиения слов на слоги.

### 14.4. Константы и язык

- `MIN_FRAMES = 10` — определена локально, но в коде ниже **не используется**. По смыслу — пара `MIN_FRAMES_FOR_CHAR` из родительского `ctc_aligner.py:23`. Мёртвая.
- `LANG_ISO3 = {"ru": "rus", "en": "eng"}` — закрытая карта только для двух языков.
- `lang_flags(language)`: возвращает `(iso3, romanize=language!="en")` — то есть **romanize=True для всего, кроме `en`**. Для русского текст романизируется в латиницу.

### 14.5. Загрузка ONNX-модели

```python
aligner = AlignmentSingleton()                         # singleton от ctc_forced_aligner
tokenizer = Tokenizer()
sess_opts = onnxruntime.SessionOptions()
sess_opts.intra_op_num_threads = 2
sess_opts.inter_op_num_threads = 1
model = onnxruntime.InferenceSession(
    aligner.model_path,
    sess_options=sess_opts,
    providers=["CPUExecutionProvider"],                # CPU only
)
```

**CPU-провайдер захардкожен.** Это документирует поведение, описанное в комментарии `worker/app/config.py:60-63` про CUDA EP / wav2vec2.

### 14.6. Алгоритм `main()` — `worker/common/ctc_subprocess.py:22-186`

1. Парсинг аргументов, чтение `lyrics_text` из файла.
2. Lazy-импорт всех тяжёлых библиотек **внутри try/except** (если что-то упадёт — пишется JSON с ошибкой и exit 1).
3. Загрузка ONNX-модели и Syllabifier.
4. **Эмиссии**: `waveform = load_audio(args.vocals, ret_type="np")`; `emissions, stride_ms = generate_emissions(model, waveform, batch_size=args.batch_size)`.
5. **Защита от OOM (truncation lyrics)** — `worker/common/ctc_subprocess.py:84-100`:
   - `max_words = min(500, emissions.shape[0] // 4)`.
   - Lyrics обрезаются построчно до `max_words` слов (полные строки).
   - Комментарий: «CTC `get_alignments` allocates O(frames × tokens) memory. With romanized Russian, each word ≈ 2 tokens. Cap at 500 words to stay well within memory limits».
   - **Следствие:** в длинных треках bootstrap может терять часть текста. Для runtime-пайплайна (TorchCTCAligner) такого ограничения нет.
6. **Word-level alignment через `ctc_forced_aligner`:**
   - `lyrics_flat = lyrics.replace("\n", " ").strip()`.
   - `tokens_starred, text_starred = preprocess_text(lyrics_flat, romanize=romanize, language=lang_iso3, split_size="word")`.
   - `segments, scores, blank_token = get_alignments(emissions, tokens_starred, tokenizer)`.
   - `spans = get_spans(tokens_starred, segments, blank_token)`.
   - `word_timestamps = postprocess_results(text_starred, spans, stride_ms, scores)`.
7. **Lyrics_words с `is_first_in_line` флагом**: проход по `lyrics_text.splitlines()` → `[(word, idx==0)]`.
8. **`match_count = min(ctc_count, lyrics_count)`**.
9. **Цикл синтеза timings** (`worker/common/ctc_subprocess.py:133-177`):
   - prefix: `""` / `"\n"` / `" "` — как в `TorchCTCAligner`.
   - **Char-level CTC отключён** (комментарий 154-156): «char-level CTC (`get_alignments` on emission slices) is **disabled — repeated calls cause heap corruption in ONNX Runtime**. Word-level boundaries + proportional syllable split is used instead.»
   - Пропорциональный split на слоги через `Syllabifier._split_word(lw, language)` — идентичен `TorchCTCAligner._to_syllable_timings`.
   - `stats["proportional_fallback"] += 1` для каждого word с непустыми parts.
10. JSON-результат пишется в `args.output`, exit 0.
11. На любом исключении: `{"error": str(exc)}` в `args.output`, `sys.exit(1)`.

### 14.7. Поправка к п. 13.6 — `char_level_used` мёртвое **везде**

В п. 13.6 я написал: «Поле `char_level_used` инкрементируется здесь (точнее — в подпроцессе)». **Это неверно.** В `ctc_subprocess.py:129` поле инициализируется как `"char_level_used": 0` и **нигде не инкрементируется**. В коде явно есть комментарий-объяснение (строки 154-156): char-level CTC отключён из-за heap corruption ONNX Runtime, и для всех слов используется пропорциональный fallback.

То есть **`char_level_used` — мёртвое поле во всех актуальных реализациях**, не только в `TorchCTCAligner`. Раньше, видимо, было разделение «char-level vs word-level», но оно отключено везде. Имя поля и существование счётчика — реликт.

### 14.8. Что отличается от `TorchCTCAligner` (по существу)

| Аспект | `TorchCTCAligner` (runtime) | `ctc_subprocess.py` (bootstrap) |
|---|---|---|
| Backend | PyTorch + torchaudio | ONNX Runtime + `ctc_forced_aligner` |
| Устройство | GPU (CUDA, FP16) | CPU only (CPUExecutionProvider) |
| Изоляция | in-process | subprocess через `python -m`, обмен через JSON-файлы |
| Модель | `MahmoudAshraf/mms-300m-1130-forced-aligner` через HF | ONNX-модель из `AlignmentSingleton.model_path` |
| Silero VAD pre-trim | да | **нет** |
| RMS-постобработки (line-start, drift, sustain) | да | **нет** |
| Лимит на длину lyrics | нет | да: `min(500 слов, frames // 4)` — длинные треки могут терять текст |
| Романизация | `unidecode` для не-латиницы | `preprocess_text(romanize=True)` для всего, кроме `en` |
| Char-level alignment | отсутствует (поле `char_level_used` мёртвое) | отключён из-за heap corruption (поле `char_level_used` мёртвое) |
| Возврат | `(list[SyllableTiming], AlignmentStats)` | JSON-файл с `dict` |
| Зависимость от внешней библиотеки | `transformers`, `torchaudio` | `ctc_forced_aligner` (полностью внешняя) |

---

## 15. Поиск текстов — каркас (`worker/common/lyrics/`)

Файлы: `__init__.py`, `base_provider.py`, `fragments.py`, `filename_parser.py`, `provider_chain.py` (608 строк суммарно).

### 15.1. Публичный API — `worker/common/lyrics/__init__.py`

Экспорт: `ArtistTitleProvider`, `LyricsCandidate`, `LyricsProviderChain`, `TextSearchProvider`. Всё остальное (matching, providers, filename_parser) — внутреннее.

### 15.2. Абстрактные провайдеры — `worker/common/lyrics/base_provider.py`

- **`LyricsCandidate` dataclass** (`base_provider.py:9-16`):
  - `artist: str`
  - `title: str`
  - `lyrics: str`
  - `source: str` — имя провайдера, из которого получили кандидата.

- **`TextSearchProvider` (ABC)** — поиск по фрагменту текста:
  - `name: str` — имя провайдера.
  - `search_by_text(text_fragment) -> list[LyricsCandidate]`.

- **`ArtistTitleProvider` (ABC)** — поиск по `(artist, title)`:
  - `name: str`.
  - `search_by_metadata(artist, title) -> LyricsCandidate | None`.

**Контракт ошибок:** «not found» → `[]` / `None` (не raise); инфраструктурные сбои → `LyricsAPIError` (определение этого исключения нужно проверить при чтении `worker/common/lyrics_searcher.py`).

### 15.3. Извлечение фрагментов из ASR — `worker/common/lyrics/fragments.py`

`extract_search_fragments(asr_text, n=3)` — `worker/common/lyrics/fragments.py:8-60`:

1. Сплит по `[.!?\n]+` → `phrases`.
2. Если фраз нет → fallback на чанки по 10 слов.
3. Фильтр: фразы с **≥ 5 слов**. Если после фильтра пусто — relax (берётся всё).
4. Если фраз меньше `n` — пересборка чанками `chunk_size = max(8, min(12, len(words) // n))`.
5. Триммирование каждой фразы до **≤ 12 слов** (`" ".join(p.split()[:12])`).
6. Если фраз больше `n` — выбор `n` равномерно через `_spread_indices(len, n)` (start, middle, end).

Цель (docstring): «at least one is likely to match a lyrics database even when Whisper introduces errors. Longer fragments produce more specific search results, reducing false positives (remixes, battles, compilations)».

В config: `lyrics_search_fragments = 2` (то есть в реальном воркере используется не 3, а 2 фрагмента — start + end через `_spread_indices`).

### 15.4. Парсинг имени файла через LLM — `worker/common/lyrics/filename_parser.py`

- **`ParsedFilename` (frozen dataclass)** — `filename_parser.py:49-78`:
  - `artist_variants: tuple[str, ...]`, `title_variants: tuple[str, ...]`.
  - Properties: `.artist` / `.title` (первый элемент), `.artist_alts` / `.title_alts` (остальные).
  - `empty()` classmethod.

- **`FilenameParser`** — `filename_parser.py:81-132`:
  - Конструктор: `deepseek_api_key`, `model="deepseek-chat"`.
  - `parse(filename) -> ParsedFilename`:
    - Запускает LLM в `asyncio.to_thread`.
    - На исключении: warning `filename_parse_llm_failed`, возврат `ParsedFilename.empty()`.
    - Парсинг JSON через `_extract_json` (try `json.loads`, потом regex `\{.*\}` fallback).
  - `_call_llm`: использует `OpenAI` SDK с `base_url="https://api.deepseek.com"`, `timeout=60.0`, `temperature=0.0`, `max_tokens=256`.

- **Промпт (на русском)** — `filename_parser.py:23-46`:
  - Игнорировать мусор: номера треков, битрейт, год, теги, скобки, названия сайтов.
  - Транслитерация → каноническое имя (для русских артистов — кириллица).
  - Если каноническое и оригинальное написание отличаются — вернуть оба (`artist_original`, `title_original`).
  - При нескольких артистах — вернуть «главного» (наиболее известного).
  - Имя артиста в правильном порядке (Имя Фамилия).
  - Формат ответа: `{"artist": "...", "title": "...", "artist_original": "...", "title_original": "..."}`.

- **`_build_variants(canonical, original)`** — `filename_parser.py:135-143`: возвращает кортеж: `[canonical]` (если непустое), плюс `original` (если непустое **и не совпадает caseless** с canonical).

### 15.5. Оркестратор `LyricsProviderChain` — `worker/common/lyrics/provider_chain.py`

**Конструктор:** `text_providers, metadata_providers, matcher (опц.), filename_parser (опц.), fallback_agent (опц.), search_fragments=3`.

**Метод `search(asr_text, detected_language, artist_hint=None, title_hint=None, filename=None) -> LyricsResult`** — пять стадий (`worker/common/lyrics/provider_chain.py:60-193`):

| Стадия | Действие | Условие |
|---|---|---|
| **0. Filename parse** | `filename_parser.parse(filename)` → `ParsedFilename`. Hints, которых не было в job, заполняются; `artist_alts`/`title_alts` сохраняются для дальнейших шагов. | если `filename` есть и хотя бы один hint отсутствует и `filename_parser` задан |
| **1. Collect candidates** | `_collect_candidates(...)` параллельно через `asyncio.gather(return_exceptions=True)`. См. п. 15.6. | всегда |
| **— Deduplicate** | Ключ дедупликации: `(artist.lower().strip(), title.lower().strip())`. | всегда |
| **2. Matcher** | `matcher.match(asr_text, candidates, language, artist_hints, title_hints)` → `LyricsResult` или `None`. На успехе — `return result`. | если есть кандидаты И matcher задан |
| **3. Fallback agent** | `fallback_agent.search(asr_text, language, artist_hint, title_hint, artist_alts, title_alts)` → ещё кандидаты → дедупликация → снова `matcher.match(...)`. | если matcher отверг всех И задан `fallback_agent` |
| **4. ASR fallback** | Если `len(asr_clean) ≥ 20`: возврат `LyricsResult(artist=artist_hint or "Unknown", title=title_hint or "Unknown", lyrics=asr_clean, language, confidence="low", source_note="asr_fallback")`. Логируется warning `lyrics_using_asr_fallback`. | если стадии 2 и 3 ничего не нашли |
| — Иначе | `raise LyricsNotFoundError` | если ASR-текст < 20 символов |

**Stage 4 (ASR fallback)** — важная неочевидная деталь: пайплайн не падает, даже если ни один провайдер и ни агент не нашли текст. Вместо этого raw Whisper-вывод подаётся в CTC как lyrics, и трек создаётся с `lyrics_source="asr_fallback"` (можно отслеживать в БД).

### 15.6. Параллельный сбор кандидатов — `_collect_candidates`, `worker/common/lyrics/provider_chain.py:199-266`

Все задачи запускаются через `asyncio.create_task(...)` и собираются `asyncio.gather(return_exceptions=True)`. Композиция задач:

| Тип задачи | Условие |
|---|---|
| Каждый text-провайдер × каждый из `n` фрагментов | всегда |
| Каждый text-провайдер × `f"{artist} {title}"` для всех вариантов hints | если есть и artist, и title |
| Каждый text-провайдер × title для всех вариантов | если есть только title (без artist) |
| Каждый metadata-провайдер × `(artist, title)` для всех вариантов | если есть и artist, и title |

Защитные обёртки `_safe_text_search` / `_safe_metadata_search` — try/except с warning-логом и возвратом `[]` / `None` на любой ошибке (то есть провайдеры независимы, падение одного не убивает остальных).

**`_variants(primary, alts)`** — `provider_chain.py:307-319`: возвращает primary + non-empty уникальные альтернативы (caseless dedup, порядок сохраняется).

### 15.7. Логи

`lyrics_filename_parsed`, `lyrics_candidates_collected` (count, sources, elapsed), `lyrics_matched`, `lyrics_match_rejected_all`, `lyrics_fallback_to_agent`, `lyrics_agent_candidates`, `lyrics_matched_after_agent`, `lyrics_using_asr_fallback`, плюс warning `text_provider_error` / `metadata_provider_error` от `_safe_*`.

### 15.8. DeepSeek как центральный LLM воркера

К этому шагу подтверждено, что `deepseek-chat` (через OpenAI SDK с `base_url="https://api.deepseek.com"`) используется в:

- `FilenameParser` — извлечение `(artist, title, artist_original, title_original)` из имени файла.
- `LyricsExpander` (см. сборку в `_build_gpu_pipeline`) — назначение определю при чтении `matching/`.
- `LyricsMatcher` (см. там же) — сопоставление ASR с кандидатами; назначение определю при чтении `matching/`.
- `LyricsAgent` — резервный web-search; прочитаю отдельно.

Везде ключ один — `settings.deepseek_api_key`, модель — `settings.deepseek_model = "deepseek-chat"`.

### 15.9. Соответствие CLAUDE.md

- ✅ «Provider chain in `worker/common/lyrics/` — fetches lyrics from genius, lrclib, lyricsovh, chartlyrics, simpmusic (one of these may use the local SearXNG instance in `searxng/` as fallback).» — соответствует архитектуре.
- ✅ «Chain logic in `provider_chain.py`, candidate scoring/matching in `worker/common/lyrics/matching/`.» — соответствует.
- ⚠️ **Не описано в CLAUDE.md:**
  - **Stage 0** — парсинг имени файла через DeepSeek (`FilenameParser`).
  - **Stage 4** — ASR fallback (raw Whisper-текст становится lyrics при провале всех источников; `lyrics_source="asr_fallback"`).
  - **Параллельность** — все провайдеры запускаются `asyncio.gather` одновременно, не последовательно «по цепочке».
  - **Многократность запросов** — для каждой комбинации `(artist_variant, title_variant)` × провайдер делается отдельный запрос; для длинных alts списков это может быть много параллельных вызовов.

---

## 16. Сопоставление кандидатов — `worker/common/lyrics/matching/`

Файлы: `__init__.py`, `normalizer.py`, `linguistics.py`, `scorer.py`, `expander.py`, `matcher.py` (1129 строк суммарно). Это самый алгоритмически насыщенный модуль воркера.

### 16.1. Публичный API — `__init__.py`

Экспорт: `LyricsExpander`, `LyricsMatcher`, `MatchFeatures`, `NormalizedText`, `normalize_text`, `score_all`.

### 16.2. Нормализация текста — `normalizer.py`

`NormalizedText` dataclass: `text` (cleaned string), `words: tuple[WordFeatures, ...]`, `word_count` (property).

**`_clean_text`** — `worker/common/lyrics/matching/normalizer.py:57-72`:

1. `unicodedata.normalize("NFKC", text).lower()`.
2. Удаление section-маркеров `[Section]` / `[Куплет 1]` / `[Chorus]` через `_SECTION_RE = r"\[[^\[\]]*\]"`.
3. Удаление коротких bracketed-фрагментов (1–30 символов): `_SHORT_PARENS_RE = r"\(([^()]{1,30})\)"` — типично ad-libs / backing.
4. Удаление standalone-цифр: `_DIGIT_RUN_RE = r"\b\d+\b"` (год, номер трека).
5. Удаление пунктуации кроме апострофа: `_PUNCT_RE = r"[^\w'\s]+"` (апостроф нужен для `don't`).
6. Collapse whitespace.

**Важно:** **дубликаты подряд НЕ схлопываются** — комментарий шапки: «song hooks and chorus repetitions are legitimate (e.g. "белые розы белые розы")».

### 16.3. Лингвистические признаки — `linguistics.py`

`WordFeatures` dataclass — `worker/common/lyrics/matching/linguistics.py:30-35`:

| Поле | Описание |
|---|---|
| `text` | Lowercased оригинал. |
| `lemma` | Морфологическая нормальная форма. |
| `skeleton` | Consonant skeleton (без гласных), потом `unidecode`. Толерантен к гласным заменам и морфологическим окончаниям — главные ошибки Whisper в пении. |
| `metaphone` | Фонетический код для латиницы. Для кириллицы — пустая строка (skeleton покрывает). |

**Featurizer per language** — `make_word_featurizer(language)`:

| Язык | Lemma | Skeleton | Metaphone |
|---|---|---|---|
| `ru` | `pymorphy3.MorphAnalyzer` (через `@lru_cache(maxsize=1)` — singleton) | drop `аеёиоуыэюяьъй` + `unidecode` | пусто |
| `en` | `snowballstemmer.stemmer("english")` | drop `aeiouy` | `jellyfish.metaphone` |
| иное | `snowballstemmer.stemmer(lang)` если поддерживает, иначе lowercase | `unidecode` → `_skeleton_latin` | `metaphone` если ASCII, иначе пусто |

**Зависимости:** `pymorphy3`, `snowballstemmer`, `jellyfish`, `unidecode`.

### 16.4. Многофакторный scorer — `scorer.py`

`MatchFeatures` dataclass — `worker/common/lyrics/matching/scorer.py:61-82`. **8 числовых полей** (все нормированы в `[0..1]` плюс composite):

| Поле | Что считает |
|---|---|
| `coverage_asr` | Фракция ASR-слов с good match (`score ≥ 2`) в кандидате. |
| `coverage_cand` | Симметрично, со стороны кандидата. Низкое значение = кандидат сильно длиннее ASR (remix / long version). |
| `phonetic_match_rate` | `sum(asr_scores) / (3.0 × len(asr_scores))` — средний нормированный score (max 3). |
| `ngram_jaccard` | Jaccard 4-grams по `skeleton`'ам. |
| `rare_anchor_score` | IDF-density 5-grams лемм, шарящихся между ASR и кандидатом, нормированная по max-density. |
| `length_ratio_penalty` | `min(1, |log(cand_words / asr_words)|)`. |
| `hint_score` | Fuzzy match candidate `artist+title` против filename-hints. |
| `composite` | Взвешенная сумма (см. ниже). |

**Веса композита** — `worker/common/lyrics/matching/scorer.py:51-58`:

```
composite = 0.55 × coverage_F1
          + 0.15 × phonetic_match_rate
          + 0.10 × ngram_jaccard
          + 0.20 × rare_anchor_score
          + 0.30 × hint_score          # additive bonus, не нормирован
          - 0.10 × length_ratio_penalty
clamped to [0..1]
```

`coverage_F1 = harmonic_mean(coverage_asr, coverage_cand)` — `worker/common/lyrics/matching/scorer.py:139, 303-306`. **Защищает от remix-кандидатов**, у которых coverage_asr высокий (содержит весь ASR), но coverage_cand низкий (cand сильно длиннее).

**`_match_score(word, idx)` — целое 0/1/2/3** — `scorer.py:198-225`:
- `3` — точное совпадение `word.text` в `idx.texts`.
- `2` — совпадение по `lemma` ИЛИ `skeleton` ИЛИ `metaphone`.
- Иначе — Levenshtein fallback через `rapidfuzz.fuzz.ratio`:
  - только для слов с `|len_diff| ≤ 2` (`_LEV_LEN_TOL = 2`),
  - `ratio ≥ 80` → `2` (`_LEV_RATIO_HIGH`),
  - `ratio ≥ 60` → `1` (`_LEV_RATIO_LOW`),
  - иначе `0`.

`coverage_*` считается как фракция слов с score `≥ 2`. То есть слабые Levenshtein-матчи (1) не идут в coverage, но идут в `phonetic_match_rate`.

**`_rare_anchor_scores`** — `scorer.py:251-291`: IDF-weighted density 5-grams лемм. Для каждого 5-грамма из ASR, который встречается в кандидате, добавляется `1/df[g]` (df = document frequency среди кандидатов). Сумма делится на `len(grams)` кандидата (per-candidate normalization), чтобы не давать преимущество длинным кандидатам. Финально нормируется на `max_density`.

### 16.5. Expander — двухпроходное расширение повторов — `expander.py`

`LyricsExpander.expand(raw_lyrics) -> str` — кеширование по `sha256(raw_lyrics)`.

**Зачем:** online lyrics часто сжаты (`[Chorus x2]`, ссылки на ранее объявленный припев), а Whisper ASR содержит **все** реальные повторения. Без expansion правильный кандидат выглядит короче ASR и теряет в `length_ratio_penalty` против remix-версий, у которых тексты прописаны полностью.

**Algorithmic pass** — `_expand_algorithmic` (`expander.py:135-225`):

1. **`_parse_sections`** — парсит каждую строку на section header `^\[(.+)\]$` или body. Собирает `_Section(label, count, body)`.
2. **`_extract_count`** — извлекает счётчик из label через `_COUNT_FRAGMENT_RE` (поддерживает `x2` / `х2` / `×2` / `2 раза` / `2 times` / `2x` / `2×`).
3. **`_render_sections`** — собирает результат:
   - Если у section есть body → сохраняет в `registry[label]`.
   - Если только header без body → берёт body из `registry[label]` (повтор по reference, типа второй `[Chorus]` без тела).
   - Применяет `_expand_inline_repeats` — ищет в конце строки ` (2 раза)` / ` (2x)` / ` ×3` и повторяет строку.
   - Section целиком повторяется `count` раз.

**LLM pass** — триггерится только если в результате algo-pass есть `_META_INSTRUCTION_RE = r"\b(?:repeat|повтор\w*|снова)\s+(?:chorus|verse|bridge|припев|куплет|бридж)"` И есть `deepseek_api_key`:

- DeepSeek-вызов через OpenAI SDK (`base_url="https://api.deepseek.com"`, `temperature=0.0`, `max_tokens=8192`, timeout 60).
- System prompt (на русском): «Разверни эти инструкции так, чтобы получился полный текст, который реально поётся. Не меняй слова, не сокращай, не добавляй комментариев. Верни только развёрнутый текст».

Логи: `expander_algorithmic_applied`, `expander_llm_applied`, `expander_llm_skipped`.

### 16.6. Matcher — `matcher.py`

**Конструктор** — `worker/common/lyrics/matching/matcher.py:42-56`:

```
expander          — LyricsExpander | None
deepseek_api_key  — для tiebreaker LLM
model             — "deepseek-chat"
thresh_strong     — 0.65
thresh_weak       — 0.45
margin            — 0.05
```

**`match(asr_text, candidates, language, artist_hints, title_hints) -> LyricsResult | None`** — `matcher.py:58-159`:

Конвейер:

1. **Expand all candidates параллельно**: `asyncio.gather(*[expander.expand(c.lyrics) for c in candidates])` (если `expander` есть).
2. **Normalize**: ASR и каждый expanded candidate → `NormalizedText`.
3. **Hint scores**: для каждого кандидата — `_hint_match_score(c.artist, c.title, artist_hints, title_hints)`.
4. **Score all**: `score_all(asr_norm, cand_norms, hint_scores)` → `list[MatchFeatures]`.
5. **Лог `matcher_features`** для каждого кандидата с **полным текстом cand_lyrics** и всеми числами признаков.
6. **Sort descending по composite**.
7. **Decision logic** — таблица:

| Условие | Outcome | Confidence | Действие |
|---|---|---|---|
| `top.composite ≥ 0.65` И `gap ≥ 0.05` (или нет second) | `strong_win` | `high` | вернуть top |
| `top.composite ≥ 0.65` И `gap < 0.05` | LLM tiebreak | `high` (если LLM решил) / `medium` (если нет) | вернуть picked или top |
| `0.45 ≤ top.composite < 0.65` И есть second | `weak_tiebreaker` или `weak_win` | `medium` | LLM tiebreak; если не сработал — top |
| `0.45 ≤ top.composite < 0.65` И нет second | `weak_win` | `medium` | вернуть top |
| `top.composite < 0.45` | `reject` | — | вернуть `None` (поднимется fallback agent в provider chain) |

**Особенность для weak band:** даже когда top-2 близки (gap мал), LLM tiebreak вызывается **всегда**, потому что близкие кандидаты в weak полосе обычно — это «одна и та же песня от разных провайдеров с чуть разным написанием артиста». Отказывать обоим и падать на ASR fallback — потеря правильного текста.

### 16.7. LLM tiebreak — `_tiebreak`, `matcher.py:191-275`

Триггерится только если есть `deepseek_api_key`.

- **DeepSeek через OpenAI SDK** (`base_url="https://api.deepseek.com"`, `temperature=0.0`, **`max_tokens=4`** — только цифра).
- **System prompt (русский):** «Ты выбираешь, какой из двух текстов песни ТОЧНЕЕ соответствует приблизительной расшифровке от Whisper. Учитывай, что Whisper мог исказить слова — ищи смысловое совпадение. Если присутствует `<filename_hint>` — это артист/название из имени файла, СИЛЬНЫЙ приоритетный сигнал, особенно когда ASR содержит мало распознаваемых слов (инструменталки, скэт, повторы la-la). Ответь строго одной цифрой: 1 или 2. Никаких пояснений».
- **User payload (XML-разметка):**
  ```
  <asr language="ru">{asr_text}</asr>
  <filename_hint>
    artist: a1 / a2
    title: t1
  </filename_hint>
  <candidate id="1" artist="A1" title="T1">{lyrics1}</candidate>
  <candidate id="2" artist="A2" title="T2">{lyrics2}</candidate>
  ```
- Парсинг ответа: `cleaned.startswith("1")` → A; `cleaned.startswith("2")` → B; иначе — warning `matcher_tiebreak_unparsed` и `None`.

### 16.8. Hint score — `_hint_match_score`, `matcher.py:285-329`

**`haystack = f"{cand_artist} {cand_title}".casefold()`** — комбинируется в одну строку. Комментарий объясняет: некоторые провайдеры (например, Genius) хранят канонического артиста внутри title (`artist="Genius English Translations"`, `title="Eduard Khil — Я очень рад…"`), поэтому надо матчить по объединённой строке.

Для каждого hint-варианта: `fuzz.partial_ratio(h, haystack) / 100.0`. Берётся лучший.

**Noise floor `_HINT_NOISE_FLOOR = 0.65`** — `matcher.py:282`:
- Ниже floor → `0.0` (шум).
- Выше floor → `(best - 0.65) / (1 - 0.65)` — линейное растягивание `[0.65..1] → [0..1]`.

Если есть и artist_hints, и title_hints — финальный score = среднее двух сторон.

### 16.9. Заметки и потенциальные находки

1. **Веса композита подобраны вручную**, нигде не закомментировано «откуда числа». Это типичный кандидат на ablation в Главе 4 (как изменяются метрики при выключении каждого признака).
2. **`hint_score` с весом 0.30** — самый «весомый» бонус (больше, чем основной `coverage_F1` × 0.55 в принципе не может перевесить, но в close-cases hint решает). Комментарий: «Decisive for songs whose ASR carries little signal — e.g. instrumental humming where every la-la candidate scores high on coverage by accident».
3. **Логирование полных текстов кандидатов** в `matcher_features` (`cand_lyrics=exp_text`) — даёт богатый диагностический след, но в production может быть многословным.
4. **`pymorphy3` подгружается ленивно через `@lru_cache(maxsize=1)`** — фактически singleton. Загружается при первом вызове `_ru_featurizer`.
5. **Levenshtein fallback** в scorer работает только при `|len_diff| ≤ 2` — это резко сужает применимость. Для пары «петь/спел» (3 vs 4 символа) сработает, но «пел/петь» с морфологическим окончанием уже зависит от lemma-матча.
6. **Confidence в `LyricsResult`** имеет три значения: `"high"`, `"medium"`, `"low"` (последнее — только в ASR fallback).
7. **`clean_lyrics`** импортируется из `worker/common/lyrics_searcher` (нужно прочитать).

---

## 17. Провайдеры текстов — `worker/common/lyrics/providers/`

Файлы: `__init__.py` (пустой, 0 строк), `genius.py` (117), `lrclib.py` (72), `lyricsovh.py` (46), `chartlyrics.py` (117), `simpmusic.py` (77). Все провайдеры — обёртки над разными HTTP-API; общий контракт через `TextSearchProvider` / `ArtistTitleProvider` (см. п. 15.2). Все используют `httpx.AsyncClient`.

### 17.1. Сводная таблица

| Провайдер | Класс | Контракт | Источник | Auth | Стратегия запроса | Шаги | Min len | Max результатов |
|---|---|---|---|---|---|---|---|---|
| Genius | `GeniusProvider` | `TextSearch` | api.genius.com + scrape | `Authorization: Bearer GENIUS_TOKEN` | `q=text_fragment` | search → scrape HTML (BeautifulSoup) | 20 | 3 |
| LRCLib | `LRCLibProvider` | `ArtistTitle` | lrclib.net/api | none | `q="{a} {t}"`, fallback structured | 1 (с retry × 2) | 20 | 3 (берётся первый) |
| Lyrics.ovh | `LyricsOvhProvider` | `ArtistTitle` | api.lyrics.ovh/v1 | none | path `/{artist}/{title}` | 1 | 20 | 1 |
| ChartLyrics | `ChartLyricsProvider` | `TextSearch` | api.chartlyrics.com (HTTP only) | none | `lyricText=fragment` | search XML → fetch full XML | 20 | 3 |
| SimpMusic | `SimpMusicProvider` | `TextSearch` | api-lyrics.simpmusic.org/v1 (YouTube Music backend) | none | `q=fragment` → `/{video_id}` | search → fetch lyrics | 20 | 3 |

Общие свойства всех провайдеров:
- Контракт ошибок: ловят `httpx.HTTPError` → warning-лог + `[]` / `None`. Соответствует контракту base_provider (см. п. 15.2).
- Минимальная длина текста для приёма: `len(lyrics) >= 20` (отсев пустых/коротких ответов).
- `name` — строковый class-attribute, попадает в `LyricsCandidate.source`.
- `timeout` — параметр конструктора (default 10 сек). В фактическом конфиге `settings.lyrics_provider_timeout = 10.0`.

### 17.2. ⚠️ Из 5 провайдеров фактически используются только 3

`grep` по `*Provider(` (инстанциация) показывает (`worker/app/main.py:88-100`):

| Провайдер | Импортируется | Инстанциируется | Условие |
|---|---|---|---|
| `GeniusProvider` | да | **да** | если задан `settings.genius_token` |
| `LRCLibProvider` | да | **да** | всегда (один из metadata-providers) |
| `LyricsOvhProvider` | да | **да** | всегда (один из metadata-providers) |
| `ChartLyricsProvider` | **нет** | **нет** | **мёртвый код** — класс определён, никогда не подключается к chain'у |
| `SimpMusicProvider` | **нет** | **нет** | **мёртвый код** — класс определён, никогда не подключается к chain'у |

**Это противоречило описанию CLAUDE.md** в разделе SEARCHING_LYRICS: «fetches lyrics from genius, lrclib, lyricsovh, chartlyrics, simpmusic» — на самом деле в реальном пайплайне работают только первые три. ChartLyrics и SimpMusic были реликтовым кодом и удалены в cleanup'е 2026-05-13 (см. `git log`).

`__init__.py` файла пустой — то есть нет «общего» импорта, который бы все провайдеры поднимал автоматически. `_build_gpu_pipeline` вручную выбирает, какие подключать.

### 17.3. GeniusProvider — особенности

- **Bearer-аутентификация:** `Authorization: Bearer {GENIUS_TOKEN}`.
- 2-шаговый процесс:
  1. `GET https://api.genius.com/search?q={text_fragment}` — JSON-ответ с `response.hits[]`.
  2. Для каждого hit (макс 3): `GET song.url` (HTML-страница) → BeautifulSoup → `[data-lyrics-container='true']` div'ы.
- **`_BROWSER_HEADERS`** (User-Agent Chrome 120 + Accept) для скрейпинга — обходит base-bot-фильтры.
- Преобразование: `<br>` → `\n`, склейка через `\n`.
- **Чистка header-блока:** ищет маркер `"Lyrics\n"` или `"Contributors"` в первом контейнере, отрезает всё до маркера; затем удаляет всё до `"Read More"` (description-блок).
- artist берётся из `song.primary_artist.name`, title из `song.title`. **Замечание:** комментарий в `_hint_match_score` (см. п. 16.8) указывает, что у Genius каноническое имя артиста иногда лежит внутри title (`artist="Genius English Translations"`, `title="Eduard Khil — Я очень рад…"`). Это известная проблема Genius для не-английского контента.

### 17.4. LRCLibProvider — особенности

- **Двухступенчатый поиск** — `lrclib.py:36-44`:
  1. Combined: `?q=f"{artist} {title}"` (комментарий: «handles partial artist names better»).
  2. Fallback structured: `?track_name={title}&artist_name={artist}` если первый ничего не вернул.
- Возвращает первый кандидат из ответа (хотя обрабатывает до 3).
- **Единственный провайдер с явными retries**: `httpx.AsyncHTTPTransport(retries=2)` — `lrclib.py:31`.
- `plainLyrics` берётся (без LRC-таймингов), а не `syncedLyrics`. То есть LRCLib используется как plain-text провайдер, хотя имя сервиса намекает на synced-LRC.
- artist/title берутся из ответа API: `artistName`, `trackName`.

### 17.5. LyricsOvhProvider — особенности

- **Самый минималистичный:** один GET `/{artist}/{title}`, один результат, без ретраев, без fallback.
- **artist и title берутся из входных параметров, а не из ответа API** — `lyricsovh.py:42-43`. Это означает, что lyrics.ovh возвращает текст «как есть», и провайдер не верифицирует, что нашёл правильную песню. Все полагается на алгоритмический matcher (см. п. 16).
- Максимум 1 кандидат на запрос.

### 17.6. ChartLyricsProvider — особенности (мёртвый код, *файл удалён 2026-05-13*)

- Шапка: «**HTTP only (no HTTPS)**» — `_SEARCH_URL = http://...`.
- XML API через `xml.etree.ElementTree`.
- 2 endpoint'а: `/SearchLyricText` (вернёт XML с `LyricId` + `LyricChecksum` для каждого hit) → `/GetLyric` (полный текст по id+checksum).
- `_detect_namespace` извлекает namespace из root tag (XML namespaces).
- `_text` — helper для безопасного получения текста дочернего элемента.
- Пропускает entries с `lyric_id == "0"` или без checksum.
- Финальные artist/title — из `LyricArtist`/`LyricSong` API-ответа, fallback на search-результат.

### 17.7. SimpMusicProvider — особенности (мёртвый код, *файл удалён 2026-05-13*)

- Backend: api-lyrics.simpmusic.org (YouTube Music через third-party gateway).
- 2-шаговый: search → `/{video_id}`.
- **Гибкий парсинг ответа**: `data.get("data") or data.get("items") or []`, lyrics — `plainLyrics or lyrics`. Это указывает на нестабильность схемы API.
- artist/title из meta search-ответа: `meta.get("artist", meta.get("artists", ""))`, `meta.get("title", meta.get("name", ""))`.

### 17.8. Соответствие CLAUDE.md

- ⚠️ **Устранённое расхождение:** CLAUDE.md перечислял «genius, lrclib, lyricsovh, chartlyrics, simpmusic» как провайдеры. Фактически в `_build_gpu_pipeline` собирались **только первые три** (Genius — условно, при наличии токена; LRCLib и LyricsOvh — всегда). ChartLyrics и SimpMusic удалены как мёртвый код 2026-05-13 (см. `git log`).
- ✅ «one of these may use the local SearXNG instance in `searxng/` as fallback» — **не относится к провайдерам как таковым**: SearXNG используется только в `LyricsAgent` (см. сборку в `_build_gpu_pipeline`). Среди провайдеров файла `searxng.py` нет.

---

## 18. Общие типы и `clean_lyrics` — `worker/common/lyrics_searcher.py`

Файл: 52 строки.

### 18.1. `LyricsResult` — `worker/common/lyrics_searcher.py:9-17`

```python
@dataclass
class LyricsResult:
    artist: str
    title: str
    lyrics: str
    language: str
    confidence: str   # "high" / "medium" / "low"
    source_note: str  # имя провайдера или "asr_fallback"
```

Используется во всём пайплайне поиска текстов: возвращается из `LyricsProviderChain.search`, далее передаётся в `gpu_pipeline.py` (через `lyrics_result.artist/title/lyrics/language/source_note`).

### 18.2. Исключения

- `LyricsSearchError` — базовый класс.
- `LyricsNotFoundError(LyricsSearchError)` — песня не идентифицирована / текст не найден. Бросается из `LyricsProviderChain.search` если все 4 стадии провалились.
- `LyricsAPIError(LyricsSearchError)` — сетевая/API-ошибка (retryable). Бросается из `LyricsAgent.search` на исключениях DeepSeek API.

В `gpu_pipeline.py:190-195` ловится `LyricsSearchError` — то есть и Found, и API. На любом случае → `mark_permanently_failed`.

### 18.3. `clean_lyrics(raw)` — `worker/common/lyrics_searcher.py:32-52`

Постобработка scraped lyrics перед передачей в CTC. Вызывается из `LyricsMatcher._build_result` (см. п. 16.6).

| Шаг | Регулярка | Назначение |
|---|---|---|
| 1 | `\[.*?\]\n?` (DOTALL) | Удаление section markers `[Intro]`, `[Verse 1]`, `[Припев: Artist\n& Artist2\n]`. |
| 2 | `\n+(?=[ \t]*[,.;:!?)\]…—–])` | Если строка начинается с закрывающей пунктуации — удалить предшествующие переводы. |
| 3 | `\n+(?=[ \t]+\S)` | Если строка начинается с горизонтального пробела — continuation, `\n` → ` `. |
| 4 | `([(\[])[ \t]*\n+` | Если предыдущая строка закончилась открывающей скобкой — соединить. |
| 5 | `\n{3,}` → `\n\n` | Schmoosh 3+ пустых строк до 2. |

**Мотивация (комментарий 38-42):** Genius вставляет `<br>` перед хвостовой пунктуацией или внутри parenthesised aside, оставляя «Она не твоя\n, ты ...» или «ты? (\nРядом с кем-то\n)». Без склейки aligner трактует их как отдельные строки (что собьёт line-breaker).

**Замечание:** этот же шаг 1 ([…]) дублирует функциональность `_SECTION_RE` из normalizer (см. п. 16.2). Применяется на финальной стадии перед сохранением в БД — текст в треке (`tracks.lyrics_text`) идёт без section markers.

---

## 19. Резервный LLM-агент — `worker/common/lyrics_agent.py`

Файл: 575 строк. Самый сложный компонент поиска текстов.

### 19.1. Назначение и общая стратегия

Шапка `worker/common/lyrics_agent.py:1-7`: «Lyrics search agent — collects 1-3 raw lyrics candidates from the web. Uses an agentic tool-calling loop: the LLM can invoke `web_search` (SearXNG primary, Yandex fallback) and `fetch_webpage` (httpx + BeautifulSoup) to find pages that look like the song's lyrics. It returns a JSON array of candidates — **selection between them is the matcher's job, not the agent's**.»

Двухпроходный pass-by-backend (см. п. 19.6).

### 19.2. Параметры конструктора и связь с конфигом

| Параметр | Default класса | Default `WorkerSettings` |
|---|---|---|
| `deepseek_api_key` | required | `settings.deepseek_api_key` |
| `yandex_search_api_key` | `""` | `settings.yandex_search_api_key` |
| `yandex_search_folder_id` | `""` | `settings.yandex_search_folder_id` |
| `model` | `"deepseek-chat"` | `settings.deepseek_model` |
| `max_iterations` | **20** | **`settings.lyrics_agent_max_iterations = 15`** |
| `timeout` | `15.0` | `settings.lyrics_agent_timeout = 15.0` |
| `searxng_url` | `None` | `settings.searxng_url = "http://searxng:8080"` |

В реальном воркере применяется `max_iterations=15` (settings переопределяет class default).

### 19.3. System prompt — `worker/common/lyrics_agent.py:33-70`

**Язык: русский.** Основные пункты:

- Задача: найти 1-3 страницы с текстом песни и **вернуть сырое содержимое**. Выбор лучшего — НЕ задача агента.
- **Обязательный алгоритм:**
  1. `web_search` с КОРОТКИМ запросом (2-4 слова: артист + название + «текст»).
  2. `fetch_webpage` самой релевантной ссылки.
  3. Анализ текста на странице.
  4. Если подходит — добавить в финальный JSON.
  5. Если нет — повторить с шага 1 с другой формулировкой.
- **Запрещено:**
  - 2 `web_search` подряд без `fetch_webpage` между ними.
  - Кавычки длиннее 3 слов (Whisper искажает, exact-match не работает).
- **Подсказки:** если артист транслитерирован — пробовать и оригинал, и канонический; для русских — `<artist> <title> текст песни`, для английских — `<artist> <title> lyrics`.
- **Формат ответа: строго JSON-массив** `[{"artist": "...", "title": "...", "lyrics": "..."}]`. Не объект.
- Текст со страницы можно отдавать с маркерами `[Куплет]/[Chorus]` — их чистит `clean_lyrics`.

### 19.4. Tool-определения — `worker/common/lyrics_agent.py:72-109`

Стандартный OpenAI Tools schema, два инструмента:

```json
{ "name": "web_search", "parameters": { "query": "string" } }
{ "name": "fetch_webpage", "parameters": { "url": "string" } }
```

### 19.5. Бэкенды веб-поиска

#### 19.5.1. SearXNG — `_searxng_search`, `lyrics_agent.py:139-167`

- GET `{searxng_url}/search?q={query}&format=json&categories=general&language=ru`.
- Первые **10** результатов: `{title, href (=url), body (=content)}`.
- **`language=ru` захардкожен** — потенциальная проблема для не-русского контента (для en будет работать, но релевантность может страдать).
- На ошибке: warning `searxng_search_failed` + `None`.

#### 19.5.2. Yandex Search API — `_yandex_search`, `lyrics_agent.py:170-227`

- POST `https://searchapi.api.cloud.yandex.net/v2/web/search`.
- Auth: `Authorization: Api-Key {yandex_api_key}`.
- Body: `{query: {searchType: "SEARCH_TYPE_RU", queryText, familyMode: "FAMILY_MODE_NONE", page: 0}, folderId, groupSpec: {groupMode: "GROUP_MODE_FLAT", groupsOnPage: 10, docsInGroup: 1}, maxPassages: 2, l10n: "LOCALIZATION_RU", responseFormat: "FORMAT_XML"}`.
- **Response: JSON с `rawData`, который base64-encoded XML.** Парсинг через `xml.etree.ElementTree` с wildcard namespaces (`{*}doc`, `{*}url`, `{*}title`, `{*}passages/{*}passage`).
- На ошибке: warning `yandex_search_failed` + `None`.

#### 19.5.3. `_web_search(query, backend, ...)` — `lyrics_agent.py:230-280`

Маршрутизация по `backend` ("searxng" / "yandex"). Перед вызовом — валидация:

- **`_quoted_phrase_too_long`** — `lyrics_agent.py:125-131`: если в query есть `"..."` с >3 слов внутри → tool возвращает JSON с ошибкой и инструкцией использовать короткие quotes. Это видит LLM в next-turn и переформулирует запрос. `_MAX_WORDS_PER_QUOTED_PHRASE = 3` — `lyrics_agent.py:119`.
- Если бэкенд не сконфигурирован → JSON-ошибка («SearXNG/Yandex backend not configured»).
- Если поиск ничего не нашёл → JSON `{"error": "Ничего не найдено"}`.

### 19.6. `_fetch_webpage(url, timeout)` — `lyrics_agent.py:283-302`

- httpx с `User-Agent` Chrome 120 (`_BROWSER_UA`).
- `follow_redirects=True`.
- BeautifulSoup: **удаляются теги** `script, style, nav, header, footer, aside, iframe, noscript`.
- `text = soup.get_text(separator="\n", strip=True)`.
- **Обрезка до 12000 символов** + `"\n...[обрезано]"` если длиннее.
- На любой ошибке: JSON `{"error": str(e)}` (видит LLM).

### 19.7. Алгоритм `LyricsAgent.search` — `lyrics_agent.py:334-426`

1. Собирает **user_message** на русском с расшифровкой ASR, языком, всеми вариантами hints (включая `artist_alts`/`title_alts` от FilenameParser).
2. **Sequential two-pass по бэкендам:**
   - Если `searxng_url` задан → backend "searxng" первый.
   - Если `yandex_api_key && folder_id` → backend "yandex" второй.
   - **SearXNG идёт первым** (бесплатный, broad), Yandex — только если SearXNG ничего не вернул (для экономии quota — комментарий 372-374).
3. На втором pass'е к user_message добавляется системная подсказка: «предыдущая попытка через {prior} НЕ нашла подходящего текста. Сейчас активен {backend} — попробуй другие формулировки запросов».
4. Каждый pass: `_run_agent(message, backend)` через `asyncio.to_thread`.
5. На исключении: `LyricsSearchError` пробрасывается, иначе wrapping в `LyricsAPIError`.
6. Если pass нашёл кандидатов → `break`, дальнейшие бэкенды не запускаются.

### 19.8. Цикл агента — `_run_agent`, `lyrics_agent.py:432-525`

- DeepSeek через OpenAI SDK: `base_url="https://api.deepseek.com"`, **`timeout=120.0`** (более долгий, чем 60 у FilenameParser/expander и 30 у matcher tiebreak).
- Параметры запроса: `model`, `messages`, `tools=_TOOLS`, `max_tokens=8192`.
- Цикл до `max_iterations` итераций.
- На каждой итерации:
  - `client.chat.completions.create(...)` → `message`.
  - Если у `message.tool_calls` пусто → возврат `message.content` (это финальный ответ агента, JSON-массив).
  - Иначе выполняются tool_calls.
- **Защита от инфинит-search-loop** — `_MAX_CONSECUTIVE_SEARCHES = 2`:
  - Если уже сделано 2 `web_search` подряд и пришёл третий → не вызывается реальный поиск, возвращается JSON-ошибка: «Сейчас ОБЯЗАТЕЛЬНО загрузи самую релевантную ссылку через `fetch_webpage`. Только после fetch_webpage можно будет снова искать».
  - Счётчик `consecutive_searches` инкрементируется на `web_search`, сбрасывается на `fetch_webpage`.
  - Комментарий 458 ссылается на наблюдаемый failure pattern: «Dzetta - Кометы.mp3».
- Если итерации исчерпаны → warning `agent_iterations_exhausted` + возврат `"[]"`.

### 19.9. Парсинг ответа — `_parse_candidates`, `lyrics_agent.py:531-567`

- Прямой `json.loads(raw)`.
- Fallback: regex `\[\s*\{.*?\}\s*\]` (DOTALL) — поиск JSON-массива внутри ответа.
- Если не list → warning `agent_response_not_array` + `[]`.
- Для каждого item:
  - `len(item["lyrics"]) >= 20` (та же эвристика, что у провайдеров).
  - `artist` / `title` defaults к `"Unknown"`.
  - **`source = backend`** (`"searxng"` или `"yandex"`) — не имя сервиса лирики, а имя поискового бэкенда. Это будет видно в логах matcher'а и в `tracks.lyrics_source`.

### 19.10. Логи

- Жизненный цикл: `lyrics_agent_no_backends_configured`, `lyrics_agent_starting`, `lyrics_agent_pass_starting`, `lyrics_agent_pass_completed`, `lyrics_agent_completed`.
- Tool-level: `agent_tool_call` (debug), `agent_search_blocked_force_fetch`, `agent_iterations_exhausted`, `web_search_via` (debug), `web_search_rejected_long_quote`.
- Бэкенды: `searxng_search_failed`, `yandex_search_failed`.
- Парсинг: `agent_response_not_array`.

### 19.11. Соответствие CLAUDE.md и наблюдения

- ✅ «one of these may use the local SearXNG instance in `searxng/` as fallback» — соответствует архитектуре, но описание неточное: SearXNG не «один из провайдеров», а **бэкенд внутри `LyricsAgent`**. Этим же агентом используется и Yandex Search.
- ⚠️ **CLAUDE.md не упоминает Yandex Search API** как paid fallback. Это значимое неосвещённое поведение.
- ⚠️ **CLAUDE.md не упоминает все защиты агента**: запрет длинных кавычек, блокировка >2 web_search подряд, обрезка fetch_webpage до 12000 символов, two-pass для экономии Yandex quota.
- ⚠️ **`source` в LyricsCandidate от агента — это backend** (`"searxng"`/`"yandex"`), не реальный домен страницы, с которой взят текст. Если нужна полная атрибуция (для аналитики «какие сайты дают качественные тексты»), эта информация теряется.
- 🆕 Подтверждённое суммарное число точек использования DeepSeek в воркере: **4** — FilenameParser, LyricsExpander, LyricsMatcher (tiebreak), LyricsAgent (агентный loop). Один ключ, одна модель `"deepseek-chat"`.

---

## 20. `worker/common/segment_builder.py` — мёртвый модуль *(удалён 2026-05-13)*

**Файл:** `worker/common/segment_builder.py` (119 строк).

### 20.1. Назначение

Шапка `worker/common/segment_builder.py:1-6`: «Build alignment segments from VAD intervals. Utility for distributing lyrics lines across audio regions. **Currently unused by the main pipeline (single-pass CTC alignment), but kept for potential future segmented alignment approaches.**»

То есть автор сам помечает файл как **резерв на случай перехода на segmented-alignment**.

### 20.2. Подтверждение мёртвости

`grep -rn 'build_segments_from_vad\|segment_builder'` по worker/, shared/, backend/ — найдено только определение в самом `segment_builder.py:18`. Нигде не импортируется и не вызывается.

### 20.3. Содержимое (для справки)

- **`build_segments_from_vad(vad_segments, lyrics_text) -> list[tuple[float, float, str]]`**:
  1. Merge VAD-интервалов с `gap < _MERGE_GAP = 0.5` сек.
  2. Сегменты короче `_MIN_SEGMENT_SEC = 1.0` сек присоединяются к предыдущему.
  3. Lyrics-строки распределяются по merged-сегментам пропорционально длительности (`frac = reg_dur / total_dur`).
  4. Внутри сегмента строки получают доли времени пропорционально числу слов.
  5. Хвостовые строки (если осталось после последнего сегмента) дописываются к последнему сегменту.

### 20.4. Связь с прочитанным ранее

Это объясняет смысл двух мёртвых артефактов в `VADProcessor` (см. п. 12.5–12.6):
- `VADResult.segments` собирается «впрок» — именно для входа в `build_segments_from_vad`.
- `map_cleaned_to_original` — также «впрок» для возможного segmented-варианта.

Все три артефакта (`VADResult.segments`, `VADProcessor.map_cleaned_to_original`, `segment_builder.py`) образуют **согласованный задел на «segmented alignment»**, который не реализован в текущем коде. Автор оставил весь каркас на месте для возможного будущего перехода. Это не три независимые ошибки — это единая фича-заготовка.

---

## 21. Разбиение на строки — `shared/karaoke_shared/utils/line_breaker.py`

**Файл:** `shared/karaoke_shared/utils/line_breaker.py` (185 строк). Точка входа: `detect_line_breaks(timings, vocal_path=None) -> list[SyllableTiming]`. Вызывается из `gpu_pipeline.py:234, 236-238` после CTC.

### 21.1. Назначение

Шапка `line_breaker.py:1-15`: «When LRC data is not available (e.g. online Sonoix flow), we need to determine where to place `\n` line-break markers in the syllable stream».

Два режима + автовыбор. Если в timings уже есть `\n` (от LRC) — модуль возвращает как есть.

### 21.2. Главный алгоритм `detect_line_breaks` — `line_breaker.py:29-82`

1. **Защиты:**
   - `len(timings) < 2` → возврат `list(timings)` без изменений.
   - Если хоть один `s.syllable.startswith("\n")` → возврат как есть («LRC уже разметил»).
2. **Сбор gap'ов**: `gaps = [timings[i].start - timings[i-1].end for i in range(1, len(timings))]`.
3. **Метрика выбора режима**: `large_gap_count = sum(1 for g in gaps if g > 0.4)`.
4. **Auto-select:**
   - **`large_gap_count >= 5`** → `_gap_mode(timings, gaps)` (default `threshold_floor=0.3`).
   - **иначе если `vocal_path` задан** → `_beat_mode(timings, vocal_path)`.
   - **иначе** → `_gap_mode(timings, gaps, threshold_floor=0.2)` (relaxed fallback).
5. `_inject_breaks(timings, break_indices)` → новый список с `\n`.
6. Лог `line_break_detection_completed` с `breaks` (count) и `duration_sec`.

### 21.3. Gap mode — `_gap_mode`, `line_breaker.py:85-121`

- **Динамический порог:** `threshold = max(threshold_floor, P75(gaps) * 2.5)`.
- Параметр `threshold_floor`: **0.3 сек** (default), **0.2 сек** (relaxed fallback).
- Tracking `char_count` для force-break длинных строк.
- **Break фиксируется при выполнении ОБОИХ условий:**
  - `gap > threshold` И слог начинается с пробела (`syl.startswith(" ")` — индикатор word boundary, см. п. 11.13);
  - либо `char_count > 50` И word boundary — force-break длинных строк.
- Гарантия: разрыв всегда на word boundary, не внутри слова.

### 21.4. Beat mode — `_beat_mode`, `line_breaker.py:124-164`

- **Lazy импорт `librosa`** (`line_breaker.py:137`).
- `y, sr = librosa.load(vocal_path, sr=22050)`.
- `tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)`.
- `beat_times = librosa.frames_to_time(beat_frames, sr=sr)`.
- **Защита от слабого beat detection:** если `len(beat_times) < 4` → fallback к relaxed gap mode (`threshold_floor=0.2`).
- **Группировка в 4-битовые бары:** `bar_times = beat_times[::4]` — каждый 4-й beat (предположение размера 4/4).
- Для каждого слога: если `is_word_boundary` И `syl.start >= bar_times[bar_idx] - 0.3` (tolerance 300 мс) → break, `bar_idx += 1`.
- Skip first bar (`bar_idx = 1` стартовое значение) — комментарий: «start of song».

### 21.5. Инъекция переводов — `_inject_breaks`, `line_breaker.py:167-185`

- Для каждого индекса в `break_indices`:
  - Если слог начинается с `" "` → заменить пробел на `\n` (`text = "\n" + text[1:]`).
  - Иначе если не с `\n` → prepend `\n`.
- Создаётся новый `SyllableTiming` с тем же `start`, `end`.

### 21.6. Где используется librosa в коде воркера

Этот файл — **единственное место в воркере, где используется librosa напрямую**. Подтверждено grep'ом раньше (см. п. 12.6 — VAD написан без librosa). Зависимость librosa в `worker/pyproject.toml` оправдана исключительно этим модулем (плюс `feature_extractor` в `shared/`, но он используется только rec-сервисом, не воркером).

### 21.7. Соответствие CLAUDE.md

- ✅ «Injects `\n` markers into the syllable stream (`shared/karaoke_shared/utils/line_breaker.py`, called from `worker/gpu/gpu_pipeline.py`).» — соответствует.
- ✅ «Auto-selects between *gap mode* (break at inter-syllable gaps above a track-adaptive threshold) and *beat mode* (`librosa.beat.beat_track` on the vocal audio — used when too few large gaps, typical for rap).» — соответствует, плюс уточнения: критерий выбора — `large_gap_count >= 5` с порогом `0.4` сек.
- ✅ «Skipped when timings already carry `\n` from LRC.» — соответствует (`any(s.syllable.startswith("\n"))`).
- 🆕 **Конкретные параметры, не описанные в CLAUDE.md:**
  - Gap threshold: `max(0.3, P75 × 2.5)` (или `0.2` в fallback).
  - Force-break длинных строк: `char_count > 50`.
  - Beat group: каждый 4-й beat (предположение 4/4).
  - Beat tolerance: 300 мс.
  - Sample rate librosa: 22050.

---

## 22. Слогоразбиение — `shared/karaoke_shared/utils/syllabifier.py`

**Файл:** `shared/karaoke_shared/utils/syllabifier.py` (207 строк). Класс `Syllabifier`.

### 22.1. Назначение

Шапка класса (`syllabifier.py:18-31`): «Converts ASR tokens to syllable-level timings for karaoke display. Supports two token formats: BPE sub-word (Soniox) и word-level (WhisperX)». Auto-detect: если хоть один токен начинается с пробела → BPE, иначе word-level.

### 22.2. Зависимости

- **`pyphen`** — единственная сторонняя библиотека для слогоразбиения.
- Внутренний кеш `_dicts: dict[str, pyphen.Pyphen]` с ленивой загрузкой по языкам.

### 22.3. Языковая поддержка

- **`_SUPPORTED_PYPHEN_LANGS = {"en", "ru"}`** — `syllabifier.py:12`.
- Для других языков → fallback на `en`.
- Маппинг кодов: `ru → "ru_RU"`, `en → "en_US"`.
- **`_detect_word_lang(word)`** (`syllabifier.py:168-177`): per-word script detection — `"ru"` если в слове есть Cyrillic (`_CYRILLIC_RE = r"[Ѐ-ӿ]"`), иначе `"en"`. Это позволяет корректно обрабатывать английские слова в русском треке (и наоборот).

### 22.4. Главный метод `_split_word(word, lang)` — `syllabifier.py:179-207`

**⚠️ Нюанс:** хотя метод принимает параметр `lang`, **он его игнорирует** — вместо этого использует `_detect_word_lang(alpha_core)`. То есть переданный `language` из TorchCTCAligner и ctc_subprocess не влияет на выбор pyphen-словаря.

**Алгоритм:**

1. `_ALPHA_RE = r"[^\W\d_]+"` — извлекает только буквы (без цифр).
2. Из слова отделяются `prefix` (пунктуация в начале), `alpha_core` (только буквы), `suffix` (пунктуация в конце).
3. **`_detect_word_lang(alpha_core)`** определяет язык по script.
4. **`pyphen.Pyphen.inserted(alpha_core)`** вставляет `-` между слогами.
5. `inserted.split("-")` → список слогов.
6. Префикс приклеивается к первому слогу, суффикс — к последнему.

### 22.5. Использование в воркере

`grep` подтвердил: класс инстанциируется в двух местах:

| Где | Что вызывается |
|---|---|
| `worker/gpu/torch_ctc_aligner.py:64` (`__init__`) | используется только `_syllabifier._split_word(word, language)` (см. п. 11.13) |
| `worker/common/ctc_subprocess.py:76` | то же — только `_split_word` (см. п. 14.6) |

**Оба CTC-варианта вызывают приватный метод `_split_word`** — нарушение инкапсуляции, но de-facto такова реальность кода.

### 22.6. Мёртвые публичные API

`grep -rn '\.syllabify(\|split_text_to_syllables'` показал, что **оба публичных метода класса не вызываются нигде, кроме определения**:

- **`syllabify(tokens)`** — определён, но не вызывается. Был задуман для конвертации Soniox/WhisperX-токенов в SyllableTiming. Обе ветки (BPE и word-level) — мёртвый код.
- **`split_text_to_syllables(text, language)`** — определён, не вызывается. Был задуман как preparation для WhisperX `force_align()` (split до alignment, чтобы каждый слог получил свой timestamp). Поскольку текущий пайплайн использует MMS-300m через torchaudio, а не WhisperX, метод не нужен.
- Внутренние методы `_from_bpe_tokens`, `_from_word_tokens` — мёртвые (используются только из мёртвого `syllabify`).

**Из 207 строк файла фактически работающие — это `_get_dict`, `_detect_word_lang`, `_split_word` (≈50 строк).** Остальное — заготовки под Soniox/WhisperX-варианты, которые больше не используются.

### 22.7. Соответствие CLAUDE.md

CLAUDE.md этот файл прямо не упоминает — он часть `shared/`. Но косвенно:
- ✅ Слогоразбиение работает в CTC через `pyphen` — соответствует общей картине.
- 🆕 Не описано: per-word script detection через `_detect_word_lang` — это означает, что для **смешанных треков** (русский + английский в одной песне) каждый слог сегментируется правильным словарём независимо от глобального `detected_language` от Whisper. Полезный материал для главы про устойчивость.

### 22.8. Итог: накопленный мёртвый код

**Cleanup 2026-05-13:** все 10 артефактов, ранее перечисленных в этой таблице, удалены из репозитория (см. `git log`). Историческое содержание таблицы оставлено ниже для отслеживания технического долга / истории эволюции подсистемы — все артефакты теперь имеют статус **УДАЛЕНО**.

| # | Артефакт | Тип | Намерение | Статус |
|---|---|---|---|---|
| 1 | `WhisperTranscriber.warmup()` | метод | прогрев CUDA-ядер | удалено 2026-05-13 |
| 2 | `GpuPipeline._parse_hints_from_path` | static method | парсинг «Artist - Title.mp3» (заменён filename_parser/job-полями) | удалено 2026-05-13 |
| 3 | `AlignmentStats.char_level_used` | поле dataclass | счётчик char-level alignment (отключён из-за heap corruption) | удалено 2026-05-13 (во всех 4 файлах) |
| 4 | `VADProcessor.map_cleaned_to_original` | static method | маппинг таймингов из cleaned обратно в оригинал | удалено 2026-05-13 |
| 5 | `VADResult.segments` (как потребляемое поле) | возврат | для segmented alignment | удалено 2026-05-13 |
| 6 | `ChartLyricsProvider` (~117 строк) | целый класс | один из провайдеров (отключён) | удалён файл 2026-05-13 |
| 7 | `SimpMusicProvider` (~77 строк) | целый класс | один из провайдеров (отключён) | удалён файл 2026-05-13 |
| 8 | `worker/common/segment_builder.py` (119 строк) | целый модуль | segmented alignment по VAD-интервалам | удалён файл 2026-05-13 |
| 9 | `Syllabifier.syllabify` + `_from_bpe_tokens` + `_from_word_tokens` | публичный API | конвертация Soniox/WhisperX-токенов | удалено 2026-05-13 |
| 10 | `Syllabifier.split_text_to_syllables` | публичный API | подготовка для WhisperX force_align | удалено 2026-05-13 |

**Группировка по «фичам-заготовкам»** (исторически — все группы устранены):

- **Segmented alignment** (одна история): `VADResult.segments` + `map_cleaned_to_original` + `segment_builder.py`.
- **Альтернативные провайдеры** (отключены): `ChartLyricsProvider` + `SimpMusicProvider`.
- **WhisperX/Soniox миграция-заготовки** (использовались в прошлых архитектурах): `Syllabifier.syllabify` + связанные.
- **Прочие реликты:** `warmup`, `_parse_hints_from_path`, `char_level_used`.

Сохраняется как материал для главы ВКР про **технический долг и эволюцию подсистемы** — фиксирует наблюдение и устранение, а не текущее состояние.

---

## 23. RabbitMQ — топология и клиент (`shared/karaoke_shared/messaging/`)

**Файлы:** `__init__.py` (экспорт `RabbitMQClient`), `rabbitmq.py` (171 строка).

Шапка `rabbitmq.py:5-9` сразу даёт ASCII-схему топологии. Backend — **`aio_pika`** (async-AMQP клиент).

### 23.1. Клиент `RabbitMQClient`

- Создание: `RabbitMQClient(url)`.
- `connect()` → `aio_pika.connect_robust(url)` — **robust-connection** с auto-reconnect.
- Один канал на клиент (`self._connection.channel()`).
- `close()` — graceful disconnect.

### 23.2. Полная топология — `declare_topology`, `rabbitmq.py:53-108`

| Exchange | Type | Durable | Назначение |
|---|---|---|---|
| **`jobs`** | direct | да | задачи воркеру (от backend) |
| **`job.progress`** | **fanout** | **нет** | SSE-broadcast прогресса |
| **`rec`** | direct | да | взаимодействие с рекомендательной подсистемой |
| **`dlq`** | direct | да | dead-letter routing |

| Queue | Durable | Bind | DLX | Особые args |
|---|---|---|---|---|
| `jobs.process` | да | exchange `jobs`, rk=`""` | exchange `dlq`, rk=`jobs` | **`x-max-priority=10`** |
| `rec.index` | да | exchange `rec`, rk=`""` | exchange `dlq`, rk=`rec` | — |
| `rec.indexed` | да | exchange `rec`, rk=**`indexed`** | **нет** | — |
| `jobs.dlq` | да | exchange `dlq`, rk=`jobs` | — | **`x-message-ttl=259200000`** (72 ч) |
| `rec.dlq` | да | exchange `dlq`, rk=`rec` | — | **`x-message-ttl=259200000`** (72 ч) |

**Сценарии:**

- `jobs.process` ← worker consumes (см. п. 3). На `nack(requeue=False)` → DLX → `jobs.dlq`.
- `rec.index` ← rec-service consumes (вне ВКР). На отказе → DLX → `rec.dlq`.
- `rec.indexed` ← **backend consumes**. Комментарий `rabbitmq.py:103-104`: «rec-service publishes after QDrant upsert, backend consumes to update `tracks.qdrant_synced` in PG». **Этот обмен не упомянут в CLAUDE.md** — фиксирую как несоответствие.
- `job.progress` ← SSE-endpoints в backend создают exclusive auto-delete queues per-connection.

### 23.3. Особенности топологии

1. **Priority queue для задач воркера:** `jobs.process` объявлена с `x-max-priority=10`. Это позволяет backend'у указывать `priority` параметр при `publish()` (срочные пользовательские upload'ы выше обычных). В `publish()` (см. п. 23.4) параметр `priority` передаётся в сообщение.

2. **DLX-цепочка:** `x-dead-letter-exchange` + `x-dead-letter-routing-key` в args очередей. Когда сообщение в `jobs.process` или `rec.index` reject'ится `nack(requeue=False)` (что и делает консьюмер при exception, см. п. 3) или таймаутится — оно автоматически уходит в соответствующий DLQ.

3. **Fanout без durable** для `job.progress`: само сообщение прогресса не имеет смысла хранить, если нет подписчика. SSE-подключение создаёт собственную exclusive queue.

4. **Exclusive auto-delete queues для SSE** — `create_exclusive_queue(exchange)`, `rabbitmq.py:156-171`:
   - `declare_queue(exclusive=True, auto_delete=True)` — анонимное имя, удаляется при disconnect.
   - Bind к fanout-exchange `job.progress`.
   - Идиоматический pattern для SSE-broadcast.

### 23.4. `publish` — `rabbitmq.py:110-135`

```python
message = aio_pika.Message(
    body=json.dumps(body).encode(),
    content_type="application/json",
    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,  # хранится на диске
    priority=priority,
)
await ex.publish(message, routing_key=routing_key)
```

- **`delivery_mode=PERSISTENT`** — сообщения переживают рестарт брокера (важно для `jobs`, `rec`).
- Content-type `application/json`, тело — JSON.
- `priority` опционально (0-10), эффективен только для очередей с `x-max-priority`.

### 23.5. `consume` — `rabbitmq.py:137-154`

```python
await ch.set_qos(prefetch_count=prefetch_count)
q = await ch.get_queue(queue)
await q.consume(callback)
```

- Default `prefetch_count=1` — fair dispatch (один-в-работе на consumer).
- Callback — async-функция, принимающая `AbstractIncomingMessage` (manual ack/nack).

### 23.6. Сводная картина сетевого протокола обработки задачи

Применяя факты из п. 3, 6, 23 — полный flow одной задачи:

```
1. Backend → publish to exchange "jobs" → queue "jobs.process"
   (durable, persistent message, optional priority 0-10)

2. Worker → consume from "jobs.process" (prefetch_count=1, manual ack)
   - mark_step events → publish to exchange "job.progress" (fanout)
     - SSE clients receive via their exclusive auto-delete queues

3. Worker pipeline succeeds:
   - INSERT INTO tracks (status=ready, qdrant_synced=0)
   - publish to exchange "rec" with routing_key="" → queue "rec.index"
   - ack original message in "jobs.process"

4. Rec-service → consume "rec.index" → embed/index → QDrant upsert
   - publish to exchange "rec" with routing_key="indexed" → queue "rec.indexed"

5. Backend → consume "rec.indexed" → UPDATE tracks.qdrant_synced=1

Error paths:
- Worker exception → nack(requeue=False) → DLX → "jobs.dlq"
- Rec-service exception → nack(requeue=False) → DLX → "rec.dlq"
```

### 23.7. Соответствие CLAUDE.md

- ✅ «**RabbitMQ:** 3 exchanges — `jobs` (direct), `job.progress` (fanout), `rec` (direct).» — описаны корректно, но **фактически 4 exchange'а** (плюс `dlq`).
- ✅ «DLQ: `jobs.dlq`, `rec.dlq`» — соответствует.
- ⚠️ **Несоответствие №8 (новое):** CLAUDE.md не упоминает очередь **`rec.indexed`** (durable, bound to `rec` с routing_key=`indexed`), которая используется для подтверждения rec-service → backend о завершении QDrant-индексации. Это закрывает цикл взаимодействия с рекомендательной подсистемой.
- 🆕 Не описаны:
  - `x-max-priority = 10` на `jobs.process` (priority queue).
  - `delivery_mode=PERSISTENT` для всех сообщений.
  - Exclusive auto-delete queues для SSE.
  - `aio_pika.connect_robust` — auto-reconnect.

---

## 24. PostgreSQL-репозиторий — worker-релевантная часть (`shared/karaoke_shared/repositories/pg_repository.py`)

Файл: 1121 строка (читались только worker-релевантные диапазоны). Класс `PgRepository`, конструируется с `asyncpg.Pool`. В каталоге также есть `qdrant_repository.py` (246 строк) — для рекомендаций, вне ВКР.

### 24.1. Базис

- **Backend:** `asyncpg.Pool` (async-нативный PostgreSQL).
- Все методы — `async`, используют `pool.execute`, `pool.fetch`, `pool.fetchrow`.
- Сериализация `data`/`result` JSONB полей через `json.dumps` / `json.loads`.
- Время — через хелперы `_now_dt()` / `_to_dt()` / `_ts()` (даты как `datetime` для asyncpg).

### 24.2. Tracks — `create_track`, `get_track`, `update_track` (`pg_repository.py:86-174`)

**`create_track(TrackCreate) -> Track`** — `pg_repository.py:86-123`:

INSERT с 21 параметром:
```
id, artist, title, duration_sec, instrumental_key,
lyrics_text, lyrics_source, syllable_timings (JSONB!), language, source,
status, error_message, play_count, qdrant_synced,
popularity_category, chart_count, chart_last_seen,
catalog_cluster_id, rec_cluster_id, created_at, updated_at
```

- `syllable_timings` сериализуется как JSON: `[st.model_dump() for st in data.syllable_timings]`.
- `error_message` всегда `None` при создании.
- После INSERT вызывается `get_track(data.id)`, если возвращает `None` → `RuntimeError`.

**Воркер вызывает `create_track` в `gpu_pipeline.py:266` на финализации** с полями:
```
artist, title, source="user_upload", instrumental_key, lyrics_text,
lyrics_source=lyrics_result.source_note, syllable_timings, language,
status="ready"
```
Поле `qdrant_synced` получает дефолт `0` из модели `TrackCreate`.

### 24.3. Jobs — список worker-релевантных операций

| Метод | Назначение | Где вызывается |
|---|---|---|
| `get_job(id)` | Чтение строки `job_queue` по id. | consumer.py, gpu_pipeline.py (для перечитывания после `update_job_data`) |
| `lock_job(id, worker_id)` | Pessimistic lock через CAS. | consumer.py |
| `poll_and_lock(worker_id)` | Атомарный pick+lock через FOR UPDATE SKIP LOCKED. | legacy DB-poll режим (не используется в RabbitMQ-режиме) |
| `complete_job(id, result)` | UPDATE status=completed + result JSONB. | job_service (увидим в следующем шаге) |
| `fail_job(id, error)` | UPDATE с увеличением attempts; статус FAILED при `attempts >= max_attempts`, иначе PENDING. | job_service |
| `fail_job_permanently(id, error)` | UPDATE status=FAILED безусловно. | job_service (`mark_permanently_failed`) |
| `reset_stale_running_jobs(worker_id)` | Сброс висящих RUNNING-задач этого воркера обратно в PENDING. | main.py при старте воркера |
| `find_stale_pending_jobs(older_than_seconds)` | SELECT pending-задач (с `mp3_key IS NOT NULL`) у которых `updated_at < now() - interval`. | backend `JobSweeper` |
| `mark_step(id, step, progress)` | UPDATE current_step + progress. | job_service |
| `update_job_data(id, new_data)` | **JSONB merge** через `||`. | gpu_pipeline.py |
| `set_job_track_id(id, track_id)` | UPDATE track_id после создания трека. | gpu_pipeline.py:282 |

### 24.4. `lock_job(id, worker_id) -> bool` — `pg_repository.py:936-947`

```sql
UPDATE job_queue
SET status = 'running', locked_by = $worker_id,
    locked_at = NOW(), updated_at = NOW()
WHERE id = $id AND status = 'pending'
```

- **Атомарная CAS-операция:** условие `status = 'pending'` в WHERE гарантирует, что только один воркер сможет залочить задачу.
- Возврат — `result.endswith("1")` (asyncpg возвращает строку вида `"UPDATE 1"` или `"UPDATE 0"`). Если 0 — кто-то другой уже залочил, lock_job возвращает `False`.
- Это **второй уровень защиты** от дубликатов после `prefetch_count=1` в RabbitMQ (см. п. 3.5). Даже если по какой-то причине одно сообщение придёт двум воркерам, БД-уровень не даст обоим начать обработку.

### 24.5. `poll_and_lock(worker_id) -> Job | None` — `pg_repository.py:898-921`

Legacy DB-poll режим (до миграции на RabbitMQ — см. шапку consumer.py: «replaces DB-polling JobPoller»):

```sql
UPDATE job_queue
SET status = 'running', locked_by = $worker_id, ...
WHERE id = (
    SELECT id FROM job_queue
    WHERE status = 'pending'
    ORDER BY priority DESC, created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *
```

- **`FOR UPDATE SKIP LOCKED`** — корректный multi-worker concurrency pattern для polling.
- `ORDER BY priority DESC, created_at ASC` — учёт приоритета (соответствует `x-max-priority=10` в RabbitMQ — см. п. 23.3).
- В RabbitMQ-режиме **не вызывается воркером**, но метод остался в репозитории как fallback.

### 24.6. Retry-логика — `fail_job` vs DLQ

`fail_job(id, error)` (`pg_repository.py:960-981`) реализует **БД-уровневый retry**:
- Читает текущие `attempts`, `max_attempts`.
- `attempts += 1`.
- Если `attempts < max_attempts` → `status = PENDING` (готово к повтору).
- Иначе → `status = FAILED`.
- В обоих случаях `locked_by = NULL`, `locked_at = NULL`.

**Однако в реальном воркере этот механизм не сработает в полной мере:**

- В `consumer.py:101-103` на исключении делается `message.nack(requeue=False)` — то есть сообщение **сразу уходит в DLQ** через RabbitMQ DLX (см. п. 23.2).
- `pipeline.process` бросает исключение → `job_service.mark_permanently_failed` → `fail_job_permanently` (status=FAILED безусловно), **а не `fail_job`**.
- Поэтому **`fail_job` с retry-механикой через RabbitMQ-канал фактически не запускается**. Поле `max_attempts` (default 3, см. `_job_from_row`) в новой архитектуре «висит» — оно может быть полезно для случаев, когда backend сам перепубликует сообщение из DLQ обратно в `jobs.process`, но автоматического retry воркером не происходит.
- Это потенциальная находка: «retry-counter в БД и DLX-routing — две независимые системы обработки отказов, и в текущей конфигурации работает только DLX».

### 24.7. `reset_stale_running_jobs(worker_id) -> int` — `pg_repository.py:995-1010`

```sql
UPDATE job_queue
SET status = 'pending', locked_by = NULL, locked_at = NULL, updated_at = NOW()
WHERE status = 'running' AND locked_by = $worker_id
  AND attempts < max_attempts
```

- Сбрасывает только задачи **этого воркера** (по `locked_by = worker_id`).
- Дополнительная защита: `attempts < max_attempts` — не возвращает в pending многократно отказавшие.
- Возврат — число восстановленных задач (парсится из `"UPDATE N"`).
- Вызывается в `worker/app/main.py:219-221` при старте воркера. Это часть **graceful recovery после рестарта**: задачи, которые остались в RUNNING после падения воркера, возвращаются в pending.
- ⚠️ **Эффект ограничен:** требуется, чтобы `worker_id` был стабильным между рестартами. В коде `worker_id = f"{socket.gethostname()}-{os.getpid()}"` — при рестарте PID меняется, поэтому **этот метод сбросит задачи только если запустился тот же контейнер**. После полного рестарта контейнера висящие задачи останутся в RUNNING до тех пор, пока кто-то не запустит cleanup вручную.

### 24.8. `update_job_data` — JSONB merge — `pg_repository.py:1032-1042`

```sql
UPDATE job_queue
SET data = COALESCE(data, '{}'::jsonb) || $1::jsonb,
    updated_at = $2
WHERE id = $3
```

- **PostgreSQL JSONB merge через `||`**: новые ключи добавляются, существующие — перезаписываются.
- В пайплайне используется накопительно:
  - В фоновой задаче `_encode_and_upload_instrumental` пишется `{"instrumental_key": ...}`.
  - После lyrics: `{"artist": ..., "title": ..., "lyrics": ..., "language": ...}`.
  - Возможно прочее (filename).
- Это позволяет частично восстановить состояние при возобновлении: read `get_job` → читай поля `data.artist`, `data.title`, `data.lyrics`, чтобы понять, на каком шаге закончилось.

### 24.9. `Job` модель — десериализация — `pg_repository.py:1051-1088`

Поля Job (видны в `_job_from_row`):
- `id`, `track_id`, `mp3_key` (может быть `None`).
- `artist_hint`, `title_hint` — заполняются backend'ом при создании задачи.
- `priority` (default 1), `status`, `attempts` (default 0), `max_attempts` (default 3).
- `locked_by`, `locked_at`.
- `data` (JSONB) — накопительное промежуточное состояние пайплайна.
- `result` (JSONB) — финальный payload (`{track_id, instrumental_key, language}`).
- `error_message`, `current_step`, `progress` (default 0).
- `created_at`, `updated_at`.

### 24.10. Соответствие CLAUDE.md

- ✅ «PostgreSQL: sessions, participants, queue_entries, **tracks (with syllable_timings JSONB)**, **job_queue (with data JSONB)**, mood_tags, catalog_clusters, artists.» — соответствует. Схему таблиц подтверждаем по используемым полям.
- ✅ «No foreign keys in PostgreSQL (denormalized by design, see ADR-03)» — из кода это явно не видно, но FK действительно нет в INSERT-ах.
- 🆕 Не упомянуто: **двойной механизм отказоустойчивости** (БД-уровневый attempts + RabbitMQ DLX), при этом БД-уровневый де-факто отключён через `nack(requeue=False)`.
- 🆕 Не упомянуто: **JSONB merge через `||`** — изящный pattern для накопительного состояния задачи.

### 24.11. Открытые вопросы по этому файлу

- Что вызывает `fail_job` vs `fail_job_permanently` — это в `JobService` (читаю следующим).
- Есть ли где-то `record_api_cost`, `get_monthly_costs` — это другая фича (cost tracking), вне пайплайна.

---

## 25. Сервисный слой — `JobService` и `ProgressPublisher` (`shared/karaoke_shared/services/`)

Файлы: `__init__.py` (пустой), `progress_publisher.py` (53 строки), `job_service.py` (81 строка). Тонкий слой между PgRepository, RabbitMQ и пайплайном.

### 25.1. `ProgressPublisher` — `progress_publisher.py`

Публикует события прогресса в exchange **`job.progress`** (fanout, см. п. 23.2) с пустым routing_key. Формат — JSON.

**3 типа событий:**

| Метод | Поля сообщения |
|---|---|
| `publish_progress(job_id, step, progress)` | `{"job_id", "status": "running", "step", "progress"}` |
| `publish_completed(job_id, track_id)` | `{"job_id", "status": "completed", "track_id", "clip_url": "/api/v1/tracks/{track_id}/stream"}` |
| `publish_error(job_id, error)` | `{"job_id", "status": "failed", "error"}` |

**Замечание:** `publish_completed` **жёстко генерирует URL для backend API** (`/api/v1/tracks/{track_id}/stream`). Это означает, что:
- shared-слой неявно знает о backend API.
- Если URL backend изменится (например, при API-versioning) — придётся править `shared/`.
- Это нарушение разделения ответственности, но упрощающее frontend (он сразу получает clip_url через SSE).

### 25.2. `JobService` — `job_service.py`

Тонкая обёртка над `PgRepository` + опциональный `ProgressPublisher`. Используется и backend'ом (для enqueue), и воркером (для poll/lock/update).

**Pattern для всех методов:** сначала запись в БД, потом (если publisher настроен) — публикация в SSE через try/except. **На сбое RabbitMQ-публикации БД-запись остаётся** — логируется warning (`progress_publish_failed` / `completed_publish_failed` / `error_publish_failed`).

### 25.3. Сводная таблица методов JobService

| Метод | PgRepository | ProgressPublisher | Где вызывается в воркере |
|---|---|---|---|
| `create_job(JobCreate)` | `repo.create_job` | — | (только backend) |
| `poll_and_lock(worker_id)` | `repo.poll_and_lock` | — | (legacy DB-poll, не runtime) |
| `mark_step(id, step, progress)` | `repo.mark_step` | `publish_progress` | runtime: 6 раз (separating ×2, transcribing ×2, searching_lyrics ×2). bootstrap: 4 раза (separating ×2, aligning ×2). |
| `mark_completed(id, result)` | `repo.complete_job` | `publish_completed(track_id)` | runtime + bootstrap (на финализации) |
| `mark_failed(id, error)` | **`repo.fail_job`** (с attempts++) | `publish_error` | runtime: **1 раз** (только при отсутствии mp3_key, `gpu_pipeline.py:91`). bootstrap: **4 раза** (на разных ошибках обработки). |
| `mark_permanently_failed(id, error)` | **`repo.fail_job_permanently`** (status=FAILED безусловно) | `publish_error` | runtime: **3 раза** (`lyrics_searcher is None`, `LyricsSearchError`, общий `except Exception`). |
| `get_job(id)` | `repo.get_job` | — | (worker, hint enrichment) |

### 25.4. Различие `mark_failed` vs `mark_permanently_failed` — где какой используется

`grep` подтвердил два разных подхода в runtime vs bootstrap:

- **Runtime пайплайн (`gpu_pipeline.py`):** `mark_failed` вызывается **только при отсутствии `mp3_key`** (потенциально race condition с backend, который ещё не дописал ключ — даём шанс на retry). Все остальные ошибки → `mark_permanently_failed` (трек не получится создать в принципе).
- **Bootstrap (`bootstrap/pipeline.py`):** `mark_failed` используется на **всех ошибках обработки** (4 места) — БД-уровневая retry-механика помогает массовому импорту справляться с сетевыми флуктуациями.

Это решение архитектурно осмысленное:
- Пользователь, загрузивший MP3 через UI, получает быстрый результат «failed» через SSE — не молчаливые повторы.
- Bulk-импорт каталога должен максимально устойчиво проглатывать треки.

### 25.5. Связь с RabbitMQ DLX (см. п. 23.2)

Полная цепочка обработки исключения в runtime:

```
gpu_pipeline.py:317 (except Exception)
  → mark_permanently_failed
    → repo.fail_job_permanently  (status=FAILED безусловно)
    → publisher.publish_error    (SSE event "failed")
  → exception пробрасывается дальше...
  
consumer.py:96-103 (try/except в _on_message)
  → ловит exception
  → message.nack(requeue=False)  (RabbitMQ DLX → jobs.dlq)
```

То есть на исключении в пайплайне **выполняются ОБА действия:**
1. БД статус → FAILED + SSE error event (через `mark_permanently_failed`).
2. Сообщение → DLQ (через `nack(requeue=False)`).

**`fail_job` с attempts++ при этом не вызывается** — то есть БД-уровневый retry-counter в runtime неактивен (см. п. 24.6).

### 25.6. Связь со статусами `JobStatus`

`publish_progress` использует строку `"running"` — это `JobStatus.RUNNING.value`. `publish_completed` — `"completed"` (`JobStatus.COMPLETED`). `publish_error` — `"failed"` (`JobStatus.FAILED`). Соответствует enum из `shared/karaoke_shared/constants.py` (см. п. 5).

### 25.7. Соответствие CLAUDE.md

- ✅ «Job progress: worker publishes to RabbitMQ → SSE endpoint consumes from fanout exchange» — соответствует.
- 🆕 Не описан pattern «БД-write всегда, RabbitMQ-publish best-effort» (try/except).
- 🆕 Не описана архитектурная разница `mark_failed` (runtime: только race-условие mp3_key) vs `mark_permanently_failed` (runtime: всё остальное) vs `mark_failed` (bootstrap: всё).
- 🆕 Не описан жёсткий URL `/api/v1/tracks/{id}/stream` в shared-слое.

---

## 26. S3-хранилище — `S3Storage` (`shared/karaoke_shared/storage/`)

Файлы: `__init__.py` (экспорт), `s3_storage.py`.

### 26.1. Backend и совместимость

- Реализация через **`aioboto3` (async-native SDK поверх `aiobotocore`)** — все network-методы — нативные `await`-вызовы без `asyncio.to_thread`-обёрток.
- `signature_version="s3v4"` через `aiobotocore.config.AioConfig`; retry-policy: `retries={"max_attempts": 5, "mode": "adaptive"}`, `connect_timeout=10`, `read_timeout=60`.
- Дополнительно создаётся **синхронный `boto3.client("s3", ...)` ТОЛЬКО для presigned URL** — `generate_presigned_url` это pure-crypto (HMAC-SHA256), не делает network call, и оставлен синхронным чтобы не ломать вызывающие сигнатуры (`backend/app/api/v1/playback.py:54`).
- Совместимо с: **AWS S3, MinIO, Yandex Object Storage** и любыми S3-совместимыми хранилищами (через `endpoint_url`).
- В воркере используется MinIO (`settings.s3_endpoint_url = "http://minio:9000"`, см. п. 2.1).

### 26.2. Конструктор, lifecycle и двойной клиент

`S3Storage(bucket, endpoint_url, access_key, secret_key, region="us-east-1", presigned_url_base=None)`.

**Lifecycle (новое в aioboto3-варианте):**

- `__init__` создаёт только синхронный presign-клиент (sync boto3). Async-клиент НЕ создаётся.
- `await storage.connect()` — открывает `aioboto3.Session().client("s3", ...)` как async-context manager и сохраняет вошедший клиент в `self._client`. Идемпотентен.
- `await storage.close()` — выходит из async-context manager. Идемпотентен.
- До вызова `connect()` любой async-метод (`upload`/`download_*`/`delete`/`exists`/`ensure_bucket`) бросает `RuntimeError("S3Storage not connected ...")`.
- В startup-функциях воркера/бэкенда/rec-service `await storage.connect()` вызывается сразу после конструктора; `await storage.close()` — в shutdown-секциях.

**Двойной клиент (purpose без изменений):**

- `_client` (aioboto3, async) — для всех network-операций. Использует `endpoint_url` (внутренний, например `http://minio:9000`).
- `_presign_client` (boto3, sync) — только для `generate_presigned_url`. Использует `endpoint_url=presigned_url_base or endpoint_url` (публичный, который видит браузер).

Зачем: presigned URL содержит **подпись, привязанную к Host-header'у**. Если backend подпишет URL для внутреннего `minio:9000`, а браузер пойдёт через nginx → подпись не совпадёт. Поэтому подпись делается для публичного endpoint, а реальный HTTP-доступ — через тот, который доступен браузеру.

### 26.3. Методы

| Метод | Async | aioboto3/boto3 операция | Where used in worker |
|---|---|---|---|
| `connect()` / `close()` | да | enter/exit `session.client("s3", ...)` async-context | startup/shutdown (worker/backend/rec-service `main.py`) |
| `upload(key, data)` | да | `await client.put_object(Bucket, Key, Body, ContentType)` | `gpu_pipeline.py:387` (instrumental MP3) |
| `download_to_file(key, local_path)` | да | `await client.download_file(Bucket, Key, local_path)` (aioboto3 патчит с aiofiles + concurrent range-get) | `gpu_pipeline.py:99` (input MP3) |
| `download(key) -> bytes` | да | `await client.get_object → async with response["Body"] as s: await s.read()` | (не вызывается воркером; используется backend'ом) |
| `delete(key)` | да | `await client.delete_object` | rec-service `indexer.py:134` (cleanup оригинала) |
| `exists(key) -> bool` | да | `await client.head_object` (ClientError → False) | (не вызывается воркером) |
| `presigned_url(key, expires_in=3600)` | **нет** (sync, local crypto через отдельный boto3-клиент) | `generate_presigned_url("get_object", ...)` | (используется backend'ом для редиректа на playback) |
| `ensure_bucket()` | да | `await client.head_bucket` → `await client.create_bucket` (idempotent) | startup helper |

### 26.4. Автоматическое определение Content-Type

В `upload` (`s3_storage.py:94-97`):
```python
content_type, _ = mimetypes.guess_type(key)
if content_type:
    extra["ContentType"] = content_type
```

То есть для `instrumentals/{job_id}.mp3` → `Content-Type: audio/mpeg` устанавливается автоматически на основе расширения. Это важно для браузерного `<audio>`-тега.

### 26.5. Где S3 используется воркером

Согласно поиску по коду:

- `download_to_file("uploads/{job_id}.mp3", local_path)` — `gpu_pipeline.py:99` (скачивание оригинала с MinIO в `/tmp`).
- `upload("instrumentals/{job_id}.mp3", bytes)` — `gpu_pipeline.py:387` (заливка результата конвертации WAV→MP3 в S3).

Все остальные операции (download bytes / delete / exists / presigned URL) — для backend'а или утилитарных скриптов.

### 26.6. Логи

- `s3_storage_initialized` (bucket, endpoint) — после конструктора.
- `s3_storage_connected` (bucket) — после успешного `await connect()`.
- `s3_storage_closed` (bucket) — после `await close()`.
- `s3_object_uploaded` (key).
- `s3_object_downloaded` (key, local_path).
- `s3_object_deleted` (key).
- `s3_bucket_created` (bucket).

### 26.7. Соответствие CLAUDE.md

- ✅ «MinIO (S3-compatible): `uploads/{job_id}.mp3` (temporary), `instrumentals/{job_id}.mp3` (permanent)» — соответствует.
- ⚠️ «S3 storage via `karaoke_shared.storage.S3Storage` (boto3-based, works with MinIO/AWS/Yandex)» — **устарело**: реализация переехала на `aioboto3` (async-native, `await connect()`/`await close()` lifecycle); `boto3` остался только для синхронного `presigned_url`.
- ✅ «Redirects audio playback to S3 presigned URLs» — соответствует (метод `presigned_url`).
- 🆕 Не описан **двойной клиент** для presigned URL (для разделения internal/external endpoint).
- 🆕 Не описан auto-`ContentType` через `mimetypes`.
- 🆕 Не описан `ensure_bucket` — idempotent helper.

### 26.8. Замечания для главы 2.4.3

1. **boto3 синхронный** — `asyncio.to_thread` обходит блокировку, но в высокой нагрузке может стать узким местом (limited thread pool). В альтернатива — `aioboto3`, но команда выбрала boto3.
2. **Нет retry-логики на уровне S3-клиента** — boto3 имеет встроенные стандартные retries (`Config(retries=...)`), но в коде это не настроено. Используется default-конфиг.
3. **Нет жизненного цикла объектов** (lifecycle policy) — `uploads/*` остаются в bucket после успешной обработки. Очистка должна быть отдельным процессом / lifecycle rule на стороне MinIO.

---

## 27. Модели данных — `Job`, `Track`, `SyllableTiming` (`shared/karaoke_shared/models/`)

Прочитаны только worker-релевантные файлы: `job.py` (76 строк), `track.py` (108 строк). Остальные модели в каталоге (`session.py`, `queue.py`, `recommendation.py`, `mood_tag.py`, `catalog_cluster.py`, `artist.py`, `play_history.py`) — для других сущностей, вне ВКР.

### 27.1. `models/job.py` — три Pydantic-модели

Шапка файла прямо документирует схему таблицы `job_queue` (`job.py:3-7`):
```
id, track_id (nullable), mp3_key, artist_hint, title_hint,
priority, status, attempts, max_attempts, locked_by, locked_at,
data (JSONB), result (JSONB), error_message, current_step, progress,
created_at, updated_at
```

**`Job`** — полная запись (зеркало строки таблицы):

| Поле | Тип | Default | Назначение |
|---|---|---|---|
| `id` | str | — | UUID |
| `track_id` | `str \| None` | None | заполняется при `set_job_track_id` после создания трека |
| `mp3_key` | `str \| None` | None | S3-ключ оригинала (для user uploads) |
| `artist_hint`, `title_hint` | `str \| None` | None | hints от backend'а или FilenameParser |
| `priority` | int | 1 | 0-10, влияет на `x-max-priority` в очереди |
| `status` | str | — | `JobStatus.{PENDING/RUNNING/COMPLETED/FAILED}` |
| `attempts`, `max_attempts` | int | 0, 3 | retry-counter (рудиментарен в runtime, см. п. 24.6) |
| `locked_by`, `locked_at` | `str \| None` | None | для CAS-lock |
| `data` | `dict \| None` | None | JSONB, накопительное промежуточное состояние |
| `result` | `dict \| None` | None | JSONB, финальный payload |
| `error_message` | `str \| None` | None | сообщение об ошибке |
| `current_step` | `str \| None` | None | имя текущего PipelineStep |
| `progress` | int | 0 | 0-100 |
| `created_at`, `updated_at` | str | — | **ISO-string**, не `datetime` |

**`JobCreate`** — для enqueue (используется backend'ом):
- Server-side defaults: `id = uuid4()`, `status = PENDING`, `attempts = 0`, `max_attempts = 3`, timestamps = `now UTC ISO`.

**`JobUpdate`** — partial-апдейт, все поля optional. В воркере **не используется напрямую** — пайплайн вызывает методы PgRepository, которые делают SQL UPDATE сами.

### 27.2. `models/track.py` — четыре Pydantic-модели

Шапка (`track.py:3-6`):
```
tracks: id, artist, title, duration_sec, instrumental_key,
lyrics_text, lyrics_source, syllable_timings (JSONB), language, source,
status, error_message, play_count, qdrant_synced, created_at, updated_at
```

**`SyllableTiming`** — атомарная единица karaoke (`track.py:19-24`):
```python
class SyllableTiming(BaseModel):
    syllable: str
    start: float
    end: float
```

Это **выходной формат CTC-aligner'а** (и `LineBreaker`'а после инжекции `\n`-маркеров). Хранится в `tracks.syllable_timings` как JSONB-массив. Прочитанные ранее места, где создаётся SyllableTiming:
- `TorchCTCAligner._to_syllable_timings` (см. п. 11.13) — `round(start/end, 3)`.
- `LineBreaker._inject_breaks` (см. п. 21.5) — модифицирует `syllable` (заменяет ведущий пробел на `\n` или добавляет `\n` префикс), не трогая `start`/`end`.

**`Track`** — полная запись (зеркало `tracks`):

| Поле | Тип | Default | Заполняется |
|---|---|---|---|
| `id` | str | — | UUID |
| `artist`, `title` | str | — | от lyrics_result |
| `duration_sec` | `int \| None` | None | (не заполняется воркером) |
| `instrumental_key` | `str \| None` | None | S3-ключ MP3 без вокала |
| `lyrics_text` | `str \| None` | None | финальный текст (после `clean_lyrics`) |
| `lyrics_source` | `str \| None` | None | имя провайдера: `genius/lrclib/lyricsovh/searxng/yandex/asr_fallback` (см. п. 15.5, 19.9) |
| `syllable_timings` | `list[SyllableTiming] \| None` | None | результат CTC + LineBreaker |
| `language` | `str \| None` | None | от Whisper |
| `source` | str | — | `TrackSource.{CATALOG, USER_UPLOAD}` |
| `status` | str | `TrackStatus.PENDING` | воркер ставит `READY` при финализации |
| `error_message` | `str \| None` | None | — |
| `play_count` | int | 0 | счётчик воспроизведений (обновляется backend'ом) |
| `qdrant_synced` | **int** | 0 | **0 или 1** (не bool); обновляется backend'ом из `rec.indexed` |
| `popularity_category` | str | `REGULAR` | для рекомендаций |
| `chart_count`, `chart_last_seen` | int, `str\|None` | 0, None | для рекомендаций |
| `catalog_cluster_id`, `rec_cluster_id` | `int \| None` | None | для рекомендаций |
| `created_at`, `updated_at` | str | — | ISO-string |

**`TrackCreate`** — для INSERT (используется воркером в `gpu_pipeline.py:266`). Server-side defaults: `id = uuid4()`, `status = PENDING` (воркер переопределяет на `"ready"`), `play_count = 0`, `qdrant_synced = 0`, timestamps.

**`TrackUpdate`** — partial-апдейт. Используется в `repo.update_track` (`pg_repository.py:134`).

### 27.3. Стилистика моделей

1. **Timestamps как строки в ISO-формате**, а не `datetime`. Pydantic-валидация — только формальная (string), но конверсия в `datetime` для asyncpg делается в репозитории (`_to_dt`/`_ts`).
2. **`qdrant_synced` как int (0/1)**, не bool. Это совместимо с PostgreSQL `smallint` и упрощает индексы.
3. **`status` как str**, не как enum-тип Pydantic. StrEnum'ы из `constants.py` используются как источник значений, но поля приняты как `str` для гибкости.
4. **Все nullable-поля имеют default `None`**, а не required. Это позволяет создавать промежуточные объекты с минимумом обязательных полей.
5. **`id` через `default_factory=lambda: str(uuid4())`** — UUID-строка.

---

## История чтения

- `worker/app/config.py` — полностью.
- `worker/app/consumer.py` — полностью.
- `worker/app/main.py` — полностью.
- `worker/common/base_pipeline.py` — полностью.
- `worker/gpu/gpu_pipeline.py` — полностью.
- `worker/gpu/uvr_separator.py` — полностью.
- `worker/gpu/back_vocal_separator.py` — полностью.
- `worker/gpu/whisper_transcriber.py` — полностью.
- `worker/gpu/torch_ctc_aligner.py` — полностью.
- `worker/common/vad_processor.py` — полностью.
- `worker/common/ctc_aligner.py` — полностью.
- `worker/common/ctc_subprocess.py` — полностью.
- `worker/common/lyrics/__init__.py` — полностью.
- `worker/common/lyrics/base_provider.py` — полностью.
- `worker/common/lyrics/fragments.py` — полностью.
- `worker/common/lyrics/filename_parser.py` — полностью.
- `worker/common/lyrics/provider_chain.py` — полностью.
- `worker/common/lyrics/matching/__init__.py` — полностью.
- `worker/common/lyrics/matching/normalizer.py` — полностью.
- `worker/common/lyrics/matching/linguistics.py` — полностью.
- `worker/common/lyrics/matching/scorer.py` — полностью.
- `worker/common/lyrics/matching/expander.py` — полностью.
- `worker/common/lyrics/matching/matcher.py` — полностью.
- `worker/common/lyrics/providers/__init__.py` — полностью (пустой).
- `worker/common/lyrics/providers/genius.py` — полностью.
- `worker/common/lyrics/providers/lrclib.py` — полностью.
- `worker/common/lyrics/providers/lyricsovh.py` — полностью.
- `worker/common/lyrics/providers/chartlyrics.py` — полностью.
- `worker/common/lyrics/providers/simpmusic.py` — полностью.
- `worker/common/lyrics_searcher.py` — полностью.
- `worker/common/lyrics_agent.py` — полностью.
- `worker/common/segment_builder.py` — полностью.
- `shared/karaoke_shared/constants.py` — полностью.
- `shared/karaoke_shared/utils/line_breaker.py` — полностью.
- `shared/karaoke_shared/utils/syllabifier.py` — полностью.
- `shared/karaoke_shared/messaging/__init__.py` — полностью.
- `shared/karaoke_shared/messaging/rabbitmq.py` — полностью.
- `shared/karaoke_shared/repositories/pg_repository.py` — worker-релевантная часть (методы Jobs + create_track + _job_from_row, ≈350 строк из 1121).
- `shared/karaoke_shared/services/__init__.py` — полностью (пустой).
- `shared/karaoke_shared/services/progress_publisher.py` — полностью.
- `shared/karaoke_shared/services/job_service.py` — полностью.
- `shared/karaoke_shared/storage/__init__.py` — полностью.
- `shared/karaoke_shared/storage/s3_storage.py` — полностью.
- `shared/karaoke_shared/models/job.py` — полностью.
- `shared/karaoke_shared/models/track.py` — полностью.
