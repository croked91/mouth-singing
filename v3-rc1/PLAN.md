# v3-rc1: Plan реализации — вариант с выделенным GPU-сервером

**Целевое железо:** Intel Core i3-14100, 32 GB DDR5, Tesla T4 16 GB, 2 TB NVMe
**Цель:** максимально локальный пайплайн, внешнее API только для поиска текста песни
**Дата плана:** 2026-03-07

---

## Содержание

1. [Обзор изменений относительно v2](#1-обзор-изменений-относительно-v2)
2. [Структура директорий](#2-структура-директорий)
3. [Новые классы и модули](#3-новые-классы-и-модули)
4. [Оркестрация пайплайна](#4-оркестрация-пайплайна)
5. [Конфигурация и переменные окружения](#5-конфигурация-и-переменные-окружения)
6. [Docker-окружение](#6-docker-окружение)
7. [Обработка ошибок и цепочки fallback](#7-обработка-ошибок-и-цепочки-fallback)
8. [Управление моделями](#8-управление-моделями)
9. [Миграция с v2](#9-миграция-с-v2)
10. [Зависимости (pip)](#10-зависимости-pip)
11. [Оценка ресурсов](#11-оценка-ресурсов)
12. [Стратегия тестирования](#12-стратегия-тестирования)

---

## 1. Обзор изменений относительно v2

| Компонент         | v2                              | v3-rc1                                           |
|-------------------|---------------------------------|--------------------------------------------------|
| UVR-модель        | 2_HP-UVR.pth, CPU, ~1 мин       | BS-Roformer, T4 GPU, ~60-90 с                    |
| ASR               | Soniox API (внешний)            | faster-whisper tiny/base, T4 GPU, ~5-10 с        |
| VAD               | отсутствует                     | librosa.effects.split, CPU, перед ASR            |
| Поиск текста      | отсутствует (Soniox = ASR+текст)| OpenAI gpt-4o-mini, ~$0.001/трек                 |
| Выравнивание      | BPE-токены Soniox → pyphen      | CTC-гибрид (word+char), ~22 с CPU               |
| Embedding модель  | CPU (sentence-transformers)     | GPU (sentence-transformers на T4) или CPU        |
| Внешние API       | Soniox (ASR+текст)              | только OpenAI (поиск текста)                     |

**Единственный внешний вызов в продакшн-пайплайне — OpenAI API для поиска текста песни.**
Если API недоступен — трек получает статус `error`, пользователь может ввести текст вручную через UI (этот flow уже есть в v2, только надо убедиться что endpoint `/tracks/{id}` поддерживает PATCH с `lyrics_text`).

---

## 2. Структура директорий

Полное дерево нового и изменённого кода. Файлы, помеченные `[NEW]`, создаются с нуля; `[COPY]` — копируются из v2 без изменений; `[MODIFY]` — копируются и изменяются.

```
v3-rc1/
├── PLAN.md                              # этот файл
│
├── worker/                              # единственный изменённый сервис
│   ├── Dockerfile                       # [NEW] CUDA base image + GPU deps
│   ├── pyproject.toml                   # [MODIFY] новые зависимости
│   ├── entrypoint.sh                    # [COPY] из v2/worker/
│   │
│   └── app/
│       ├── __init__.py                  # [COPY]
│       ├── main.py                      # [MODIFY] wire-up новых компонентов
│       ├── config.py                    # [MODIFY] новые env-переменные
│       │
│       └── pipeline/
│           ├── __init__.py              # [COPY]
│           ├── audio_pipeline.py        # [NEW] новая оркестрация 10 шагов
│           ├── uvr_separator.py         # [MODIFY] добавить GPU device param
│           ├── vad_processor.py         # [NEW] VAD через librosa
│           ├── whisper_transcriber.py   # [NEW] faster-whisper wrapper
│           ├── lyrics_searcher.py       # [NEW] OpenAI-based поиск текста
│           └── ctc_aligner.py           # [NEW] гибридный CTC-алайнер
│
├── shared/                              # [COPY] v2/shared/ без изменений
│   └── karaoke_shared/
│       ├── models/track.py
│       ├── repositories/
│       ├── services/
│       ├── utils/
│       │   ├── syllabifier.py
│       │   └── line_breaker.py
│       └── ml/
│           ├── feature_extractor.py
│           └── lyric_embedder.py
│
├── backend/                             # [COPY] v2/backend/ без изменений
├── frontend/                            # [COPY] v2/frontend/ без изменений
│
├── docker-compose.yml                   # [COPY] v2/docker-compose.yml
└── docker-compose.prod.yml              # [MODIFY] GPU passthrough для worker
```

**Важно:** backend, frontend, shared — не изменяются. Вся работа — только в `v3-rc1/worker/`.

---

## 3. Новые классы и модули

### 3.1 `VADProcessor` — `app/pipeline/vad_processor.py`

**Назначение:** очистить вокальный трек от тишины перед подачей в ASR. Уменьшает длину обрабатываемого аудио на 20-40%, сокращая время Whisper.

**Зависимости:** `librosa`, `numpy`, `soundfile`

```python
class VADProcessor:
    """
    Убирает тишину из вокального WAV-файла.

    Использует librosa.effects.split для обнаружения вокальных сегментов
    и конкатенирует их в один массив.

    Результат: temporray WAV-файл cleaned_vocals.wav рядом с источником.
    """

    def __init__(self, top_db: int = 35) -> None:
        """
        Args:
            top_db: Порог в dB ниже пика для определения тишины.
                    35 dB — хорошее значение для вокала; меньше = строже.
        """

    def process(self, vocals_path: str) -> str:
        """
        Обрезает тишину и сохраняет результат.

        Args:
            vocals_path: Абсолютный путь к vocals.wav от UVR.

        Returns:
            Абсолютный путь к очищенному WAV-файлу.
            Если загрузка не удалась — возвращает исходный vocals_path.
            Если после VAD ничего не осталось (< 1 секунды) — возвращает
            исходный vocals_path и логирует предупреждение.

        Side effects:
            Создаёт файл <vocals_dir>/cleaned_vocals.wav.
            НЕ удаляет исходный vocals.wav (это делает AudioPipeline после CTC).
        """
```

**Алгоритм `process()`:**

```
1. librosa.load(vocals_path, sr=16000, mono=True)  # 16kHz для Whisper
2. intervals = librosa.effects.split(y, top_db=top_db, frame_length=2048, hop_length=512)
3. Если intervals пустой → вернуть исходный файл
4. voiced_segments = [y[start:end] for start, end in intervals]
5. cleaned = numpy.concatenate(voiced_segments)
6. Если len(cleaned) / 16000 < 1.0 → вернуть исходный файл
7. soundfile.write(out_path, cleaned, 16000, subtype='PCM_16')
8. Вернуть out_path
```

**Почему 16kHz:** faster-whisper ожидает 16kHz, librosa.load сразу даёт нужный sr, soundfile сохраняет без конвертации.

---

### 3.2 `WhisperTranscriber` — `app/pipeline/whisper_transcriber.py`

**Назначение:** локальная ASR для идентификации трека. Точность не критична — нам нужен примерный текст для передачи в LLM. Ошибки OCR в 20-30% слов допустимы.

**Зависимости:** `faster-whisper>=1.0.3`

```python
from dataclasses import dataclass

@dataclass
class WhisperResult:
    text: str           # полный текст, сегменты объединены через ' '
    language: str       # двухбуквенный код ('ru', 'en', ...)
    confidence: float   # среднее log-prob по сегментам (0..1 после exp)


class WhisperTranscriber:
    """
    Обёртка над faster-whisper для локальной ASR.

    Модель загружается один раз при создании объекта и держится в памяти.
    Методы вызываются синхронно; для async-кода использовать asyncio.to_thread.

    Args:
        model_size: 'tiny' (~70MB, ~5с на T4) или 'base' (~140MB, ~10с на T4).
                    Для продакшна рекомендуется 'tiny' — нам нужна только
                    идентификация, а не точный текст.
        device: 'cuda' или 'cpu'. По умолчанию 'cuda' если доступна.
        compute_type: 'float16' для GPU (быстро), 'int8' для CPU (быстро).
        model_cache_dir: Директория для кэша Hugging Face моделей.
        language_hints: Список языков для подсказки модели ['ru', 'en'].
                        Если None — автодетект (чуть медленнее).
    """

    def __init__(
        self,
        model_size: str = "tiny",
        device: str = "cuda",
        compute_type: str = "float16",
        model_cache_dir: str | None = None,
        language_hints: list[str] | None = None,
    ) -> None:
        ...  # загрузка модели, сохранение в self._model

    def transcribe(self, audio_path: str) -> WhisperResult:
        """
        Транскрибирует аудиофайл.

        Args:
            audio_path: Путь к WAV-файлу (желательно 16kHz mono после VAD).

        Returns:
            WhisperResult с text, language, confidence.

        Raises:
            RuntimeError: Если faster-whisper не смог загрузить аудио.

        Note:
            Параметры вызова model.transcribe:
              - beam_size=1 (greedy, быстрее; качество для идентификации достаточно)
              - vad_filter=False (VAD уже сделан нами)
              - language=None (автодетект по первым 30с)
              - condition_on_previous_text=False (независимые сегменты)
        """

    def cleanup(self) -> None:
        """Освобождает VRAM. Вызывается после завершения транскрипции."""
```

**Детали реализации `transcribe()`:**

```python
def transcribe(self, audio_path: str) -> WhisperResult:
    segments_gen, info = self._model.transcribe(
        audio_path,
        beam_size=1,
        vad_filter=False,
        language=None,  # автодетект
        condition_on_previous_text=False,
        temperature=0.0,  # детерминированный вывод
    )

    segments = list(segments_gen)  # материализуем генератор

    if not segments:
        return WhisperResult(text="", language=info.language, confidence=0.0)

    text = " ".join(s.text.strip() for s in segments if s.text.strip())

    # Среднее log-prob → prob
    avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
    confidence = min(1.0, max(0.0, math.exp(avg_logprob)))

    return WhisperResult(
        text=text,
        language=info.language,
        confidence=confidence,
    )
```

---

### 3.3 `LyricsSearcher` — `app/pipeline/lyrics_searcher.py`

**Назначение:** получить точный текст песни по приблизительному распознаванию. Это ключевое звено пайплайна: без точного текста CTC-выравнивание невозможно.

**Зависимости:** `httpx>=0.27` (уже есть), `openai>=1.30` — или напрямую через httpx (без SDK для минимизации зависимостей).

**Выбор:** использовать `httpx` напрямую, без openai SDK. Причина: openai SDK тянет лишние зависимости, а нам нужен один endpoint.

```python
from dataclasses import dataclass

@dataclass
class LyricsResult:
    artist: str          # найденный исполнитель
    title: str           # найденное название
    lyrics: str          # полный текст с переносами строк
    language: str        # 'ru' | 'en' | 'other'
    confidence: str      # 'high' | 'medium' | 'low' — самооценка LLM
    source_note: str     # откуда взял ('genius', 'azlyrics', 'yandex.music', etc.)


class LyricsSearchError(Exception):
    """Базовый класс ошибок поиска текста."""

class LyricsNotFoundError(LyricsSearchError):
    """LLM явно ответил что текст не найден."""

class LyricsAPIError(LyricsSearchError):
    """Ошибка сети или API (retryable)."""


class LyricsSearcher:
    """
    Поиск текста песни через OpenAI gpt-4o-mini.

    LLM получает на вход приблизительный ASR-текст + опциональные метаданные
    (artist, title из filename) и возвращает структурированный JSON с
    найденным текстом или явным отказом.

    Стоимость: ~$0.001/трек (gpt-4o-mini: $0.15/1M input tokens,
    запрос ~500 токенов + ответ ~1000 токенов = $0.000225 + $0.0006 ≈ $0.001).

    Args:
        api_key: OpenAI API key.
        model: Модель для использования. По умолчанию 'gpt-4o-mini'.
        timeout: HTTP-таймаут в секундах. По умолчанию 30.
        max_retries: Количество повторных попыток при 5xx ошибках.
        base_url: Для тестирования с mock-сервером.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        timeout: float = 30.0,
        max_retries: int = 2,
        base_url: str = "https://api.openai.com",
    ) -> None:
        ...

    async def search(
        self,
        asr_text: str,
        detected_language: str,
        artist_hint: str | None = None,
        title_hint: str | None = None,
    ) -> LyricsResult:
        """
        Ищет текст песни по ASR-тексту.

        Args:
            asr_text: Приблизительный текст от Whisper (может содержать ошибки).
            detected_language: Язык, определённый Whisper ('ru', 'en', ...).
            artist_hint: Имя исполнителя из имени файла (если есть).
            title_hint: Название песни из имени файла (если есть).

        Returns:
            LyricsResult с найденным текстом.

        Raises:
            LyricsNotFoundError: Если LLM явно сообщил что текст не найден.
            LyricsAPIError: Если запрос к API завершился ошибкой.

        Note:
            Метод async, но внутри использует httpx.AsyncClient.
        """
```

**Prompt для LLM (системный):**

```
You are a music lyrics assistant. The user will give you an approximate speech recognition
transcript of a song (may contain errors), the detected language, and optionally the artist/title.

Your task:
1. Identify the song from the transcript.
2. Find the COMPLETE and ACCURATE lyrics for the song.
3. Return a JSON object with these fields:
   - "found": true/false
   - "artist": string (canonical artist name)
   - "title": string (canonical song title)
   - "lyrics": string (full lyrics with \n line breaks, verses separated by \n\n)
   - "language": "ru" | "en" | "other"
   - "confidence": "high" | "medium" | "low"
   - "source_note": string (brief note about the source, e.g. "genius.com")
   - "not_found_reason": string (only if found=false)

IMPORTANT:
- Return ONLY the JSON, no markdown, no explanation.
- If you cannot identify the song or find lyrics, return found=false.
- The lyrics must be COMPLETE, not just the first verse.
- Preserve the original line structure (verse/chorus/bridge separations).
- Do NOT add [Verse], [Chorus] labels — just the lyrics text.
```

**Пользовательский prompt:**

```
Approximate transcript (may have errors): {asr_text}
Detected language: {detected_language}
Artist hint: {artist_hint or 'unknown'}
Title hint: {title_hint or 'unknown'}
```

**Детали retry-логики:** повторяем при HTTP 429 (rate limit) с задержкой 5с, при 5xx — с задержкой 2с. При 4xx (кроме 429) — не повторяем.

**Парсинг ответа:**

```python
# LLM возвращает JSON строку
data = json.loads(response_text)
if not data.get("found", False):
    raise LyricsNotFoundError(data.get("not_found_reason", "not found"))
return LyricsResult(
    artist=data["artist"],
    title=data["title"],
    lyrics=data["lyrics"],
    language=data["language"],
    confidence=data["confidence"],
    source_note=data.get("source_note", ""),
)
```

**Защита от некорректного JSON:** если LLM вернул невалидный JSON (bывает ~1%), попробовать найти JSON-объект в ответе через регулярное выражение `r'\{.*\}'` с флагом `re.DOTALL`. Если не нашли — `LyricsAPIError("Invalid JSON response")`.

---

### 3.4 `CTCAligner` — `app/pipeline/ctc_aligner.py`

**Назначение:** выравнивание текста песни с аудио, получение тайминга на уровне слогов. Реализует гибридный алгоритм из `m3_test/variant_ctc/experiment_hybrid.py`.

**Зависимости:** `ctc-forced-aligner==1.0.2`, `numpy`

```python
from dataclasses import dataclass, field

@dataclass
class AlignmentStats:
    """Статистика выравнивания для мониторинга качества."""
    total_words: int = 0
    char_level_used: int = 0      # слова, выровненные char-CTC
    proportional_fallback: int = 0  # слова, выровненные пропорционально


class CTCAligner:
    """
    Гибридное CTC-выравнивание: word-level boundary + per-word char-level.

    Алгоритм:
      1. load_audio(vocals_path) → waveform 16kHz numpy float32
      2. generate_emissions(model, waveform) ОДИН РАЗ → emissions (T, V) + stride_ms
      3. get_alignments на полных emissions → word timings
      4. Для каждого слова:
         a. Слайс emissions[frame_start:frame_end]
         b. Если frames < MIN_FRAMES (10): proportional fallback
         c. Иначе: char-level get_alignments на слайсе
         d. Если char-count mismatch или ошибка: proportional fallback
         e. Иначе: собрать слоги из char timings через pyphen
      5. Вернуть список SyllableTiming

    КРИТИЧЕСКИ ВАЖНО:
      - generate_emissions вызывается РОВНО ОДИН РАЗ на трек.
        Повторный вызов на разных аудио-слайсах вызывает heap corruption в ONNX Runtime.
      - Перед char-level alignment: проверить n_frames > n_targets (CTC constraint).
        Нарушение этого условия вызывает C++ std::runtime_error — не ловится Python-try.

    Модель:
      - AlignmentSingleton() загружает MMS-300m ONNX (~300MB) при первом вызове.
      - CPU-only; VRAM не используется.
      - Загружается один раз в __init__ и переиспользуется для всех треков.

    Args:
        model_cache_dir: Директория для кэша ONNX-модели.
        syllabifier: Экземпляр Syllabifier для pyphen-разбивки.
        min_frames_for_char: Минимум фреймов для char-CTC (default 10 = ~200ms).
    """

    def __init__(
        self,
        syllabifier,          # karaoke_shared.utils.syllabifier.Syllabifier
        model_cache_dir: str | None = None,
        min_frames_for_char: int = 10,
    ) -> None:
        from ctc_forced_aligner import AlignmentSingleton

        self._min_frames = min_frames_for_char
        self._syllabifier = syllabifier

        # Загрузка модели. AlignmentSingleton — singleton, безопасен для повторного вызова.
        aligner = AlignmentSingleton()
        self._model = aligner.alignment_model
        self._tokenizer = aligner.alignment_tokenizer

    def align(
        self,
        vocals_path: str,
        lyrics_text: str,
        language: str,
    ) -> tuple[list, AlignmentStats]:
        """
        Выравнивает текст с аудио и возвращает тайминги слогов.

        Args:
            vocals_path: Путь к вокальному WAV (16kHz, после VAD).
            lyrics_text: Полный текст с \n (от LyricsSearcher).
            language: Двухбуквенный код ('ru', 'en').

        Returns:
            Кортеж (syllable_timings, stats), где:
            - syllable_timings: list[SyllableTiming] из karaoke_shared.models.track
            - stats: AlignmentStats с метриками качества

        Raises:
            RuntimeError: Если load_audio или generate_emissions падает.
            ValueError: Если lyrics_text пуст или содержит только пробелы.

        Note:
            Метод синхронный. Вызывать через asyncio.to_thread.
            Время выполнения: ~20-25с CPU на трек длиной 3-5 мин.
        """

    # ------------------------------------------------------------------
    # Private helpers (переносятся из experiment_hybrid.py)
    # ------------------------------------------------------------------

    def _compute_emissions(self, waveform) -> tuple:
        """generate_emissions ОДИН РАЗ. Возвращает (emissions, stride_ms)."""

    def _run_word_alignment(
        self, emissions, stride_ms: int, lyrics_flat: str, language: str
    ) -> list[dict]:
        """Word-level CTC на полных emissions. Возвращает [{text, start, end}]."""

    def _run_char_alignment_on_slice(
        self, word_emissions, stride_ms: int, word_text: str, language: str
    ) -> list[dict] | None:
        """
        Char-level CTC на слайсе emissions для одного слова.

        ОБЯЗАТЕЛЬНО проверить n_frames > n_targets перед вызовом get_alignments.
        Возвращает None при любой ошибке (proportional fallback для слова).

        Timings возвращаются ОТНОСИТЕЛЬНО начала слайса (добавить word_start в caller).
        """

    def _build_syllable_timings(
        self,
        word_timestamps: list[dict],
        lyrics_text: str,
        emissions,
        stride_ms: int,
        language: str,
    ) -> tuple[list, AlignmentStats]:
        """Основной loop: word→char→syllable assembly."""

    @staticmethod
    def _lang_flags(language: str) -> tuple[str, bool]:
        """Возвращает (iso639_3, romanize). Например: 'ru' → ('rus', True)."""
        mapping = {"ru": "rus", "en": "eng"}
        lang_iso3 = mapping.get(language, "eng")
        romanize = (language != "en")
        return lang_iso3, romanize

    @staticmethod
    def _time_to_frame(time_sec: float, stride_ms: int) -> int:
        """Перевод времени в индекс фрейма эмиссий."""
        return int(time_sec * 1000 / stride_ms)
```

**Константа `MIN_FRAMES_FOR_CHAR = 10`** соответствует ~200ms при stride=20ms. Слова короче этого порога выравниваются пропорционально.

---

### 3.5 Изменения в `UVRSeparator` — `app/pipeline/uvr_separator.py`

Копируем из v2, добавляем поддержку `torch_device` для запуска BS-Roformer на GPU.

**Изменения:**

1. Добавить параметр `torch_device: str = "cuda"` в `__init__`.
2. Передавать `torch_device` в `Separator(...)`:

```python
self._separator = Separator(
    output_dir=self._output_dir,
    model_file_dir=self.model_cache_dir,
    output_format="MP3",
    torch_device=self.torch_device,  # NEW
    output_single_stem="instrumental",  # NEW: сохраняем только инструментал
)
```

3. Добавить `output_single_stem="instrumental"` — BS-Roformer поддерживает этот параметр, он экономит время на кодировании второго стема. Нам нужен вокал для ASR/CTC, поэтому на самом деле нам нужны ОБА стема. Убрать `output_single_stem`.

4. В `cleanup()` убедиться что вызывается `torch.cuda.empty_cache()` — уже есть в v2, проверить что вызывается.

**Важное замечание по BS-Roformer и audio-separator:**
audio-separator поддерживает BS-Roformer через `model_filename="model_bs_roformer_ep_317_sdr_12.9755.ckpt"`. Файл весит ~640MB и скачивается автоматически при первом запуске.

---

### 3.6 Новый `AudioPipeline` — `app/pipeline/audio_pipeline.py`

Полностью новая оркестрация 10 шагов (см. раздел 4).

---

### 3.7 Изменения в `config.py`

Новые настройки поверх v2 (см. раздел 5).

---

## 4. Оркестрация пайплайна

### 4.1 Граф зависимостей шагов

```
Шаг 1: UVR separation (GPU)
  ├── Шаг 2: Feature extraction (CPU) ←┐
  │                                     ├─ asyncio.gather (параллельно)
  ├── Шаг 3: VAD on vocals (CPU)        │
  │           ↓                         │
  │   Шаг 4: Whisper ASR (GPU) ────────┘ (запускается параллельно с шагом 2)
  │           ↓
  │   Шаг 5: Lyrics search (API)
  │           ↓
  │   Шаг 6+7: CTC alignment (CPU)
  │           ↓
  │   Шаг 8: Line break detection (CPU)
  │           ↓
  │   Шаг 9: Lyric embedding (GPU/CPU)
  │
  └── Шаг 10: QDrant sync
```

**Параллелизм:**
- Шаги 2 (features) и 3+4 (VAD+ASR) выполняются параллельно через `asyncio.gather` после UVR.
- Шаги 6-9 последовательны (каждый зависит от предыдущего).

### 4.2 Псевдокод `AudioPipeline.process()`

```python
async def process(self, job: Job) -> None:
    track = await self.repo.get_track(job.track_id)
    # валидация track.mp3_path...

    try:
        # ================================================================
        # ШАГ 1: UVR separation на GPU (~60-90с)
        # ================================================================
        await self.job_service.mark_step(job.id, "separating", 0)

        vocals_path, instrumental_path = await asyncio.to_thread(
            self.uvr.separate, track.mp3_path
        )
        # self.uvr.cleanup() — НЕ вызываем здесь, ждём завершения ASR
        # (UVR и Whisper не работают одновременно, но cleanup освобождает VRAM
        # которая нужна Whisper следующим шагом)

        await self.uvr.cleanup_async()  # освобождаем VRAM до Whisper

        await self.repo.update_track(job.track_id, TrackUpdate(
            instrumental_path=instrumental_path, status="processing"
        ))
        await self.job_service.mark_step(job.id, "separating", 100)

        # ================================================================
        # ПАРАЛЛЕЛЬНО: Шаг 2 (features from MP3) + Шаги 3+4 (VAD+ASR on vocals)
        # ================================================================
        await self.job_service.mark_step(job.id, "extracting_features", 0)
        await self.job_service.mark_step(job.id, "transcribing", 0)

        feature_vector, whisper_result = await asyncio.gather(
            self._extract_features(track.mp3_path, job.id),
            self._vad_and_transcribe(vocals_path, job.id),
        )

        # Whisper больше не нужен в VRAM
        await asyncio.to_thread(self.whisper.cleanup)

        # ================================================================
        # ШАГ 5: Поиск текста через LLM (~2-5с сетевой вызов)
        # ================================================================
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
            # Текст не найден — трек помечается как error, но не блокирует очередь
            await self.repo.update_track(job.track_id, TrackUpdate(
                status="error",
                error_message=f"Lyrics not found: {exc}",
                # feature_vector уже есть — сохраняем его
            ))
            if feature_vector:
                await self._sync_qdrant_audio_only(job.track_id, track, feature_vector)
            await self.job_service.mark_failed(job.id, f"Lyrics not found: {exc}")
            return

        await self.job_service.mark_step(job.id, "searching_lyrics", 100)

        # Обновляем artist/title если LLM нашёл правильные
        await self.repo.update_track(job.track_id, TrackUpdate(
            artist=lyrics_result.artist,
            title=lyrics_result.title,
            lyrics_text=lyrics_result.lyrics,
            language=lyrics_result.language,
        ))

        # ================================================================
        # ШАГИ 6+7: CTC-выравнивание (~20-25с CPU)
        # vocals_path уже существует (удалим после)
        # ================================================================
        await self.job_service.mark_step(job.id, "aligning", 0)

        syllable_timings, align_stats = await asyncio.to_thread(
            self.ctc_aligner.align,
            vocals_path,            # WAV от UVR (или cleaned_vocals от VAD)
            lyrics_result.lyrics,
            lyrics_result.language,
        )

        await self.job_service.mark_step(job.id, "aligning", 100)
        logger.info(
            "ctc_alignment_done",
            job_id=job.id,
            total_words=align_stats.total_words,
            char_level_pct=align_stats.char_level_used / max(align_stats.total_words, 1),
        )

        # ================================================================
        # ШАГ 8: Line break detection (CPU, быстро <1с)
        # ================================================================
        from karaoke_shared.utils.line_breaker import detect_line_breaks

        syllable_timings = await asyncio.to_thread(
            detect_line_breaks, syllable_timings, None
            # vocals_path=None: lyrics уже содержат \n, detect_line_breaks
            # вернёт их нетронутыми (see: "already-marked" check в line_breaker.py)
        )

        # Удаляем вокальный WAV — больше не нужен
        Path(vocals_path).unlink(missing_ok=True)
        # cleaned_vocals.wav (если создавался VADProcessor) тоже удаляем
        cleaned_path = Path(vocals_path).parent / "cleaned_vocals.wav"
        cleaned_path.unlink(missing_ok=True)

        await self.repo.update_track(job.track_id, TrackUpdate(
            syllable_timings=syllable_timings,
            status="processing",
        ))

        # ================================================================
        # ШАГ 9: Lyric embedding (GPU ~1-2с или CPU ~5с)
        # ================================================================
        await self.job_service.mark_step(job.id, "embedding_lyrics", 0)

        lyric_vector = await asyncio.to_thread(
            self.lyric_embedder.embed, lyrics_result.lyrics
        )

        await self.job_service.mark_step(job.id, "embedding_lyrics", 100)

        # ================================================================
        # ШАГ 10: QDrant sync
        # ================================================================
        await self._sync_qdrant(job.id, job.track_id, track, feature_vector, lyric_vector)

        # Финализация
        await self.repo.update_track(job.track_id, TrackUpdate(
            status="ready", qdrant_synced=1
        ))
        await self.job_service.mark_completed(job.id, {
            "instrumental_path": instrumental_path,
            "language": lyrics_result.language,
            "align_stats": {
                "total_words": align_stats.total_words,
                "char_level_used": align_stats.char_level_used,
            },
        })

    except Exception as exc:
        logger.error("pipeline_failed", job_id=job.id, error=str(exc), exc_info=True)
        await self.job_service.mark_failed(job.id, str(exc))
```

### 4.3 Вспомогательные методы

```python
async def _extract_features(self, mp3_path: str, job_id: str) -> list[float] | None:
    """Обёртка для asyncio.to_thread с логированием."""
    result = await asyncio.to_thread(self.feature_extractor.extract, mp3_path)
    await self.job_service.mark_step(job_id, "extracting_features", 100)
    return result

async def _vad_and_transcribe(self, vocals_path: str, job_id: str):
    """VAD + Whisper последовательно, возвращает WhisperResult."""
    # VAD: очистка тишины
    cleaned_path = await asyncio.to_thread(self.vad_processor.process, vocals_path)
    # Whisper ASR
    result = await asyncio.to_thread(self.whisper.transcribe, cleaned_path)
    await self.job_service.mark_step(job_id, "transcribing", 100)
    return result

def _parse_hints_from_path(self, mp3_path: str) -> tuple[str | None, str | None]:
    """
    Извлекает artist/title из имени файла.
    Ожидаемые форматы:
      - "Artist - Title.mp3"
      - "Artist_Title.mp3"
    Если формат не распознан — возвращает (None, None).
    """
    name = Path(mp3_path).stem
    if " - " in name:
        parts = name.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return None, None

async def uvr_cleanup_async(self):
    """cleanup() через to_thread (torch.cuda.empty_cache блокирует)."""
    await asyncio.to_thread(self.uvr.cleanup)
```

### 4.4 Шаги и их статусы в job_service

| Шаг | mark_step label        | Параллельность |
|-----|------------------------|----------------|
| 1   | `separating`           | нет            |
| 2   | `extracting_features`  | параллельно с 3+4 |
| 3   | `transcribing` (часть) | параллельно с 2 |
| 4   | `transcribing` (конец) | параллельно с 2 |
| 5   | `searching_lyrics`     | нет            |
| 6+7 | `aligning`             | нет            |
| 8   | нет (внутри `aligning`)| нет            |
| 9   | `embedding_lyrics`     | нет            |
| 10  | `syncing_qdrant`       | нет            |

---

## 5. Конфигурация и переменные окружения

Файл `v3-rc1/worker/app/config.py`. Все поля из v2 сохраняются, добавляются новые.

```python
class WorkerSettings(BaseSettings):
    # --- Унаследовано из v2 (без изменений) ---
    database_url: str = "/data/sqlite/karaoke.db"
    media_root: str = "/data/media"
    model_cache_dir: str = "/data/models"
    worker_id: str = "worker-1"
    poll_interval_sec: float = 2.0
    log_level: str = "INFO"
    normalization_stats_path: str = ""
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # --- UVR (изменено: GPU + новая модель) ---
    uvr_model_name: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    uvr_torch_device: str = "cuda"        # NEW: "cuda" | "cpu"

    # --- Whisper (NEW) ---
    whisper_model_size: str = "tiny"      # "tiny" | "base" | "small"
    whisper_device: str = "cuda"          # "cuda" | "cpu"
    whisper_compute_type: str = "float16" # "float16" (GPU) | "int8" (CPU)

    # --- VAD (NEW) ---
    vad_top_db: int = 35                  # dB порог тишины

    # --- Lyrics search (NEW) ---
    openai_api_key: str = ""              # ОБЯЗАТЕЛЬНО в prod
    openai_model: str = "gpt-4o-mini"
    openai_timeout: float = 30.0
    openai_max_retries: int = 2
    openai_base_url: str = "https://api.openai.com"

    # --- CTC aligner (NEW) ---
    ctc_min_frames_for_char: int = 10     # min фреймов для char-CTC

    # --- Soniox (сохраняем как fallback, но по умолчанию отключён) ---
    sonoix_api_key: str = ""
    sonoix_api_url: str = "https://api.soniox.com"
    sonoix_timeout: float = 120.0

    model_config = {"env_prefix": ""}
```

**Полный список переменных окружения для docker-compose.prod.yml:**

```yaml
# Обязательные
OPENAI_API_KEY: "sk-..."

# Переопределяемые (у всех есть дефолты)
DATABASE_URL: "/data/sqlite/karaoke.db"
MEDIA_ROOT: "/data/media"
MODEL_CACHE_DIR: "/data/models"
NORMALIZATION_STATS_PATH: "/data/sqlite/feature_normalization_stats.json"
QDRANT_HOST: "qdrant"
QDRANT_PORT: "6333"
UVR_MODEL_NAME: "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
UVR_TORCH_DEVICE: "cuda"
WHISPER_MODEL_SIZE: "tiny"
WHISPER_DEVICE: "cuda"
WHISPER_COMPUTE_TYPE: "float16"
VAD_TOP_DB: "35"
OPENAI_MODEL: "gpt-4o-mini"
CTC_MIN_FRAMES_FOR_CHAR: "10"
WORKER_ID: "worker-1"
POLL_INTERVAL_SEC: "2.0"
LOG_LEVEL: "INFO"
```

---

## 6. Docker-окружение

### 6.1 Dockerfile

```dockerfile
# v3-rc1/worker/Dockerfile
# Базовый образ с CUDA 12.1 + cuDNN 8 + Python 3.11
# CUDA 12.1 совместима с драйверами ≥ 525 (T4 поддерживает CUDA 12.x)
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3-pip \
    ffmpeg wget gcc libc6-dev libsndfile1 \
    && rm -rf /var/lib/lists/*

# Создаём симлинк python3.11 → python3 → python
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

ENV PIP_DEFAULT_TIMEOUT=300
ENV PYTHONUNBUFFERED=1

WORKDIR /worker

# ШАГ 1: Установить PyTorch с CUDA 12.1
# Делаем это первым чтобы слой кэшировался — это самый тяжёлый пакет (~2GB)
RUN pip install --no-cache-dir \
    torch==2.3.1 torchaudio==2.3.1 \
    --index-url https://download.pytorch.org/whl/cu121

# ШАГ 2: Установить shared с ML-extras
# librosa, sentence-transformers, numpy, scipy — не зависят от CUDA
COPY shared/ /shared/
RUN pip install --no-cache-dir "/shared/[ml]"

# ШАГ 3: Установить CTranslate2 (faster-whisper backend) с CUDA
# Версия должна соответствовать CUDA 12.1
RUN pip install --no-cache-dir \
    ctranslate2==4.4.0 \
    faster-whisper==1.0.3

# ШАГ 4: Установить CTC aligner
# ctc-forced-aligner использует CPU ONNX, не требует CUDA
RUN pip install --no-cache-dir \
    ctc-forced-aligner==1.0.2 \
    soundfile>=0.12.1

# ШАГ 5: Установить audio-separator
# PyTorch уже установлен — pip не будет его переустанавливать
RUN pip install --no-cache-dir \
    audio-separator>=0.24 \
    onnxruntime-gpu>=1.18

# ШАГ 6: Установить worker package
COPY worker/pyproject.toml /worker/pyproject.toml
COPY worker/app/ /worker/app/
RUN pip install --no-cache-dir --no-deps /worker/

COPY worker/entrypoint.sh /worker/entrypoint.sh
RUN chmod +x /worker/entrypoint.sh

WORKDIR /worker
ENTRYPOINT ["/worker/entrypoint.sh"]
```

**Важные замечания по Dockerfile:**

1. `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` — runtime-образ (не devel), достаточно для инференса.
2. Порядок слоёв критичен: PyTorch первым → самый тяжёлый слой кэшируется при rebuildах.
3. `onnxruntime-gpu` вместо `onnxruntime` — UVR использует ONNX на GPU для BS-Roformer. CTC-aligner использует CPU ONNX (MMS-300m) — это совместимо, т.к. CTranslate2 и onnxruntime-gpu мирно сосуществуют.
4. `libsndfile1` — runtime-зависимость soundfile.
5. Python 3.11 вместо 3.12 — лучшая совместимость с ctc-forced-aligner.

### 6.2 `docker-compose.yml` — база (без изменений от v2)

Копируется как есть из `v2/docker-compose.yml`.

### 6.3 `docker-compose.prod.yml` — prod override с GPU

```yaml
# v3-rc1/docker-compose.prod.yml
version: "3.9"

services:
  worker:
    build:
      context: .
      dockerfile: worker/Dockerfile

    # GPU passthrough для Tesla T4
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

    # Альтернативный синтаксис для старых версий Docker Compose:
    # runtime: nvidia

    environment:
      - DATABASE_URL=/data/sqlite/karaoke.db
      - MEDIA_ROOT=/data/media
      - MODEL_CACHE_DIR=/data/models
      - NORMALIZATION_STATS_PATH=/data/sqlite/feature_normalization_stats.json
      - QDRANT_HOST=qdrant
      - QDRANT_PORT=6333
      - UVR_MODEL_NAME=model_bs_roformer_ep_317_sdr_12.9755.ckpt
      - UVR_TORCH_DEVICE=cuda
      - WHISPER_MODEL_SIZE=tiny
      - WHISPER_DEVICE=cuda
      - WHISPER_COMPUTE_TYPE=float16
      - VAD_TOP_DB=35
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - OPENAI_MODEL=gpt-4o-mini
      - WORKER_ID=worker-1
      - LOG_LEVEL=INFO

    volumes:
      - /root/bootstrap_output:/data/sqlite
      - /root/mp3_library:/data/media/uploads:ro
      - /root/models:/data/models

    # Лимиты ресурсов (не обязательно, T4 16GB достаточно)
    # mem_limit: 16g

    restart: unless-stopped

  # Остальные сервисы (backend, frontend, qdrant) — без изменений от v2
```

**Проверка GPU в контейнере:**

```bash
# Перед деплоем убедиться что NVIDIA Container Toolkit установлен:
nvidia-smi  # должен видеть T4
docker run --rm --gpus all nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 nvidia-smi
```

**Установка NVIDIA Container Toolkit на Ubuntu 24.04:**

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

---

## 7. Обработка ошибок и цепочки fallback

### 7.1 Матрица ошибок по шагам

| Шаг | Возможная ошибка | Поведение |
|-----|-----------------|-----------|
| 1 UVR | CUDA OOM | cleanup() + retry с `uvr_torch_device=cpu`; если снова ошибка → job failed |
| 1 UVR | Файл не найден | job failed немедленно |
| 2 Features | librosa error | вернуть zero-vector 45d; не блокировать остальное |
| 3 VAD | soundfile error | вернуть исходный vocals_path; продолжить с ним |
| 4 Whisper | CUDA OOM | cleanup() + retry с `whisper_device=cpu`; если снова → asr_text="" |
| 4 Whisper | Пустой результат | asr_text=""; lyrics search будет полагаться только на hints |
| 5 LyricsSearch | LyricsNotFoundError | track.status="error"; qdrant синкается только audio_features; job failed |
| 5 LyricsSearch | LyricsAPIError (network) | retry x2 (уже в LyricsSearcher); затем job failed |
| 5 LyricsSearch | Rate limit 429 | retry с задержкой 5с x2; затем job failed |
| 6+7 CTC | ValueError (пустые lyrics) | job failed |
| 6+7 CTC | RuntimeError (ONNX heap) | job failed (это критическая ошибка, пайплайн должен рестартовать) |
| 8 LineBreak | Exception | syllable_timings без line breaks; предупреждение в лог |
| 9 Embedding | Exception | zero-vector 384d; qdrant lyrics_embeddings не синкается |
| 10 QDrant | Connection error | retry x3 с задержкой 1с; предупреждение в лог; track.qdrant_synced=0 |

### 7.2 CUDA OOM fallback для UVR

```python
async def _separate_with_fallback(self, mp3_path: str) -> tuple[str, str]:
    """Пробует GPU, при OOM переключается на CPU."""
    try:
        return await asyncio.to_thread(self.uvr.separate, mp3_path)
    except RuntimeError as exc:
        if "out of memory" in str(exc).lower() or "cuda" in str(exc).lower():
            logger.warning("uvr_cuda_oom_fallback", error=str(exc))
            await asyncio.to_thread(self.uvr.cleanup)
            # Пересоздаём сепаратор на CPU
            self.uvr = UVRSeparator(
                model_cache_dir=self.uvr.model_cache_dir,
                media_root=self.uvr.media_root,
                model_name=self.settings.uvr_model_name,
                torch_device="cpu",
            )
            return await asyncio.to_thread(self.uvr.separate, mp3_path)
        raise
```

### 7.3 Стратегия при `LyricsNotFoundError`

Трек не теряется: audio feature vector сохраняется в QDrant (рекомендации работают), но `syllable_timings=null` и `status="error"`. UI должен показывать кнопку "Ввести текст вручную" для таких треков.

Проверить что в v2 backend есть `PATCH /tracks/{id}` с полями `lyrics_text` + `syllable_timings`. Если нет — добавить в backlog.

---

## 8. Управление моделями

### 8.1 Список моделей и размеры

| Модель | Файл | Размер | Устройство | Как скачать |
|--------|------|--------|------------|-------------|
| BS-Roformer | `model_bs_roformer_ep_317_sdr_12.9755.ckpt` | ~640 MB | GPU (T4) | auto при первом запуске audio-separator |
| faster-whisper tiny | (CTranslate2 формат) | ~70 MB | GPU (T4) | auto при первом запуске faster-whisper |
| MMS-300m ONNX | (внутри ctc-forced-aligner) | ~300 MB | CPU | auto при первом вызове AlignmentSingleton() |
| sentence-transformers | `paraphrase-multilingual-MiniLM-L12-v2` | ~130 MB | GPU/CPU | auto при первом вызове LyricEmbedder() |

**Итого: ~1.14 GB на диске для всех моделей**

### 8.2 Директория кэша

Все модели кладём в `/data/models` (volume-маппинг: `/root/models:/data/models`).

Переменные окружения для направления кэша:
- `MODEL_CACHE_DIR=/data/models` → передаётся в UVRSeparator (model_file_dir) и LyricEmbedder (cache_dir)
- `HF_HOME=/data/models/hf` → в Dockerfile: `ENV HF_HOME=/data/models/hf` для Hugging Face (faster-whisper, sentence-transformers)
- Для ctc-forced-aligner: `AlignmentSingleton()` использует `~/.cache/ctc_forced_aligner` по умолчанию; нужно добавить в Dockerfile: `ENV XDG_CACHE_HOME=/data/models/xdg`

**Добавить в Dockerfile:**
```dockerfile
ENV HF_HOME=/data/models/hf
ENV XDG_CACHE_HOME=/data/models/xdg
```

### 8.3 Pre-download скрипт (опционально)

Создать `v3-rc1/worker/tools/download_models.py` для предварительной загрузки всех моделей при деплое:

```python
#!/usr/bin/env python3
"""Предварительно скачивает все модели в MODEL_CACHE_DIR.

Запускать до старта сервиса:
    docker compose run --rm worker python /worker/tools/download_models.py
"""

import os
import sys

MODEL_CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "/data/models")

print("1/4 Загрузка faster-whisper tiny...")
from faster_whisper import WhisperModel
WhisperModel("tiny", device="cpu", compute_type="int8",
             download_root=f"{MODEL_CACHE_DIR}/hf")
print("   OK")

print("2/4 Загрузка sentence-transformers...")
from sentence_transformers import SentenceTransformer
SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    cache_folder=MODEL_CACHE_DIR)
print("   OK")

print("3/4 Загрузка MMS-300m (ctc-forced-aligner)...")
from ctc_forced_aligner import AlignmentSingleton
AlignmentSingleton()
print("   OK")

print("4/4 Загрузка BS-Roformer (audio-separator)...")
from audio_separator.separator import Separator
import pathlib
pathlib.Path(f"{MODEL_CACHE_DIR}/uvr").mkdir(parents=True, exist_ok=True)
sep = Separator(
    output_dir="/tmp",
    model_file_dir=f"{MODEL_CACHE_DIR}/uvr",
    torch_device="cpu",  # при скачивании GPU не нужна
)
sep.load_model("model_bs_roformer_ep_317_sdr_12.9755.ckpt")
print("   OK")

print("Все модели загружены.")
```

### 8.4 Инициализация моделей при старте воркера

В `main.py` все ML-компоненты инициализируются при старте (не lazily), чтобы первый трек не ждал загрузки моделей:

```python
# main.py: блок инициализации ML (заменяет try/except-блоки из v2)
uvr = UVRSeparator(
    model_cache_dir=settings.model_cache_dir + "/uvr",
    media_root=settings.media_root,
    model_name=settings.uvr_model_name,
    torch_device=settings.uvr_torch_device,
)
# НЕ загружаем модель сразу — UVR загружает при первом вызове separate()
# (тяжёлая, ~640MB, лучше держать до первого трека)

whisper = WhisperTranscriber(
    model_size=settings.whisper_model_size,
    device=settings.whisper_device,
    compute_type=settings.whisper_compute_type,
    model_cache_dir=settings.model_cache_dir,
)
# faster-whisper загружает модель в конструкторе — это нормально (~70MB, быстро)

vad = VADProcessor(top_db=settings.vad_top_db)

lyrics_searcher = None
if settings.openai_api_key:
    lyrics_searcher = LyricsSearcher(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout=settings.openai_timeout,
    )
else:
    logger.error("OPENAI_API_KEY not set — lyrics search will fail for all tracks")

ctc_aligner = CTCAligner(
    syllabifier=Syllabifier(),
    model_cache_dir=settings.model_cache_dir,
    min_frames_for_char=settings.ctc_min_frames_for_char,
)
# AlignmentSingleton загружает MMS-300m в конструкторе CTCAligner (~300MB, CPU, ~5с)
```

**Порядок загрузки при старте:**
1. faster-whisper (~2с, ~1GB VRAM)
2. CTCAligner/MMS-300m (~5с, CPU, ~300MB RAM)
3. LyricEmbedder (~3с, ~1GB VRAM или RAM)
4. UVRSeparator — загружается lazily при первом треке (~10с, ~4-6GB VRAM)

Общее время старта до готовности: ~10-15с (без UVR). Первый трек: +10с на загрузку UVR.

---

## 9. Миграция с v2

### 9.1 Что копируется без изменений

| Файл / директория | Действие |
|-------------------|----------|
| `v2/shared/` | rsync целиком в `v3-rc1/shared/` |
| `v2/backend/` | rsync целиком в `v3-rc1/backend/` |
| `v2/frontend/` | rsync целиком в `v3-rc1/frontend/` |
| `v2/docker-compose.yml` | скопировать |
| `v2/worker/entrypoint.sh` | скопировать |
| `v2/scripts/` | скопировать (reindex и др.) |

### 9.2 Что копируется с изменениями

| Файл | Изменения |
|------|-----------|
| `v2/worker/app/config.py` | Добавить 8 новых переменных |
| `v2/worker/app/main.py` | Добавить инициализацию 4 новых компонентов, убрать SonoixClient как основной путь |
| `v2/worker/app/pipeline/uvr_separator.py` | Добавить `torch_device` параметр |
| `v2/worker/pyproject.toml` | Добавить новые зависимости |
| `v2/docker-compose.prod.yml` | Добавить GPU passthrough |

### 9.3 Что создаётся с нуля

- `v3-rc1/worker/Dockerfile`
- `v3-rc1/worker/app/pipeline/audio_pipeline.py`
- `v3-rc1/worker/app/pipeline/vad_processor.py`
- `v3-rc1/worker/app/pipeline/whisper_transcriber.py`
- `v3-rc1/worker/app/pipeline/lyrics_searcher.py`
- `v3-rc1/worker/app/pipeline/ctc_aligner.py`
- `v3-rc1/worker/tools/download_models.py`

### 9.4 Что удаляется / не переносится

- `v2/worker/app/pipeline/sonoix_client.py` — не переносится в основной путь (можно добавить как отдельный fallback позже)
- Soniox-специфичная логика в `AudioPipeline` (BPE-токены, SonoixClient)

### 9.5 Совместимость данных

- **SQLite-схема:** не изменяется. v3 пишет в те же таблицы.
- **QDrant-коллекции:** не изменяются. Векторы совместимы (45-dim audio, 384-dim lyrics).
- **feature_normalization_stats.json:** используется как есть. `NORMALIZATION_STATS_PATH` должен указывать на файл от bootstrap.

---

## 10. Зависимости (pip)

### 10.1 `v3-rc1/worker/pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "karaoke-worker"
version = "3.0.0"
requires-python = ">=3.11"
dependencies = [
    # Унаследовано из v2
    "aiosqlite>=0.20",
    "structlog>=24.0",
    "pydantic-settings>=2.0",
    "httpx>=0.27",
    "karaoke-shared",

    # UVR (обновлённая версия с поддержкой BS-Roformer)
    "audio-separator>=0.24",
    "onnxruntime-gpu>=1.18",  # вместо onnxruntime — GPU inference для UVR

    # ASR
    "faster-whisper==1.0.3",
    "ctranslate2==4.4.0",    # бэкенд для faster-whisper, CUDA 12.1

    # Audio processing
    "soundfile>=0.12.1",      # для VADProcessor.write()

    # CTC alignment
    "ctc-forced-aligner==1.0.2",

    # Lyrics search
    # openai SDK не используем — только httpx (уже есть выше)
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

**Важно:** `torch` и `torchaudio` НЕ указываем в pyproject.toml — они устанавливаются отдельным слоем в Dockerfile с `--index-url https://download.pytorch.org/whl/cu121`. Если указать в зависимостях, pip может скачать CPU-версию.

### 10.2 Матрица совместимости версий

| Пакет | Версия | Зависит от |
|-------|--------|------------|
| torch | 2.3.1+cu121 | CUDA 12.1 |
| torchaudio | 2.3.1+cu121 | torch 2.3.1 |
| ctranslate2 | 4.4.0 | CUDA 12.1, libcudnn8 |
| faster-whisper | 1.0.3 | ctranslate2 4.x |
| onnxruntime-gpu | 1.18.x | CUDA 12.x |
| audio-separator | >=0.24 | torch (любой) |
| ctc-forced-aligner | 1.0.2 | onnxruntime (CPU), torch |

**Проверить совместимость перед деплоем:** на тестовой машине выполнить:
```bash
docker build -t karaoke-worker-v3-rc1 -f worker/Dockerfile .
docker run --rm --gpus all karaoke-worker-v3-rc1 python -c "
import torch; print('CUDA:', torch.cuda.is_available())
import ctranslate2; print('CT2:', ctranslate2.__version__)
from faster_whisper import WhisperModel; print('FW: OK')
from ctc_forced_aligner import AlignmentSingleton; print('CTC: OK')
from audio_separator.separator import Separator; print('UVR: OK')
"
```

---

## 11. Оценка ресурсов

### 11.1 VRAM (Tesla T4 16 GB)

| Фаза | Занятые модели | Пиковое VRAM |
|------|----------------|--------------|
| Старт воркера | faster-whisper tiny | ~1 GB |
| + LyricEmbedder | + sentence-transformers | ~2 GB |
| UVR separation | BS-Roformer (UVR загружен) | ~6-8 GB |
| После UVR cleanup | BS-Roformer выгружен | ~2 GB |
| Whisper ASR | tiny | ~2 GB |
| После Whisper cleanup | | ~1 GB (только embedder) |
| Lyric embedding | sentence-transformers | ~2 GB |

**Максимальный пик:** ~8 GB (во время UVR). T4 16 GB — достаточно с большим запасом.
**Steady state (между треками):** ~2 GB.

### 11.2 RAM (32 GB)

| Компонент | RAM |
|-----------|-----|
| Python процесс (воркер) | ~500 MB |
| librosa (feature extraction) | ~200-400 MB (на 5-мин трек) |
| CTC aligner (emissions 5-мин трек) | ~50-100 MB |
| VAD (numpy array 16kHz 5-мин) | ~30 MB |
| Суммарно пиковое | ~1.5-2 GB |

32 GB RAM — с большим запасом. Ограничений нет.

### 11.3 Диск (2 TB NVMe)

| Данные | Размер |
|--------|--------|
| Модели (`/root/models/`) | ~1.1 GB |
| MP3 каталог (17k треков) | ~141 GB |
| instrumental MP3 per track | ~5-10 MB/трек |
| vocals WAV (временный) | ~40-80 MB/трек, удаляется после CTC |
| SQLite + qdrant | ~5 GB |

При обработке нового трека пиковая потребность: ~100 MB временных файлов (удаляются).

### 11.4 Время обработки одного трека

| Шаг | Время | Устройство |
|-----|-------|------------|
| UVR (BS-Roformer) | ~60-90 с | T4 GPU |
| Feature extraction | ~5-10 с | CPU (параллельно с ASR) |
| VAD | ~1-2 с | CPU |
| Whisper ASR tiny | ~5-10 с | T4 GPU |
| LLM lyrics search | ~3-8 с | сеть (OpenAI) |
| CTC alignment | ~20-25 с | CPU |
| Lyric embedding | ~1-2 с | T4 GPU |
| QDrant sync | ~0.5 с | сеть |
| **ИТОГО** | **~95-140 с** | |

**vs v2:** v2 = ~1 мин UVR (CPU) + ~30-60 с Soniox = ~90-120 с. v3-rc1 медленнее на ~15-30с (BS-Roformer тяжелее 2_HP-UVR), но качество разделения значительно выше (SDR 12.9 vs ~8).

Если критична скорость — можно переключиться на `htdemucs_ft` (SDR ~9.5, ~30-45с на T4) или `MDX23C` (SDR ~11, ~40-50с на T4).

---

## 12. Стратегия тестирования

### 12.1 Unit-тесты новых компонентов

**Структура тестов:**

```
v3-rc1/worker/tests/
├── conftest.py
├── test_vad_processor.py
├── test_whisper_transcriber.py
├── test_lyrics_searcher.py
├── test_ctc_aligner.py
└── test_audio_pipeline.py
```

**`test_vad_processor.py`:**

```python
# Тест 1: silence-only файл → возвращает исходный путь
# Тест 2: нормальный вокал → cleaned_vocals.wav создан, короче оригинала
# Тест 3: файл не существует → RuntimeError с понятным сообщением
# Тест 4: слишком короткий результат (<1с) → возвращает исходный путь
```

**`test_lyrics_searcher.py`:**

```python
# Используем respx (httpx mock) для мока OpenAI API
# Тест 1: успешный ответ → LyricsResult с корректными полями
# Тест 2: found=false → LyricsNotFoundError
# Тест 3: невалидный JSON → LyricsAPIError
# Тест 4: HTTP 500 → retry x2, затем LyricsAPIError
# Тест 5: HTTP 429 → retry с задержкой 5с
```

**`test_ctc_aligner.py`:**

```python
# Используем тестовые данные из m3_test/test_data/
# Тест 1: трек 1 (русский) → syllable_timings непустой, stats.total_words > 0
# Тест 2: трек с коротким clips (< MIN_FRAMES_FOR_CHAR) → proportional_fallback > 0
# Тест 3: пустые lyrics → ValueError
# Тест 4: align_stats.char_level_used / total_words > 0.5 (большинство слов через char-CTC)
```

**`test_audio_pipeline.py`:**

```python
# Mock-тест полного пайплайна
# Все компоненты заменяются mock-объектами
# Тест 1: успешный проход → track.status="ready", qdrant_synced=1
# Тест 2: LyricsNotFoundError → track.status="error", job.failed
# Тест 3: UVR RuntimeError → job.failed
# Тест 4: audio_features всё равно синкается при LyricsNotFoundError
```

### 12.2 Интеграционный тест (локально с GPU)

Создать `v3-rc1/worker/tools/smoke_test.py`:

```python
#!/usr/bin/env python3
"""
Smoke test: прогоняет один трек через полный пайплайн.

Использование:
    docker compose run --rm worker python /worker/tools/smoke_test.py \
        --mp3 /data/media/uploads/some_track.mp3 \
        --artist "Земфира" --title "Хочешь"

Ожидаемый результат: вывод syllable_timings (первые 20) в stdout.
"""
```

### 12.3 Проверка качества выравнивания

После первых 10 обработанных треков:

1. Выбрать трек с известными тайминговыми данными (из m3_test/test_data/).
2. Запустить CTC-алайнер на нём через smoke_test.
3. Сравнить с эталонными тайминговыми данными (`reference_timings.json`).
4. Ожидаемые метрики (из эксперимента): MAE < 0.3с, hit rate > 70%.

### 12.4 Нагрузочный тест (после деплоя)

Загрузить 5 треков одновременно через UI и убедиться:
- Только один трек обрабатывается в момент времени (job lock работает).
- VRAM не переполняется (мониторинг через `nvidia-smi` или `watch -n1 nvidia-smi`).
- Все 5 треков получили `status="ready"` в течение ~10-15 мин.

### 12.5 Чеклист перед деплоем

```
[ ] docker build завершается без ошибок
[ ] nvidia-smi видит T4 в контейнере (docker run --rm --gpus all ...)
[ ] download_models.py завершился успешно (все 4 модели)
[ ] smoke_test.py для 1 трека проходит за < 150с
[ ] OPENAI_API_KEY установлен в prod-окружении
[ ] NORMALIZATION_STATS_PATH указывает на существующий JSON
[ ] qdrant коллекции созданы (init-qdrant.py уже был запущен в v2)
[ ] feature_normalization_stats.json скопирован в /root/models/
```

---

## Приложения

### Приложение A: Зависимость Whisper cleanup от параллелизма

В секции `asyncio.gather(features, vad_and_transcribe)` Whisper-модель остаётся в VRAM всё время, пока работает feature_extractor (librosa на CPU). Это нормально: Whisper держит только ~1GB VRAM, а BS-Roformer уже выгружен. Общая VRAM в этот момент: ~2 GB — хорошо.

Cleanup Whisper вызывается ПОСЛЕ gather, не внутри него — это правильно, потому что `asyncio.to_thread(self.whisper.cleanup)` освобождает ресурс только когда оба потока завершились.

### Приложение B: Почему не используем OpenAI SDK

Пакет `openai` версии 1.x тянет `anyio`, `httpcore`, `pydantic-core` и другие зависимости. У нас уже есть `httpx` для Soniox-клиента. Прямые запросы через httpx к `api.openai.com/v1/chat/completions` — 20 строк кода против дополнительного пакета. Стоимость: нулевая.

### Приложение C: Замечание о CTC и романизации кириллицы

ctc-forced-aligner при `romanize=True` (для русского языка) транслитерирует кириллицу в латиницу для работы с MMS-300m моделью. Это значит что для `language="ru"` передаём `romanize=True`. Языки отличные от ru/en: по умолчанию `romanize=True` (модель обучена на транслитерации). Если язык `"other"` (из LyricsResult) — передать `romanize=True` и `lang_iso3="eng"` как безопасный дефолт.

### Приложение D: Проверка audio-separator версии для BS-Roformer

В v2 использовалась версия `audio-separator>=0.20`. BS-Roformer добавлен в audio-separator начиная с версии `0.22`. Убедиться что в pyproject.toml стоит `>=0.24` (стабильная поддержка BS-Roformer + PyTorch models).

Имя файла модели: `model_bs_roformer_ep_317_sdr_12.9755.ckpt` — точное, с учётом метрики SDR в имени файла. При вызове `sep.load_model()` audio-separator автоматически скачает файл если его нет в `model_file_dir`.
