## Фаза 13: Bootstrap CLI

### Входные артефакты
- Результат Фаз 3-8b (backend + worker с полным пайплайном)
- `journals/ARCHITECTURE.md` — раздел 3.8 «BootstrapCLI», раздел 7.2 «Bootstrap Pipeline», раздел 8 «Структура проекта» (bootstrap/)
- `journals/ADR.md` — ADR-002 (WhisperX для бутстрапа), ADR-007 (CPU-only), ADR-009 (lrc-lib дамп)
- `shared/` пакет: repositories, models, FeatureExtractor, LyricEmbedder

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `python-developer` задачу на создание Bootstrap CLI — утилиты для массовой обработки 5000-10000 треков. CLI переиспользует компоненты из shared/ (repositories, models, FeatureExtractor, LyricEmbedder) и worker/ (UVR, VideoGenerator). ASR — через WhisperX (CPU-only, бесплатный), а не Sonoix. Тексты берутся из локального дампа lrc-lib. ML-аспекты (WhisperX настройка) делегируются `ml-sota-expert`.

#### Подагент `python-developer`
Создаёт Bootstrap CLI:

1. **CLI** (`bootstrap/app/cli.py`):
   - На базе `typer` (или argparse)
   - Аргументы: `--input-dir` (директория с MP3), `--workers` (число процессов, default=CPU-1), `--lrclib-dump` (путь к дампу lrc-lib), `--language` (ru/en, default=ru), `--output-dir` (куда класть результаты)
   - Прогресс: `tqdm` прогресс-бар
   - Ошибки: логируются в `bootstrap_errors.log`, не останавливают весь процесс

2. **LRCLibDump** (`bootstrap/app/pipeline/lrclib_dump.py`):
   - Импорт дампа lrc-lib в временную SQLite-базу
   - `search(artist: str, title: str) -> str | None` — ищет LRC текст по artist+title
   - Парсинг LRC формата → построчные тайминги `[{text, start_ms, end_ms}]`
   - После завершения бутстрапа дамп и временная база могут быть удалены (ADR-009)

3. **WhisperXTranscriber** (`bootstrap/app/pipeline/whisperx_transcriber.py`):
   - CPU-only WhisperX
   - Два режима:
     a. Force-align: если текст из LRC найден → `whisperx.align(audio, text)` → word timestamps → Syllabifier
     b. Full transcription: если текста нет → `whisperx.transcribe(audio)` с `word_timestamps=True` → Syllabifier
   - Модель: `large-v3` (или `medium` для баланса скорость/качество — решает ml-expert)

4. **Syllabifier** (`shared/karaoke_shared/utils/syllabifier.py` — уже создан в 7b, переиспользуется):
   - pyphen (ru_RU + en_US)
   - word timestamps → syllable timestamps через пропорциональное деление

5. **BootstrapRunner** (`bootstrap/app/bootstrap_runner.py`):
   - `multiprocessing.Pool(N)` для параллельной обработки
   - Для каждого трека:
     1. UVR разделение (из shared/worker)
     2. Поиск LRC в дампе → WhisperX force-align или full transcription
     3. VideoGenerator (из worker/)
     4. FeatureExtractor + LyricEmbedder (из shared/)
     5. SQLite INSERT + QDrant batch upsert (по 100 треков)
   - Batch QDrant upsert для эффективности

6. **bootstrap/Dockerfile**: тяжёлый образ с WhisperX, torch CPU, ffmpeg, audio-separator.

#### Подагент `ml-sota-expert`
Настройка WhisperX:
- Выбор оптимальной модели для CPU (medium vs large-v3)
- Настройка force-align параметров
- Проверка качества таймингов на тестовых треках (ru + en)

#### Подагент `polyglot-test-engineer`
Тесты:
- CLI обрабатывает 10 тестовых треков с `--workers 2`
- Треки появляются в SQLite + QDrant с корректными векторами
- При наличии LRC → тайминги точнее (force-align)
- При отсутствии LRC → полная транскрипция
- Ошибки логируются, не останавливают процесс
- `docker build ./bootstrap` проходит

#### Пользователь
Запускает CLI на тестовом наборе из 10-20 треков. Проверяет качество результатов. Подтверждает или вносит замечания.

### Выходные артефакты
- `bootstrap/` — полный CLI для массовой обработки
- WhisperX + lrc-lib дамп интеграция
- multiprocessing.Pool runner с tqdm
- `docker build ./bootstrap` проходит
- CLI обрабатывает тестовый набор (10+ треков)
- Коммит

