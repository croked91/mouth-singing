# Вариант 2: WhisperX ASR → сопоставление с текстом → force_align

## Суть подхода

У нас есть:
- Вокальная дорожка (WAV/MP3)
- Правильный текст песни (без таймингов)

Мы хотим получить послоговые тайминги качества, сопоставимого с путём LRC + force_align.

Идея: использовать WhisperX ASR только для получения **пословных таймингов** (не текста!),
затем сопоставить распознанные слова с известным текстом, чтобы получить **построчные границы**,
и уже с этими границами запустить force_align для слогов — как в пути 1 бутстрапа.

## Контекст проекта (для субагента)

### Что такое "путь 1" (эталон)
В бутстрапе, когда для трека находится LRC-файл из lrclib, происходит следующее:
1. LRC парсится в строки с таймингами: `[{"text": "строка текста", "start_ms": 12340, "end_ms": 15670}, ...]`
2. Каждая строка разбивается на слоги через `pyphen` (класс `Syllabifier.split_text_to_syllables`)
3. Слоги каждой строки склеиваются пробелами и подаются как "текст сегмента" в `WhisperX.force_align()`:
   ```python
   segments = [{"text": "лю бовь не об ман", "start": 12.34, "end": 15.67}, ...]
   ```
4. WhisperX (wav2vec2) выравнивает каждый "слог-слово" по аудио в рамках заданного временного окна
5. Результат — точный тайминг каждого слога

### Что такое "путь 2" (fallback, плохое качество)
Когда LRC не найден:
1. WhisperX ASR (`transcribe()`) распознаёт текст + даёт пословные тайминги
2. Каждое слово разбивается на слоги через pyphen
3. Время слова делится между слогами **пропорционально числу символов** — грубая аппроксимация

### Проблема
Путь 2 даёт плохое качество потому что:
- Текст из ASR может быть неправильным (но нам неважно — у нас есть правильный текст)
- Тайминги слогов пропорциональные, а не реальные

### Ключевые файлы проекта (только для чтения/справки)
- `v2/bootstrap/app/bootstrap_runner.py` — основной пайплайн бутстрапа, строки 425-548
- `v2/bootstrap/app/pipeline/whisperx_transcriber.py` — обёртка WhisperX (transcribe + force_align)
- `v2/shared/karaoke_shared/utils/syllabifier.py` — разбивка на слоги (pyphen)
- `v2/shared/karaoke_shared/models/track.py` — модель `SyllableTiming(syllable, start, end)`

## План эксперимента

### Шаг 0: Подготовка окружения
- Создать директорию `m3_test/variant2/`
- Убедиться, что доступна conda-среда: `source /home/croked/miniforge3/etc/profile.d/conda.sh && conda activate bootstrap`
- В этой среде уже установлены: whisperx, torch, pyphen, pydantic, structlog
- Проверить наличие GPU: `python -c "import torch; print(torch.cuda.is_available())"`
  - Если GPU нет — использовать `device="cpu"` (будет медленнее, но работает)

### Шаг 1: Тестовые данные (уже подготовлены)
Тестовые данные готовятся отдельным скриптом — см. `PLAN_PREPARE_DATA.md`.

После подготовки данные лежат в:
```
m3_test/test_data/{N}/    # N = 1..5
  ├── original.mp3          # Оригинальный MP3
  ├── vocals.wav            # Вокальная дорожка
  ├── lyrics.txt            # Правильный текст песни
  ├── reference_timings.json # Эталонные syllable_timings (JSON-массив SyllableTiming)
  └── meta.json             # {"artist", "title", "language", "track_id"}
```

### Шаг 2: Основной скрипт эксперимента
Создать скрипт `m3_test/variant2/experiment.py`:

#### 2.1 WhisperX ASR — получение пословных таймингов

**ВАЖНО**: Для импорта модулей проекта нужно добавить пути в sys.path:
```python
import sys
sys.path.insert(0, "/home/croked/karaoke/v2/bootstrap")
sys.path.insert(0, "/home/croked/karaoke/v2/shared")
```

```python
from app.pipeline.whisperx_transcriber import WhisperXTranscriber

transcriber = WhisperXTranscriber(language=language, device=device)
asr_words = transcriber.transcribe(vocals_path)
# asr_words = [{"word": "любовь", "start": 12.5, "end": 13.1}, ...]
transcriber.cleanup()  # обязательно! освобождает GPU память
```
Результат: список слов с таймингами. Текст может быть неточным — это нормально.

**Важно про WhisperX**: метод `transcribe()` загружает тяжёлую ASR-модель при первом вызове.
Метод `force_align()` использует лёгкую модель wav2vec2.
После использования **обязательно** вызывать `cleanup()` для освобождения GPU памяти.
Нельзя использовать один экземпляр для transcribe и force_align подряд — после cleanup
нужно создавать новый экземпляр.

#### 2.2 Сопоставление ASR-слов с известным текстом
Это **ключевой и самый сложный шаг**. Нужно:
1. Разбить известный текст на строки и слова
2. Сопоставить ASR-слова с известными словами, чтобы понять, где в аудио начинается/заканчивается каждая строка

Алгоритм сопоставления (fuzzy alignment):
```
known_lines = lyrics_text.split("\n")  # ["Она растет прям из земли", "Она к тебе на полпути", ...]
known_words = [line.split() for line in known_lines]  # [["Она", "растет", ...], ...]
flat_known = [w for line in known_words for w in line]

asr_texts = [w["word"].lower().strip(".,!?") for w in asr_words]
known_texts = [w.lower() for w in flat_known]
```

Использовать **динамическое программирование (edit distance alignment)** или библиотеку `difflib.SequenceMatcher`:
- Выровнять `asr_texts` и `known_texts`
- Для каждого known_word найти ближайший matched asr_word (и его тайминг)
- Из сопоставленных слов вычислить тайминги строк:
  - `line_start = тайминг первого matched слова строки`
  - `line_end = тайминг последнего matched слова строки`

**Важные edge cases**:
- ASR может пропустить слова (нет match) — интерполировать тайминг между соседними
- ASR может добавить лишние слова — игнорировать
- ASR может исказить слово ("любовь" → "любов") — fuzzy match по Левенштейну

Рекомендуется попробовать два подхода:
- **A**: `difflib.SequenceMatcher` на списках слов
- **B**: DTW (Dynamic Time Warping) — если difflib даёт плохие результаты

#### 2.3 Формирование сегментов для force_align
После сопоставления у нас есть тайминги строк. Дальше — как в пути 1:
```python
from karaoke_shared.utils.syllabifier import Syllabifier

syllabifier = Syllabifier()
segments = []
all_syl_strings = []
all_is_word_start = []
all_is_line_start = []

for line, (start, end) in zip(known_lines, line_timings):
    syl_strings, is_word_start = syllabifier.split_text_to_syllables(line, language)
    if not syl_strings:
        continue
    syl_text = " ".join(syl_strings)
    segments.append({"text": syl_text, "start": start, "end": end})

    # Отслеживаем границы строк (нужно для маппинга)
    line_flags = [False] * len(syl_strings)
    line_flags[0] = True
    all_syl_strings.extend(syl_strings)
    all_is_word_start.extend(is_word_start)
    all_is_line_start.extend(line_flags)
```

#### 2.4 WhisperX force_align
```python
from app.pipeline.whisperx_transcriber import WhisperXTranscriber

transcriber2 = WhisperXTranscriber(language=language, device=device)
syl_timestamps = transcriber2.force_align(vocals_path, segments)
transcriber2.cleanup()
```

#### 2.5 Маппинг результатов
Скопировать функцию `_map_syllable_timestamps` из `bootstrap_runner.py` (строки 272-327) в свой скрипт.
Она принимает результат force_align и списки all_syl_strings, all_is_word_start, all_is_line_start,
и возвращает список `SyllableTiming` с правильными маркерами пробелов и переносов строк.

```python
# Импорт модели
from karaoke_shared.models.track import SyllableTiming

# Вызов (функцию скопировать из bootstrap_runner.py:272-327)
syllable_timings = _map_syllable_timestamps(
    syl_timestamps, all_syl_strings, all_is_word_start, all_is_line_start
)
```

### Шаг 3: Метрика качества
Создать скрипт `m3_test/variant2/evaluate.py`:

Для каждого трека сравнить полученные тайминги с эталонными:
1. **Mean Absolute Error (MAE)** по start-таймингам слогов:
   - Сопоставить слоги по тексту (strip пробелов и \n)
   - MAE = среднее |predicted_start - reference_start|
   - Хорошо: < 0.05с, Приемлемо: < 0.15с, Плохо: > 0.3с

2. **Процент "попаданий"**: доля слогов, где |delta_start| < 0.1с

3. **Визуальное сравнение**: вывести первые 20 слогов рядом:
   ```
   Слог         | Эталон start | Получено start | Delta
   лю           | 12.340       | 12.380         | +0.040
   бовь         | 12.560       | 12.610         | +0.050
   ...
   ```

### Шаг 4: Контрольный прогон (baseline)
Для сравнения, также прогнать путь 2 (fallback) на тех же треках:
- WhisperX ASR → pyphen proportional split
- Замерить те же метрики
- Это даст понимание, насколько вариант 2 лучше текущего fallback

### Результаты
Сохранить в `m3_test/variant2/results/`:
- `results.json` — метрики по каждому треку
- `summary.txt` — краткий вывод: какой подход лучше, насколько, рекомендация

## Критерии успеха
- MAE < 0.15с для большинства треков (4 из 5)
- Значительное улучшение по сравнению с путём 2 (baseline)
- Алгоритм сопоставления работает стабильно на разных языках

## Потенциальные проблемы и решения
1. **WhisperX ASR даёт слишком мало слов** — модель не распознала часть текста.
   Решение: пропущенные строки получают интерполированные тайминги.
2. **Fuzzy matching работает плохо** — слова сильно искажены.
   Решение: переключиться на DTW с фонетическим расстоянием.
3. **WhisperX force_align падает на больших сегментах** — если строка слишком длинная.
   Решение: разбить на подсегменты.
