# v3-rc2: План реализации — дешёвый VPS + API-вариант

**Дата составления:** 2026-03-07 (обновлено 2026-03-08 после завершения v3-rc1)
**Целевая инфраструктура:** VPS 2–4 vCPU, 4–8 GB RAM, без GPU
**Цель:** максимально сдвинуть тяжёлые вычисления в API, оставив на сервере только лёгкие задачи
**Базируется на:** v3-rc1 (завершённая реализация с GPU-worker)

---

## Содержание

1. [Обзор изменений относительно v3-rc1](#1-обзор-изменений-относительно-v3-rc1)
2. [Структура директорий](#2-структура-директорий)
3. [Переменные окружения и конфигурация](#3-переменные-окружения-и-конфигурация)
4. [Описание каждого нового класса/модуля](#4-описание-каждого-нового-классамодуля)
5. [Оркестрация пайплайна — пошаговый поток](#5-оркестрация-пайплайна--пошаговый-поток)
6. [Стратегия обработки ошибок и ретраи](#6-стратегия-обработки-ошибок-и-ретраи)
7. [Docker: Dockerfile и Compose](#7-docker-dockerfile-и-compose)
8. [Зависимости (pip-пакеты)](#8-зависимости-pip-пакеты)
9. [Отслеживание стоимости API](#9-отслеживание-стоимости-api)
10. [Оценка ресурсов: RAM, диск, время на трек](#10-оценка-ресурсов-ram-диск-время-на-трек)
11. [Миграция из v3-rc1: что переиспользуется](#11-миграция-из-v3-rc1-что-переиспользуется)
12. [Стратегия тестирования](#12-стратегия-тестирования)
13. [Модель стоимости](#13-модель-стоимости)

---

## 1. Обзор изменений относительно v3-rc1

### Что убрать из worker v3-rc1

| Компонент v3-rc1 | Причина удаления |
|---|---|
| `UVRSeparator` (audio-separator, BS-Roformer, GPU) | Заменяется MVSEP API |
| `WhisperTranscriber` (faster-whisper, GPU) | Заменяется OpenAI Whisper API |
| `LyricEmbedder` (sentence-transformers локально) | Опционально: заменяется OpenAI Embeddings API |

### Что добавить (нового относительно v3-rc1)

| Новый компонент | Задача |
|---|---|
| `MVSEPClient` | Загрузить MP3, запустить сепарацию sep_type=49, скачать stems |
| `WhisperAPIClient` | Транскрибировать вокал через OpenAI whisper-1, вернуть текст + язык |
| `OpenAIEmbedder` | Опциональная замена sentence-transformers |
| `CostTracker` | Записывать стоимость каждого API-вызова в SQLite |

### Что переиспользуется из v3-rc1 без изменений

- `LyricsSearcher` — двухступенчатый поиск текста (LLM identify → Genius scrape → web_search fallback). **Копируется из v3-rc1 как есть.**
- `VADProcessor` — librosa VAD. **Копируется из v3-rc1 как есть.**
- `CTCAligner` — hybrid word+char CTC alignment. **Копируется из v3-rc1 как есть.**
- Всё содержимое `v3-rc1/shared/karaoke_shared/` — переиспользуется (symlink или pip-install)
- `JobPoller` / `JobService` — логика опроса очереди не меняется
- `FeatureExtractor` — librosa 45-d вектор, локальный, без изменений
- `detect_line_breaks` — определение переносов строк
- `SQLiteRepository` / `QDrantRepository` — хранение не меняется
- Схема SQLite — та же; треки те же; добавляется только таблица `api_costs`

---

## 2. Структура директорий

```
v3-rc2/
├── PLAN.md                          ← этот файл
├── .env.example                     ← пример всех переменных окружения
├── docker-compose.yml               ← базовый compose (dev)
├── docker-compose.prod.yml          ← prod-оверлей (bind mounts, restart)
│
├── shared/                          ← СИМЛИНК на ../v3-rc1/shared или полная копия
│   └── karaoke_shared/              ← переиспользуется из v3-rc1 без изменений
│
└── worker/
    ├── Dockerfile
    ├── pyproject.toml
    ├── entrypoint.sh
    └── app/
        ├── __init__.py
        ├── main.py                  ← точка входа, JobPoller (аналог v2)
        ├── config.py                ← WorkerSettings (расширенный)
        │
        └── pipeline/
            ├── __init__.py
            ├── audio_pipeline.py    ← новый оркестратор
            ├── mvsep_client.py      ← НОВЫЙ: MVSEP API клиент
            ├── whisper_client.py    ← НОВЫЙ: OpenAI Whisper API клиент
            ├── lyrics_searcher.py   ← КОПИЯ из v3-rc1: LLM identify + Genius + web search
            ├── vad_processor.py     ← КОПИЯ из v3-rc1: VAD на librosa
            ├── ctc_aligner.py       ← КОПИЯ из v3-rc1: CTC hybrid alignment
            └── openai_embedder.py   ← НОВЫЙ: OpenAI Embeddings API (опц.)
```

**Примечание по shared:** Если v3-rc2 деплоится отдельно от v3-rc1, скопировать `v3-rc1/shared/` в `v3-rc2/shared/`. Если деплоится рядом — использовать симлинк. В Dockerfile путь прописан явно (см. раздел 7).

---

## 3. Переменные окружения и конфигурация

### 3.1 Полный список переменных

Класс `WorkerSettings` (pydantic-settings, без префикса):

```python
# --- Общие (из v3-rc1, без изменений) ---
DATABASE_URL          = "/data/sqlite/karaoke.db"
MEDIA_ROOT            = "/data/media"
MODEL_CACHE_DIR       = "/data/models"       # для CTC модели + norm stats
WORKER_ID             = "worker-1"
POLL_INTERVAL_SEC     = 2.0
LOG_LEVEL             = "INFO"

# --- QDrant (из v3-rc1, без изменений) ---
QDRANT_HOST           = "qdrant"
QDRANT_PORT           = 6333

# --- Feature normalization (из v3-rc1) ---
NORMALIZATION_STATS_PATH = ""               # путь к feature_normalization_stats.json

# --- MVSEP API (НОВОЕ для rc2) ---
MVSEP_API_KEY         = ""                  # обязательно!
MVSEP_API_URL         = "https://mvsep.com/api"
MVSEP_SEP_TYPE        = 49                  # 49 = Karaoke (vocals + instrumental)
MVSEP_OUTPUT_FORMAT   = "mp3"               # mp3 достаточно для инструментала
MVSEP_POLL_INTERVAL_SEC = 10.0              # как часто проверять статус задания
MVSEP_TIMEOUT_SEC     = 600.0               # максимальное время ожидания (10 мин)
MVSEP_MAX_RETRIES     = 3

# --- OpenAI (общий ключ для Whisper, LyricsSearcher, Embedder) ---
OPENAI_API_KEY        = ""                  # обязательно!
OPENAI_MODEL          = "gpt-4o-mini"       # для LyricsSearcher (идентификация + fallback)
OPENAI_TIMEOUT        = 30.0                # HTTP-таймаут для OpenAI Chat API
OPENAI_MAX_RETRIES    = 2
OPENAI_BASE_URL       = "https://api.openai.com"  # для тестирования с mock-сервером

# --- Genius API (для LyricsSearcher, из v3-rc1) ---
GENIUS_TOKEN          = ""                  # обязательно! Genius API bearer token

# --- OpenAI Whisper API (НОВОЕ для rc2) ---
WHISPER_MODEL         = "whisper-1"
WHISPER_TIMEOUT_SEC   = 120.0
WHISPER_MAX_RETRIES   = 2
WHISPER_LANGUAGE_HINT = ""                  # "" = автодетект; "ru" или "en" для подсказки

# --- OpenAI Embeddings API (НОВОЕ для rc2, опциональное) ---
LYRIC_EMBEDDER_BACKEND = "local"            # "local" | "openai"
OPENAI_EMBED_MODEL    = "text-embedding-3-small"
OPENAI_EMBED_DIMENSIONS = 384              # ВАЖНО: должно совпадать с QDrant коллекцией!
OPENAI_EMBED_TIMEOUT_SEC = 30.0

# --- CTC aligner (из v3-rc1) ---
CTC_MIN_FRAMES_FOR_CHAR = 10               # мин. фреймов для char-level CTC (~200ms)

# --- VAD (из v3-rc1) ---
VAD_TOP_DB            = 35                  # порог тишины (librosa.effects.split)

# --- Cost tracking (НОВОЕ для rc2) ---
COST_TRACKING_ENABLED = true               # записывать стоимость в SQLite
```

### 3.2 Файл .env.example

```env
# ОБЯЗАТЕЛЬНЫЕ КЛЮЧИ
MVSEP_API_KEY=ваш_ключ_mvsep
OPENAI_API_KEY=sk-...
GENIUS_TOKEN=ваш_токен_genius

# QDrant
QDRANT_HOST=qdrant
QDRANT_PORT=6333

# Нормализация фичей (обязательно после bootstrap!)
NORMALIZATION_STATS_PATH=/data/models/feature_normalization_stats.json

# Опционально: использовать OpenAI для embed вместо локальной модели
# LYRIC_EMBEDDER_BACKEND=openai

# Опционально: подсказка языка для Whisper
# WHISPER_LANGUAGE_HINT=ru
```

---

## 4. Описание каждого нового класса/модуля

### 4.1 `MVSEPClient` — `pipeline/mvsep_client.py`

**Назначение:** Загрузить MP3 на MVSEP, запустить разделение, дождаться результата, скачать stems (вокал + инструментал).

**MVSEP API flow:**
1. `POST /api/separation/create` — создать задание (multipart: `api_token`, `sep_type`, `add_to_cloud=0`, файл)
2. Ответ: `{"success": true, "data": {"id": "JOB_ID", ...}}`
3. `GET /api/separation/get?id=JOB_ID&api_token=KEY` — опрашивать статус
4. Ответ со статусом `finished`: содержит `output_files` — массив объектов с `filename` и `preview_path`/`large_path`
5. Скачать каждый файл по URL

**Интерфейс:**

```python
class StemResult:
    vocals_path: str      # абсолютный путь к скачанному vocals.mp3/wav
    instrumental_path: str  # абсолютный путь к скачанному instrumental.mp3

class MVSEPClient:
    def __init__(
        self,
        api_key: str,
        api_url: str = "https://mvsep.com/api",
        sep_type: int = 49,
        output_format: str = "mp3",
        poll_interval_sec: float = 10.0,
        timeout_sec: float = 600.0,
        max_retries: int = 3,
        media_root: str = "/data/media",
    ) -> None: ...

    async def separate(self, mp3_path: str) -> StemResult:
        """
        Загрузить mp3_path на MVSEP, дождаться завершения, скачать stems.
        Stems сохраняются в media_root/instrumental/{track_id}_vocals.mp3
        и media_root/instrumental/{track_id}_instrumental.mp3

        Returns: StemResult с путями к скачанным файлам.
        Raises: MVSEPError если API вернул ошибку или истёк таймаут.
        """

    async def _create_job(self, client: httpx.AsyncClient, mp3_path: str) -> str:
        """POST /api/separation/create, вернуть job_id."""

    async def _poll_until_done(
        self, client: httpx.AsyncClient, job_id: str
    ) -> list[dict]:
        """Опрашивать GET /api/separation/get до статуса finished/error.
        Возвращает список output_files из ответа API."""

    async def _download_stems(
        self,
        client: httpx.AsyncClient,
        output_files: list[dict],
        base_name: str,
    ) -> StemResult:
        """Скачать файлы и определить какой вокал, какой инструментал.
        Определение: ищем 'vocal'/'voice' в имени → vocals,
        'instrum'/'accomp'/'no_vocal'/'karaoke' → instrumental."""

    def _request_with_retry(self, ...) -> dict:
        """Retry только на 5xx и сетевые ошибки (не на 4xx)."""
```

**Детали реализации:**

- Загрузка файла через `httpx.AsyncClient` с `multipart` (файл + параметры)
- При загрузке использовать `timeout=httpx.Timeout(connect=30.0, read=300.0, write=300.0)` — файл может быть большим
- Polling: `asyncio.sleep(poll_interval_sec)` между опросами
- Таймаут полного ожидания: считать суммарное elapsed время, бросать `MVSEPTimeoutError` при превышении
- Скачивание: использовать `client.stream()` для больших файлов (stems могут быть 20-50MB)
- Хранение скачанных stems: `{media_root}/instrumental/{stem_base_name}`
- После загрузки stems — оригинальные файлы на MVSEP не удалять (нет такого API)
- Определение типа stem: сначала проверять `no_vocal`/`karaoke`/`instrum` (содержат слово `vocal`!), затем `vocal`/`voice` — порядок важен

**Обработка ошибок MVSEP:**

```
HTTP 4xx → не ретраить, бросить MVSEPAPIError(status_code, message)
HTTP 5xx → ретраить до max_retries с экспоненциальным backoff (1s, 2s, 4s)
Таймаут → MVSEPTimeoutError
Статус задания "error" → MVSEPJobError(job_id, error_message из API)
Не нашли vocals или instrumental в output_files → MVSEPParseError
```

---

### 4.2 `WhisperAPIClient` — `pipeline/whisper_client.py`

**Назначение:** Передать аудио (вокал после VAD) в OpenAI Whisper API, получить текст + определённый язык. Точность не критична — текст нужен только для поиска правильного текста песни в LLM.

**Интерфейс:**

```python
class WhisperResult:
    text: str             # распознанный текст
    language: str | None  # "ru", "en", etc. (ISO 639-1 из API)

class WhisperAPIClient:
    def __init__(
        self,
        api_key: str,
        model: str = "whisper-1",
        timeout: float = 120.0,
        max_retries: int = 2,
        language_hint: str = "",  # "" = автодетект
    ) -> None: ...

    async def transcribe(self, audio_path: str) -> WhisperResult:
        """
        Транскрибировать аудиофайл через OpenAI whisper-1.

        Args:
            audio_path: путь к WAV/MP3 файлу (после VAD).
                        Важно: OpenAI принимает файлы до 25MB.
                        Если файл больше — нужна предварительная компрессия.

        Returns: WhisperResult с текстом и языком.
        Raises: WhisperAPIError при ошибке API.
        """

    async def _check_file_size(self, audio_path: str) -> None:
        """Проверить, что файл < 25MB. Если больше — сжать до нужного размера
        через ffmpeg (понизить битрейт)."""

    async def _compress_audio(self, audio_path: str) -> str:
        """Запустить ffmpeg для уменьшения размера файла.
        Возвращает путь к временному сжатому файлу."""
```

**Детали реализации:**

- Endpoint: `POST https://api.openai.com/v1/audio/transcriptions`
- Content-Type: `multipart/form-data`
- Параметры: `file` (бинарный), `model=whisper-1`, опционально `language=ru/en`
- Ответ: `{"text": "...", "language": "russian"}` — язык приходит полным словом ("russian", "english"), нужно конвертировать в ISO 639-1
- Маппинг языков: `{"russian": "ru", "english": "en", "ukrainian": "uk", ...}` — делать через таблицу, не угадывать
- Ретрай: экспоненциальный backoff на 5xx, только 2 попытки (Whisper дорогой, не стоит ретраить много)
- Файл больше 25MB: запустить `ffmpeg` subprocess для понижения битрейта до 64k mono
  ```python
  cmd = ["ffmpeg", "-i", audio_path, "-b:a", "64k", "-ac", "1", "-y", out_path]
  proc = await asyncio.create_subprocess_exec(*cmd, ...)
  ```
- Возвращать оригинальный файл на удаление после использования, временный файл удалять немедленно

**Обработка ошибок:**
```
HTTP 429 (rate limit) → sleep(60s) + 1 retry
HTTP 400 (файл слишком большой) → попробовать сжать и повторить
HTTP 401 → WhisperAuthError (неверный ключ, не ретраить)
HTTP 5xx → retry с backoff
```

---

### 4.3 `LyricsSearcher` — `pipeline/lyrics_searcher.py`

**КОПИРУЕТСЯ из v3-rc1 без изменений.** Это единственный компонент поиска текста, не зависящий от GPU. Работает через API (OpenAI + Genius + web scraping).

**Назначение:** Двухступенчатый поиск текста песни с fallback-цепочкой.

**Зависимости:** `httpx>=0.27`, `beautifulsoup4>=4.12`, `lxml>=5.0`

**Интерфейс (из v3-rc1):**

```python
@dataclass
class LyricsResult:
    artist: str          # канонический исполнитель (определён LLM)
    title: str           # каноническое название (определено LLM)
    lyrics: str          # полный текст с переносами строк
    language: str        # 'ru' | 'en' | 'other'
    confidence: str      # 'high' | 'medium' | 'low'
    source_note: str     # 'genius.com', 'web_search+genius', 'web_search+domain'

class LyricsSearchError(Exception): ...      # базовый
class LyricsNotFoundError(LyricsSearchError): ...  # текст не найден
class LyricsAPIError(LyricsSearchError): ...       # сетевая ошибка (retryable)

class LyricsSearcher:
    def __init__(
        self,
        openai_api_key: str,
        genius_token: str,
        model: str = "gpt-4o-mini",
        timeout: float = 30.0,
        max_retries: int = 2,
        openai_base_url: str = "https://api.openai.com",
    ) -> None: ...

    async def search(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None = None,
        title_hint: str | None = None,
    ) -> LyricsResult:
        """
        Найти текст песни по ASR-транскрипции.

        Raises:
            LyricsNotFoundError: если все пути исчерпаны.
            LyricsAPIError: если API-запросы упали после ретраев.
        """
```

**Алгоритм (реализован в v3-rc1):**

1. **Primary path** (дешёвый, ~$0.0005):
   - `_identify_song()` — gpt-4o-mini определяет artist + title из ASR-текста (JSON response)
   - `_fetch_genius_lyrics()` — поиск через Genius API + скрейпинг страницы через CSS-селектор `[data-lyrics-container="true"]`
   - Возвращает `LyricsResult(source_note="genius.com")`

2. **Fallback path** (если primary упал, ~$0.003):
   - `_web_search_fallback()` — OpenAI `/v1/responses` API с `web_search_preview` tool
   - Пытается Genius с новым artist/title
   - Скрейпит найденные URL через `_scrape_generic_page()`:
     - CSS: `[data-lyrics-container]` → `[class*=lyric]` → `<pre>` tags
     - LLM extraction как последний resort (`_llm_extract_lyrics()`)
   - Возвращает `LyricsResult(source_note="web_search+domain")`

3. Если оба пути упали → `LyricsNotFoundError`

**Очистка текста (`_clean_lyrics`):**
- Пропускает Genius header noise (до первого `[Section]` маркера)
- Удаляет section markers `[Intro]`, `[Verse]`, `[Припев]` и т.д.
- Сворачивает 3+ пустые строки в 2

**Обработка ошибок:**
```
LyricsNotFoundError → пайплайн синхронизирует audio-only в QDrant,
    помечает трек status="error", error_message="Lyrics not found: ..."
LyricsAPIError → retry с backoff (429: 5s, 5xx: 2s)
```

**ВАЖНО из v3-rc1 опыта:** OpenAI content_filter иногда блокирует тексты с нецензурной лексикой в LLM-extraction режиме. CSS-селекторы работают как primary fallback и не подвержены этой проблеме.

---

### 4.4 `VADProcessor` — `pipeline/vad_processor.py`

**КОПИРУЕТСЯ из v3-rc1 без изменений.**

**Назначение:** Убрать тишину из вокала перед подачей в Whisper. Склеивает voiced-сегменты, возвращает путь к "сжатому" WAV.

**Интерфейс (из v3-rc1):**

```python
class VADProcessor:
    def __init__(self, top_db: int = 35) -> None: ...

    def process(self, vocals_path: str) -> str:
        """
        Загрузить vocals_path, применить VAD, сохранить рядом как cleaned_vocals.wav.
        Синхронный метод — вызывать через asyncio.to_thread.

        Args:
            vocals_path: путь к vocals.mp3/wav от MVSEP.

        Returns:
            Путь к cleaned_vocals.wav.
            Если загрузка не удалась или voiced < 1 сек — возвращает исходный vocals_path.
            Никогда не бросает исключений (graceful fallback).
        """
```

**Алгоритм (реализован в v3-rc1):**

```python
def process(self, vocals_path: str) -> str:
    y, sr = librosa.load(vocals_path, sr=16000, mono=True)
    intervals = librosa.effects.split(y, top_db=35, frame_length=2048, hop_length=512)

    voiced_segments = [y[start:end] for start, end in intervals]

    if not voiced_segments:
        return vocals_path  # нет voiced — вернуть оригинал

    cleaned = np.concatenate(voiced_segments)
    if len(cleaned) / 16000 < 1.0:  # < 1 секунды
        return vocals_path

    out_path = Path(vocals_path).parent / "cleaned_vocals.wav"
    sf.write(str(out_path), cleaned, 16000, subtype='PCM_16')
    return str(out_path)
```

- Загрузка с `sr=16000` — Whisper ресемплирует, ctc-forced-aligner требует 16kHz
- Сохранение в WAV (PCM_16) — без артефактов компрессии
- При любой ошибке (import, load, process) — возвращает оригинал, не бросает исключений
- Удалять cleaned_vocals.wav после того, как Whisper закончил транскрипцию

---

### 4.5 `CTCAligner` — `pipeline/ctc_aligner.py`

**КОПИРУЕТСЯ из v3-rc1 без изменений.**

**Назначение:** Принять текст песни (правильный, найденный LyricsSearcher) + вокал, вернуть syllable_timings с точностью MAE ~0.24s. Гибридный word+char алгоритм.

**Интерфейс (из v3-rc1):**

```python
from dataclasses import dataclass

@dataclass
class AlignmentStats:
    total_words: int = 0
    char_level_used: int = 0
    proportional_fallback: int = 0

class CTCAligner:
    """
    Hybrid CTC forced alignment.

    ВАЖНО: AlignmentSingleton (ONNX модель) загружается при инициализации
    и переиспользуется для всех треков.
    Не создавать новый экземпляр CTCAligner на каждый трек!
    """

    def __init__(
        self,
        syllabifier,                    # karaoke_shared.utils.syllabifier.Syllabifier
        model_cache_dir: str | None = None,
        min_frames_for_char: int = 10,  # ~200ms при stride=20ms
    ) -> None:
        """
        Загружает ONNX модель MMS-300m через AlignmentSingleton СРАЗУ.
        model_cache_dir используется как HF_HOME для кеша модели (~300MB).
        """
        from ctc_forced_aligner import AlignmentSingleton
        aligner = AlignmentSingleton()
        self._model = aligner.alignment_model
        self._tokenizer = aligner.alignment_tokenizer
        self._syllabifier = syllabifier
        self._min_frames = min_frames_for_char

    def align(
        self,
        vocals_path: str,
        lyrics_text: str,
        language: str,
    ) -> tuple[list[SyllableTiming], AlignmentStats]:
        """
        Запустить hybrid CTC alignment.
        Синхронный метод — вызывать через asyncio.to_thread.

        Args:
            vocals_path: путь к WAV с вокалом (16kHz mono).
                         ВАЖНО: это должен быть ОРИГИНАЛЬНЫЙ вокал с тишиной
                         (не VAD-сжатый!), т.к. тайминги должны совпадать
                         с реальным временем трека.
            lyrics_text: полный текст с переносами строк (\n).
            language: "ru" | "en" (ISO 639-1).

        Returns:
            Кортеж (syllable_timings, stats).
        """
```

**КРИТИЧЕСКИ ВАЖНЫЕ детали (подтверждены в v3-rc1):**

1. `generate_emissions()` вызывается **строго один раз** на трек (heap corruption если per-word).
2. Перед char-level alignment: проверка `n_frames > n_targets` (C++ abort если нарушено).
3. Маппинг языков: `{"ru": "rus", "en": "eng"}` (ISO 639-3).
4. Romanize: `True` для non-English, `False` для English.
5. `MIN_FRAMES_FOR_CHAR = 10` → proportional fallback для коротких слов.
6. Вокал для CTC — оригинальный (с паузами), не VAD-сжатый!
7. При word count mismatch (CTC vs lyrics) — использует `min(ctc_count, lyrics_count)`, не бросает исключение.

---

### 4.6 `OpenAIEmbedder` — `pipeline/openai_embedder.py`

**Назначение:** Опциональная замена локального `LyricEmbedder` (sentence-transformers). Если `LYRIC_EMBEDDER_BACKEND=openai`.

**ВАЖНО:** OpenAI `text-embedding-3-small` с `dimensions=384` НЕ совместим с существующими векторами v2 (паразфраза-multilingual-MiniLM-L12-v2). Если в QDrant уже есть векторы от v2 — нельзя просто переключить бэкенд. Нужно либо:
- Реиндексировать все существующие треки через новый embedder
- ИЛИ использовать отдельную QDrant-коллекцию для v3-rc2 треков

**Рекомендация:** Оставить `LYRIC_EMBEDDER_BACKEND=local` по умолчанию. Переключать на `openai` только для новых инсталляций без существующих векторов.

**Интерфейс:**

```python
class OpenAIEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 384,
        timeout: float = 30.0,
    ) -> None: ...

    def embed(self, text: str) -> list[float]:
        """
        Синхронный — вызывать через asyncio.to_thread.
        Возвращает список из dimensions float-значений.
        """
```

**Детали реализации:**

- Endpoint: `POST https://api.openai.com/v1/embeddings`
- Параметры: `{"input": text, "model": "text-embedding-3-small", "dimensions": 384}`
- Ответ: `{"data": [{"embedding": [...]}]}`
- Использовать синхронный `httpx.Client` (обёртку), т.к. embed вызывается через `to_thread`
- Логировать стоимость: `usage.total_tokens * $0.00002 / 1000`

---

### 4.7 `CostTracker` — интегрирован в `pipeline/audio_pipeline.py`

**Назначение:** Записывать стоимость каждого API-вызова в SQLite, позволять считать месячные расходы.

**Схема таблицы (добавить в `init.sql`):**

```sql
CREATE TABLE IF NOT EXISTS api_costs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id   TEXT NOT NULL,
    service    TEXT NOT NULL,     -- 'mvsep' | 'openai_whisper' | 'openai_chat' | 'openai_embed'
    cost_usd   REAL NOT NULL,     -- стоимость в долларах
    tokens     INTEGER,           -- для OpenAI (prompt + completion)
    duration_sec REAL,            -- для Whisper (длина аудио)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_api_costs_created_at ON api_costs(created_at);
CREATE INDEX IF NOT EXISTS idx_api_costs_track_id ON api_costs(track_id);
```

**Использование в пайплайне:**

```python
# После каждого API вызова:
await repo.record_api_cost(
    track_id=job.track_id,
    service="mvsep",
    cost_usd=0.15,
    duration_sec=audio_duration,
)
```

**Добавить в `SQLiteRepository`:**

```python
async def record_api_cost(
    self,
    track_id: str,
    service: str,
    cost_usd: float,
    tokens: int | None = None,
    duration_sec: float | None = None,
) -> None: ...

async def get_monthly_costs(self) -> dict:
    """SELECT service, SUM(cost_usd) FROM api_costs
    WHERE created_at >= date('now', 'start of month')
    GROUP BY service"""
```

**Backend endpoint (добавить в v3 backend):**

```
GET /admin/costs
Response: {
  "current_month": {"mvsep": 45.00, "openai_whisper": 6.00, "total": 51.00},
  "this_month_tracks": 300,
  "avg_per_track": 0.17
}
```

---

### 4.8 Новый `AudioPipeline` — `pipeline/audio_pipeline.py`

**Назначение:** Оркестрирует все шаги нового пайплайна. Подробнее — в разделе 5.

**Конструктор:**

```python
class AudioPipeline:
    def __init__(
        self,
        job_service: JobService,
        repo: SQLiteRepository,
        mvsep: MVSEPClient,
        whisper: WhisperAPIClient,
        vad: VADProcessor,
        lyrics_searcher: LyricsSearcher | None,
        ctc_aligner: CTCAligner,
        feature_extractor: object | None = None,
        lyric_embedder: LyricEmbedder | OpenAIEmbedder | None = None,
        qdrant_repo: QDrantRepository | None = None,
        settings: object | None = None,
    ) -> None: ...

    async def process(self, job: Job) -> None: ...
```

---

## 5. Оркестрация пайплайна — пошаговый поток

### 5.1 Обзор шагов

```
[MP3 на диске]
      │
      ▼
Step 1: MVSEP API — сепарация (2–3 мин, async poll)
      │
      ├──► vocals.mp3 (временный)
      └──► instrumental.mp3 (постоянный в media_root/instrumental/)
                │
                ▼ ПАРАЛЛЕЛЬНО (asyncio.gather):
┌────────────────────────────────────────────────────────────────┐
│                                                                │
│  VAD + ASR:                          FeatureExtractor:         │
│                                                                │
│  Step 2: VAD (локально, ~2s)         Step 4: FeatureExtractor  │
│  vocals.mp3 → cleaned_vocals.wav     instrumental.mp3 →       │
│          │                           45-d vector (~10s)        │
│          ▼                                                     │
│  Step 3: Whisper API (~15s)                                    │
│  cleaned_vocals.wav → text + language                          │
│          │                                                     │
│  [удалить cleaned_vocals.wav]                                  │
│                                                                │
└────────────────────────────────────────────────────────────────┘
      │
      ▼
Step 5: LyricsSearcher (из v3-rc1: identify → Genius → fallback, ~3-5s)
      asr_text → LyricsResult(artist, title, lyrics, language)
      │
      ▼ (если LyricsNotFoundError → sync audio-only, mark error, return)
      │
Step 6+7: CTC alignment (локально, ~22s)
      vocals.mp3 + lyrics → syllable_timings + AlignmentStats
      │
      ▼
Step 8: detect_line_breaks (~1s)
      │
      ▼
Step 9: LyricEmbedder (локально ~5s или OpenAI ~1s)
      │
      ▼
Step 10: QDrant sync (локально, <1s) + cost записи в SQLite
      │
      ▼
[удалить vocals.mp3]
mark_completed
```

### 5.2 Псевдокод `AudioPipeline.process()`

```python
async def process(self, job: Job) -> None:
    track = await self.repo.get_track(job.track_id)
    if not track or not track.mp3_path:
        await self.job_service.mark_failed(job.id, "no track or mp3")
        return

    vocals_path: str | None = None

    try:
        # ----------------------------------------------------------------
        # Step 1: Сепарация (MVSEP API)
        # ----------------------------------------------------------------
        await self.job_service.mark_step(job.id, "separating", 0)

        stem_result = await self.mvsep.separate(track.mp3_path)
        vocals_path = stem_result.vocals_path
        instrumental_path = stem_result.instrumental_path

        await self.repo.update_track(
            job.track_id,
            TrackUpdate(instrumental_path=instrumental_path, status="processing"),
        )
        await self.job_service.mark_step(job.id, "separating", 100)

        # Записать стоимость MVSEP
        audio_duration = await asyncio.to_thread(self._get_audio_duration, track.mp3_path)
        await self.repo.record_api_cost(job.track_id, "mvsep", 0.15, duration_sec=audio_duration)

        # ----------------------------------------------------------------
        # Параллельно: Ветка А (VAD+ASR) и Ветка Б (features)
        # ----------------------------------------------------------------
        await self.job_service.mark_step(job.id, "processing", 0)

        feature_result, whisper_result = await asyncio.gather(
            self._run_branch_b(job, track, instrumental_path),
            self._vad_and_transcribe(job, vocals_path),
            return_exceptions=True,
        )

        feature_vector = feature_result if not isinstance(feature_result, Exception) else None
        if isinstance(feature_result, Exception):
            logger.error("feature_extraction_failed", error=str(feature_result))

        if isinstance(whisper_result, Exception):
            raise whisper_result  # ASR failure is critical

        # ----------------------------------------------------------------
        # Step 5: Поиск текста через LyricsSearcher (из v3-rc1)
        # ----------------------------------------------------------------
        await self.job_service.mark_step(job.id, "searching_lyrics", 0)

        artist_hint, title_hint = self._parse_hints_from_path(track.mp3_path)

        try:
            lyrics_result = await self.lyrics_searcher.search(
                asr_text=whisper_result.text,
                detected_language=whisper_result.language,
                artist_hint=artist_hint or track.artist,
                title_hint=title_hint or track.title,
            )
        except LyricsNotFoundError as exc:
            # Текст не найден — sync audio-only, mark error (как в v3-rc1)
            if feature_vector:
                await self._sync_qdrant_audio_only(job.track_id, track, feature_vector)
            await self.job_service.mark_failed(job.id, f"Lyrics not found: {exc}")
            return

        await self.job_service.mark_step(job.id, "searching_lyrics", 100)

        # Обновляем artist/title/lyrics из LyricsResult (как в v3-rc1)
        await self.repo.update_track(job.track_id, TrackUpdate(
            artist=lyrics_result.artist,
            title=lyrics_result.title,
            lyrics_text=lyrics_result.lyrics,
            language=lyrics_result.language,
        ))

        # Записать стоимость LyricsSearcher
        await self.repo.record_api_cost(job.track_id, "openai_chat", 0.001)

        # ----------------------------------------------------------------
        # Steps 6+7: CTC alignment (~22s CPU)
        # ----------------------------------------------------------------
        await self.job_service.mark_step(job.id, "aligning", 0)

        raw_timings, align_stats = await asyncio.to_thread(
            self.ctc_aligner.align, vocals_path, lyrics_result.lyrics, lyrics_result.language
        )

        # Step 8: detect_line_breaks
        syllable_timings = await asyncio.to_thread(
            detect_line_breaks, raw_timings, vocals_path
        )
        await self.job_service.mark_step(job.id, "aligning", 100)

        # Step 9: Lyric embedding
        lyric_vector = None
        if self.lyric_embedder:
            lyric_vector = await asyncio.to_thread(
                self.lyric_embedder.embed, lyrics_result.lyrics
            )

        # ----------------------------------------------------------------
        # Step 10: QDrant sync
        # ----------------------------------------------------------------
        qdrant_synced = await self._sync_qdrant(job, track, feature_vector, lyric_vector)

        # ----------------------------------------------------------------
        # Финализация
        # ----------------------------------------------------------------
        update = TrackUpdate(
            status="ready",
            syllable_timings=syllable_timings,
            qdrant_synced=1 if qdrant_synced else 0,
        )
        await self.repo.update_track(job.track_id, update)
        await self.job_service.mark_completed(job.id, {
            "instrumental_path": instrumental_path,
            "language": lyrics_result.language,
            "align_stats": {"total_words": align_stats.total_words,
                            "char_level_used": align_stats.char_level_used},
        })

    except Exception as exc:
        logger.error("pipeline_failed", job_id=job.id, error=str(exc))
        await self.job_service.mark_failed(job.id, str(exc))
    finally:
        # Удалить вокальный стем — больше не нужен
        if vocals_path and Path(vocals_path).exists():
            Path(vocals_path).unlink(missing_ok=True)
```

### 5.3 VAD + Whisper ASR (параллельно с features)

```python
async def _vad_and_transcribe(self, job, vocals_path: str) -> WhisperResult:
    """VAD → Whisper API → вернуть текст + язык."""

    # Step 2: VAD
    await self.job_service.mark_step(job.id, "vad", 0)
    vad_path = await asyncio.to_thread(self.vad.process, vocals_path)
    await self.job_service.mark_step(job.id, "vad", 100)

    try:
        # Step 3: Whisper API ASR
        await self.job_service.mark_step(job.id, "transcribing", 0)
        whisper_result = await self.whisper.transcribe(vad_path)

        # Записать стоимость Whisper
        vad_duration = await asyncio.to_thread(self._get_audio_duration, vad_path)
        whisper_cost = (vad_duration / 60.0) * 0.006
        await self.repo.record_api_cost(
            job.track_id, "openai_whisper", whisper_cost, duration_sec=vad_duration
        )

        await self.job_service.mark_step(job.id, "transcribing", 100)
        return whisper_result

    finally:
        # VAD-сжатый файл больше не нужен
        if vad_path != vocals_path:
            Path(vad_path).unlink(missing_ok=True)
```

**Примечание:** В отличие от начального плана, LyricsSearcher, CTC alignment и embedding
вынесены в основной flow `process()` (после gather), а не в параллельную ветку.
Это соответствует архитектуре v3-rc1, где lyrics search, CTC и embedding последовательны
и зависят от результата предыдущего шага.

### 5.4 Ветка Б: Feature extraction

```python
async def _run_branch_b(self, job, track, instrumental_path: str) -> list[float] | None:
    """Feature extraction на инструментале."""
    await self.job_service.mark_step(job.id, "extracting_features", 0)
    vector = await asyncio.to_thread(self.feature_extractor.extract, instrumental_path)
    await self.job_service.mark_step(job.id, "extracting_features", 100)
    return vector
```

### 5.5 Параллелизм

Ветки А и Б запускаются через `asyncio.gather`. Важные ограничения:

- **CTC alignment (ветка А) синхронный и CPU-bound** — `asyncio.to_thread` корректен, но занимает поток ~22s
- **Feature extraction (ветка Б) синхронный и CPU-bound** — тоже ~10s через `to_thread`
- На 2-vCPU машине они будут выполняться в разных потоках, давая реальный параллелизм
- Для 4-vCPU можно запустить несколько воркеров (WORKER_ID=worker-1, worker-2) — каждый берёт отдельный job

**Итоговое время обработки трека:**
- MVSEP: ~150s (2.5 мин) — sequential, ждём API
- Параллельно: max(Ветка А ~45s, Ветка Б ~10s) = ~45s
- QDrant sync: ~1s
- **Итого: ~3–4 мин на трек**

---

## 6. Стратегия обработки ошибок и ретраи

### 6.1 Таблица ошибок по шагам

| Шаг | Тип ошибки | Поведение |
|-----|-----------|----------|
| MVSEP сепарация | HTTP 5xx | Retry x3, exp backoff (2s, 4s, 8s) |
| MVSEP сепарация | HTTP 4xx (неверный ключ) | Fatal, mark_failed |
| MVSEP сепарация | Таймаут (>10 мин) | Fatal, mark_failed |
| MVSEP сепарация | Job error (API вернул error) | Retry 1x, затем mark_failed |
| Whisper | HTTP 5xx | Retry x2, backoff |
| Whisper | HTTP 429 | sleep(60s) + retry 1x |
| Whisper | HTTP 400 (файл >25MB) | Сжать + retry 1x |
| Whisper | HTTP 401 | Fatal |
| LyricsSearcher | LyricsNotFoundError | Sync audio-only QDrant, mark track error |
| LyricsSearcher | LyricsAPIError | Retry с backoff (429: 5s, 5xx: 2s), затем mark_failed |
| CTC alignment | Exception | Graceful: трек без syllable_timings |
| Feature extraction | Exception | Graceful: трек без audio vector в QDrant |
| LyricEmbedder | Exception | Graceful: трек без lyric vector в QDrant |
| QDrant | Недоступен | Graceful: qdrant_synced=0, трек помечен ready |

### 6.2 Retry helper — общий для всех API клиентов

```python
# utils/retry.py
import asyncio
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")

async def retry_async(
    coro_factory: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay_sec: float = 1.0,
    retryable_exceptions: tuple = (Exception,),
    non_retryable_exceptions: tuple = (),
) -> T:
    """
    Выполнить корутину с экспоненциальным backoff.

    Args:
        coro_factory: callable, возвращающий новую корутину при каждом вызове.
                      Нельзя передавать одну и ту же корутину — она уже исчерпана.
        max_retries: максимальное число повторных попыток (не считая первую).
        base_delay_sec: базовая задержка; реальная = base_delay_sec * 2^attempt.
        retryable_exceptions: какие исключения ретраить.
        non_retryable_exceptions: какие НЕ ретраить (проверяется первым).
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except non_retryable_exceptions:
            raise
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay_sec * (2 ** attempt)
                await asyncio.sleep(delay)
    raise last_exc
```

### 6.3 Статус трека при частичных ошибках

Трек помечается `status="ready"` если все основные шаги прошли успешно (сепарация + lyrics + alignment).

Исключения (как в v3-rc1):
- MVSEP упал → `status="error"`, `error_message` содержит детали
- LyricsNotFoundError → `status="error"`, `error_message="Lyrics not found: ..."`, но audio features синхронизируются в QDrant
- Feature extraction / QDrant sync упал → `status="ready"`, `qdrant_synced=0` (не критично)

---

## 7. Docker: Dockerfile и Compose

### 7.1 Dockerfile

```dockerfile
# v3-rc2/worker/Dockerfile
# Минимальный образ: нет ONNX audio-separator, нет sentence-transformers по умолчанию
FROM python:3.12-slim

# Системные зависимости:
# - ffmpeg: для компрессии аудио (Whisper лимит 25MB) + soundfile
# - libsndfile1: soundfile
# - gcc + libc6-dev: для сборки некоторых Python-пакетов
# НЕТ: torch GPU, CUDA, audio-separator — заменены MVSEP API
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

ENV PIP_DEFAULT_TIMEOUT=300

# Shared package (из v3-rc1, без ML extras — sentence-transformers не нужен по умолчанию)
COPY shared/ /shared/
RUN pip install --no-cache-dir "/shared/"

# ctc-forced-aligner: нужен PyTorch CPU (только inference, не training)
# Ставим СНАЧАЛА CPU torch, потом ctc-forced-aligner, чтобы не подтянул CUDA (~3GB)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# ctc-forced-aligner использует ONNX Runtime (легче, чем полный torch для inference)
# librosa + soundfile: локальный VAD и feature extraction
# httpx: HTTP клиент для всех API
# Весь список зависимостей — минимальный
COPY worker/pyproject.toml /worker/pyproject.toml
COPY worker/app/ /worker/app/
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu /worker/

COPY worker/entrypoint.sh /worker/entrypoint.sh
RUN chmod +x /worker/entrypoint.sh

ENTRYPOINT ["/worker/entrypoint.sh"]
```

**ВАЖНО:** `ctc-forced-aligner` при первом запуске скачивает модель MMS-300m (~300MB) из HuggingFace в `HF_HOME`. Убедиться, что `MODEL_CACHE_DIR` примонтирован и `HF_HOME` указывает туда. Иначе при каждом рестарте контейнера модель будет скачиваться заново.

Добавить в `entrypoint.sh`:
```bash
export HF_HOME="${MODEL_CACHE_DIR}/huggingface"
```

### 7.2 `pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "karaoke-worker-v3rc2"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    # --- из v3-rc1 (без изменений) ---
    "aiosqlite>=0.20",
    "structlog>=24.0",
    "pydantic-settings>=2.0",
    "httpx>=0.27",
    "karaoke-shared",

    # --- CTC forced alignment (из v3-rc1) ---
    "ctc-forced-aligner==1.0.2",

    # --- Audio: VAD + feature extraction (из v3-rc1) ---
    "librosa>=0.10",
    "soundfile>=0.12",
    "numpy>=1.24",

    # --- LyricsSearcher scraping (из v3-rc1) ---
    "beautifulsoup4>=4.12",
    "lxml>=5.0",

    # --- Лирика embed (local вариант, из v3-rc1) ---
    "sentence-transformers>=2.2",
    # ПРИМЕЧАНИЕ: sentence-transformers тянет torch.
    # Если хотим убрать — сделать optional и управлять через LYRIC_EMBEDDER_BACKEND.
    # Пока оставляем: модель 80MB, инференс ~5s на CPU, нет зависимости от OpenAI.
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

**Оценка размера образа:**
- python:3.12-slim base: ~130MB
- ffmpeg + libsndfile: ~50MB
- torch CPU: ~700MB
- ctc-forced-aligner + ONNX RT: ~100MB
- librosa + numpy + soundfile: ~80MB
- beautifulsoup4 + lxml: ~15MB
- sentence-transformers: ~500MB (тянет torch — уже есть)
- httpx + structlog + pydantic: ~20MB
- **Итого: ~1.6GB** (vs ~3.5GB в v3-rc1 с CUDA + audio-separator + faster-whisper)

Если убрать sentence-transformers (переключить на `openai` embed): ~1.1GB.

### 7.3 `docker-compose.yml` (базовый, dev)

```yaml
# v3-rc2/docker-compose.yml
services:
  qdrant:
    image: qdrant/qdrant:v1.13.6
    container_name: karaoke_qdrant
    restart: unless-stopped
    volumes:
      - qdrant_data:/qdrant/storage
    environment:
      QDRANT__SERVICE__HTTP_PORT: 6333
    networks:
      - karaoke_net
    healthcheck:
      test: ["CMD-SHELL", "bash -c 'echo > /dev/tcp/localhost/6333'"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    build:
      context: .
      dockerfile: backend/Dockerfile   # переиспользуем из v2 без изменений
    container_name: karaoke_backend
    restart: unless-stopped
    depends_on:
      qdrant:
        condition: service_healthy
    environment:
      DATABASE_URL: /data/sqlite/karaoke.db
      QDRANT_HOST: qdrant
      QDRANT_PORT: "6333"
      MEDIA_ROOT: /data/media
      ADMIN_SECRET: ${ADMIN_SECRET:-changeme}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    volumes:
      - sqlite_data:/data/sqlite
      - media_data:/data/media
    networks:
      - karaoke_net
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  worker:
    build:
      context: .
      dockerfile: worker/Dockerfile
    container_name: karaoke_worker
    restart: unless-stopped
    depends_on:
      backend:
        condition: service_healthy
    environment:
      DATABASE_URL: /data/sqlite/karaoke.db
      MEDIA_ROOT: /data/media
      MODEL_CACHE_DIR: /data/models
      WORKER_ID: worker-1
      POLL_INTERVAL_SEC: ${WORKER_POLL_INTERVAL:-2}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      # QDrant
      QDRANT_HOST: qdrant
      QDRANT_PORT: "6333"
      NORMALIZATION_STATS_PATH: ${NORMALIZATION_STATS_PATH:-/data/models/feature_normalization_stats.json}
      # MVSEP
      MVSEP_API_KEY: ${MVSEP_API_KEY:?err}
      MVSEP_SEP_TYPE: "49"
      MVSEP_POLL_INTERVAL_SEC: "10"
      MVSEP_TIMEOUT_SEC: "600"
      # OpenAI (Whisper API + LyricsSearcher + optional Embedder)
      OPENAI_API_KEY: ${OPENAI_API_KEY:?err}
      OPENAI_MODEL: "gpt-4o-mini"
      WHISPER_MODEL: "whisper-1"
      # Genius API (для LyricsSearcher, из v3-rc1)
      GENIUS_TOKEN: ${GENIUS_TOKEN:?err}
      # Embedder (local по умолчанию)
      LYRIC_EMBEDDER_BACKEND: ${LYRIC_EMBEDDER_BACKEND:-local}
      # CTC model cache
      HF_HOME: /data/models/huggingface
    volumes:
      - sqlite_data:/data/sqlite
      - media_data:/data/media
      - models_data:/data/models
    networks:
      - karaoke_net

  frontend:
    build:
      context: frontend
      dockerfile: Dockerfile           # переиспользуем из v2 без изменений
    container_name: karaoke_frontend
    restart: unless-stopped
    depends_on:
      backend:
        condition: service_healthy
    ports:
      - "${APP_PORT:-80}:80"
    networks:
      - karaoke_net

volumes:
  qdrant_data:
  sqlite_data:
  media_data:
  models_data:

networks:
  karaoke_net:
    driver: bridge
```

### 7.4 `docker-compose.prod.yml` (prod-оверлей)

```yaml
# v3-rc2/docker-compose.prod.yml
services:
  backend:
    volumes:
      - /root/karaoke_data/sqlite:/data/sqlite
      - /root/karaoke_data/media:/data/media

  worker:
    volumes:
      - /root/karaoke_data/sqlite:/data/sqlite
      - /root/karaoke_data/media:/data/media
      - /root/karaoke_data/models:/data/models
    # На дешёвом VPS: 1 воркер, чтобы не перегружать CPU
    # При необходимости можно добавить worker-2 с отдельным container_name

  qdrant:
    volumes:
      - /root/karaoke_data/qdrant:/qdrant/storage
```

### 7.5 `entrypoint.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Важно: MODEL_CACHE_DIR должен быть примонтирован, иначе ctc модель
# будет скачиваться при каждом запуске (~300MB)
export HF_HOME="${MODEL_CACHE_DIR:-/data/models}/huggingface"
mkdir -p "${HF_HOME}"

echo "[entrypoint] Starting worker v3-rc2..."
echo "[entrypoint] HF_HOME=${HF_HOME}"
echo "[entrypoint] MVSEP_API_KEY=${MVSEP_API_KEY:0:8}..."
echo "[entrypoint] OPENAI_API_KEY=${OPENAI_API_KEY:0:8}..."
echo "[entrypoint] GENIUS_TOKEN=${GENIUS_TOKEN:0:8}..."
echo "[entrypoint] LYRIC_EMBEDDER_BACKEND=${LYRIC_EMBEDDER_BACKEND:-local}"

exec python -m app.main
```

---

## 8. Зависимости (pip-пакеты)

### 8.1 Минимальный список с версиями

```
# Ядро (из v3-rc1)
aiosqlite>=0.20,<1.0
structlog>=24.0,<25.0
pydantic-settings>=2.0,<3.0
httpx>=0.27,<1.0

# CTC alignment (из v3-rc1)
ctc-forced-aligner==1.0.2      # точная версия, т.к. API нестабильный
torch>=2.0,<3.0                # CPU-only через --index-url
onnxruntime>=1.17,<2.0         # для ctc-forced-aligner

# Audio processing (из v3-rc1)
librosa>=0.10,<1.0
soundfile>=0.12,<1.0
numpy>=1.24,<2.0

# LyricsSearcher scraping (из v3-rc1)
beautifulsoup4>=4.12,<5.0
lxml>=5.0,<6.0

# Lyric embedder (local вариант, из v3-rc1)
sentence-transformers>=2.2,<4.0

# Shared package
karaoke-shared                 # из /shared/ папки (v3-rc1)
```

### 8.2 НЕ включаем

- `audio-separator` — UVR заменён MVSEP API
- `faster-whisper` / `ctranslate2` — Whisper заменён OpenAI API
- `onnxruntime-gpu` — нет GPU (но `onnxruntime` CPU остаётся для ctc-forced-aligner)
- `openai` SDK — используем `httpx` напрямую (как в v3-rc1)

**Почему не использовать `openai` Python SDK (решение из v3-rc1):**
- httpx уже есть в проекте
- Прямые HTTP вызовы прозрачнее: легче отлаживать, легче мокать в тестах
- Полный контроль над retry-логикой

---

## 9. Отслеживание стоимости API

### 9.1 Расчёт стоимости по сервисам

```python
# pipeline/cost_calculator.py

MVSEP_COST_PER_TRACK = 0.15           # фиксированная, не зависит от длины

def calc_whisper_cost(duration_sec: float) -> float:
    """$0.006 per minute, округлять вверх до секунды."""
    return (duration_sec / 60.0) * 0.006

def calc_chat_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """gpt-4o-mini: $0.15/1M input, $0.60/1M output."""
    return (prompt_tokens * 0.15 + completion_tokens * 0.60) / 1_000_000

def calc_embed_cost(total_tokens: int) -> float:
    """text-embedding-3-small: $0.02/1M tokens."""
    return total_tokens * 0.02 / 1_000_000
```

### 9.2 Структура таблицы и запросы

Таблица `api_costs` (см. раздел 4.7).

Полезные SQL-запросы для мониторинга:

```sql
-- Общая стоимость за текущий месяц по сервисам
SELECT
    service,
    COUNT(*) as track_count,
    ROUND(SUM(cost_usd), 4) as total_usd,
    ROUND(AVG(cost_usd), 4) as avg_per_track
FROM api_costs
WHERE created_at >= date('now', 'start of month')
GROUP BY service;

-- Стоимость одного трека
SELECT service, cost_usd, tokens, duration_sec
FROM api_costs
WHERE track_id = 'UUID'
ORDER BY created_at;

-- Треки, где суммарная стоимость превысила $0.30 (аномалии)
SELECT track_id, SUM(cost_usd) as total
FROM api_costs
GROUP BY track_id
HAVING total > 0.30
ORDER BY total DESC;
```

### 9.3 Алерты (опционально)

Добавить в `main.py` простую проверку при старте воркера:

```python
async def _check_monthly_budget(repo: SQLiteRepository) -> None:
    """Логировать предупреждение если месячные расходы > $100."""
    costs = await repo.get_monthly_costs()
    total = sum(costs.values())
    if total > 100.0:
        logger.warning(
            "monthly_budget_exceeded",
            total_usd=total,
            breakdown=costs,
        )
```

---

## 10. Оценка ресурсов: RAM, диск, время на трек

### 10.1 RAM

| Компонент | RAM в пике |
|-----------|-----------|
| Python process + asyncio | ~50MB |
| ctc-forced-aligner (MMS-300m ONNX) | ~1.2GB (модель в памяти после загрузки) |
| librosa (full track, ~4 мин, 16kHz) | ~50MB |
| sentence-transformers (paraphrase-multilingual) | ~400MB |
| httpx клиенты + буферы | ~30MB |
| **Итого** | **~1.7GB** |

Для 4GB RAM VPS: нормально, остаётся ~2GB для ОС + backend + QDrant.
Для 8GB RAM VPS: комфортно, можно запустить 2 воркера.

**Критический момент:** ctc-forced-aligner загружает 300MB ONNX модель в первый раз и держит её в RAM всё время. Это приемлемо, т.к. модель переиспользуется для всех треков.

Если ctc-forced-aligner недоступен или нет памяти — можно отключить CTC и использовать только пропорциональное разбиение (degraded mode без точных таймингов).

### 10.2 Диск

| Файл | Размер | Время жизни |
|------|--------|-------------|
| Исходный MP3 (3-5 мин) | ~5-8MB | Постоянно (исходник) |
| vocals.mp3 (от MVSEP) | ~3-5MB | Удаляется после alignment |
| instrumental.mp3 (от MVSEP) | ~3-5MB | Постоянно |
| vad_vocals.wav (16kHz) | ~10MB | Удаляется после Whisper |
| ctc модель (MMS-300m) | ~300MB | Постоянно в /data/models |
| paraphrase-multilingual | ~80MB | Постоянно в /data/models |

Временные файлы: пик ~25MB на трек, очищаются в `finally`.

### 10.3 Время обработки трека (3 мин трек)

| Шаг | Время | Тип |
|-----|-------|-----|
| MVSEP separation (upload + wait + download) | ~150s | API, async |
| VAD | ~2s | CPU local |
| Whisper ASR (upload + inference) | ~15s | API, async |
| LyricsSearcher (GPT) | ~3s | API, async |
| CTC alignment (word + char) | ~22s | CPU local (thread) |
| detect_line_breaks | ~1s | CPU local |
| FeatureExtractor | ~10s | CPU local (thread, параллельно с A) |
| LyricEmbedder | ~5s | CPU local (thread, после lyrics found) |
| QDrant sync | ~1s | local |
| **ИТОГО** | **~200s (~3.3 мин)** | |

Доминирует MVSEP (~75% времени). Всё остальное параллелится.

---

## 11. Миграция из v3-rc1: что переиспользуется

### 11.1 Компоненты shared/ — копируются из v3-rc1 без изменений

| Файл | Статус |
|------|--------|
| `karaoke_shared/models/track.py` | Из v3-rc1 без изменений |
| `karaoke_shared/models/job.py` | Из v3-rc1 без изменений |
| `karaoke_shared/repositories/sqlite_repository.py` | Из v3-rc1 + новый метод `record_api_cost`, `get_monthly_costs` |
| `karaoke_shared/repositories/qdrant_repository.py` | Из v3-rc1 без изменений |
| `karaoke_shared/services/job_service.py` | Из v3-rc1 без изменений (включая `mark_step`) |
| `karaoke_shared/utils/syllabifier.py` | Из v3-rc1 без изменений |
| `karaoke_shared/utils/line_breaker.py` | Из v3-rc1 без изменений |
| `karaoke_shared/ml/feature_extractor.py` | Из v3-rc1 без изменений |
| `karaoke_shared/ml/lyric_embedder.py` | Из v3-rc1 без изменений (local вариант) |

### 11.2 Worker pipeline — что копируется из v3-rc1

| Файл v3-rc1 | Судьба в v3-rc2 |
|-------------|----------------|
| `pipeline/lyrics_searcher.py` | **КОПИЯ** — LLM identify + Genius + web search |
| `pipeline/vad_processor.py` | **КОПИЯ** — librosa VAD |
| `pipeline/ctc_aligner.py` | **КОПИЯ** — hybrid CTC alignment |
| `pipeline/uvr_separator.py` | **УДАЛИТЬ** — заменён MVSEP API |
| `pipeline/whisper_transcriber.py` | **УДАЛИТЬ** — заменён OpenAI Whisper API |
| `pipeline/audio_pipeline.py` | **ПЕРЕПИСАТЬ** — новая оркестрация с MVSEP + Whisper API |
| `app/config.py` | **МОДИФИЦИРОВАТЬ** — добавить MVSEP + Whisper API vars, убрать UVR/faster-whisper |
| `app/main.py` | **МОДИФИЦИРОВАТЬ** — wire-up новых компонентов |

### 11.3 init.sql — минимальные изменения

Добавить только таблицу `api_costs` (см. раздел 4.7). Все остальные таблицы без изменений.

### 11.4 Backend — без изменений

Backend v3-rc1 переиспользуется полностью. Добавить только один endpoint `/admin/costs` (опционально).

### 11.5 Frontend — без изменений

Frontend v3-rc1 переиспользуется полностью.

### 11.6 НЕ нужен перенос из эксперимента

CTC aligner уже реализован в v3-rc1 (`pipeline/ctc_aligner.py`) — просто скопировать.
LyricsSearcher тоже уже реализован в v3-rc1 — просто скопировать.
Переносить код из `m3_test/variant_ctc/experiment_hybrid.py` **не нужно** — это уже сделано в v3-rc1.

---

## 12. Стратегия тестирования

### 12.1 Структура тестов

```
worker/
└── tests/
    ├── __init__.py
    ├── conftest.py                    ← фикстуры: mock_httpx_client, tmp_dirs
    │
    ├── unit/
    │   ├── test_mvsep_client.py       ← unit тест MVSEPClient
    │   ├── test_whisper_client.py     ← unit тест WhisperAPIClient
    │   ├── test_lyrics_searcher.py    ← unit тест LyricsSearcher (из v3-rc1)
    │   ├── test_vad_processor.py      ← unit тест VADProcessor
    │   ├── test_ctc_aligner.py        ← unit тест CTCAligner (с моком emissions)
    │   └── test_cost_calculator.py   ← unit тест формул стоимости
    │
    └── integration/
        ├── test_pipeline_full.py      ← полный пайплайн с моками всех API
        └── test_pipeline_no_lyrics.py ← сценарий: LLM не нашёл текст
```

### 12.2 Мокирование API

**Подход:** использовать `respx` (HTTP mock для httpx) или `unittest.mock.AsyncMock`.

```python
# tests/conftest.py
import pytest
import respx
import httpx

@pytest.fixture
def mock_mvsep():
    """Мок MVSEP API: create → poll → download."""
    with respx.mock:
        # POST /api/separation/create
        respx.post("https://mvsep.com/api/separation/create").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"id": "job-123"}})
        )
        # GET /api/separation/get?id=job-123
        respx.get(
            url__regex=r"https://mvsep\.com/api/separation/get\?.*id=job-123.*"
        ).mock(return_value=httpx.Response(200, json={
            "success": True,
            "data": {
                "status": "finished",
                "output_files": [
                    {"filename": "track_vocals.mp3", "large_path": "http://cdn.mvsep.com/v.mp3"},
                    {"filename": "track_instrumental.mp3", "large_path": "http://cdn.mvsep.com/i.mp3"},
                ]
            }
        }))
        # Скачивание файлов
        respx.get("http://cdn.mvsep.com/v.mp3").mock(
            return_value=httpx.Response(200, content=b"fake_vocals_audio")
        )
        respx.get("http://cdn.mvsep.com/i.mp3").mock(
            return_value=httpx.Response(200, content=b"fake_instrumental_audio")
        )
        yield

@pytest.fixture
def mock_openai_whisper():
    """Мок OpenAI Whisper API."""
    with respx.mock:
        respx.post("https://api.openai.com/v1/audio/transcriptions").mock(
            return_value=httpx.Response(200, json={
                "text": "ты помнишь как всё начиналось",
                "language": "russian",
            })
        )
        yield

@pytest.fixture
def mock_openai_chat():
    """Мок gpt-4o-mini для идентификации песни (v3-rc1 LyricsSearcher)."""
    with respx.mock:
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "found": True,
                    "artist": "Машина Времени",
                    "title": "Поворот",
                    "confidence": "high",
                })}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            })
        )
        yield

@pytest.fixture
def mock_genius():
    """Мок Genius API для LyricsSearcher."""
    with respx.mock:
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={
                "response": {"hits": [{"result": {
                    "url": "https://genius.com/song-lyrics",
                    "full_title": "Поворот by Машина Времени",
                }}]}
            })
        )
        respx.get("https://genius.com/song-lyrics").mock(
            return_value=httpx.Response(200, text=(
                '<div data-lyrics-container="true">'
                'Ты помнишь, как всё начиналось<br>'
                'Мы были молоды и влюблены</div>'
            ))
        )
        yield
```

### 12.3 Ключевые unit-тесты

**test_mvsep_client.py:**
```python
async def test_separate_success(mock_mvsep, tmp_path):
    """Успешная сепарация: создаёт job, дожидается, скачивает."""
    client = MVSEPClient(api_key="test-key", media_root=str(tmp_path))
    # Создать фейковый MP3
    fake_mp3 = tmp_path / "track.mp3"
    fake_mp3.write_bytes(b"fake_mp3_data")

    result = await client.separate(str(fake_mp3))

    assert Path(result.vocals_path).exists()
    assert Path(result.instrumental_path).exists()
    assert "vocals" in result.vocals_path.lower() or "vocal" in result.vocals_path.lower()

async def test_separate_timeout(tmp_path):
    """Таймаут: бросает MVSEPTimeoutError."""
    with respx.mock:
        respx.post(...).mock(return_value=httpx.Response(200, json={"data": {"id": "j1"}}))
        respx.get(...).mock(return_value=httpx.Response(200, json={"data": {"status": "processing"}}))

        client = MVSEPClient(api_key="k", timeout_sec=0.01, poll_interval_sec=0.001, media_root=str(tmp_path))
        fake_mp3 = tmp_path / "t.mp3"
        fake_mp3.write_bytes(b"x")

        with pytest.raises(MVSEPTimeoutError):
            await client.separate(str(fake_mp3))

async def test_separate_retries_on_5xx(tmp_path):
    """5xx ошибки ретраятся, после успеха возвращает результат."""
    ...
```

**test_ctc_aligner.py:**
```python
def test_align_proportional_fallback():
    """Если emissions слишком узкие — используется proportional."""
    # Мокать AlignmentSingleton чтобы не загружать 300MB модель в тестах
    from unittest.mock import MagicMock, patch

    aligner = CTCAligner.__new__(CTCAligner)
    aligner._syllabifier = Syllabifier()
    aligner._model = MagicMock()
    aligner._tokenizer = MagicMock()

    # Передать слишком узкий emissions slice → ожидаем proportional
    import numpy as np
    tiny_emissions = np.zeros((2, 100), dtype=np.float32)  # 2 фрейма < MIN_FRAMES_FOR_CHAR

    result = aligner._run_char_alignment_on_slice(
        tiny_emissions, stride_ms=20, word_text="привет", language="ru"
    )
    assert result is None  # должен вернуть None → proportional fallback

def test_build_hybrid_syllable_timings_shape():
    """Результат содержит правильное количество слогов."""
    ...
```

**test_pipeline_full.py:**
```python
async def test_full_pipeline_success(
    mock_mvsep, mock_openai_whisper, mock_openai_chat, mock_genius,
    tmp_path, sqlite_db
):
    """Полный пайплайн: все шаги успешны."""
    # Создать Job + Track в SQLite
    # Мокать CTCAligner (не загружать модель)
    # Мокать FeatureExtractor
    # Мокать QDrantRepository

    pipeline = AudioPipeline(
        mvsep=MVSEPClient(api_key="k", media_root=str(tmp_path)),
        whisper=WhisperAPIClient(api_key="k"),
        lyrics_searcher=LyricsSearcher(
            openai_api_key="k", genius_token="g",  # v3-rc1 интерфейс
        ),
        vad=VADProcessor(),
        ctc_aligner=MockCTCAligner(),   # возвращает фиксированные timings
        feature_extractor=MockFeatureExtractor(),
        lyric_embedder=MockEmbedder(),
        qdrant_repo=MockQDrantRepo(),
        ...
    )

    await pipeline.process(job)

    track = await repo.get_track(job.track_id)
    assert track.status == "ready"
    assert track.lyrics_text is not None
    assert track.syllable_timings is not None
    assert track.instrumental_path is not None
```

### 12.4 Тест деградированного режима

```python
async def test_lyrics_not_found(mock_mvsep, mock_openai_whisper, tmp_path, sqlite_db):
    """LyricsNotFoundError → трек error, но audio features синхронизированы."""
    with respx.mock:
        # gpt-4o-mini возвращает found=false
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "found": False,
                    "not_found_reason": "Cannot identify song",
                })}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            })
        )
        # Genius тоже пуст
        respx.get("https://api.genius.com/search").mock(
            return_value=httpx.Response(200, json={"response": {"hits": []}})
        )
        # Web search fallback тоже не нашёл
        respx.post(url__regex=r".*/v1/responses").mock(
            return_value=httpx.Response(200, json={
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": '{"found":false,"reason":"not found"}'}
                ]}]
            })
        )

        await pipeline.process(job)

        track = await repo.get_track(job.track_id)
        assert track.status == "error"          # error, не ready (как в v3-rc1)
        assert "Lyrics not found" in track.error_message
        assert track.instrumental_path is not None  # инструментал есть
```

### 12.5 Запуск тестов

```bash
# В dev-окружении (conda bootstrap или virtualenv)
cd v3-rc2/worker
pip install -e ".[test]"
pytest tests/unit/ -v                    # быстрые unit, без модели
pytest tests/integration/ -v             # с моками API, без реальных вызовов

# Реальный интеграционный тест (нужны API ключи)
MVSEP_API_KEY=xxx OPENAI_API_KEY=sk-... pytest tests/e2e/ -v -s
```

---

## 13. Модель стоимости

### 13.1 Стоимость на трек

| Сервис | Цена | На трек (3 мин) |
|--------|------|----------------|
| MVSEP (sep_type=49) | $0.15/трек | $0.15 |
| OpenAI Whisper | $0.006/мин | $0.018 (3 мин вокала) |
| LyricsSearcher primary (identify + Genius) | ~$0.0005/вызов | $0.0005 |
| LyricsSearcher fallback (web_search, ~10% случаев) | ~$0.003/вызов | $0.0003 avg |
| OpenAI Embed (если local=off) | $0.000002/токен | $0.0001 |
| **Итого** | | **~$0.17/трек** |

### 13.2 Месячная стоимость

| Объём | API ($) | VPS ($) | Итого |
|-------|---------|---------|-------|
| 50 треков/мес | $8.50 | $10 | **$18.50** |
| 100 треков/мес | $17.00 | $10 | **$27.00** |
| 300 треков/мес | $51.00 | $10 | **$61.00** |
| 500 треков/мес | $85.00 | $10 | **$95.00** |
| 1000 треков/мес | $170.00 | $10 | **$180.00** |

### 13.3 Сравнение с альтернативами

**LALAL.AI вместо MVSEP:**
- $0.28/трек vs $0.15/трек (+87%)
- Качество выше, но для каробоке разница незначительна
- Не рекомендуется

**Deepgram вместо Whisper:**
- $0.0043/мин vs $0.006/мин (-28%)
- Хуже для русского языка
- Не рекомендуется если >50% треков на русском

**Стратегия снижения стоимости:**
- Если треки уже есть в каталоге — не обрабатывать повторно (дедупликация по MD5 файла перед отправкой в очередь)
- Сохранять ASR-текст из Whisper в отдельном поле — при повторной обработке с другим текстом песни можно пропустить шаг Whisper
- MVSEP: проверить `add_to_cloud=0` — не хранить файлы на их серверах (приватность + экономия если есть платный тариф)

---

## Приложение: Порядок реализации (для разработчика)

Реализовывать в следующем порядке:

1. Скопировать из v3-rc1 (без изменений):
   - `shared/` — symlink или копия `v3-rc1/shared/`
   - `pipeline/lyrics_searcher.py` — LLM identify + Genius + web search
   - `pipeline/vad_processor.py` — librosa VAD
   - `pipeline/ctc_aligner.py` — hybrid CTC alignment
2. `config.py` — расширить v3-rc1 конфиг: убрать UVR/faster-whisper vars, добавить MVSEP + Whisper API vars.
3. `utils/retry.py` — переиспользуется во всех клиентах.
4. `pipeline/mvsep_client.py` + тест — самый дорогой шаг, тестировать сразу.
5. `pipeline/whisper_client.py` + тест — OpenAI Whisper API (заменяет faster-whisper).
6. `pipeline/openai_embedder.py` + тест (только если LYRIC_EMBEDDER_BACKEND=openai нужен)
7. Обновить `shared/karaoke_shared/repositories/sqlite_repository.py`: добавить `record_api_cost`, `get_monthly_costs` + миграция init.sql
8. `pipeline/audio_pipeline.py` — оркестратор: MVSEP вместо UVR, Whisper API вместо faster-whisper, остальное из v3-rc1.
9. `main.py` — точка входа, wire всё вместе (убрать GPU-компоненты, добавить MVSEP/Whisper API).
10. `Dockerfile` + `docker-compose.yml` — финальная сборка (CPU-only, без CUDA).
11. Интеграционные тесты с моками.
12. E2E тест с реальными ключами на 1 треке.

---

*Конец плана. Версия: v3-rc2-plan-v1.1 от 2026-03-08. Обновлено после завершения v3-rc1.*
