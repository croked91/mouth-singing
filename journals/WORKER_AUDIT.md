# WORKER_AUDIT — соответствие реализации воркера промышленным стандартам

Аудит проведён 2026-05-14 на основании `journals/WORKER_FACTS.md` (фактологическая карта) и прицельной проверки кода. Каждый пункт — несоответствие или отступление от типичных промышленных практик с указанием `file:line`, причины и потенциального риска.

**Категории:** надёжность / observability / тестируемость / ресурсы и производительность.

> Этот документ содержит оценочные суждения. Источник «голых фактов без оценок» — `journals/WORKER_FACTS.md`. Здесь — следующий слой: интерпретация фактов сквозь призму типичных требований к долгоживущему сервису (24/7 GPU-воркер, обработка пользовательских задач, multi-instance scaling).

---

## 1. Надёжность и обработка ошибок

### 1.1. Утечка временных файлов при сбое пайплайна
**Где:** `worker/gpu/gpu_pipeline.py:255-267` (success-cleanup vocals/lead/backing/cleaned), `:327-328` (success-cleanup local_mp3 + instrumental_path), `:330-339` (except-блок).
**Что:** удаление всех 5 временных файлов (`/tmp/{job_id}.mp3`, `instrumental.wav`, `vocals.wav`, `lead/backing.wav`, `cleaned_vocals_*.wav`) находится **внутри** try-блока, до `except Exception`. Любое падение между шагами 1 и 16 оставляет от 1 до 5 файлов в `/tmp` и `{media_root}/instrumental` навсегда.
**Причина несоответствия:** отсутствует `finally`-блок или `tempfile.TemporaryDirectory` контекст; cleanup путей размазан по success-ветке.
**Риск:** при стабильном потоке отказов диск контейнера заполняется тихо.

### 1.2. Background-task инструментала не отменяется при сбое
**Где:** `worker/gpu/gpu_pipeline.py:115-122` (`asyncio.create_task(_encode_and_upload_instrumental(...))`), `:354-402` (тело таски).
**Что:** задача стартует сразу после UVR и не сохраняется как `self._tasks` / не отменяется в except-блоке. При падении пайплайна на шаге 4-7 эта таска продолжает крутиться, делает `update_job_data` уже permanently-failed-задачи и заливает инструментал для трека, который никогда не будет создан.
**Причина:** «fire-and-forget» pattern без отмены; таска привязана к успеху финализации (`await instrumental_upload_task`), но не к её отсутствию.
**Риск:** orphan-объекты в S3 (`instrumentals/{job_id}.mp3` без соответствующего трека) и misleading-state в `job_queue.data`.

### 1.3. ffmpeg/ffprobe сбои проглатываются молча
**Где:** `worker/gpu/gpu_pipeline.py:362-381` (ffprobe в `try/except: pass`), `:384-397` (ffmpeg, `proc.wait()` без проверки `returncode`).
**Что:** падение ffprobe тихо откатывается на дефолт `192k`. Падение ffmpeg оставляет несуществующий файл, и следующий `open(instrumental_mp3, "rb")` упадёт с `FileNotFoundError` — но уже без указания, что виноват именно ffmpeg.
**Причина:** subprocess без проверки кода выхода и без логирования stderr (последний явно перенаправлен в DEVNULL).
**Риск:** диагностика «почему сломался instrumental upload» требует ручного воспроизведения.

### 1.4. Двойная система отказоустойчивости при работающей одной
**Где:** `shared/karaoke_shared/repositories/pg_repository.py:960-981` (`fail_job` с attempts++), `worker/app/consumer.py:101-104` (`nack(requeue=False)`).
**Что:** в БД-схеме `job_queue` живут поля `attempts`/`max_attempts=3` для retry, есть метод `fail_job` с инкрементом. Но в runtime-пайплайне на любую ошибку вызывается `mark_permanently_failed` (status=FAILED безусловно) + `nack(requeue=False)` (DLQ). БД-уровневый retry никогда не запускается.
**Причина:** механизм оставлен от legacy DB-poll-режима, новая RabbitMQ-схема его не использует.
**Риск:** мёртвый код вводит в заблуждение читателя кода («у нас же есть retry!»); фактически любая транзиентная ошибка → permanent fail.

### 1.5. `reset_stale_running_jobs` не работает после restart контейнера
**Где:** `shared/karaoke_shared/repositories/pg_repository.py:995-1010`, `worker/app/main.py:219-221`, `worker/app/config.py` (`worker_id = f"{hostname}-{os.getpid()}"`).
**Что:** метод сбрасывает RUNNING-задачи только этого `worker_id`. После полного рестарта контейнера PID меняется → `worker_id` другой → старые залоченные задачи остаются в RUNNING навсегда. `lock_job` их не заберёт (`status='pending'` не выполнится).
**Причина:** `worker_id` зависит от ephemeral-PID, а recovery-фильтр требует стабильности.
**Риск:** при единичном краше — зависшие задачи требуют ручного SQL-вмешательства; UI-индикация прогресса по такой задаче никогда не сменится.

### 1.6. Битый JSON в сообщении не залогируется корректно
**Где:** `worker/app/consumer.py:75, 100`.
**Что:** при `json.loads(message.body)` падении переменная `body` не существует, но в except используется `body.get("job_id", "unknown") if 'body' in dir() else "unknown"` — `'body' in dir()` всегда `True` в локальном scope (имя присвоено в `try`), что приведёт к `NameError` внутри логирования и потере диагностики.
**Причина:** некорректная проверка существования переменной (нужно `'body' in locals()`).
**Риск:** на битом JSON логирование упадёт; сообщение пойдёт в DLQ без `job_id`-корреляции.

### 1.7. Бесконечная re-delivery при job_lock_failed
**Где:** `worker/app/consumer.py:80-83`.
**Что:** при `lock_job → False` делается `nack(requeue=True)`. RabbitMQ может быстро вернуть это же сообщение тому же воркеру (особенно если он единственный) → tight loop. Нет cooldown / dead-letter-after-N-retries.
**Причина:** простое `requeue=True` без счётчика повторов.
**Риск:** CPU spin при race с другим воркером, который успел залочить раньше; в логах — постоянный `job_lock_failed`.

### 1.8. SearXNG язык поиска захардкожен на `ru`
**Где:** `worker/common/lyrics_agent.py:139-167`.
**Что:** `_searxng_search` использует `language=ru` константой. Для англоязычного контента SearXNG будет вторично сортировать русскоязычные результаты выше → деградация релевантности.
**Причина:** однозначно русский дефолт без передачи `detected_language` от Whisper.
**Риск:** для не-русских песен агентский fallback хуже работает. Для production с многоязычным контентом — узкое место.

### 1.9. CPU-fallback UVR использует приватные атрибуты
**Где:** `worker/gpu/gpu_pipeline.py:391-409` (`_separate_with_fallback`).
**Что:** при OOM пересоздаёт `UVRSeparator` через `self.uvr._model_name`, `self.uvr._overlap` (доступ к приватным полям другого объекта). Если `UVRSeparator` переименует поле — fallback развалится молча, без warning'а.
**Причина:** fallback вынесен в `gpu_pipeline.py` вместо метода `UVRSeparator.fallback_to_cpu()`.
**Риск:** хрупкая coupling; рефакторинг сепаратора может тихо сломать OOM-recovery.

### 1.10. Нет таймаута на CTC alignment в runtime
**Где:** `worker/gpu/torch_ctc_aligner.py:151-252` (`align`), `worker/gpu/gpu_pipeline.py:218-225` (вызов).
**Что:** legacy `CTCAligner` (subprocess) имел `subprocess.run(timeout=300)`. `TorchCTCAligner` крутится in-process без таймаута. Зависший CUDA-kernel или pathological lyrics → задача висит до перезапуска контейнера.
**Причина:** PyTorch in-process не отменяется через `asyncio.timeout` (отмена не доходит до `to_thread`).
**Риск:** один зависший трек блокирует worker (`prefetch_count=1`); требуется external watchdog или per-step timeout на уровне `asyncio.wait_for(asyncio.to_thread(...))`.

### 1.11. S3 без явной retry-конфигурации
**Где:** `shared/karaoke_shared/storage/s3_storage.py:54-76`.
**Что:** `boto3.client("s3", config=Config(signature_version="s3v4"))` без `retries={"max_attempts": ..., "mode": "adaptive"}`. Используется boto3 default (5 попыток standard mode).
**Причина:** retry-policy не специфицирована.
**Риск:** при кратковременной недоступности MinIO одна сетевая ошибка может убить задачу; явная конфигурация дала бы детерминированное поведение и наблюдаемость.

---

## 2. Observability

### 2.1. Прогресс-репорты по 2 события на шаг (без промежуточных значений)
**Где:** `worker/gpu/gpu_pipeline.py` — все `mark_step("...", 0)` / `mark_step("...", 100)`.
**Что:** SSE-канал получает 12 (или 14) событий на задачу. Долгие шаги (UVR на 5-минутном треке ≈ 30-60с, CTC ≈ 10с) выглядят «зависшими» 0% → 100%. Внутри моделей нет колбэков.
**Причина:** нет плотного reporting (UVR обрабатывает по чанкам — мог бы репортить `done_chunks/total_chunks`).
**Риск:** UX-индикатор прогресса бесполезен для диагностики «висит или работает»; невозможно отличить медленный шаг от зависшего.

### 2.2. Логирование полных текстов кандидатов и ASR
**Где:** `worker/common/lyrics/matching/matcher.py:87` (`cand_lyrics=exp_text` в `matcher_features`), `worker/gpu/whisper_transcriber.py:193` (`text=text` в `whisper_completed`).
**Что:** на каждый трек в логах оказывается до 5 копий полного текста песни (по числу кандидатов) + полный ASR-вывод. Для 200-строчной песни это десятки KB/трек.
**Причина:** диагностически удобно, но без флага «verbose» — постоянно.
**Риск:** диск контейнера в production заполняется быстрее ожидаемого; экспорт логов в централизованное хранилище становится дорогим.

### 2.3. Отсутствие метрик (Prometheus/OpenTelemetry)
**Где:** проверено grep'ом — `metric|prometheus|otel|opentelemetry|StatsD` нет нигде в `worker/` и `shared/karaoke_shared/`.
**Что:** наблюдаемость только через JSON-логи. Нет: гистограммы duration по шагам пайплайна, счётчик lyrics_source распределений, размер DLQ, model-load duration, GPU memory.
**Причина:** изначально не закладывалось.
**Риск:** SLO/SLA расчёт по логам — дорого и неточно; алертинг возможен только через post-processing логов.

### 2.4. Двойная запись об одной ошибке lyrics
**Где:** `worker/gpu/gpu_pipeline.py:200-204`.
**Что:** при `LyricsSearchError` сначала `logger.error("lyrics_search_failed", ...)`, затем `mark_permanently_failed` (который сам публикует error-event и пишет в БД). Одна ошибка → 3 записи: error log + БД UPDATE + SSE event.
**Причина:** `logger.error` дублирует то, что и так попадёт в `pipeline_failed` через общий except-handler (если бы он сработал — но `mark_permanently_failed` возвращается без re-raise).
**Риск:** noise в логах; пересчёт error rate по логам даёт удвоенные числа.

### 2.5. `WhisperResult.confidence` собирается, но никем не наблюдается
**Где:** `worker/gpu/whisper_transcriber.py:211-223` (расчёт), grep по `confidence` в `worker/gpu/gpu_pipeline.py` — не используется.
**Что:** дорогостоящий per-token log-softmax считается, но не логируется и не сохраняется в `tracks`. Информация для качественной отладки («низкая уверенность Whisper → проверить vocals») потеряна.
**Причина:** забытая часть API; не подключена к downstream.
**Риск:** невозможность объяснить «почему этот трек плохо распознался» ретроспективно.

### 2.6. Нет correlation-id за пределами `job_id`
**Где:** все `logger.*` вызовы.
**Что:** `job_id` корректно прокидывается, но нет request-id от backend (например, для трассировки «пользователь открыл UI → upload → enqueue → worker → SSE»). Невозможно сшить worker-логи с backend-логами по конкретному пользовательскому действию.
**Причина:** message body содержит только `job_id`, без correlation-context.
**Риск:** инцидент-расследование на стыке backend↔worker требует ручной кросс-корреляции по timestamp.

---

## 3. Тестируемость и тесты

### 3.1. Тривиальные тесты-заглушки для критичных компонентов
**Где:** `tests/worker/test_vad_processor.py` (14 строк, 1 тест на `__init__`-аргумент), `tests/worker/test_whisper_transcriber.py` (14 строк, 1 тест на dataclass).
**Что:** оба файла фактически проверяют только Python-механику присваивания, не алгоритм. `VADProcessor.process()` (RMS-фреймы, threshold, маска→интервалы, конкатенация — ~80 строк логики) не покрыт ни одним тестом.
**Причина:** GPU/большие зависимости трудно мокать → ставится «декоративный» smoke-test.
**Риск:** регрессия в RMS-расчёте или интервал-маппинге пройдёт через CI незамеченной.

### 3.2. Отсутствие интеграционных тестов пайплайна
**Где:** `tests/worker/` — нет ни одного теста уровня «mp3 → проверка результата».
**Что:** `GpuPipeline.process()` (430 строк, 7 шагов, async-композиция, обработка ошибок) тестируется только косвенно через unit-тесты компонентов. Нет тестов на: race между success/failure, корректность cleanup при exception, отмена background-task'и, корректность последовательности `mark_step`.
**Причина:** требует heavy fixture (контейнер с PG+RMQ+S3) либо тщательного мокинга.
**Риск:** баги в orchestration-слое (как 1.1, 1.2 выше) ловятся только в production.

### 3.3. Нет тестов для consumer и messaging
**Где:** `worker/app/consumer.py`, `shared/karaoke_shared/messaging/rabbitmq.py` — не покрыты.
**Что:** `_on_message` (lock → process → ack/nack семантика), declare_topology (4 exchange + 5 queues + DLX), handle reconnect — без тестов. Issue 1.6 (битый JSON) и 1.7 (job_lock_failed loop) обнаружились бы юнит-тестами с мок-message.
**Причина:** aio_pika требует тестов с моком или реальным брокером.
**Риск:** изменения в топологии не валидируются автоматически; semantics ack/nack может «съехать» при рефакторинге.

### 3.4. Концентрация тестов в одной зоне
**Где:** `tests/worker/test_lyrics_matcher.py` (44 теста, 709 строк), `test_lyrics_agent.py` (40 тестов, 558 строк), `test_torch_ctc_aligner_adjustments.py` (26 тестов, 573 строки) — суммарно ~74% строк тестов.
**Что:** глубоко покрыты matcher и aligner-adjustments (наиболее «вычислительные» алгоритмы). Не покрыты: провайдеры лирики (`genius/lrclib/lyricsovh`), `filename_parser`, `expander`, `fragments`, `line_breaker`, `syllabifier`, `S3Storage`, `JobService`, `ProgressPublisher`, `PgRepository`-методы для job_queue.
**Причина:** покрытие складывалось ad-hoc, нет coverage-цели.
**Риск:** «дешёвые» куски (HTTP-провайдеры) могут содержать баги (валидация ответов, ошибки парсинга), которые проявятся только при смене API.

---

## 4. Управление ресурсами и производительность

### 4.1. Whisper выгружается после каждой задачи (re-load each job)
**Где:** `worker/gpu/whisper_transcriber.py:238-256` (`cleanup`), `worker/gpu/gpu_pipeline.py:164` (`asyncio.to_thread(self.whisper.cleanup)` после каждой задачи), `transcribe()` re-loads if `_model is None`.
**Что:** между задачами модель `del`-ится, `torch.cuda.empty_cache()`, потом снова `WhisperForConditionalGeneration.from_pretrained` → `.to(device)`. Hot-cache HF, но всё равно расход времени и I/O.
**Причина:** освобождение VRAM выбрано в пользу «всегда есть место для UVR/CTC». Однако `whisper-tiny` ≈ 150 MB; проиграть его за свободу VRAM — спорно.
**Риск:** добавочные ~1-2 сек к каждой задаче на бесполезной работе. Под нагрузкой N задач/мин → суммарно заметно.

### 4.2. Нет lifecycle policy для S3 `uploads/`
**Где:** `worker/gpu/gpu_pipeline.py:99` (download), `shared/karaoke_shared/storage/` — нет `lifecycle_*` методов; `make` файлы / docker-compose — нет конфигурации.
**Что:** оригинальные MP3 в `uploads/{job_id}.mp3` остаются в MinIO навсегда после успешной/неуспешной обработки. Per-track usage никем не обрабатывается.
**Причина:** cleanup-этап не реализован; MinIO lifecycle не настроен.
**Риск:** линейный рост занятого места, со временем — переполнение bucket.

### 4.3. boto3 синхронный, узкое место на thread pool
**Где:** `shared/karaoke_shared/storage/s3_storage.py` — все методы `await asyncio.to_thread(self._client.<sync_call>)`.
**Что:** под нагрузкой ≥ дефолтных ~32 потоков asyncio-loop'а параллельные S3-операции начнут стоять в очереди. У воркера это пока не критично (`prefetch_count=1`), но если параметр поднять или добавить параллельные подзагрузки — bottleneck.
**Причина:** выбран синхронный SDK; альтернатива (`aioboto3`) даёт нативный async.
**Риск:** масштабирование multi-instance бутылочное горлышко на S3 I/O thread pool.

### 4.4. `pymorphy3` singleton без thread-safety
**Где:** `worker/common/lyrics/matching/linguistics.py` — `@lru_cache(maxsize=1)` поверх `MorphAnalyzer()`.
**Что:** `lru_cache` потокобезопасен на уровне Python (GIL), но первый вызов из конкурентных контекстов может инициализировать дважды (см. CPython issue с lru_cache и race). Нагрузка на GIL во время инициализации (~2с) блокирует loop.
**Причина:** ленивая инициализация, без явного `__init__`-вызова в pipeline build.
**Риск:** первая русскоязычная задача после старта блокирует event-loop на ~2с (latency hit). Решается eager-init в `_build_gpu_pipeline`.

### 4.5. Silero VAD никогда не выгружается
**Где:** `worker/gpu/torch_ctc_aligner.py:316-347` (lazy load), `:254-263` (`cleanup` — del MMS, но не Silero).
**Что:** Silero VAD-модель грузится через `torch.hub.load`, занимает ~2-5 MB VRAM/RAM, висит до конца жизни процесса.
**Причина:** забыли в cleanup; не критично по объёму, но архитектурно непоследовательно.
**Риск:** низкий (мало памяти), но мешает «полному релизу» VRAM при OOM-fallback.

### 4.6. DLQ растёт неограниченно
**Где:** `shared/karaoke_shared/messaging/rabbitmq.py:53-108` — `jobs.dlq` и `rec.dlq` объявлены без `x-message-ttl` или `x-max-length`.
**Что:** failed-задачи накапливаются в DLQ навсегда. Нет ни ретеншена, ни ручного процесса очистки.
**Причина:** RMQ default — durable queue без лимитов.
**Риск:** RMQ disk usage растёт; recovery-операции на DLQ становятся медленнее.

### 4.7. `_fetch_webpage` не валидирует Content-Type
**Где:** `worker/common/lyrics_agent.py:283-302`.
**Что:** httpx.get без проверки Content-Type. Если LLM попросит загрузить PDF/бинарь, BeautifulSoup отработает на нём (вернёт мусор), который попадёт в LLM-контекст и потратит токены.
**Причина:** недостаёт guard `if "text/html" not in resp.headers.get("content-type", "")`.
**Риск:** LLM-стоимость и латентность вверх; редкие провалы агента из-за бессмысленного контента.

### 4.8. UVR без OOM-fallback на CUDA-side ошибках другого характера
**Где:** `worker/gpu/gpu_pipeline.py:391-409`.
**Что:** ловятся только `RuntimeError` с подстроками `"out of memory"` / `"cuda"`. Другие RuntimeError (например, кончился shared memory `cudaErrorAssert`) пробрасываются → permanent fail.
**Причина:** узкая семантика поиска подстроки.
**Риск:** транзиентные GPU-проблемы, не подпадающие под OOM-фразу, попадают сразу в DLQ; recovery нет.

---

## Сводный вердикт

| Категория | Найдено пунктов | Критично | Средне | Низко |
|---|---|---|---|---|
| Надёжность | 11 | 1.1, 1.2, 1.5, 1.10 | 1.3, 1.4, 1.6, 1.7, 1.11 | 1.8, 1.9 |
| Observability | 6 | — | 2.1, 2.2, 2.3, 2.6 | 2.4, 2.5 |
| Тестируемость | 4 | 3.2 | 3.1, 3.3 | 3.4 |
| Ресурсы | 8 | 4.2 | 4.1, 4.3, 4.6 | 4.4, 4.5, 4.7, 4.8 |

**Самые приоритетные на устранение** (по соотношению «риск × лёгкость фикса»):
1. **1.1 + 1.2** — `try/finally` + `task.cancel()` в except-блоке `gpu_pipeline.process()`.
2. **3.2** — добавить хотя бы один integration-тест уровня «job → completed track» (с моком моделей).
3. **2.2** — флаг `verbose_logs` для отключения полных текстов кандидатов в production.
4. **4.2** — настроить MinIO lifecycle policy на `uploads/*` (без кода).
5. **1.5** — стабильный `worker_id` через ENV-переменную (деплой-time), а не PID.

Остальные пункты — материал для постепенного улучшения и/или для соответствующих глав ВКР про эволюцию подсистемы.
