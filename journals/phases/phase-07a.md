## Фаза 7a: Audio Worker — JobService + UVR сепаратор

### Входные артефакты
- Результат Фаз 3-6 (работающий backend с сессиями, треками, поиском)
- `journals/ARCHITECTURE.md` — раздел 3.6 «JobService», раздел 3.7 «AudioPipeline» (шаг 1), раздел 7.1 «Online Pipeline», Container Diagram (Worker)
- `journals/ADR.md` — ADR-007 (без GPU, CPU-only)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `python-developer` задачу на создание Audio Worker — отдельного сервиса, который берёт задачи из SQLite job_queue и обрабатывает их. В этой фазе реализуется только первый шаг пайплайна (UVR разделение вокала и инструментала). Полная интеграция с Sonoix и видеогенерацией — в Фазе 7b.

#### Подагент `python-developer`
Реализует воркер и первый шаг аудио-пайплайна:

1. **JobService** (`shared/karaoke_shared/services/job_service.py` — в shared, т.к. используется и backend, и worker):
   - `create_job(track_id, priority=1) -> Job`
   - `poll_pending(limit=1) -> list[Job]` — берёт задачу с pessimistic lock
   - `mark_running(job_id, worker_id)` — блокирует задачу
   - `mark_step(job_id, step: str, progress: int)` — обновляет текущий шаг
   - `mark_completed(job_id, result: dict)`
   - `mark_failed(job_id, error: str)` — если attempts < max_attempts → status=pending (retry), иначе status=failed
   - Retry: до 3 попыток с экспоненциальной задержкой (2, 4, 8 сек)

2. **Worker process** (`worker/app/main.py`):
   - asyncio event loop
   - JobPoller: каждые 2 секунды делает poll_pending
   - При получении задачи → запускает AudioPipeline
   - Graceful shutdown по SIGTERM/SIGINT

3. **UVRSeparator** (`worker/app/pipeline/uvr_separator.py`):
   - Обёртка над `audio-separator` с моделью `UVR-MDX-NET-Voc_FT.onnx` (CPU)
   - `separate(mp3_path) -> (vocals_path, instrumental_path)` — разделяет на вокал и инструментал
   - Результат сохраняется в `MEDIA_ROOT/instrumental/` и temp-директорию для вокала

4. **AudioPipeline** (`worker/app/pipeline/audio_pipeline.py`):
   - Пока реализует только шаг 1 (UVR). Шаги 2-6 — заглушки с TODO, будут реализованы в 7b и 8a.
   - Обновляет статус Job на каждом шаге через JobService.mark_step()

5. **Entrypoint** (`worker/entrypoint.sh`):
   - Проверяет наличие модели UVR в `MODEL_CACHE_DIR`
   - Если модели нет → скачивает UVR-MDX-NET-Voc_FT.onnx (~170 MB)
   - Запускает worker

6. **worker/Dockerfile**: Python 3.12, ffmpeg, audio-separator, ENTRYPOINT → entrypoint.sh

7. **Обновление docker-compose.yml**: добавить сервис `worker` с зависимостью от backend и qdrant, volumes (sqlite_data, media_data, models_data).

#### Подагент `polyglot-test-engineer`
Тесты:
- Worker стартует, проверяет наличие UVR модели
- Создание Job → worker берёт задачу → UVR разделяет MP3 → vocals.wav + instrumental.wav существуют
- Job status прогрессирует: pending → running (step=separating) → completed
- Retry при сбое (mock ошибки UVR → attempts инкрементируется, задача возвращается в pending)

#### Пользователь
Проверяет, что worker корректно обрабатывает тестовый MP3. Подтверждает или вносит замечания.

### Выходные артефакты
- `JobService` в shared/ (переиспользуется backend и worker)
- Worker с asyncio поллером
- `UVRSeparator` — разделение вокала/инструментала на CPU
- `AudioPipeline` с шагом 1 (остальные — заглушки)
- `worker/Dockerfile` + entrypoint с автозагрузкой модели
- Обновлённый `docker-compose.yml` с worker-сервисом
- `docker build ./worker` проходит
- Коммит

