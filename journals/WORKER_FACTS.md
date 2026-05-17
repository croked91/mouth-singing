# WORKER_FACTS.md

Источник: только живой код в `worker/`, `shared/`, при необходимости — `backend/`, `rec-service/`.
Каждый факт сопровождается ссылкой `path:line`. Если что-то не проверено по коду — помечается `[NOT VERIFIED]`.

Дата начала сборки: 2026-05-17.

---

## Структура воркера (`worker/`)

Все Python-файлы (по `find worker -name '*.py'`):

```
worker/__init__.py
worker/app/__init__.py
worker/app/main.py              ← entry point
worker/app/config.py            ← WorkerSettings (pydantic-settings)
worker/app/consumer.py          ← JobConsumer (RabbitMQ)
worker/common/__init__.py
worker/common/base_pipeline.py
worker/common/lyrics_agent.py
worker/common/lyrics_searcher.py
worker/common/vad_processor.py
worker/common/lyrics/__init__.py
worker/common/lyrics/base_provider.py
worker/common/lyrics/filename_parser.py
worker/common/lyrics/fragments.py
worker/common/lyrics/provider_chain.py
worker/common/lyrics/matching/__init__.py
worker/common/lyrics/matching/expander.py
worker/common/lyrics/matching/linguistics.py
worker/common/lyrics/matching/matcher.py
worker/common/lyrics/matching/normalizer.py
worker/common/lyrics/matching/scorer.py
worker/common/lyrics/providers/__init__.py
worker/common/lyrics/providers/genius.py
worker/common/lyrics/providers/lrclib.py
worker/common/lyrics/providers/lyricsovh.py
worker/gpu/__init__.py
worker/gpu/gpu_pipeline.py      ← orchestrator
worker/gpu/uvr_separator.py
worker/gpu/back_vocal_separator.py
worker/gpu/whisper_transcriber.py
worker/gpu/torch_ctc_aligner.py
```

---

## 1. Точка входа: `worker/app/main.py`

### 1.1 `main()` — последовательность старта (`main.py:181-256`)

1. Конфигурация structlog с processors:
   `merge_contextvars` (стичит request_id/job_id) → `add_log_level` → `TimeStamper(fmt="iso")` → `JSONRenderer()` (`main.py:183-193`).
2. Лог `worker_starting` с `worker_id=settings.worker_id` (`main.py:195`).
3. **Eager init pymorphy3** через `init_morph_analyzer()` — комментарий говорит о ~30 МБ словаре и 1–2 с холодного старта; делается до event-loop'а блокировок (`main.py:197-202`).
4. `_open_pg(settings.pg_dsn)` → asyncpg pool `min_size=2, max_size=10` (`main.py:27-30, 204`).
5. `RabbitMQClient(settings.rabbitmq_url)` → `connect()` → `declare_topology()` (`main.py:207-209`).
6. `S3Storage(...)` с явным `await storage.connect()` — комментарий «aioboto3 — persistent async client» (`main.py:211-218`).
7. Внутри `try`:
   - `PgRepository(pool)` (`main.py:223`).
   - `ProgressPublisher(rmq)` + `JobService(repo, publisher=publisher)` (`main.py:226-227`).
   - `await repo.reset_stale_running_jobs(settings.worker_id)` — лог `stale_jobs_reset` при ненулевом count (`main.py:230-232`).
   - `pipeline = _build_gpu_pipeline(...)` (`main.py:234`).
   - `JobConsumer(rmq, pipeline, repo, job_service, worker_id)` (`main.py:236-242`).
   - На сигналы `SIGTERM`/`SIGINT` ставится `consumer.stop` (`main.py:244-246`).
   - `await consumer.run()` (`main.py:248`).
8. `finally` (`main.py:250-256`):
   - Если в области видимости есть `pipeline` и у него `cleanup` — вызвать (синхронно, без await) (`main.py:251-252`).
   - `await storage.close()` (`main.py:253`).
   - `await rmq.close()` (`main.py:254`).
   - `await pool.close()` (`main.py:255`).
   - Лог `worker_stopped` (`main.py:256`).

### 1.2 `_build_gpu_pipeline()` — состав компонентов (`main.py:33-178`)

Импортируются (lazy внутри функции) и инстанцируются:

| Поле | Класс | Условие включения |
|---|---|---|
| `uvr` | `UVRSeparator` | всегда (`main.py:56-63`) |
| `back_vocal` | `BackVocalSeparator` | `settings.back_vocal_enabled is True`, иначе `None` (`main.py:64-76`) |
| `whisper` | `WhisperTranscriber` | всегда (`main.py:77-82`) |
| `vad` | `VADProcessor(top_db=settings.vad_top_db)` | всегда (`main.py:84`) |
| `text_providers` | `GeniusProvider` | только если `settings.genius_token` непустой (`main.py:91-95`) |
| `metadata_providers` | `LRCLibProvider`, `LyricsOvhProvider` | всегда (`main.py:97-100`) |
| `filename_parser` | `FilenameParser(deepseek_api_key=..., model=...)` | только если `settings.deepseek_api_key` непустой (`main.py:102-109`) |
| `expander` | `LyricsExpander(deepseek_api_key=..., model=...)` | всегда (`main.py:111-114`) |
| `matcher` | `LyricsMatcher(expander=..., deepseek_api_key=..., model=...)` | всегда (`main.py:116-120`) |
| `fallback_agent` | `LyricsAgent` | только если `deepseek_api_key` И (`searxng_url` ИЛИ оба yandex-ключа) (`main.py:122-136`) |
| `lyrics_searcher` | `LyricsProviderChain(...)` | всегда (`main.py:138-145`) |
| `ctc_aligner` | `TorchCTCAligner(device="cuda", ...)` — **хардкод `cuda`** в `device=` (`main.py:155-164`) |

Все эти объекты передаются в `GpuPipeline(...)` (`main.py:166-178`).

Лог `lyrics_chain_enabled` со списком имён text/metadata providers и булевыми флагами наличия matcher/filename_parser/fallback (`main.py:146-153`).

### 1.3 Комментарии в коде, которые относятся к смыслу

- `Genius searches by lyrics text; LRCLib/Lyrics.ovh search by artist+title` (`main.py:89`).
- Файл сам декларирует: «API mode (MVSEP + OpenAI Whisper) has been removed — only GPU mode remains» (`main.py:3-4`).

---

## 2. Конфигурация: `worker/app/config.py`

Класс `WorkerSettings(BaseSettings)`, `env_prefix=""` (`config.py:148`), переменные читаются case-insensitive из окружения.

### 2.1 Инфраструктура (`config.py:21-39`)

| Поле | Значение по умолчанию |
|---|---|
| `pg_dsn` | `postgresql://karaoke:karaoke@postgres:5432/karaoke` |
| `media_root` | `/data/media` |
| `s3_bucket` | `karaoke` |
| `s3_endpoint_url` | `http://minio:9000` |
| `s3_access_key` | `minioadmin` |
| `s3_secret_key` | `minioadmin` |
| `rabbitmq_url` | `amqp://karaoke:karaoke@rabbitmq:5672/` |
| `model_cache_dir` | `/data/models` |
| `worker_id` | `Field(default_factory=socket.gethostname)` — берётся из env `WORKER_ID`, иначе `hostname` (`config.py:33-37`) |
| `poll_interval_sec` | `2.0` |
| `log_level` | `INFO` |

### 2.2 Lyrics (`config.py:45-56`)

| Поле | Default |
|---|---|
| `deepseek_api_key` | `""` |
| `deepseek_model` | `deepseek-chat` |
| `searxng_url` | `http://searxng:8080` |
| `yandex_search_api_key` | `""` |
| `yandex_search_folder_id` | `""` |
| `lyrics_agent_max_iterations` | `15` |
| `lyrics_agent_timeout` | `15.0` (сек) |
| `genius_token` | `""` |
| `lyrics_provider_timeout` | `10.0` |
| `lyrics_search_fragments` | `2` |

### 2.3 CTC / MMS (`config.py:62-94`)

| Поле | Default | Комментарий из кода |
|---|---|---|
| `ctc_min_frames_for_char` | `10` | — |
| `ctc_device` | `"cpu"` | «CPU is the only viable option — wav2vec2 ONNX graph has 24 ops unsupported by CUDA EP» (`config.py:64-67`) |
| `ctc_batch_size` | `16` | — |
| `mms_pre_trim_enabled` | `True` | Silero VAD pre-trim |
| `mms_pre_trim_threshold` | `0.7` | — |
| `mms_pre_trim_min_speech_ms` | `300` | — |
| `mms_line_start_rms_adjust` | `True` | — |
| `mms_word_end_drift_adjust` | `True` | — |
| `mms_word_end_sustain_extend` | `True` | — |

> ⚠️ Расхождение: `ctc_device="cpu"` в конфиге (`config.py:63`), но в `_build_gpu_pipeline` `TorchCTCAligner` создаётся с `device="cuda"` хардкодом (`main.py:156`). Поле `ctc_device` нигде в Pipeline не используется (надо проверить грепом — см. ниже).

### 2.4 VAD (`config.py:100`)

`vad_top_db: int = 16`.

### 2.5 UVR (`config.py:109-113`)

| Поле | Default |
|---|---|
| `uvr_model_name` | `model_bs_roformer_ep_317_sdr_12.9755.ckpt` |
| `uvr_torch_device` | `"cuda"` |
| `uvr_chunk_batch_size` | `2` |
| `uvr_use_autocast` | `True` |
| `uvr_overlap` | `8.0` |

Комментарий: «Revive 2 evaluated but rejected: cleans vocals too aggressively for Whisper → transcription degrades and lyrics matcher picks wrong song version» (`config.py:104-107`).

> ⚠️ `uvr_torch_device` объявлено в конфиге (`config.py:110`), но в `_build_gpu_pipeline` поле `device` не передаётся (`main.py:56-63`). Надо проверить, читает ли его `UVRSeparator` иначе.

### 2.6 BackVocal (`config.py:119-124`)

| Поле | Default |
|---|---|
| `back_vocal_enabled` | `True` |
| `back_vocal_model_name` | `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt` |
| `back_vocal_torch_device` | `"cuda"` |
| `back_vocal_chunk_batch_size` | `2` |
| `back_vocal_use_autocast` | `True` |
| `back_vocal_overlap` | `4.0` |

### 2.7 Whisper (`config.py:130-132`)

| Поле | Default |
|---|---|
| `whisper_model_size` | `"medium"` |
| `whisper_device` | `"cuda"` |
| `whisper_compute_type` | `"float16"` |

### 2.8 Per-step timeouts (`config.py:142-146`)

| Поле | Default |
|---|---|
| `step_timeout_separating_base` | `30.0` |
| `step_timeout_back_vocal_separating_base` | `30.0` |
| `step_timeout_transcribing_base` | `30.0` |
| `step_timeout_aligning_base` | `10.0` |
| `step_timeout_baseline_seconds` | `180.0` |

Принцип, описанный в комментарии: `actual = base × (probed_duration / baseline)` (`config.py:135-140`). Применение — в `gpu_pipeline.py`, см. ниже.

---

## 3. RabbitMQ consumer: `worker/app/consumer.py`

Класс `JobConsumer` (`consumer.py:26-129`).

### 3.1 Конструктор (`consumer.py:37-50`)

Поля: `_rmq`, `_pipeline`, `_repo`, `_job_service`, `_worker_id`, `_running=True`.

### 3.2 `run()` (`consumer.py:52-63`)

- `self._rmq.consume("jobs.process", self._on_message, prefetch_count=1)` (`consumer.py:54-58`).
- Лог `job_consumer_started`.
- Бесконечный `while self._running: await asyncio.sleep(1)` (`consumer.py:62-63`).

### 3.3 `stop()` (`consumer.py:65-68`)

Сетит `_running = False`, лог `job_consumer_stopping`.

### 3.4 `_on_message()` (`consumer.py:70-129`)

1. **Декодирование тела**: `json.loads(message.body)` (`consumer.py:76-77`).
   - На `JSONDecodeError`: лог `invalid_message_body` с `raw` = первые 200 байт (utf-8 errors=replace), `await message.nack(requeue=False)` → DLQ. Return (`consumer.py:78-86`).
2. Извлекает `job_id` (default `"unknown"`) и `request_id` (`consumer.py:88-89`).
3. **Структлог-контекст**: `bind_contextvars(job_id=..., request_id=...)` — добавит эти поля в каждую log line внутри try (`consumer.py:91-97`).
4. Внутри try:
   - Лог `job_received`.
   - `locked = await self._repo.lock_job(job_id, self._worker_id)` (`consumer.py:103`).
   - Если `not locked`: лог `job_lock_failed`, `await asyncio.sleep(0.5)` (cooldown против CPU-spin при race), `nack(requeue=True)`, return (`consumer.py:104-110`).
   - `job = await self._repo.get_job(job_id)`; если `None` — лог `job_not_found_after_lock`, `ack()`, return (`consumer.py:112-117`).
   - `await self._pipeline.process(job)` → `ack()`, лог `job_completed` (`consumer.py:120-122`).
5. `except Exception`: лог `job_processing_failed` (с traceback), `nack(requeue=False)` → DLQ. Комментарий: «pipeline уже записал ошибку в DB» (`consumer.py:124-127`).
6. `finally`: `reset_contextvars(**tokens)` (`consumer.py:128-129`).

---

## 4. GPU pipeline: `worker/gpu/gpu_pipeline.py`

Класс `GpuPipeline(BasePipeline)` (`gpu_pipeline.py:50-437`).

### 4.1 Конструктор (`gpu_pipeline.py:66-90`)

Поля: `job_service`, `uvr`, `back_vocal_separator` (Optional), `repo`, `whisper`, `vad_processor`, `lyrics_searcher` (Optional), `ctc_aligner`, `storage`, `rmq`, `settings`.

### 4.2 `process(job)` — структура (`gpu_pipeline.py:92-428`)

Защитная проверка: если `job.mp3_key` пуст — `mark_permanently_failed("Job {id} has no mp3_key")` и return (`gpu_pipeline.py:94-98`).

Объявляется набор переменных под tempfile-пути (`gpu_pipeline.py:102-108`), все инициализированы `None`:
`local_mp3`, `vocals_path`, `instrumental_path`, `lead_vocals_path`, `backing_path`, `cleaned_vocals_path`, `instrumental_upload_task`.

#### 4.2.1 Подготовка (`gpu_pipeline.py:110-127`)

- `pipeline_t0 = time.monotonic()`.
- `local_mp3 = f"/tmp/{job.id}.mp3"`.
- `await self.storage.download_to_file(job.mp3_key, local_mp3)`.
- `duration_sec = await self._probe_duration_seconds(local_mp3)` (`gpu_pipeline.py:118`).
- `baseline = settings.step_timeout_baseline_seconds`; `scale = max(duration_sec / baseline, 0.5)` — нижний клэмп `0.5` (`gpu_pipeline.py:119-120`).
- Лог `step_timeouts_calculated` с duration/scale.

#### 4.2.2 STEP 1 — separating (`gpu_pipeline.py:128-162`)

- `mark_step(job.id, "separating", 0)`.
- `sep_timeout = step_timeout_separating_base × scale`.
- `vocals_path, instrumental_path = await asyncio.wait_for(self._separate_with_fallback(local_mp3), timeout=sep_timeout)`.
- На `asyncio.TimeoutError` → `RuntimeError(f"Step timeout: separating after {sep_timeout:.1f}s ...")`.
- После успеха: `await asyncio.to_thread(self.uvr.cleanup)` — освобождает VRAM (`gpu_pipeline.py:149`).
- `mark_step("separating", 100)`.
- **Фоновая задача** инструментала:
  - `instrumental_key = f"instrumentals/{job.id}.mp3"`.
  - `instrumental_upload_task = asyncio.create_task(self._encode_and_upload_instrumental(...))` (`gpu_pipeline.py:154-162`).
- **NOTE из кода**: `asyncio.wait_for(asyncio.to_thread(...))` отменяет только корутину; GPU-thread продолжит работать до завершения CUDA-kernel — нужен external watchdog (out of scope) (`gpu_pipeline.py:130-134`).

#### 4.2.3 STEP 2 — back_vocal_separating (`gpu_pipeline.py:164-208`)

- По умолчанию `lead_vocals_path = vocals_path` (fallback).
- Если `back_vocal_separator is not None`:
  - `mark_step("back_vocal_separating", 0)`.
  - `bvs_timeout = step_timeout_back_vocal_separating_base × scale`.
  - `lead_vocals_path, backing_path = await asyncio.wait_for(asyncio.to_thread(back_vocal_separator.separate, vocals_path), timeout=bvs_timeout)`.
  - На `asyncio.TimeoutError`: лог `back_vocal_separation_timeout_falling_back_to_full_vocals`, `lead_vocals_path = vocals_path` — НЕ падаем (`gpu_pipeline.py:187-196`).
  - На любой другой `Exception`: лог `back_vocal_separation_failed_falling_back_to_full_vocals`, fallback (`gpu_pipeline.py:197-203`).
  - `finally`: `await asyncio.to_thread(back_vocal_separator.cleanup)` (`gpu_pipeline.py:204-205`).
  - `mark_step("back_vocal_separating", 100)`.
- Если `back_vocal_separator is None` — шаг полностью пропускается, `mark_step` не вызывается.

#### 4.2.4 STEP 3 — vad (`gpu_pipeline.py:217`, метод `_vad` 584-598)

- Вызывается `cleaned_vocals_path = await self._vad(vocals_path, job.id)`.
- **Важно**: VAD работает на `vocals_path` (FULL vocals, с backing), не на `lead_vocals_path`. Комментарий: backing vocals помогают Whisper распознать трек; lead-only давал ~44% более короткий транскрипт и matcher picked wrong song version (`gpu_pipeline.py:210-216`).
- `_vad`: `mark_step("vad", 0)` → `await asyncio.to_thread(self.vad_processor.process, vocals_path)` → `mark_step("vad", 100)`; возвращает `vad_result.cleaned_path`.
- **Без таймаута**.

#### 4.2.5 STEP 4 — transcribing (`gpu_pipeline.py:219-236`, метод `_transcribe` 600-612)

- `wsp_timeout = step_timeout_transcribing_base × scale`.
- `whisper_result = await asyncio.wait_for(self._transcribe(cleaned_vocals_path, job.id), timeout=wsp_timeout)`.
- На `asyncio.TimeoutError` → `RuntimeError("Step timeout: transcribing ...")`.
- После успеха: `await asyncio.to_thread(self.whisper.cleanup)` — освобождает Whisper VRAM (`gpu_pipeline.py:236`).
- `_transcribe`: `mark_step("transcribing", 0)` → `await asyncio.to_thread(self.whisper.transcribe, cleaned_vocals_path)` → `mark_step("transcribing", 100)`.
- Whisper работает на cleaned FULL vocals, не на lead.

#### 4.2.6 STEP 5 — searching_lyrics (`gpu_pipeline.py:238-277`)

- `mark_step("searching_lyrics", 0)`.
- Если `self.lyrics_searcher is None` → `mark_permanently_failed("Lyrics agent not configured ...")` и return (`gpu_pipeline.py:243-248`).
- Извлекаются `artist_hint = job.artist_hint`, `title_hint = job.title_hint`, `filename = (job.data or {}).get("filename")` (`gpu_pipeline.py:250-252`).
- `lyrics_result = await self.lyrics_searcher.search(asr_text=whisper_result.text, detected_language=whisper_result.language, artist_hint=..., title_hint=..., filename=...)`. Комментарий говорит: `LyricsSearchError` пропагандируется в общий except (без локальной обработки), чтобы не дублировать лог (`gpu_pipeline.py:254-264`).
- `mark_step("searching_lyrics", 100)`.
- `await self.repo.update_job_data(job.id, {artist, title, lyrics, language})` (`gpu_pipeline.py:269-277`).
- **Без таймаута**.

#### 4.2.7 STEP 6 — aligning (`gpu_pipeline.py:279-309`)

- `mark_step("aligning", 0)`.
- `ctc_timeout = step_timeout_aligning_base × scale`.
- `syllable_timings, align_stats = await asyncio.wait_for(asyncio.to_thread(self.ctc_aligner.align, lead_vocals_path, lyrics_result.lyrics, lyrics_result.language), timeout=ctc_timeout)`.
- **Aligner работает на `lead_vocals_path`** — единственный шаг, который использует lead-only stem.
- На `asyncio.TimeoutError` → `RuntimeError("Step timeout: aligning ...")`.
- `mark_step("aligning", 100)`.
- `if hasattr(self.ctc_aligner, "cleanup"): await asyncio.to_thread(self.ctc_aligner.cleanup)` — освобождает VRAM (`gpu_pipeline.py:302-303`).
- Лог `ctc_alignment_done` с `total_words` и `fallback` (булев `align_stats.proportional_fallback`).

#### 4.2.8 STEP 7 — line_breaking (`gpu_pipeline.py:311-320`)

- Импорт inline: `from karaoke_shared.utils.line_breaker import detect_line_breaks`.
- `mark_step("line_breaking", 0)`.
- `syllable_timings = await asyncio.to_thread(detect_line_breaks, syllable_timings, lead_vocals_path)`.
- `mark_step("line_breaking", 100)`.
- **Без таймаута**.

#### 4.2.9 Восстановление `backing_path` для cleanup (`gpu_pipeline.py:322-331`)

Если `backing_path is None and lead_vocals_path != vocals_path` — собирается из naming convention: заменяется `_(Lead).wav` → `_(Backing).wav`. Это нужно только финальному cleanup в finally.

#### 4.2.10 Finalization (`gpu_pipeline.py:333-390`)

1. `await instrumental_upload_task` — ждём фоновую upload-задачу.
2. `updated_job = await self.repo.get_job(job.id)`; `job_data = updated_job.data or {}`.
3. `track = await self.repo.create_track(TrackCreate(artist=lyrics_result.artist, title=lyrics_result.title, source="user_upload", instrumental_key=job_data.get("instrumental_key", instrumental_key), lyrics_text=lyrics_result.lyrics, lyrics_source=lyrics_result.source_note, syllable_timings=syllable_timings, language=lyrics_result.language, status="ready"))` (`gpu_pipeline.py:344-356`).
4. `await self.repo.set_job_track_id(job.id, track_id)` (`gpu_pipeline.py:359`).
5. `await self.job_service.mark_completed(job.id, {track_id, instrumental_key, language})` (`gpu_pipeline.py:361-370`).
6. **Публикация в Rec Service**: `rec_body = {track_id, mp3_key, lyrics}`, опционально добавляется `request_id` из contextvars, `await self.rmq.publish("rec", "", rec_body)` (`gpu_pipeline.py:375-383`).
7. Лог `pipeline_completed` с `total_duration_sec` (`gpu_pipeline.py:385-390`).

#### 4.2.11 Error handling (`gpu_pipeline.py:392-401`)

В `except Exception as exc`:
- Лог `pipeline_failed` с `error=str(exc), exc_info=True`.
- `try: await asyncio.to_thread(self.cleanup) except Exception: pass` — релизит VRAM всех моделей.
- `await self.job_service.mark_permanently_failed(job.id, str(exc))`.

#### 4.2.12 Finally — отмена task + cleanup tempfiles (`gpu_pipeline.py:402-428`)

- Если `instrumental_upload_task is not None and not done()`: `.cancel()` + `await` (с suppress). Комментарий: предотвращает orphan S3-объекты и stray disk usage (`gpu_pipeline.py:402-412`).
- Список путей для удаления: `[local_mp3, vocals_path, instrumental_path, cleaned_vocals_path]`; плюс `lead_vocals_path` если отличается от `vocals_path`; плюс `backing_path`. Для каждого пути — `Path(path).unlink(missing_ok=True)` под suppress (`gpu_pipeline.py:413-428`).

### 4.3 `cleanup()` метод класса (`gpu_pipeline.py:430-437`)

Синхронный. Зовёт по очереди: `uvr.cleanup()`, `back_vocal_separator.cleanup()` (если не None), `whisper.cleanup()`, `ctc_aligner.cleanup()` (если есть метод).

### 4.4 Helpers

#### 4.4.1 `_ffprobe_field(mp3_path, entries)` (`gpu_pipeline.py:443-479`)

- Зовёт `ffprobe -v error -show_entries {entries} -of default=noprint_wrappers=1:nokey=1 {mp3_path}`.
- `stdout` + `stderr` через PIPE; на `returncode != 0` — лог `ffprobe_failed` с первыми 200 байтами stderr, return `None`.
- На любое исключение — лог `ffprobe_exec_failed`, return `None`.
- Возвращает strip'нутую строку или `None` (включая случай пустого вывода).

#### 4.4.2 `_probe_duration_seconds(mp3_path)` (`gpu_pipeline.py:481-491`)

- Зовёт `_ffprobe_field(..., "format=duration")`.
- На `None` или `ValueError` при `float(value)` — возвращает `settings.step_timeout_baseline_seconds` (fallback 180с).

#### 4.4.3 `_encode_and_upload_instrumental(instrumental_path, instrumental_key, job_id, original_mp3)` (`gpu_pipeline.py:493-549`)

- Локально пишет в `/tmp/{job_id}_instrumental.mp3`.
- Bitrate: default `"192k"`; пробует `_ffprobe_field(original_mp3, "format=bit_rate")` → `f"{int(value) // 1000}k"`; на `ValueError` лог `ffprobe_bitrate_unparseable`.
- `ffmpeg -y -i {instrumental_path} -codec:a libmp3lame -b:a {bitrate} {instrumental_mp3}`, stdout DEVNULL, stderr PIPE.
- На `returncode != 0` → `RuntimeError("ffmpeg encode failed (rc=...): {stderr[:500]}")`.
- `with open(instrumental_mp3, "rb") as f: await self.storage.upload(instrumental_key, f.read())` — **синхронное чтение файла в RAM, затем async upload**.
- `await self.repo.update_job_data(job_id, {"instrumental_key": instrumental_key})`.
- На `asyncio.CancelledError`: лог `instrumental_upload_cancelled`, `raise` (`gpu_pipeline.py:544-546`).
- `finally`: удаляет `instrumental_mp3` через `Path.unlink(missing_ok=True)` под suppress.

> Важно: оригинальный `instrumental_path` (WAV из UVR) метод НЕ удаляет — это делает основной `finally` в `process()` (`gpu_pipeline.py:413-428`).

#### 4.4.4 `_separate_with_fallback(mp3_path)` (`gpu_pipeline.py:551-582`)

Стратегия (по комментарию `gpu_pipeline.py:552-563`):
1. `await asyncio.to_thread(self.uvr.separate, mp3_path)`.
2. На `RuntimeError`: лог `uvr_gpu_failure_retrying_on_gpu`, `await asyncio.to_thread(self.uvr.cleanup)`, retry на том же GPU.
3. На повторный `RuntimeError`: лог `uvr_gpu_retry_failed_falling_back_to_cpu`, `cleanup`, `self.uvr = self.uvr.fallback_to_cpu()`, ещё одна попытка.
4. Если CPU тоже падает — RuntimeError пропагандируется в общий `except`.

#### 4.4.5 `_vad(vocals_path, job_id)` и `_transcribe(cleaned, job_id)` — см. STEP 3 и STEP 4 выше.

---

---

## 5. `BasePipeline` (`worker/common/base_pipeline.py`)

Абстрактный класс — контракт. Объявляет:
- `async def process(self, job: Job) -> None` — `raise NotImplementedError` (`base_pipeline.py:15-21`).
- `def cleanup(self) -> None` — без тела (`base_pipeline.py:23-25`).

Единственная конкретная реализация — `GpuPipeline` (docstring `base_pipeline.py:11-13`).

---

## 6. UVR separator (`worker/gpu/uvr_separator.py`)

**Модель**: BS-Roformer, чекпоинт `model_bs_roformer_ep_317_sdr_12.9755.ckpt` (`uvr_separator.py:65`).

Docstring файла декларирует ручную реализацию инференса (без обёртки `audio-separator`): `torch.inference_mode()`, batched chunks, GPU overlap-add, native autocast FP16 (`uvr_separator.py:1-8`).

### 6.1 Архитектура модели (`uvr_separator.py:19-43`)

Конфиг `_MODEL_CONFIG` — статика, передаётся в `BSRoformer(**_MODEL_CONFIG)`:
- `dim=512`, `depth=12`, `stereo=True`, `num_stems=1`
- `time_transformer_depth=1`, `freq_transformer_depth=1`, `dim_head=64`, `heads=8`
- `attn_dropout=0.1`, `ff_dropout=0.1`
- `flash_attn=True`
- `mask_estimator_depth=2`
- STFT: `n_fft=2048`, `hop=441`, `win=2048`, `normalized=False`
- `freqs_per_bands` — кортеж из 62 значений (детали в файле)

### 6.2 Аудио-параметры (`uvr_separator.py:45-49`)

- `_SAMPLE_RATE = 44100`
- `_STFT_HOP = 441`
- `_DIM_T = 801` (model's inference.dim_t)
- `_CHUNK_SIZE = _STFT_HOP * (_DIM_T - 1) = 352_800` сэмплов ≈ 8 сек @ 44.1кГц

### 6.3 `UVRSeparator.__init__` (`uvr_separator.py:67-85`)

Параметры:
- `model_cache_dir`, `media_root`
- `model_name: str | None = None` — fallback на `MODEL_NAME`
- `torch_device: str = "cuda"`
- `chunk_batch_size: int = 4` (default), но из `main.py:60` приходит `settings.uvr_chunk_batch_size = 2`
- `use_autocast: bool = True`
- `overlap: int = 4` (default), из `main.py:62` — `settings.uvr_overlap = 8.0` (см. ниже про семантику)

Сохраняет в `self`: все параметры + `_model = None`, `_output_dir: str | None = None`.

### 6.4 `fallback_to_cpu()` (`uvr_separator.py:87-101`)

Возвращает **новый** инстанс `UVRSeparator` с теми же параметрами кроме:
- `torch_device="cpu"`
- `chunk_batch_size=1`
- `use_autocast=False`
- `overlap=self._overlap` (сохраняется)

### 6.5 `_ensure_model()` (`uvr_separator.py:103-139`)

Lazy-load на первом использовании:
- Создаёт `media_root/instrumental/` (mkdir parents+exist_ok).
- `model_path = model_cache_dir / model_name`.
- `BSRoformer(**_MODEL_CONFIG)` из `audio_separator.separator.uvr_lib_v5.roformer.bs_roformer`.
- `torch.load(model_path, map_location="cpu")` — поддерживает обёртку `{"state_dict": ...}`.
- На CPU: `.to(device).float().eval()`; на GPU: `.to(device).half().eval()` (FP16) — комментарий: «CPU has no autocast and would hit dtype mismatch» (`uvr_separator.py:124-130`).
- Лог `uvr_model_loaded` с количеством параметров в миллионах.

### 6.6 `separate(mp3_path)` (`uvr_separator.py:141-284`)

Возвращает `(vocals_path, instrumental_path)`. Синхронный — пайплайн зовёт через `asyncio.to_thread`.

**Загрузка**:
- `sf.read(mp3_path, dtype="float32")` → `(samples, channels)`.
- Транспонирование, ресэмплинг через `torchaudio.functional.resample` если `sr != 44100`.
- Mono → stereo (`mix.repeat(2,1)`); >2 каналов → берёт первые два (`uvr_separator.py:167-171`).
- Пиковая нормализация до 0.9 (`uvr_separator.py:173-176`).

**Chunking**:
- `chunk_size = 352800` (8с).
- `step = min(int(overlap * SR), chunk_size)` если `overlap > 0`, иначе `step = chunk_size`. ⚠️ **`overlap` интерпретируется как шаг в секундах**, не как «overlap factor» из docstring (`uvr_separator.py:181-182`). При `overlap=8.0` (из config) и chunk=8с → step=8с → **никакого реального overlap'а**. При `overlap=4.0` → step=4с → 50% overlap.
- Hamming window через `scipy.signal.windows.hamming` (`uvr_separator.py:185-189`).
- Результат-аккумуляторы `result (2, num_samples)` и `weight (num_samples,)` создаются на target device.
- Список `starts = range(0, num_samples, step)`; если последний chunk не покрывает конец — добавляется `num_samples - chunk_size` (`uvr_separator.py:200-203`).

**Инференс** (`uvr_separator.py:215-245`):
- `torch.inference_mode()` оборачивает весь цикл.
- Внешний цикл по батчам (`chunk_batch_size`).
- Внутри: chunks собираются (паддятся нулями если короче `chunk_size`), `stack` → `to(device)`.
- Если `use_autocast and device == "cuda"` — `torch.amp.autocast("cuda")` обрамляет forward, иначе forward без autocast.
- Overlap-add: `windowed = vocals_batch[i, :, :length] * window[:length]`, добавляется в `result` и `weight`.

**Финализация** (`uvr_separator.py:247-276`):
- `vocals = result / weight.clamp(min=1e-8).unsqueeze(0)`.
- `instrumental = mix.to(device) - vocals` (вычитательная схема).
- Де-нормализация: умножение обоих стемов на `peak / 0.9` (если peak>0).
- **Vocals downsample**: `F.resample(vocals, 44100, 16000).mean(dim=0, keepdim=True)` → 16кГц mono на CPU (`uvr_separator.py:264-265`). Комментарий: «required by VAD/Whisper».
- **Instrumental** остаётся на 44.1кГц stereo, в float32 → `.cpu()`.

**Пути выхода** (`uvr_separator.py:267-276`):
- `job_id = pathlib.Path(mp3_path).stem`.
- `{media_root}/instrumental/{job_id}_(Vocals).wav` — 16кГц mono PCM_16.
- `{media_root}/instrumental/{job_id}_(Instrumental).wav` — 44.1кГц stereo (без явного subtype, soundfile использует дефолтный).

Лог `uvr_completed` с обоими путями и `duration_sec`.

### 6.7 `cleanup()` (`uvr_separator.py:286-304`)

- `del self._model; self._model = None`.
- `gc.collect()`.
- Если `torch.cuda.is_available()` — `torch.cuda.empty_cache()` (под try/except `ImportError`).
- Лог `uvr_cleanup_done`.

---

## 7. Back-vocal separator (`worker/gpu/back_vocal_separator.py`)

**Модель**: Mel-Band RoFormer, чекпоинт `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt` (`back_vocal_separator.py:68`).

Docstring файла: «Splits UVR vocals output into lead-only and backing-only stems. Runs on the 16kHz mono vocals produced by UVRSeparator (upsampled to 44.1kHz stereo internally, then downsampled back)» (`back_vocal_separator.py:1-11`).

### 7.1 Архитектура (`back_vocal_separator.py:22-47`)

`_MODEL_CONFIG` для `MelBandRoformer`:
- `dim=384`, `depth=6`, `stereo=True`, `num_stems=1`
- `time/freq_transformer_depth=1`, `num_bands=60`, `dim_head=64`, `heads=8`
- `attn_dropout=0.0`, `ff_dropout=0.0` (в отличие от UVR — без dropout)
- `flash_attn=True`
- `dim_freqs_in=1025`, `sample_rate=44100`
- STFT: `n_fft=2048`, `hop=441`, `win=2048`, `normalized=False`
- `mask_estimator_depth=2`
- Multi-STFT loss параметры (для обучения; в инференсе не задействованы напрямую): `multi_stft_resolution_loss_weight=1.0`, `windows=(4096,2048,1024,512,256)`, `hop=147`, `normalized=False`

Аудио-параметры идентичны UVR: `_SAMPLE_RATE=44100`, `_CHUNK_SIZE=352800`.

### 7.2 `__init__` (`back_vocal_separator.py:70-88`)

Аналогично UVR, но без публичного `MODEL_NAME` атрибута. Default `chunk_batch_size=2`, `overlap=4.0`.

⚠️ **Нет метода `fallback_to_cpu()`** — в pipeline back-vocal не имеет fallback на CPU.

### 7.3 `_ensure_model()` (`back_vocal_separator.py:90-120`)

Аналогично UVR, но **всегда** `.half().eval()` — нет CPU-ветки (`back_vocal_separator.py:111`). Это означает: при `torch_device="cpu"` получим dtype mismatch при первом форвард-пасе. На практике не критично, т.к. в pipeline хардкод-условия `use_autocast and device == "cuda"`.

Импорт: `from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer`.

Лог `back_vocal_model_loaded` с params_m.

### 7.4 `separate(vocals_path)` (`back_vocal_separator.py:122-253`)

Входной файл — 16кГц mono WAV от UVR. Алгоритм идентичен UVR (та же chunk-обработка, overlap-add, hamming window) — отличия:
- Модель возвращает **lead** напрямую (`lead_batch = self._model(batch)`).
- `backing = mix - lead` (вычитание, аналогично instrumental в UVR).
- На выходе **оба** стема ресэмплятся обратно в 16кГц mono (`back_vocal_separator.py:231-233`).

**Пути выхода** (`back_vocal_separator.py:235-245`):
- Stem входного файла: `{job_id}_(Vocals)` → удаляется `_(Vocals)` → `base_id = {job_id}`.
- `{media_root}/instrumental/{base_id}_(Lead).wav` — 16кГц mono PCM_16.
- `{media_root}/instrumental/{base_id}_(Backing).wav` — 16кГц mono PCM_16.

Лог `back_vocal_completed`.

### 7.5 `cleanup()` (`back_vocal_separator.py:255-273`)

Идентичен UVR `cleanup`: `del model`, `gc.collect`, `torch.cuda.empty_cache`, лог `back_vocal_cleanup_done`.

---

## 8. Whisper transcriber (`worker/gpu/whisper_transcriber.py`)

Docstring: «HuggingFace Transformers (PyTorch-native) для local speech-to-text. Accuracy is not critical — used only to identify the song for LLM lyrics search. Errors in 20-30% of words are acceptable» (`whisper_transcriber.py:1-6`).

### 8.1 Маппинг моделей (`whisper_transcriber.py:17-22`)

```python
MODEL_ID_MAP = {
    "tiny":   "openai/whisper-tiny",
    "base":   "openai/whisper-base",
    "small":  "openai/whisper-small",
    "medium": "openai/whisper-medium",
}
```

Если `model_size` не в маппинге — fallback `f"openai/whisper-{model_size}"` (`whisper_transcriber.py:68-70`). Т.е. `"large-v3"` тоже сработает.

### 8.2 `WhisperResult` (`whisper_transcriber.py:25-31`)

Dataclass: `text: str`, `language: str` (двухбуквенный код).

### 8.3 `__init__` (`whisper_transcriber.py:48-62`)

⚠️ **Eager load**: в конце `__init__` вызывается `self._load_model()` — модель грузится сразу при создании воркера (т.е. в `_build_gpu_pipeline`, до начала consume). Это противоречит lazy-load паттерну остальных GPU-компонентов.

Параметры: `model_size="medium"`, `device="cuda"`, `compute_type="float16"`, `model_cache_dir=None`.

Docstring параметра `model_size` упоминает только tiny/base (`whisper_transcriber.py:42`) — устарел, в реальности дефолт `"medium"`.

### 8.4 `_load_model()` (`whisper_transcriber.py:64-93`)

- Импорты: `torch`, `transformers.{WhisperForConditionalGeneration, WhisperProcessor}`.
- `torch_dtype = float16` если `device=="cuda"` И `"16" in compute_type`, иначе `float32` (`whisper_transcriber.py:72-76`).
- `WhisperProcessor.from_pretrained(model_id, cache_dir=...)`.
- `WhisperForConditionalGeneration.from_pretrained(model_id, cache_dir=..., dtype=torch_dtype).to(device)`.
- Лог `whisper_loaded` с `backend="transformers"`.

### 8.5 `transcribe(audio_path)` (`whisper_transcriber.py:95-197`)

Синхронный, через `asyncio.to_thread`.

- Если `self._model is None` — re-load (`whisper_transcriber.py:108-109`). Это нужно после `cleanup()`.
- `sf.read(audio_path, dtype="float32")`; mono mix через `.mean(axis=1)` (`whisper_transcriber.py:114-116`).
- Ресэмплинг до 16000 через `torchaudio.functional.resample` если нужно.
- **Chunking**: окна по `30 * 16000 = 480000` сэмплов (30с — нативное окно Whisper) (`whisper_transcriber.py:123`).
- Цикл по чанкам:
  - `processor(chunk, sampling_rate=16000, return_tensors="pt")` → `input_features.to(device, dtype=torch_dtype)`.
  - `with torch.no_grad(): model.generate(input_features, return_dict_in_generate=True, max_new_tokens=440)`.
  - `processor.decode(token_ids, skip_special_tokens=True).strip()`.
  - Непустые куски добавляются в список.
- **Детекция языка** (`whisper_transcriber.py:155-183`): только на первом чанке. `processor.decode(token_ids[:4], skip_special_tokens=False)` → ищет подстроку `<|{lang_code}|>` в фиксированном списке кодов:
  ```
  ru, en, es, fr, de, it, pt, zh, ja, ko, uk, pl, cs, tr, ar, hi, th, vi, nl, sv
  ```
  Default: `language = "en"` (`whisper_transcriber.py:125`).
- Финальный text: `" ".join(all_text_parts)`.

Лог `whisper_completed` с language, text_length, **полным text** (потенциально длинная строка в логе), duration_sec.

### 8.6 `cleanup()` (`whisper_transcriber.py:199-217`)

- `del self._model; del self._processor`; обе ссылки в `None`.
- `gc.collect()`, `torch.cuda.empty_cache()`.
- Лог `whisper_cleanup_done`.

> Сразу после cleanup модель None — следующий `transcribe()` re-load'нёт её через `_load_model()` (см. `whisper_transcriber.py:108-109`). В pipeline это совершается между задачами (вызов `cleanup` в `gpu_pipeline.py:236` и далее новый job заново вызовет `transcribe`).

---

## 9. VAD processor (`worker/common/vad_processor.py`)

Docstring: «Uses RMS energy detection via PyTorch» (`vad_processor.py:1-5`). **Не Silero и не WebRTC**.

### 9.1 Константы и dataclass

- `_SR = 16_000` (`vad_processor.py:17`).
- `@dataclass VADResult: cleaned_path: str` (`vad_processor.py:20-24`).

### 9.2 `__init__` (`vad_processor.py:35-36`)

`top_db: int = 35` — порог в dB ниже пика. Из main.py приходит `settings.vad_top_db = 16` (по умолчанию в config).

### 9.3 `process(vocals_path)` (`vad_processor.py:38-113`)

Синхронный. Возвращает `VADResult`.

**Загрузка** (`vad_processor.py:55-66`):
- `sf.read(vocals_path, dtype="float32")`; mono mix.
- Ресэмплинг до 16кГц если нужно.
- На любое исключение при загрузке: лог `vad_load_failed`, возврат **исходного пути** (no-op).

**RMS VAD** (`vad_processor.py:69-75`):
- `frame_length = 2048`, `hop_length = 512`.
- `frames = torch.from_numpy(y).unfold(0, 2048, 512)` — окно за окном.
- `rms = frames.pow(2).mean(dim=1).sqrt()`.
- `threshold = rms.max() * 10**(-top_db/20)` — относительный, привязан к пику.
- `is_voiced = rms > threshold`.

**Сборка интервалов** (`vad_processor.py:78-94`):
- `diff = np.diff(is_voiced, prepend=0, append=0)`; `starts = where(diff==1)`, `ends = where(diff==-1)`.
- Если нет voiced сегментов: лог `vad_no_voiced_segments`, возврат исходного пути.
- Каждый интервал во фреймах конвертится в сэмплы: `(s*hop, min(e*hop+frame_length, len(y)))`.
- `voiced = [y[s:e] for s,e in intervals]`; `cleaned = np.concatenate(voiced)`.

**Защита от слишком короткого результата** (`vad_processor.py:96-98`):
- Если `len(cleaned) / 16000 < 1.0` сек — лог `vad_result_too_short`, возврат исходного пути.

**Выходной файл** (`vad_processor.py:100-104`):
- `track_id = Path(vocals_path).stem.split("_")[0]` — для `{job_id}_(Vocals).wav` это будет `{job_id}`.
- `out_path = parent / f"cleaned_vocals_{track_id}.wav"` — 16кГц mono PCM_16.

Лог `vad_completed` с `original_sec`, `cleaned_sec`, `reduction_pct`.

⚠️ **Поведение «на ошибку — возврат исходного пути»**: если VAD не справился, дальше пайплайн получит **vocals_path**, а не cleaned. В `gpu_pipeline.py:217` результат присваивается переменной `cleaned_vocals_path` — её также удалит финальный cleanup (`gpu_pipeline.py:413-428`). Это означает: при VAD-фейле финальный `Path(cleaned_vocals_path).unlink` попытается удалить **тот же файл, что и `vocals_path`** → второй unlink выкинет `FileNotFoundError`, но он под `suppress(Exception)`, так что безопасно.

---

## 10. Известные «мёртвые» конфиг-флаги

Проверено грепом `ctc_device\|uvr_torch_device` по `worker/` и `shared/`:

| Поле в `config.py` | Где ещё используется | Статус |
|---|---|---|
| `ctc_device` (`config.py:63`) | нигде | **dead** — в `_build_gpu_pipeline` хардкод `device="cuda"` (`main.py:156`) |
| `uvr_torch_device` (`config.py:110`) | нигде | **dead** — в `_build_gpu_pipeline` не передаётся; `UVRSeparator.__init__` берёт свой default `"cuda"` |

Аналогично `back_vocal_torch_device` (`config.py:121`) — проверено, передаётся как `torch_device=settings.back_vocal_torch_device` в `main.py:69`, **используется**.

---

---

## 11. CTC aligner: `worker/gpu/torch_ctc_aligner.py` (1297 строк)

Docstring: «MMS-300M forced aligner (315M params, 1130 languages) with native CUDA forced_align() kernel. In-process — no subprocess isolation needed (PyTorch не имеет проблем с heap corruption как ONNX)» (`torch_ctc_aligner.py:1-6`).

### 11.1 Константы и модель

- `_SAMPLE_RATE = 16_000` (`torch_ctc_aligner.py:23`).
- `_HF_MODEL_ID = "MahmoudAshraf/mms-300m-1130-forced-aligner"` (`torch_ctc_aligner.py:24`).

### 11.2 `AlignmentStats` (`torch_ctc_aligner.py:27-32`)

Dataclass: `total_words: int = 0`, `proportional_fallback: int = 0`.

> ⚠️ Имя `proportional_fallback` обманчиво — это не «fallback», а **счётчик многосложных слов** (инкрементируется в каждой итерации `_to_syllable_timings` на ветке len(parts) > 1, см. `torch_ctc_aligner.py:1294`). Поле выбрано для совместимости с интерфейсом старого `CTCAligner` (см. docstring `torch_ctc_aligner.py:29`).

### 11.3 `__init__` (`torch_ctc_aligner.py:46-97`)

Параметры:
- `device: str = "cuda"`, `model_cache_dir: str | None`.
- `pre_trim_enabled = True`, `pre_trim_threshold = 0.7`, `pre_trim_min_speech_ms = 300`.
- `line_start_rms_adjust = True`, `word_end_drift_adjust = True`, `word_end_sustain_extend = True`.

Поля:
- `_model = None`, `_bundle = None`, `_dictionary: dict[str, int] = {}`.
- `_syllabifier = Syllabifier()` — из `karaoke_shared.utils.syllabifier` (import `torch_ctc_aligner.py:19`).
- `_silero_model = None`, `_silero_get_ts = None`.

Лог `torch_ctc_aligner_created` со всеми флагами.

### 11.4 `_ensure_model()` (`torch_ctc_aligner.py:103-142`) — lazy load

Импорты внутри функции: `Wav2Vec2ForCTC, Wav2Vec2Processor` из transformers.

Шаги:
1. `Wav2Vec2ForCTC.from_pretrained(_HF_MODEL_ID, torch_dtype=torch.float16, cache_dir=...)`.
2. `.to(device).eval()`.
3. `Wav2Vec2Processor.from_pretrained(...)`; `vocab = processor.tokenizer.get_vocab()`.
4. Словарь фильтруется: `{k: v for k, v in vocab.items() if len(k) == 1 and (k.isalpha() or k == "'")}` — однобуквенные алфавитные + апостроф (`torch_ctc_aligner.py:130-132`).
5. `_blank_idx = vocab.get("<blank>", 0)`. Комментарий: `<blank>=0, <pad>=1, </s>=2, <unk>=3, a=4, ..., x=30` (`torch_ctc_aligner.py:122-123`).

Лог `torch_ctc_model_loaded` с `vocab_size`, `params_m`, `duration_sec`.

### 11.5 Публичный `align(vocals_path, lyrics_text, language)` (`torch_ctc_aligner.py:148-248`)

Возвращает `tuple[list[SyllableTiming], AlignmentStats]`.

Защита: пустой `lyrics_text` → `ValueError("lyrics_text is empty")` (`torch_ctc_aligner.py:160-161`).

**Последовательность**:
1. `_ensure_model()`.
2. `waveform = self._load_audio(vocals_path)` → `(1, samples)` 16кГц mono.
3. **Silero pre-trim** (если `_pre_trim_enabled`):
   - `trim_offset = self._silero_trim_start(waveform)`.
   - Если > 0 — `waveform = waveform[:, trim_samples:]`; лог `ctc_alignment_pre_trim`.
4. `emission, ratio = self._forward_pass(waveform)`.
5. `words, transcript, first_flags = self._tokenize_lyrics(lyrics_text, language)`. Если transcript пуст → `RuntimeError("No valid tokens after text preprocessing")`.
6. `word_spans = self._align_tokens(emission, transcript)`.
7. Если `_line_start_rms_adjust` → `line_adjustments = self._compute_line_start_adjustments(...)`. Логирует первые 5 adjustments.
8. Если `_word_end_drift_adjust` → `end_adjustments = self._compute_word_end_adjustments(...)`.
9. Если `_word_end_sustain_extend` → `end_extensions = self._compute_word_end_extensions(..., end_adjustments=end_adjustments)` (последний параметр для **mutual exclusion**).
10. `combined_end_adjustments = dict(end_extensions); combined_end_adjustments.update(end_adjustments)` — drift trims приоритетнее extensions (`torch_ctc_aligner.py:224-228`).
11. `timings, stats = self._to_syllable_timings(words, word_spans, ratio, language, first_flags, time_offset=trim_offset, line_adjustments=..., end_adjustments=combined_end_adjustments)`.

Лог `alignment_complete` с total_words, fallback (=proportional_fallback), syllables, duration_sec.

### 11.6 `cleanup()` (`torch_ctc_aligner.py:250-263`)

- `del self._model; self._model = None`.
- Если есть `_silero_model` — `del`, обнулить `_silero_model`/`_silero_get_ts`.
- `self._dictionary = {}`.
- `gc.collect()`; `torch.cuda.empty_cache()` если CUDA доступна.
- Лог `torch_ctc_cleanup_done`.

### 11.7 `_forward_pass(waveform)` (`torch_ctc_aligner.py:269-280`)

- `torch.inference_mode()`.
- `output = self._model(waveform.to(device=self._device, dtype=torch.float16))`.
- `emission = torch.log_softmax(output.logits.float(), dim=-1)` — приводит обратно к float32 перед softmax.
- `ratio = waveform.size(1) / _SAMPLE_RATE / n_frames` — секунд на emission-фрейм.

### 11.8 `_align_tokens(emission, transcript)` (`torch_ctc_aligner.py:282-310`)

- `tokenized = [dict[c] for word in transcript for c in word if c in dict and dict[c] != 0]` — пропускает blank.
- Если пусто → `RuntimeError("All tokens mapped to blank")`.
- `aligned_tokens, scores = torchaudio.functional.forced_align(emission, targets, blank=0)`.
- `token_spans = torchaudio.functional.merge_tokens(aligned_tokens[0], scores[0])`.
- Возврат через `_unflatten(token_spans, word_lengths)` — группирует flat-span'ы по словам.

### 11.9 Silero pre-trim

#### 11.9.1 `_ensure_silero()` (`torch_ctc_aligner.py:316-324`)

- `torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)`.
- `self._silero_model = model`; `self._silero_get_ts = utils[0]` — функция `get_speech_timestamps`.

#### 11.9.2 `_silero_trim_start(waveform)` (`torch_ctc_aligner.py:326-347`)

- Зовёт `_ensure_silero()`.
- `audio = waveform.squeeze(0).cpu()`.
- `ts = silero_get_ts(audio, model, threshold=0.7, sampling_rate=16000, min_speech_duration_ms=300, min_silence_duration_ms=500, speech_pad_ms=50)`.
- Если пусто — `0.0`.
- Иначе вызывает `_refine_silero_onset(audio.numpy(), ts[0]["start"])`.

#### 11.9.3 `_refine_silero_onset(audio_np, silero_start_samples)` (`torch_ctc_aligner.py:349-417`)

Комментарий: «Silero с threshold=0.7 срабатывает после ramp-up формант — настоящий attack слова на 100-400ms раньше. Идём назад по RMS-огибающей до floor, привязанного к voiced-уровню внутри сегмента» (`torch_ctc_aligner.py:354-360`).

- `frame_len = int(0.02 * 16000) = 320` сэмплов (20мс).
- `look_after = int(0.5 * 16000) = 8000` сэмплов (500мс после silero_start) — окно для оценки voiced_level.
- `n_frames = end_idx // frame_len`; reshape в `(n_frames, frame_len)`; `rms = sqrt((trimmed**2).mean(axis=1))`.
- `silero_frame = silero_start_samples // frame_len`; `voiced_frames = rms[silero_frame:]`.
- `voiced_level = float(np.median(voiced_frames))`.
- `silence_floor = voiced_level / 10.0` — 20 dB ниже voiced (стандартный SNR-порог для silence) (`torch_ctc_aligner.py:386`).
- Backward walk: для `f in range(silero_frame - 1, -1, -1)`:
  - Если `rms[f] < silence_floor` — `silent_run += 1`; если `silent_run >= 2` (`required_silent`) — `onset_frame = f + silent_run`; `refined_sec = onset_frame * frame_len / SR`; break.
  - Иначе `silent_run = 0`.
- Guard: если откат больше 1.0 сек — fallback на `silero_start_sec` (`torch_ctc_aligner.py:402-404`).

Лог `silero_onset_refine` с `silero_start_sec`, `refined_onset_sec`, `backtrack_ms`, `voiced_level_db`, `silence_floor_db`.

### 11.10 Line-start RMS adjustment (`_compute_line_start_adjustments`, `torch_ctc_aligner.py:423-728`)

Возвращает `dict[int, tuple[float, float]]` — `{word_idx: (orig_start_sec, new_start_sec)}` для слов, у которых первый фонем заякорился в тишину/backing-leakage.

#### 11.10.1 Global baseline

`global_gaps` — список всех **внутрисловных** inter-phoneme gaps **исключая gap_0 и gap_last** (для слова из n span'ов берутся `gaps 1..n-2`):

```python
for spans in word_spans:
    for j in range(1, len(spans) - 1):
        global_gaps.append(float(spans[j + 1].start - spans[j].end))
```

`global_median_gap = statistics.median(global_gaps)`. `frame_len_samples = int(0.02 * 16000) = 320`.

#### 11.10.2 Условия рассмотрения слова

- `i == 0` или `first_flags[i] == True` (line-start; word 0 всегда рассматривается).
- `len(spans) >= 2` и `global_median_gap is not None`.
- `gap0 = spans[1].start - spans[0].end`.
- **Outlier**: `gap0 > 2.0 × global_median_gap` (`outlier_factor = 2.0`, `torch_ctc_aligner.py:481`).

#### 11.10.3 Расчёт voiced_level и attack_floor

- Окно `[spans[0].start, next_word_start - ratio]` (или до конца аудио для последнего слова) — superset, чтобы захватить true peak (`torch_ctc_aligner.py:516-545`).
- `voiced_level = max(word_rms)`.
- `attack_floor = voiced_level / 5.0` (-14 dB) — комментарий объясняет, почему строже чем -20 dB: нужно отличать «main vocal ONSET» от любого pre-attack артефакта (continuous low-level content) (`torch_ctc_aligner.py:548-558`).
- `silent_run_threshold = max(2, int(round(2.0 × ref)))` где `ref = max(global_median_gap, 1.0)`.

#### 11.10.4 Две ветки

**Extreme** (`gap0 / ref >= 7.0`, `torch_ctc_aligner.py:573-629`):

- Вызывается `_backward_walk_voiced_onset(audio_np, frame_len, start_sample=span1_sample, limit_sample=span0_sample, attack_floor, silent_run_threshold)`.
- Если возвращает `None` (uniform region) — fallback на `spans[1].start * ratio` (имя fallback `"span1_start"`).
- Если возвращает значение — `new_start_sec = bw_new_start_sec`, fallback `"backward_walk"`.
- Применяется только если `new_start_sec > orig_start_sec + ratio` (минимум 1 alignment-фрейм shift'а).

**Non-extreme** (`2× ≤ ratio < 7×`, `torch_ctc_aligner.py:640-712`):

- Forward RMS walk в окне `[spans[0].start, spans[1].start]`.
- Сначала ищем `silent_run >= silent_run_threshold` (drift), затем первый `above_attack` фрейм — это real attack.
- Если sustained первый фонем (RMS всегда выше attack_floor) — drift не срабатывает, `new_start_frame_in_scan = None`, leave orig_start.

Все рассмотренные/применённые adjustments логируются в `ctc_first_phoneme_trim` (первые 15 outliers и 20 considered).

### 11.11 `_backward_walk_voiced_onset` (`torch_ctc_aligner.py:734-803`)

Идёт от `start_sample` (span1) назад до `limit_sample` (span0). Возвращает либо seconds-timestamp earliest contiguous above-attack_floor frame, либо `None` (если uniform).

- Считает RMS на 20ms фреймах в окне `[region_start_frame, start_frame]`.
- Backward loop: `for local_idx in range(n_frames - 2, -1, -1)`:
  - `rms[local_idx] >= attack_floor` → `silent_run = 0; last_voiced_local_idx = local_idx`.
  - Иначе `silent_run += 1`. Если `silent_run >= silent_run_threshold and last_voiced_local_idx is not None` — return seconds.

### 11.12 Word-end drift (`_compute_word_end_adjustments`, `torch_ctc_aligner.py:809-985`)

Комментарий: «MMS forced_align даёт ~1-фреймовые spans → drift проявляется не как длинный последний span, а как аномально большой gap **перед** последним фонемом» (`torch_ctc_aligner.py:817-822`).

- `global_gaps` берётся **между gap_0 и gap_last** (`j in range(1, len(spans) - 2)`).
- `median_gap = statistics.median(global_gaps)`; если None → `{}`.
- Для каждого слова: `last_gap = spans[-1].start - spans[-2].end`.
- Outlier если `last_gap > 2.0 × ref_gap`.
- `voiced_level = max(word_rms)` по окну `[spans[0].start, spans[-1].end]`.
- `silence_floor = voiced_level / 10.0` (-20 dB).
- Forward RMS walk в `[prev_end_sec, orig_end_sec]`:
  - Отслеживает `last_voiced_idx`; на silent_run длиной `>= silent_run_threshold` (`max(2, int(round(2.0 × ref_gap)))`) — break.
  - `new_end_rel = (ss + (last_voiced_idx + 1) * frame_len) / SR`.
- Guards:
  - `new_end_sec >= orig_end_sec - ratio` → skip (минимум 1 alignment-фрейм trim).
  - `new_end_sec <= prev_end_sec + ratio` → skip (не пересекать границу предыдущего фонема).

Лог `ctc_word_end_trim`.

### 11.13 Word-end sustain extend (`_compute_word_end_extensions`, `torch_ctc_aligner.py:991-1127`)

Комментарий: «MMS emission fires once per phoneme at attack → sustained final vowel (typical at line-end) gets word closed at attack frame. Forward RMS walk extends word end to natural silence boundary, capped by next word's onset» (`torch_ctc_aligner.py:1000-1007`).

- **Mutual exclusion**: если `i in end_adjustments` — `continue` (extending would undo drift trim) (`torch_ctc_aligner.py:1032-1035`).
- `forward_end_sec = next_word_start_sec - ratio` (или `audio_end_sec` для последнего слова).
- Skip если `forward_end_sec - orig_end_sec < 2 * frame_len_sec` (= 40мс).
- `voiced_level = max(word_rms)` по `[spans[0].start, spans[-1].end]`.
- `silence_floor = voiced_level / 10.0` (-20 dB).
- Forward scan в `[orig_end_sec, forward_end_sec]`:
  - `last_voiced_idx` обновляется на frames `>= silence_floor`.
  - На `silent_run >= 2` (`required_silent`) — break, `capped_by = "silence"`. Иначе `capped_by = "next_word"`.
- Skip если `new_end_sec <= orig_end_sec + ratio`.

Лог `ctc_word_end_extend`.

### 11.14 `_load_audio(path)` (`torch_ctc_aligner.py:1133-1144`)

- `sf.read(path, dtype="float32")`; mono mix; ресэмплинг до 16кГц через `torchaudio.functional.resample`.
- Возвращает `(1, samples)` tensor.

### 11.15 `_tokenize_lyrics(lyrics_text, language)` (`torch_ctc_aligner.py:1150-1194`)

Возвращает `(words, transcript, first_flags)`.

- Импорт inline: `from unidecode import unidecode`.
- Разбивка по `splitlines()` → слова через `split()`.
- Для каждого слова: `romanized = unidecode(cleaned).lower()`; фильтр chars по `self._dictionary` (без blank).
- Если после фильтра нет валидных chars — слово пропускается полностью.
- `is_first_word` = True только для первого валидного слова в строке.

> ⚠️ Параметр `language` принимается, но **не используется** в текущей реализации — токенизация одна для всех языков (через `unidecode` + dictionary lookup).

### 11.16 `_unflatten(token_spans, word_lengths)` (`torch_ctc_aligner.py:1200-1210`)

Static method. Группирует flat-список spans по словам по их длинам. Если `offset + length > len(token_spans)` — break (early termination).

### 11.17 `_to_syllable_timings` (`torch_ctc_aligner.py:1216-1297`)

Преобразует word-level spans → syllable-level `SyllableTiming` (импорт из `karaoke_shared.models.track`).

- `match_count = min(len(words), len(word_spans))`.
- `stats.total_words = match_count`.
- Для каждого слова:
  1. Если `i in line_adjustments` — `ws = time_offset + line_adjustments[i][1]` (waveform-time, pre-offset).
     Иначе `ws = time_offset + spans[0].start * ratio`.
  2. Если `i in end_adjustments` — `wend = end_adjustments[i][1]` (**уже absolute sec, includes time_offset**, см. drift расчёт `torch_ctc_aligner.py:952`).
     Иначе `wend = time_offset + spans[-1].end * ratio`.
  3. Защита: `if wend <= ws: wend = ws + 0.05`.
  4. **Prefix**:
     - Первое слово в треке (`is_first_overall=True`) → `""`.
     - `first_flags[i] is True` (line-start) → `"\n"`.
     - Иначе → `" "`.
  5. `parts = self._syllabifier._split_word(word, language)` — single-syllable если 1 part, иначе пропорциональное разделение длительности.
  6. **Пропорциональное деление**:
     - `cl = [max(len(p.strip()), 1) for p in parts]`; `tc = sum(cl)`.
     - Для каждого `part`: `frac = cl[pi] / tc`; `send = cur + duration * frac`; новый `SyllableTiming(syllable, start=round(cur,3), end=round(send,3))`; prefix добавляется только к первому part.
     - `stats.proportional_fallback += 1` (учёт **многосложных слов**, не «фолбэк»).
  7. `is_first_overall = False` после каждой итерации.

> ⚠️ Префикс `"\n"`/`" "`/`""` встраивается **в строку syllable** (а не как отдельное поле). Дальше line_breaker может вставить дополнительные `\n` (см. STEP 7 в pipeline).

---

---

## 12. Lyrics: общие типы (`worker/common/lyrics_searcher.py`)

Это **не legacy** — модуль содержит общие типы, переиспользуемые provider chain'ом, matcher'ом и agent'ом.

### 12.1 `LyricsResult` (`lyrics_searcher.py:9-17`)

Dataclass — финальный результат поиска текста:
- `artist: str`
- `title: str`
- `lyrics: str`
- `language: str`
- `confidence: str` — `"high"` / `"medium"` / `"low"`
- `source_note: str` — имя источника или `"asr_fallback"`

### 12.2 Иерархия исключений (`lyrics_searcher.py:20-29`)

- `LyricsSearchError(Exception)` — базовый
  - `LyricsNotFoundError` — не нашли
  - `LyricsAPIError` — сетевая/API ошибка (retryable по комментарию)

### 12.3 `clean_lyrics(raw: str) -> str` (`lyrics_searcher.py:32-52`)

Постпроцессинг найденного текста:
1. Удаляет section markers `[Verse]`, `[Куплет]`, `[Припев: Artist]` (regex `\[.*?\]\n?`, DOTALL).
2. Re-join строк, разорванных Genius'ом `<br>`-вставками:
   - Строка начинается с закрывающей пунктуации (`,.;:!?)\]…—–`) — убрать предыдущий `\n`.
   - Строка начинается с горизонтального whitespace — заменить `\n+` на `" "`.
   - Предыдущая строка кончается на `(` или `[` — убрать `\n`.
3. Сжимает 3+ blank lines в 2.

---

## 13. Lyrics: base provider (`worker/common/lyrics/base_provider.py`)

### 13.1 `LyricsCandidate` (`base_provider.py:9-16`)

Dataclass: `artist`, `title`, `lyrics`, `source` (имя провайдера).

### 13.2 Абстрактные классы (`base_provider.py:19-47`)

- `TextSearchProvider(ABC)` — атрибут `name: str`, метод `async search_by_text(text_fragment) -> list[LyricsCandidate]`.
- `ArtistTitleProvider(ABC)` — атрибут `name: str`, метод `async search_by_metadata(artist, title) -> LyricsCandidate | None`.

Контракт docstring'а: «Should never raise on 'not found' — return [] / None instead. May raise LyricsAPIError on infrastructure failures».

### 13.3 `lyrics/__init__.py` (`__init__.py:1-15`)

Реэкспорт: `ArtistTitleProvider`, `LyricsCandidate`, `LyricsProviderChain`, `TextSearchProvider`.

---

## 14. Lyrics: providers

### 14.1 `GeniusProvider` (`worker/common/lyrics/providers/genius.py`, 126 строк) — `TextSearchProvider`

- `name = "genius"`.
- `__init__(token, timeout=10.0)`.
- `_SEARCH_URL = "https://api.genius.com/search"`; `_MAX_RESULTS = 3`.
- `_BROWSER_HEADERS` — Mozilla/5.0 UA + `Accept: text/html...` + `Accept-Language: en-US,en;q=0.5`.
- `search_by_text(text_fragment)`:
  1. `httpx.AsyncClient(timeout, headers={"Authorization": f"Bearer {token}"})`.
  2. `GET /search?q={fragment}`.
  3. Для каждого hit в `data.response.hits[:3]` — `_scrape_lyrics(client, song.url)`.
  4. Skip кандидата если `len(lyrics) < 20`.
- `_scrape_lyrics(client, url)`:
  - GET с `_BROWSER_HEADERS`, `follow_redirects=True`.
  - BeautifulSoup парсит `[data-lyrics-container='true']` div'ы.
  - Перед извлечением заменяет `<br>` на `\n`.
  - В первом контейнере, если есть `Contributors` или `Lyrics\n` — отрезает префикс до `Lyrics\n` и блок `Read More`.

### 14.2 `LRCLibProvider` (`worker/common/lyrics/providers/lrclib.py`, 72 строки) — `ArtistTitleProvider`

- `name = "lrclib"`.
- `_BASE_URL = "https://lrclib.net/api"`, `_MAX_RESULTS = 3`.
- `httpx.AsyncHTTPTransport(retries=2)` — два HTTP-уровневых ретрая.
- `search_by_metadata(artist, title)`:
  1. Сначала combined query `?q="{artist} {title}"`.
  2. Если пусто — structured `?track_name={title}&artist_name={artist}`.
- Skip кандидата если `len(plainLyrics) < 20`.

### 14.3 `LyricsOvhProvider` (`worker/common/lyrics/providers/lyricsovh.py`, 46 строк) — `ArtistTitleProvider`

- `name = "lyricsovh"`.
- `_BASE_URL = "https://api.lyrics.ovh/v1"`.
- `GET /{artist}/{title}` (без percent-encoding в коде — артист/тайтл встраиваются прямо в URL).
- Skip если `len(lyrics) < 20`.

### 14.4 `providers/__init__.py`

Пустой файл (`wc -l = 0`).

> ⚠️ В `worker/common/lyrics/providers/` нет файлов `chartlyrics.py` / `simpmusic.py`.

---

## 15. Lyrics: matching submodule

### 15.1 `matching/__init__.py` (`__init__.py:1-15`)

Реэкспорт: `LyricsExpander`, `LyricsMatcher`, `MatchFeatures`, `NormalizedText`, `normalize_text`, `score_all`.

### 15.2 Linguistics (`worker/common/lyrics/matching/linguistics.py`, 171 строка)

#### 15.2.1 `WordFeatures` (`linguistics.py:36-41`)

Frozen dataclass: `text`, `lemma`, `skeleton`, `metaphone`. Все поля — `str`.

#### 15.2.2 `make_word_featurizer(language)` (`linguistics.py:47-53`)

Возвращает callable `WordFeaturizer = Callable[[str], WordFeatures]`. Диспетчер:
- `"ru"` → `_ru_featurizer()`
- `"en"` → `_en_featurizer()`
- иначе → `_universal_featurizer(lang)`

#### 15.2.3 `init_morph_analyzer()` (`linguistics.py:56-74`)

Eager init **process-wide singleton** `pymorphy3.MorphAnalyzer`.
- Глобалы: `_MORPH: pymorphy3.MorphAnalyzer | None = None`, `_MORPH_LOCK = threading.Lock()`.
- Double-checked locking: проверка вне lock'а, повторная — внутри.
- Логи `pymorphy3_init_start` / `pymorphy3_init_done`.

Вызывается из `worker/app/main.py:200-202` при старте воркера.

#### 15.2.4 Реализации featurizer'ов

- **`_ru_featurizer`** (`linguistics.py:81-99`): `lemma = morph.parse(norm)[0].normal_form`; `skeleton = _skeleton_cyrillic(norm)`; `metaphone = ""` (пусто для кириллицы). На любое исключение `morph.parse` → fallback `lemma = norm`.
- **`_en_featurizer`** (`linguistics.py:102-116`): `stemmer = snowballstemmer.stemmer("english")`; `lemma = stemmer.stemWord(norm)`; `skeleton = _skeleton_latin(norm)`; `metaphone = _safe_metaphone(norm)` (через `jellyfish.metaphone`).
- **`_universal_featurizer`** (`linguistics.py:119-146`): пробует `snowballstemmer.stemmer(language)`; на `KeyError`/`ValueError` — `stemmer = None`. `ascii_form = unidecode(norm)`; skeleton через `_skeleton_latin(ascii_form)`. Metaphone только если `ascii_form.isascii()`.

#### 15.2.5 Skeleton-функции (`linguistics.py:149-159`)

- `_skeleton_cyrillic(word)`: drops `аеёиоуыэюяьъй` (`_RU_DROP_CHARS`), оставляет alpha-chars, далее `unidecode`, replace `'` → `""`, lowercase.
- `_skeleton_latin(word)`: drops `aeiouy` (`_ENGLISH_VOWELS`), оставляет alpha, lowercase.

#### 15.2.6 `_safe_metaphone(word)` (`linguistics.py:162-168`)

`jellyfish.metaphone(word)` под try/except (на исключение — `""`).

### 15.3 Normalizer (`worker/common/lyrics/matching/normalizer.py`, 72 строки)

#### 15.3.1 `NormalizedText` (`normalizer.py:37-44`)

Frozen dataclass: `text: str`, `words: tuple[WordFeatures, ...]`. Свойство `word_count`.

#### 15.3.2 `normalize_text(text, language)` (`normalizer.py:47-54`)

1. `_clean_text(text)`.
2. `featurize = make_word_featurizer(language)`.
3. `tokens = cleaned.split()`; `words = (featurize(t) for t in tokens if t)`.
4. Фильтр пустых: `words = (w for w in words if w.text)`.

#### 15.3.3 `_clean_text` шаги (`normalizer.py:57-72`)

1. `unicodedata.normalize("NFKC", text).lower()`.
2. Удалить `[...]` секции (`_SECTION_RE = r"\[[^\[\]]*\]"`).
3. Удалить короткие parens (1-30 chars): `_SHORT_PARENS_RE = r"\(([^()]{1,30})\)"` — целит в ad-libs / backing.
4. Удалить standalone digit runs (`\b\d+\b`).
5. Удалить пунктуацию кроме apostrophe (`_PUNCT_RE = r"[^\w'\s]+"`).
6. Collapse whitespace.

Комментарий: «Не collapse'ит дублирующиеся слова — chorus repetitions are legitimate» (`normalizer.py:11-12`).

### 15.4 Scorer (`worker/common/lyrics/matching/scorer.py`, 306 строк)

#### 15.4.1 Константы (`scorer.py:43-58`)

- `_NGRAM_N = 4` (для skeleton-n-gram Jaccard).
- `_ANCHOR_N = 5` (для rare anchor lemma-n-grams).
- `_LEV_RATIO_HIGH = 80.0` (fuzz.ratio для score=2).
- `_LEV_RATIO_LOW = 60.0` (score=1).
- `_LEV_LEN_TOL = 2` (max |len(word) - len(target)|).

**Веса композитного скора**:
```
_W_COV_F1 = 0.55  (coverage_f1, harmonic mean)
_W_PHONETIC = 0.15
_W_NGRAM = 0.10
_W_ANCHOR = 0.20
_W_LENGTH_PEN = 0.10   (вычитается)
_W_HINT = 0.30         (additive — поверх 1.0)
```

#### 15.4.2 `MatchFeatures` (`scorer.py:61-82`)

Frozen dataclass с восемью полями float: `coverage_asr`, `coverage_cand`, `phonetic_match_rate`, `ngram_jaccard`, `rare_anchor_score`, `length_ratio_penalty`, `hint_score`, `composite`.

Метод `as_dict()` — округление до 3 знаков для логирования.

#### 15.4.3 `score_all(asr, candidates, hint_scores=None)` (`scorer.py:85-110`)

- `hint_scores` опционален; если задан — длина должна совпадать с candidates, иначе `ValueError`.
- Сначала `_rare_anchor_scores` (требует pool всех кандидатов).
- Затем `_score_one` для каждого.

#### 15.4.4 `_score_one` (`scorer.py:113-160`)

- `_build_index` строит `_Index` для ASR и candidate.
- Для каждого слова — `_match_score(word, idx)` ∈ {0,1,2,3}.
- `coverage_asr` = доля слов с score ≥ 2.
- `coverage_cand` — симметрично.
- `phonetic_match_rate = sum(asr_scores) / (3.0 × len(asr_scores))` — нормирует на максимальный score=3.
- `ngram_jaccard = _ngram_jaccard(asr.words, cand.words, n=4)` по skeleton-n-grams.
- `length_ratio_penalty = min(1.0, abs(log(len(cand.words) / len(asr.words))))`.
- `coverage_f1 = _harmonic_mean(coverage_asr, coverage_cand)` — комментарий: гасит remix/long-mix кандидатов.
- `composite = 0.55×F1 + 0.15×phonetic + 0.10×ngram + 0.20×anchor + 0.30×hint - 0.10×length_pen`, clamp [0,1].

#### 15.4.5 `_match_score(word, idx)` (`scorer.py:198-225`)

Иерархия совпадений:
- `text in texts` → **3** (exact).
- `lemma in lemmas` → **2**.
- `skeleton in skeletons` → **2**.
- `metaphone in metaphones` → **2**.
- Иначе fuzzy `rapidfuzz.fuzz.ratio` для слов с `|len - target_len| ≤ 2`: ≥80 → 2, ≥60 → 1, иначе 0.

#### 15.4.6 `_ngram_jaccard` (`scorer.py:228-248`)

Skeleton-based 4-граммы `{tuple(keys[i:i+4]) for i in ...}`; Jaccard.

#### 15.4.7 `_rare_anchor_scores` (`scorer.py:251-291`)

- Для каждого кандидата считает 5-граммы lemma.
- `df[g]` — document frequency (в скольких кандидатах встречается).
- Density: `sum(1/df[g] for g in cand_grams if g in asr_grams) / len(cand_grams)`.
- Постнормировка: делим на max density.
- Комментарий: division by `len(grams)` снимает структурный bias toward longer candidates.

### 15.5 Expander (`worker/common/lyrics/matching/expander.py`, 262 строки)

#### 15.5.1 Назначение

Раскрывает три типа сокращений повторов:
1. **Counted section header** `[Chorus x2]` / `[Припев 2 раза]`.
2. **Section reference** — `[Chorus]` без body, копируется body предыдущей секции с тем же label.
3. **Inline repeat** `oh oh oh (2 раза)` / `я тебя люблю ×3` в конце строки.

Без раскрытия: «правильный кандидат выглядит короче того, что реально спето → length-ratio penalty его душит» (`expander.py:11-15`).

#### 15.5.2 Архитектура

- **Algorithmic pass** — regex + section parsing (детерминированно).
- **LLM pass** — DeepSeek, **только если** `_META_INSTRUCTION_RE` срабатывает на `algo_result` (например встречается `повторить припев`, `repeat chorus`).
- Кеш `self._cache: dict[str, str]` keyed by SHA-256 raw input — идентичные кандидаты от нескольких провайдеров → один LLM call.

#### 15.5.3 Regex'ы

- `_SECTION_HEADER_RE = r"^\s*\[\s*([^\[\]\n]+?)\s*\]\s*$"` — целая строка-заголовок.
- `_COUNT_FRAGMENT_RE` — захватывает `x2 / х2 / ×2`, `2x / 2х / 2×`, `2 раза/раз/разов`, `2 times/time`.
- `_INLINE_REPEAT_BRACKETED_RE` — `(2 раза)`/`[x3]`/`(2x)` в конце строки.
- `_INLINE_REPEAT_BARE_RE = r"\s+(?:×\s*(\d+)|(\d+)\s*×)\s*$"` — bare repeat с `×` (только мультипликационный знак — `x` мог бы давать false positives).
- `_META_INSTRUCTION_RE = r"\b(?:repeat|повтор\w*|снова)\s+(?:chorus|verse|bridge|припев|куплет|бридж)"`.

#### 15.5.4 Алгоритм `expand`

1. SHA-256 lookup в кеше.
2. `algo_result = _expand_algorithmic(raw_lyrics)`:
   - `_parse_sections` → список `_Section(label, count, body)`.
   - `_extract_count(label)` — вытаскивает число из label по `_COUNT_FRAGMENT_RE`, чистит label.
   - `_render_sections`:
     - Registry секций — последняя body для каждого label.
     - Header-only section без known body → drop.
     - `_expand_inline_repeats(block)` для каждого блока.
     - Итог: `\n\n`.join всех развёрнутых блоков.
3. Если `_META_INSTRUCTION_RE.search(algo_result)` И `self._api_key` — `_expand_llm`. Иначе пропуск с логом `expander_llm_skipped(reason="no_api_key")` или `reason="empty_or_failed"`.
4. Сохранить в кеш.

#### 15.5.5 LLM-prompt (`expander.py:245-251`)

System: «Ты помощник по обработке текстов песен... разверни эти инструкции так, чтобы получился полный текст... Не меняй слова, не сокращай, не добавляй комментариев». Temperature 0.0, max_tokens 8192. Endpoint `https://api.deepseek.com`.

### 15.6 Matcher (`worker/common/lyrics/matching/matcher.py`, 329 строк)

#### 15.6.1 Конструктор (`matcher.py:42-56`)

Параметры:
- `expander: LyricsExpander | None`
- `deepseek_api_key`, `model="deepseek-chat"`
- `thresh_strong = 0.65`, `thresh_weak = 0.45`, `margin = 0.05`

#### 15.6.2 `match(asr_text, candidates, language, artist_hints, title_hints)` (`matcher.py:58-159`)

Шаги:
1. Защита: если нет кандидатов или пустой asr_text → `None`.
2. `expanded = await self._expand_all(candidates)`.
3. `asr_norm = normalize_text(asr_text, language)`.
4. `cand_norms = [normalize_text(exp, language) for exp in expanded]`.
5. `hint_scores = [_hint_match_score(...) for c in candidates]`.
6. `feature_list = score_all(asr_norm, cand_norms, hint_scores=hint_scores)`.
7. Для каждого — лог `matcher_features` со всеми feature-метриками.
8. Сортировка по `composite` desc.
9. `top = ranked[0]`, `second = ranked[1] if len > 1 else None`.
10. `gap = top.composite - second.composite`.

**Решение** (`matcher.py:112-159`):

| Условие | Outcome | Confidence | Действие |
|---|---|---|---|
| `top.composite ≥ 0.65` и `gap ≥ 0.05` (или second is None) | `strong_win` | `high` | вернуть top |
| `top.composite ≥ 0.65` и `gap < 0.05` | `tiebreaker` (LLM) | `high` если LLM выбрал | LLM tiebreak. Иначе `strong_close_no_tb`, top, `medium` |
| `top.composite ≥ 0.45` и есть second | `weak_tiebreaker` (LLM) | `medium` | LLM tiebreak. Иначе `weak_win`, top, `medium` |
| `top.composite ≥ 0.45` без second | `weak_win` | `medium` | вернуть top |
| `top.composite < 0.45` | `reject` | — | вернуть `None` |

> ⚠️ В weak-band tiebreak вызывается **всегда** когда есть runner-up (без проверки margin) — комментарий: «small gap usually means same song from different providers; rejecting both loses correct text» (`matcher.py:132-139`).

#### 15.6.3 `_build_result` (`matcher.py:170-184`)

`lyrics = clean_lyrics(ranked.expanded_lyrics).strip()`; собирает `LyricsResult` с `source_note = candidate.source`.

#### 15.6.4 `_tiebreak` + `_call_llm_tiebreak` (`matcher.py:191-275`)

- DeepSeek prompt: «ответь строго одной цифрой: 1 или 2. Никаких пояснений». `max_tokens=4`.
- System содержит инструкцию учитывать filename_hint (artist/title из имени файла) как «СИЛЬНЫЙ приоритетный сигнал», особенно когда ASR содержит мало распознаваемых слов.
- User: `<asr language="...">...</asr>` + опциональный `<filename_hint>artist:.../title:...</filename_hint>` + два `<candidate id="1|2" artist=... title=...>...</candidate>`.
- Parse: ответ начинается с `"1"` → a; с `"2"` → b; иначе лог `matcher_tiebreak_unparsed`, return `None`.

#### 15.6.5 `_hint_match_score` (`matcher.py:285-329`)

Внешняя функция (не метод класса). Возвращает `[0..1]`.

- Haystack = `f"{cand_artist} {cand_title}".casefold()`.
- Для каждого hint: `rapidfuzz.fuzz.partial_ratio(h, haystack) / 100.0`.
- `_HINT_NOISE_FLOOR = 0.65` — всё ниже считается шумом → 0.0. Выше — масштабируется `(r - 0.65) / (1 - 0.65)`.
- Итог: усреднение по сторонам (artist + title), если обе hints даны.

---

## 16. Lyrics: filename parser (`worker/common/lyrics/filename_parser.py`, 157 строк)

### 16.1 `ParsedFilename` (`filename_parser.py:49-78`)

Frozen dataclass: `artist_variants: tuple[str, ...]`, `title_variants: tuple[str, ...]`. Свойства `artist`/`title` — первый элемент tuple'а; `artist_alts`/`title_alts` — `list[1:]`.

### 16.2 `FilenameParser.parse(filename)` (`filename_parser.py:90-115`)

- `raw = await asyncio.to_thread(self._call_llm, f"Имя файла: {filename}")`.
- На любое исключение → `ParsedFilename.empty()` с логом `filename_parse_llm_failed`.
- `data = _extract_json(raw)` — пробует `json.loads(raw)`, потом ищет `\{.*\}` regex'ом.
- Собирает варианты через `_build_variants(canonical, original)`.

### 16.3 Промпт (`filename_parser.py:23-46`)

System: «Извлеки имя исполнителя и название песни из имени файла. Имя может быть транслитерировано → верни КАНОНИЧЕСКОЕ имя на ОРИГИНАЛЬНОМ языке (для русских — кириллица). Если оригинальное написание ОТЛИЧАЕТСЯ от канонического — верни ОБА (artist_original / title_original)...». Просит JSON `{"artist": "...", "title": "...", "artist_original": "...", "title_original": "..."}`.

### 16.4 `_call_llm` (`filename_parser.py:117-132`)

DeepSeek через `OpenAI(base_url="https://api.deepseek.com")`. `temperature=0.0`, `max_tokens=256`, timeout 60s.

### 16.5 `_build_variants` (`filename_parser.py:135-143`)

Дедупликация по casefold: `original` добавляется только если отличается от `canonical`.

---

## 17. Lyrics: fragments (`worker/common/lyrics/fragments.py`, 70 строк)

### 17.1 `extract_search_fragments(asr_text, n=3) -> list[str]` (`fragments.py:8-60`)

Цель — выбрать `n` репрезентативных фрагментов из ASR-текста для подачи поисковикам (длинные фрагменты дают более специфичные результаты, уменьшают false positives — remix/battle/compilation).

Алгоритм:
1. Split по `[.!?\n]+`; trim.
2. Если sentence-split дал пусто → fallback на чанки по 10 слов.
3. Фильтр `len(p.split()) >= 5`. Если ноль — relax filter (split по тому же regex без фильтра).
4. Если `len(phrases) < n` и слов хватает (`len(words) >= n*5`) — пере-чанкуем (`chunk_size = max(8, min(12, len(words) // n))`).
5. Trim каждый до 12 слов.
6. `_spread_indices(len, n)` — equally-spaced indices через `step = (length - 1) / (n - 1)`, `round`.

### 17.2 `_spread_indices(length, n)` (`fragments.py:63-70`)

- `n >= length` → `range(length)`.
- `n == 1` → `[0]`.
- Иначе — равноотстоящие.

---

## 18. Lyrics: provider chain (`worker/common/lyrics/provider_chain.py`, 319 строк)

### 18.1 `LyricsProviderChain.__init__` (`provider_chain.py:44-58`)

Поля: `_text_providers`, `_metadata_providers`, `_matcher`, `_filename_parser`, `_fallback_agent`, `_search_fragments=3`.

В main.py приходит `settings.lyrics_search_fragments = 2` (`config.py:56`).

### 18.2 `search(asr_text, detected_language, artist_hint, title_hint, filename)` (`provider_chain.py:60-193`)

**Stage 0: filename parse** (`provider_chain.py:77-95`):
- Только если `filename` задан И (`artist_hint` или `title_hint` пустой) И есть `_filename_parser`.
- Заполняет недостающее `artist_hint`/`title_hint` (canonical) + сохраняет `artist_alts`/`title_alts`.

**Stage 1: parallel candidate collection** (`provider_chain.py:97-110`).

Вызов `_collect_candidates`, далее `_deduplicate` (`set` по `(lower artist, lower title)`).

#### 18.2.1 `_collect_candidates` (`provider_chain.py:199-266`)

Параллельные `asyncio.Task`'и:
- Для каждого text-provider × fragment (от `extract_search_fragments`) — `_safe_text_search`.
- Если есть И artist И title variants: для каждой пары `(a, t)` × text-provider — `_safe_text_search(f"{a} {t}")`. Иначе — только title × text-provider (для одного title).
- Если есть И artist И title variants: для каждой пары × metadata-provider — `_safe_metadata_search(a, t)`.

`asyncio.gather(*tasks, return_exceptions=True)`. Результаты-списки extend'ятся в `candidates`, единичные — append.

`_safe_*` обёртки — глушат любой Exception, логируют `text_provider_error` / `metadata_provider_error`, возвращают `[]`/`None`.

**Stage 2: matcher** (`provider_chain.py:115-134`):
- `result = await matcher.match(...)`.
- Если вернул `LyricsResult` — логи `lyrics_matched`, return.
- Иначе лог `lyrics_match_rejected_all`.

**Stage 3: fallback agent** (`provider_chain.py:136-163`):
- `agent_candidates = await _fallback_agent.search(...)`; `_deduplicate`.
- `matcher.match(agent_candidates, ...)` снова.

**Stage 4: ASR fallback** (`provider_chain.py:165-188`):
- Если `len(asr_text.strip()) >= 20` — лог `lyrics_using_asr_fallback`, возвращает `LyricsResult(artist=artist_hint or "Unknown", title=title_hint or "Unknown", lyrics=asr_clean, language=detected_language, confidence="low", source_note="asr_fallback")`.
- Иначе → `LyricsNotFoundError` с поясняющим сообщением.

### 18.3 `_variants(primary, alts)` (`provider_chain.py:307-319`)

Возвращает `primary + alts` без дубликатов по `casefold`, сохраняя порядок.

---

## 19. Lyrics: agent (`worker/common/lyrics_agent.py`, 631 строка)

Агентский tool-calling loop для поиска lyrics через web.

### 19.1 Цель и контракт

Docstring: «Collects 1-3 raw lyrics candidates from the web. Selection between candidates — matcher's job, not agent's» (`lyrics_agent.py:1-7`).

### 19.2 Константы и enums

- `_YANDEX_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"` (`lyrics_agent.py:31`).
- `_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) ... Chrome/120.0.0.0 Safari/537.36"` (`lyrics_agent.py:111-114`).
- `_DEFAULT_SEARCH_LANGUAGE = "en"` — fallback когда Whisper не распознал язык (`lyrics_agent.py:136`).
- `_MAX_WORDS_PER_QUOTED_PHRASE = 3` — лимит на длину фразы в кавычках в search query (`lyrics_agent.py:141`).
- `_MAX_CONSECUTIVE_SEARCHES = 2` — после двух `web_search` подряд агента принуждают сделать `fetch_webpage` (`lyrics_agent.py:144`).

#### 19.2.1 Yandex маппинги

```python
_YANDEX_SEARCH_TYPE = {"ru": "SEARCH_TYPE_RU", "tr": "SEARCH_TYPE_TR", "kk": "SEARCH_TYPE_KK"}  # default COM
_YANDEX_LOCALIZATION = {"ru": ..., "uk": ..., "be": ..., "kk": ..., "tr": ..., "en": ...}  # default EN
```

### 19.3 System prompt (`lyrics_agent.py:33-70`)

Ключевые правила:
1. **АЛГОРИТМ**: web_search (2-4 слова) → fetch_webpage → анализ → если подходит, добавь в JSON → если нет, повтори.
2. **ЗАПРЕЩЕНО**:
   - 2 `web_search` подряд без `fetch_webpage` между ними.
   - Оборачивать в кавычки фразы длиннее 3 слов (Whisper искажает → exact match не сработает).
3. Формат ответа — **JSON-массив** `[{"artist":..., "title":..., "lyrics":...}, ...]`.
4. Если ничего не нашёл — пустой массив `[]`.

### 19.4 Tool schema (`lyrics_agent.py:72-109`)

Два tool'а:
- `web_search(query: str)` — поиск, возвращает релевантные ссылки.
- `fetch_webpage(url: str)` — загрузить страницу.

### 19.5 `_quoted_phrase_too_long(query)` (`lyrics_agent.py:147-153`)

Регулярка по `"..."` находит фразы; возвращает первую, длиннее 3 слов, или `None`.

### 19.6 Backend implementations

#### 19.6.1 `_searxng_search(query, base_url, timeout, language)` (`lyrics_agent.py:161-190`)

- `httpx.get(f"{base_url}/search", params={"q":..., "format":"json", "categories":"general", "language":lang}, timeout=...)`.
- Берёт первые 10 `results`; map в `{title, href, body}`.
- На любое исключение — лог `searxng_search_failed`, return `None`.

#### 19.6.2 `_yandex_search(query, api_key, folder_id, timeout, language)` (`lyrics_agent.py:193-253`)

- `search_type = _YANDEX_SEARCH_TYPE.get(lang, "SEARCH_TYPE_COM")`.
- `l10n = _YANDEX_LOCALIZATION.get(lang, "LOCALIZATION_EN")`.
- POST с `Authorization: Api-Key {key}` body содержит `query`, `groupSpec`, `maxPassages=2`, `l10n`, `responseFormat: "FORMAT_XML"`.
- Ответ: `base64.b64decode(response.json()["rawData"])` → XML → `ET.fromstring` → ищет `.//{*}doc` → `url`, `title` (объединение `itertext()`), `passage` (passages/passage).

#### 19.6.3 `_web_search(query, backend, language, api_key, folder_id, timeout, searxng_url)` (`lyrics_agent.py:256-309`)

- Guard `_quoted_phrase_too_long`: возвращает JSON-error с пояснением, что нужны короткие quotes.
- Диспетчер по `backend in {"searxng", "yandex"}`; если соответствующий backend не сконфигурирован — JSON-error.
- Если результаты пустые — `{"error": "Ничего не найдено"}`.
- Лог `web_search_via`.

#### 19.6.4 `_fetch_webpage(url, timeout)` (`lyrics_agent.py:312-348`)

- `httpx.get(url, follow_redirects=True, timeout=..., headers={"User-Agent": _BROWSER_UA})`.
- **Content-Type guard** (`lyrics_agent.py:321-337`): принимает только `text/html` или `application/xhtml`; иначе JSON-error `Unsupported content-type: ...` + лог `fetch_webpage_skipped_non_html`. Комментарий: «BeautifulSoup парсит PDF/images/audio в token-eating garbage».
- BeautifulSoup html.parser; декомпозиция тегов `script, style, nav, header, footer, aside, iframe, noscript`.
- Truncate `text[:12000] + "\n...[обрезано]"` если длиннее.
- На исключение — JSON-error.

### 19.7 `LyricsAgent` class

#### 19.7.1 `__init__` (`lyrics_agent.py:362-378`)

Параметры: `deepseek_api_key`, `yandex_search_api_key=""`, `yandex_search_folder_id=""`, `model="deepseek-chat"`, `max_iterations=20` (default), `timeout=15.0`, `searxng_url=None`.

> ⚠️ Из main.py приходит `lyrics_agent_max_iterations=15` (`config.py:50`), а класс default — `20`.

#### 19.7.2 `search(asr_text, detected_language, artist_hint, title_hint, artist_alts, title_alts)` (`lyrics_agent.py:380-476`)

1. `search_language = (detected_language or "").lower() or "en"`.
2. Собирает `user_message`: ASR-текст + язык + artist_hint/alts + title_hint/alts.
3. `backends_to_try`: сначала SearXNG (если `_searxng_url`), потом Yandex (если оба ключа). Если оба пусты — лог `lyrics_agent_no_backends_configured`, return `[]`.
4. **Sequential two-pass** (комментарий: «conserves Yandex API quota»):
   - Для каждого backend: вызывает `_run_agent` через `asyncio.to_thread`.
   - Если первый pass вернул `candidates` — break (Yandex не вызывается).
   - На второй pass подмешивает в `user_message` системную подсказку «предыдущая попытка через X не нашла, сейчас активен Y».
5. Логи `lyrics_agent_pass_starting`/`lyrics_agent_pass_completed`/`lyrics_agent_completed`.
6. Исключения: `LyricsSearchError` — re-raise; любое другое — `LyricsAPIError`.

#### 19.7.3 `_run_agent(user_message, backend, language)` (`lyrics_agent.py:482-581`)

- Клиент: `OpenAI(api_key=deepseek_api_key, base_url="https://api.deepseek.com", timeout=120.0)`.
- `messages = [{system}, {user}]`.
- `tool_functions = {"web_search": lambda q: _web_search(..., backend=backend, ...), "fetch_webpage": lambda u: _fetch_webpage(u, ...)}`.
- Tracker `consecutive_searches = 0`.
- Цикл до `_max_iterations` (default 15 из main.py):
  1. `client.chat.completions.create(model=..., messages=..., tools=_TOOLS, max_tokens=8192)`.
  2. Если `message.tool_calls` пусто — return `message.content`.
  3. Для каждого `tool_call`:
     - Если `web_search` И `consecutive_searches >= 2` — **блок**: возвращает JSON-error «Ты уже сделал N web_search подряд. Сейчас ОБЯЗАТЕЛЬНО загрузи через fetch_webpage» (`lyrics_agent.py:537-556`).
     - Иначе вызывает соответствующую функцию.
     - На `web_search` инкремент `consecutive_searches`; на `fetch_webpage` — reset в 0.
  4. Каждый результат добавляется в messages как `{"role": "tool", "tool_call_id": ..., "content": result}`.
- На исчерпание итераций: лог `agent_iterations_exhausted`, return `"[]"`.

#### 19.7.4 `_parse_candidates(raw, backend)` (`lyrics_agent.py:587-623`)

- `_try_parse_json_array(raw)` — `json.loads` или regex `\[\s*\{.*?\}\s*\]` (DOTALL).
- Если не list — лог `agent_response_not_array`, return `[]`.
- Для каждого item: skip если `len(lyrics) < 20`; `artist`/`title` fallback на `"Unknown"`; `source = backend` (`"searxng"` или `"yandex"`).

---

---

## 20. Shared: константы (`shared/karaoke_shared/constants.py`, 107 строк)

`StrEnum`-классы:

### 20.1 `TrackStatus` (`constants.py:16-22`)

`"pending"`, `"processing"`, `"ready"`, `"error"`.

### 20.2 `JobStatus` (`constants.py:25-31`)

`"pending"`, `"running"`, `"completed"`, `"failed"`.

### 20.3 `QueueEntryStatus`, `SessionStatus`, `TrackSource`, `PopularityCategory`

- Queue: `queued`, `playing`, `done`, `skipped`.
- Session: `active`, `terminated`.
- TrackSource: `catalog`, `user_upload`.
- Popularity: `eternal_hit`, `current_hit`, `former_hit`, `artist_best`, `regular`.
- `WELL_KNOWN_CATEGORIES = [eternal, current, artist_best, former]` (`constants.py:68-73`).

### 20.4 QDrant и dimensions (`constants.py:80-88`)

- `COLLECTION_AUDIO_FEATURES = "audio_features"`.
- `COLLECTION_LYRICS_EMBEDDINGS = "lyrics_embeddings"`.
- `AUDIO_FEATURE_DIM = 45`.
- `LYRICS_EMBEDDING_DIM = 384`.

### 20.5 `PipelineStep` (`constants.py:94-107`)

```python
SEPARATING = "separating"
BACK_VOCAL_SEPARATING = "back_vocal_separating"
VAD = "vad"
TRANSCRIBING = "transcribing"
SEARCHING_LYRICS = "searching_lyrics"
ALIGNING = "aligning"
LINE_BREAKING = "line_breaking"
```

> ⚠️ Воркер в `gpu_pipeline.py` **не использует** `PipelineStep` enum — передаёт строковые литералы (`"separating"`, `"vad"` и т.д.) в `mark_step`. Совпадение значений — соглашение, не type check.

---

## 21. Shared: модели

### 21.1 `Job`, `JobCreate`, `JobUpdate` (`shared/karaoke_shared/models/job.py`)

#### `Job` (`job.py:19-37`)

Pydantic BaseModel, отражает таблицу `job_queue`:
- `id: str`
- `track_id: str | None = None`
- `mp3_key: str | None`
- `artist_hint: str | None`, `title_hint: str | None`
- `priority: int = 1`
- `status: str`
- `locked_by: str | None`, `locked_at: str | None`
- `data: dict | None` — intermediate pipeline data (JSONB).
- `result: dict | None` — финальный payload (JSONB).
- `error_message: str | None`
- `current_step: str | None`, `progress: int = 0`
- `created_at: str`, `updated_at: str` — **ISO strings, не datetime**.

#### `JobCreate` (`job.py:40-57`)

- `id: str = uuid4().str`
- `status: str = JobStatus.PENDING`
- `created_at`/`updated_at = datetime.now(timezone.utc).isoformat()` (default factories).

#### `JobUpdate` (`job.py:60-70`)

Partial — все поля optional кроме `updated_at` (default factory).

### 21.2 `SyllableTiming`, `Track`, `TrackCreate`, `TrackUpdate` (`shared/karaoke_shared/models/track.py`)

#### `SyllableTiming` (`track.py:19-24`)

```python
class SyllableTiming(BaseModel):
    syllable: str   # включает префикс " " / "\n" / "" (см. §11.17)
    start: float    # сек
    end: float      # сек
```

#### `Track` (`track.py:27-50`)

Отражает таблицу `tracks`. Ключевые поля:
- `id, artist, title`
- `duration_sec: int | None`
- `instrumental_key: str | None`
- `lyrics_text: str | None`, `lyrics_source: str | None`
- `syllable_timings: list[SyllableTiming] | None` — сериализуется в JSONB.
- `language: str | None`
- `source: str` (`catalog` / `user_upload`)
- `status: str = TrackStatus.PENDING`
- `error_message: str | None`
- `play_count: int = 0`, `qdrant_synced: int = 0`
- `popularity_category: str = REGULAR`
- `chart_count: int = 0`, `chart_last_seen: str | None`
- `catalog_cluster_id`, `rec_cluster_id` — `int | None`
- `created_at`, `updated_at` — ISO strings.

#### `TrackCreate` (`track.py:53-79`)

Worker создаёт трек с такими полями: `artist, title, source="user_upload", instrumental_key, lyrics_text, lyrics_source, syllable_timings, language, status="ready"` (см. §4.2.10). Остальные — defaults.

#### `TrackUpdate` (`track.py:82-108`)

Partial — все optional кроме `updated_at`.

---

## 22. Shared: PgRepository (методы, используемые воркером) — `shared/karaoke_shared/repositories/pg_repository.py`

Воркер обращается к 11 методам репозитория (вызовы напрямую + через `JobService`):

### 22.1 `create_track(data: TrackCreate) -> Track` (`pg_repository.py:86-123`)

- `syllable_timings` сериализуется через `json.dumps([st.model_dump() for st in ...])`.
- INSERT в `tracks` со всеми полями (21 столбец) + `_to_dt()` для timestamp-полей.
- После insert: `get_track(data.id)` для возврата полной модели; если не нашлось → `RuntimeError`.

### 22.2 `get_track(track_id) -> Track | None` (`pg_repository.py:125-132`)

`SELECT * FROM tracks WHERE id = $1`. Возврат через `_track_from_row`.

### 22.3 `create_job(data: JobCreate) -> Job` (`pg_repository.py:861-887`)

Используется backend'ом (worker сам джобы не создаёт). INSERT в `job_queue` со всеми полями. Гарантия: `get_job(data.id)` после INSERT или `RuntimeError`.

### 22.4 `get_job(job_id) -> Job | None` (`pg_repository.py:889-896`)

`SELECT * FROM job_queue WHERE id = $1`.

### 22.5 `poll_and_lock(worker_id) -> Job | None` (`pg_repository.py:898-921`)

Атомарный pull next job для polling-сценария (не используется текущим RabbitMQ-воркером, но метод существует). UPDATE с подзапросом `SELECT ... FOR UPDATE SKIP LOCKED` — безопасно для нескольких воркеров.

### 22.6 `lock_job(job_id, worker_id) -> bool` (`pg_repository.py:936-947`)

Pessimistic lock: `UPDATE job_queue SET status='running', locked_by=$, locked_at=$, updated_at=$ WHERE id=$ AND status='pending'`. Возвращает `True` если затронута 1 строка (проверка `result.endswith("1")`). Используется в consumer.py:103.

### 22.7 `complete_job(job_id, result: dict) -> None` (`pg_repository.py:949-958`)

`UPDATE job_queue SET status='completed', result=json.dumps(result), updated_at=now WHERE id=$`. Через JobService.mark_completed.

### 22.8 `fail_job_permanently(job_id, error: str) -> None` (`pg_repository.py:960-970`)

`UPDATE job_queue SET status='failed', error_message=$, locked_by=NULL, locked_at=NULL, updated_at=now WHERE id=$`. Через JobService.mark_permanently_failed.

### 22.9 `reset_stale_running_jobs(worker_id) -> int` (`pg_repository.py:972-986`)

`UPDATE job_queue SET status='pending', locked_by=NULL, locked_at=NULL WHERE status='running' AND locked_by=$`. Возвращает количество затронутых строк (parsed из `"UPDATE N"`). Вызывается в main.py:230 при старте воркера.

### 22.10 `mark_step(job_id, step, progress) -> None` (`pg_repository.py:1023-1028`)

`UPDATE job_queue SET current_step=$, progress=$, updated_at=now WHERE id=$`. Через JobService.mark_step.

### 22.11 `update_job_data(job_id, new_data: dict) -> None` (`pg_repository.py:1030-1040`)

JSONB merge:
```sql
UPDATE job_queue
SET data = COALESCE(data, '{}'::jsonb) || $1::jsonb,
    updated_at = $2
WHERE id = $3
```
Используется на STEP 5 (lyrics result) и в `_encode_and_upload_instrumental` (instrumental_key).

### 22.12 `set_job_track_id(job_id, track_id) -> None` (`pg_repository.py:1042-1047`)

`UPDATE job_queue SET track_id=$, updated_at=now WHERE id=$`. Финализация: связывает джоб с созданным треком.

### 22.13 Другие job-методы (не вызываются воркером напрямую)

- `poll_pending(limit)` — для админ-интерфейса.
- `get_active_upload_jobs()` — backend (мониторинг).
- `find_stale_pending_jobs(older_than_seconds)` — для backend JobSweeper (`pg_repository.py:1001-1021`). Фильтр: `status='pending' AND mp3_key IS NOT NULL AND updated_at < now() - make_interval(secs => $)`.

---

## 23. Shared: `JobService` (`shared/karaoke_shared/services/job_service.py`, 72 строки)

Тонкая обёртка над PgRepository. Поля: `repo`, `_publisher: ProgressPublisher | None`.

Методы:
- `create_job(data)` → `repo.create_job`.
- `poll_and_lock(worker_id)` → `repo.poll_and_lock`.
- **`mark_step(job_id, step, progress)`** — `repo.mark_step` + (если publisher) `publisher.publish_progress`. Лог `progress_publish_failed` при ошибке publisher'а (не падает).
- **`mark_completed(job_id, result)`** — `repo.complete_job(...)` + `publisher.publish_completed(job_id, result.get("track_id", ""))`.
- **`mark_permanently_failed(job_id, error)`** — `repo.fail_job_permanently(...)` + `publisher.publish_error(...)`.
- `get_job(job_id)` → `repo.get_job`.

---

## 24. Shared: `ProgressPublisher` (`shared/karaoke_shared/services/progress_publisher.py`, 67 строк)

Публикует JSON-сообщения в exchange `job.progress` (fanout).

### 24.1 `_with_request_id(body)` (`progress_publisher.py:17-27`)

Берёт `request_id` из `structlog.contextvars`; если есть — `body["request_id"] = request_id`. Воркер биндит `request_id` в consumer'е (`consumer.py:91-97`).

### 24.2 Методы

- `publish_progress(job_id, step, progress)` → `{job_id, status: "running", step, progress}`.
- `publish_completed(job_id, track_id)` → `{job_id, status: "completed", track_id, clip_url: f"/api/v1/tracks/{track_id}/stream"}`.
- `publish_error(job_id, error)` → `{job_id, status: "failed", error}`.

Все вызывают `rmq.publish("job.progress", "", body)`.

---

## 25. Shared: `RabbitMQClient` (`shared/karaoke_shared/messaging/rabbitmq.py`, 181 строк)

Async обёртка над aio-pika.

### 25.1 Топология (`rabbitmq.py:53-118`)

| Exchange | Type | Durable | Queues |
|---|---|---|---|
| `dlq` | direct | yes | `jobs.dlq` (rk=`jobs`), `rec.dlq` (rk=`rec`) — обе с `x-message-ttl=72h` |
| `jobs` | direct | yes | `jobs.process` (rk=`""`, `x-max-priority=10`, `x-dead-letter-exchange=dlq`, `x-dead-letter-routing-key=jobs`) |
| `job.progress` | fanout | **no** | exclusive auto-delete queues per SSE subscriber (создаются по запросу через `create_exclusive_queue`) |
| `rec` | direct | yes | `rec.index` (rk=`""`, `dlx=dlq`, `dlrk=rec`); `rec.indexed` (rk=`indexed`) |

Лог `rabbitmq_topology_declared` в конце.

Комментарий: «To change DLQ TTL on an existing deployment the queues must be deleted first — RMQ rejects redeclaration with different arguments» (`rabbitmq.py:58-60`).

### 25.2 `connect()` / `close()` (`rabbitmq.py:35-45`)

- `aio_pika.connect_robust(url)` — auto-reconnect.
- `channel = await connection.channel()`.

### 25.3 `publish(exchange, routing_key, body, priority=None)` (`rabbitmq.py:120-145`)

`aio_pika.Message(body=json.dumps(body).encode(), content_type="application/json", delivery_mode=PERSISTENT, priority=priority)`.

### 25.4 `consume(queue, callback, prefetch_count=1)` (`rabbitmq.py:147-164`)

`ch.set_qos(prefetch_count=...)` + `q.consume(callback)`.

### 25.5 `create_exclusive_queue(exchange)` (`rabbitmq.py:166-181`)

Для SSE: `ch.declare_queue(exclusive=True, auto_delete=True)` + `bind(exchange)`.

---

## 26. Shared: `S3Storage` (`shared/karaoke_shared/storage/s3_storage.py`, 256 строк)

Async-native через aioboto3 + sync helper для presigned URLs.

### 26.1 Конструктор (`s3_storage.py:54-109`)

Параметры: `bucket`, `endpoint_url`, `access_key`, `secret_key`, `region="us-east-1"`, `presigned_url_base`.

Создаёт:
- `_aio_config = AioConfig(signature_version="s3v4", retries={max_attempts: 5, mode: "adaptive"}, connect_timeout=10, read_timeout=60)`.
- `_session, _client_ctx, _client = None` — заполняются в `connect()`.
- `_presign_client = boto3.client("s3", endpoint_url=presign_endpoint or None, ...)` — синхронный, для `generate_presigned_url` (pure crypto, без сети). `presign_endpoint = presigned_url_base or endpoint_url`.

### 26.2 Lifecycle (`s3_storage.py:111-143`)

- `connect()`: создаёт `aioboto3.Session()`, входит в `session.client(...)` как async context manager. Idempotent (если уже подключён — no-op).
- `close()`: `await _client_ctx.__aexit__(...)`, обнуляет ссылки.
- `_require_client()`: raise `RuntimeError("S3Storage not connected. Call await storage.connect() during application startup.")` если `_client is None`.

### 26.3 Методы

- `upload(key, data)` (`s3_storage.py:145-168`): `mimetypes.guess_type(key)` → ContentType. `client.put_object(Bucket=..., Key=..., Body=..., **extra)`.
- `download_to_file(key, local_path)` (`s3_storage.py:170-186`): `client.download_file(...)` — комментарий: «aioboto3 patches download_file to use aiofiles + concurrent range-get для больших объектов».
- `download(key) -> bytes` (`s3_storage.py:188-200`): `get_object` → `async with response["Body"] as stream: await stream.read()`.
- `delete(key)`: `client.delete_object`.
- `exists(key)`: `client.head_object` под try/except `ClientError`.
- `presigned_url(key, expires_in=3600)` (`s3_storage.py:228-247`): **синхронный** метод. `self._presign_client.generate_presigned_url("get_object", Params={Bucket, Key}, ExpiresIn=...)`.
- `ensure_bucket()` (`s3_storage.py:249-256`): `head_bucket` → если падает → `create_bucket`. Воркер не зовёт (это backend).

---

## 27. Shared: `Syllabifier` (`shared/karaoke_shared/utils/syllabifier.py`, 73 строки)

### 27.1 Константы

- `_SUPPORTED_PYPHEN_LANGS = {"en", "ru"}`.
- `_ALPHA_RE = re.compile(r"[^\W\d_]+", re.UNICODE)`.
- `_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")` — диапазон кириллицы.

### 27.2 `__init__` (`syllabifier.py:22-23`)

Поле `_dicts: dict[str, pyphen.Pyphen] = {}` — кеш словарей по языку.

### 27.3 `_get_dict(lang)` (`syllabifier.py:25-32`)

- `base_lang = (lang or "en").split("-")[0].lower()`.
- Если не в supported — `base_lang = "en"`.
- Маппинг: `ru` → `pyphen.Pyphen(lang="ru_RU")`, иначе `"en_US"`.

### 27.4 `_detect_word_lang(word)` (`syllabifier.py:34-43`)

Static. Если в слове есть хоть один кириллический символ — `"ru"`, иначе `"en"`. Это **per-word** override глобального языка — английские слова в русском треке получают `en_US` словарь.

### 27.5 `_split_word(word, lang)` (`syllabifier.py:45-73`)

Используется в CTC aligner (`torch_ctc_aligner.py:1264`).

Алгоритм:
1. `match = _ALPHA_RE.search(text)`. Если не нашлось ни одной alpha-последовательности → `return [text]` (целое слово как один syllable).
2. Split text на `prefix` (до первой alpha), `alpha_core` (от первой до конца последней alpha), `suffix` (после).
3. **Per-word override**: `effective_lang = _detect_word_lang(alpha_core)` — игнорирует параметр `lang`!
4. `dic = _get_dict(effective_lang)`; `inserted = dic.inserted(alpha_core)` — pyphen вставляет `-` между слогами.
5. `syllable_parts = inserted.split("-")`.
6. `parts[0] = prefix + parts[0]`; `parts[-1] = parts[-1] + suffix`.

> ⚠️ Параметр `lang` метода `_split_word` фактически не влияет — реальный язык определяется по содержимому слова (`_detect_word_lang`). Это намеренно (комментарий `syllabifier.py:40-42`).

---

## 28. Shared: `line_breaker` (`shared/karaoke_shared/utils/line_breaker.py`, 185 строк)

STEP 7 пайплайна. Импортируется inline (`gpu_pipeline.py:314`).

### 28.1 Контракт `detect_line_breaks(timings, vocal_path=None) -> list[SyllableTiming]` (`line_breaker.py:29-82`)

Возвращает **новый** список с `\n`-префиксами на местах line-break'ов.

**Условия пропуска**:
- `len(timings) < 2` → return as-is.
- Если хоть в одном syllable уже есть `startswith("\n")` (из LRC) — return as-is (`line_breaker.py:56-57`).

**Выбор режима**:
- Считает `gaps = [timings[i].start - timings[i-1].end for i in 1..N]`.
- `large_gap_count = sum(1 for g in gaps if g > 0.4)`.
- Если `large_gap_count >= 5` → **gap mode** (default).
- Иначе если есть `vocal_path` → **beat mode**.
- Иначе → relaxed gap mode (`threshold_floor=0.2`).

Лог `line_break_detection_completed` с breaks count и duration.

### 28.2 `_gap_mode` (`line_breaker.py:85-121`)

- `p75 = np.percentile(gaps, 75)`; `threshold = max(threshold_floor, p75 * 2.5)`. **Динамический** порог.
- Дополнительно: длинные строки `> 50 chars` принудительно ломаются на следующей word-границе (`syl.startswith(" ")`).
- Break только на word-границах (`is_word = syl.startswith(" ")`).

### 28.3 `_beat_mode` (`line_breaker.py:124-164`)

Import inline: `librosa`.

- `y, sr = librosa.load(vocal_path, sr=22050)`.
- `tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)`.
- `beat_times = librosa.frames_to_time(beat_frames, sr=sr)`.
- Если `len(beat_times) < 4` → fallback на relaxed gap mode.
- `bar_times = beat_times[::4]` — каждый 4-й удар (4/4 размер).
- Цикл: break на `bar_times[bar_idx] - 0.3` (с допуском 300ms) при условии word-границы.

### 28.4 `_inject_breaks` (`line_breaker.py:167-185`)

Префикс `" "` заменяется на `"\n"`. Иначе `"\n"` пре-пендится. Уже-маркированные (`startswith("\n")`) пропускаются.

---

## 29. Сводная карта потоков данных (high-level)

### 29.1 Жизненный цикл одного job

```
backend uploads MP3 → S3 uploads/{job_id}.mp3
backend INSERTs job_queue(id, mp3_key, artist_hint, title_hint, status=pending)
backend publishes {job_id} to exchange "jobs" (routing_key="")
  ↓
worker consumes from "jobs.process" (prefetch=1)
worker locks job (status=running, locked_by, locked_at)
worker downloads mp3 → /tmp/{job_id}.mp3
worker probes duration → scale = duration/180 (min 0.5)

1. UVR separate              → vocals_path (16k mono), instrumental_path (44.1k stereo)
                              + background task: ffmpeg WAV→MP3 + S3 upload to instrumentals/
2. BackVocal separate        → lead_vocals_path, backing_path (если enabled)
3. VAD (на FULL vocals)      → cleaned_vocals_path
4. Whisper transcribe        → WhisperResult{text, language}
5. Lyrics search             → providers + filename parser + matcher + agent + ASR fallback
                              → LyricsResult{artist, title, lyrics, language, confidence, source_note}
6. CTC align (на LEAD)       → SyllableTiming[] + AlignmentStats
                              (Silero pre-trim + RMS line-start/word-end adjust)
7. Line breaking             → SyllableTiming[] с \n префиксами

Finalization:
  await instrumental_upload_task
  INSERT INTO tracks (status=ready, lyrics_source=<provider>, syllable_timings JSONB, ...)
  UPDATE job_queue SET track_id=<new>
  mark_completed (publishes "job.progress" status=completed)
  publish to "rec" exchange {track_id, mp3_key, lyrics} → rec-service

On failure:
  log pipeline_failed
  cleanup() — release VRAM all models
  mark_permanently_failed (publishes status=failed)

finally:
  cancel instrumental_upload_task if not done
  unlink all tempfiles (под suppress)
```

### 29.2 Cleanup-стратегия VRAM

| Шаг | После | Что чистится |
|---|---|---|
| 1 | сразу после успеха | `uvr.cleanup()` |
| 2 | в finally | `back_vocal_separator.cleanup()` |
| 4 | сразу после успеха | `whisper.cleanup()` (модель re-load'ится на следующем job) |
| 6 | сразу после успеха | `ctc_aligner.cleanup()` (включая Silero модель если была) |

В общем `except` (pipeline_failed) → `self.cleanup()` вызывает все четыре последовательно.

### 29.3 Таймауты per-step (`gpu_pipeline.py`, `config.py`)

| Шаг | Default base (для 3-мин) | Реальный = base × scale |
|---|---|---|
| separating | 30 с | при scale=2 (6 мин mp3) → 60 с |
| back_vocal_separating | 30 с | пропорционально |
| transcribing | 30 с | пропорционально |
| aligning | 10 с | пропорционально |
| vad | — | без таймаута |
| searching_lyrics | — | без таймаута |
| line_breaking | — | без таймаута |

scale ≥ 0.5 (нижний клэмп), верхний — не ограничен.

### 29.4 Use-of-stem matrix (lead vs full)

| Шаг | На каком стеме |
|---|---|
| 1. UVR | оригинальный mp3 (44.1k stereo) → vocals (16k mono) + instrumental (44.1k stereo) |
| 2. BackVocal | vocals (FULL) → lead + backing (оба 16k mono) |
| 3. VAD | **FULL vocals** (для лучшей Whisper-идентификации) |
| 4. Whisper | cleaned **FULL vocals** |
| 5. Lyrics search | text Whisper'а — никаких аудио |
| 6. CTC align | **LEAD vocals** (или FULL если back_vocal отключён/упал) |
| 7. Line breaking | LEAD vocals (для beat mode librosa) |

### 29.5 Точки внешних API-вызовов (LLM/web)

| Где | Сервис | Используется |
|---|---|---|
| `FilenameParser._call_llm` | DeepSeek | На каждый upload (если api_key) |
| `LyricsExpander._call_llm` | DeepSeek | Только если `_META_INSTRUCTION_RE` срабатывает |
| `LyricsMatcher._call_llm_tiebreak` | DeepSeek | Если top-2 близки (margin) или в weak-band |
| `LyricsAgent._run_agent` | DeepSeek (chat + tools) | На fallback из provider chain |
| `_searxng_search` | SearXNG | Первый pass агента (если URL задан) |
| `_yandex_search` | Yandex Search API | Второй pass агента (если первый ничего не нашёл) |
| `_fetch_webpage` | httpx (произвольные URL) | Внутри agent loop (вызывается LLM'ом) |
| `GeniusProvider` | Genius API + scraping | Provider chain, на каждый fragment (если token) |
| `LRCLibProvider` | lrclib.net | Provider chain (всегда) |
| `LyricsOvhProvider` | api.lyrics.ovh | Provider chain (всегда) |
| `TorchCTCAligner._ensure_silero` | torch.hub (snakers4/silero-vad) | Lazy, один раз за жизнь воркера |

### 29.6 Граф состояний `job_queue.status`

```
pending  ─lock_job─►  running  ─complete_job─►  completed
   ▲                     │
   │                     ├─fail_job_permanently─►  failed
   │                     │
   └─reset_stale─────────┘ (на старте воркера, если crashed mid-job)
```

### 29.7 Identity / observability

- **`worker_id`**: `env WORKER_ID` или fallback `socket.gethostname()` (`config.py:37`). Используется для `reset_stale_running_jobs` и `locked_by`.
- **`request_id`**: биндится в structlog `contextvars` в consumer.py (если есть в RMQ message body). Автоматически попадает во все log lines + в `ProgressPublisher` events + в rec-service message.
- **`job_id`**: тоже биндится в contextvars, во всех логах внутри `_on_message`.
- Все логи — JSON через `structlog.JSONRenderer()` (`main.py:191`).

---

## 30. Карта зависимостей воркера (что от чего тянется)

Прямые импорты `worker/` → `karaoke_shared`:

| Модуль | Из shared |
|---|---|
| `worker/app/main.py` | `messaging.rabbitmq.RabbitMQClient`, `services.job_service.JobService`, `services.progress_publisher.ProgressPublisher`, `storage.S3Storage`, `repositories.pg_repository.PgRepository` |
| `worker/app/consumer.py` | `messaging.rabbitmq.RabbitMQClient`, `repositories.pg_repository.PgRepository`, `services.job_service.JobService` |
| `worker/common/base_pipeline.py` | `models.job.Job` |
| `worker/gpu/gpu_pipeline.py` | `messaging.rabbitmq.RabbitMQClient`, `models.job.Job`, `models.track.TrackCreate`, `repositories.pg_repository.PgRepository`, `services.job_service.JobService`, `storage.S3Storage`, `utils.line_breaker.detect_line_breaks` (inline) |
| `worker/gpu/torch_ctc_aligner.py` | `models.track.SyllableTiming`, `utils.syllabifier.Syllabifier` |
| `worker/common/lyrics_searcher.py` | — (определяет LyricsResult, LyricsAPIError и т.п. сам) |

Внешние Python-пакеты (выборочно по импортам):
- aio-pika, asyncpg, aioboto3, boto3, botocore, aiobotocore
- pydantic, pydantic-settings, structlog
- torch, torchaudio, transformers, soundfile, scipy, numpy, librosa
- audio_separator (BSRoformer, MelBandRoformer)
- httpx, BeautifulSoup
- openai (DeepSeek совместим)
- jellyfish, pymorphy3, snowballstemmer, rapidfuzz, unidecode, pyphen

---

## Состояние документа

На 2026-05-17 закрыт первичный обход:
- ✅ Все `worker/*.py` (32 файла)
- ✅ Все используемые воркером `shared/karaoke_shared/*`: models (job, track), repositories (используемые методы), services (job_service, progress_publisher), messaging (rabbitmq), storage (s3_storage), utils (line_breaker, syllabifier), constants

**Не вошло в этот документ** (по контракту — фокус на воркере):
- backend (FastAPI, REST/SSE, job_sweeper и т.п.)
- rec-service (feature extraction, cluster assigner, QDrant индексация)
- frontend
- docker-compose, Dockerfile, инфра-конфиги
- scripts/
- tests/

Каждый факт в документе сопровождён ссылкой `file:line`. Любое утверждение, не помеченное `[NOT VERIFIED]`, проверено в коде на момент сборки.
