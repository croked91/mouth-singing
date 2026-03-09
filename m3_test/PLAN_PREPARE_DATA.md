# Подготовка тестовых данных для экспериментов M3

## Цель
Подготовить 5 тестовых треков со всеми необходимыми данными для экспериментов
по улучшению слоговой разметки (варианты 2 и 3).

## Тестовые треки

| # | Артист | Название | Язык |
|---|--------|----------|------|
| 1 | Слава КПСС | Владимир Путин | ru |
| 2 | Григорий Лепс | Орлы или вороны | ru |
| 3 | Red Hot Chili Peppers | Bullet Proof | en |
| 4 | Дима Билан | Я просто люблю тебя | ru |
| 5 | Король и Шут | Помнят с горечью древляне | ru |

## Что нужно получить для каждого трека

```
m3_test/test_data/{N}/
  ├── original.mp3          # Оригинальный MP3 файл
  ├── vocals.wav            # Вокальная дорожка (результат UVR-сепарации)
  ├── lyrics.txt            # Чистый текст песни (из lyrics_text в БД)
  ├── reference_timings.json # Эталонные syllable_timings (из БД, JSON-массив)
  └── meta.json             # {"artist": "...", "title": "...", "language": "...", "track_id": "..."}
```

## Где что находится

### На сервере `root@155.212.182.210`:
- **SQLite БД**: `/root/bootstrap_output/karaoke.db`
  - Таблица `tracks`: поля `id`, `artist`, `title`, `lyrics_text`, `syllable_timings` (JSON), `language`, `status`
  - Нужны треки со `status = 'ready'` и непустым `syllable_timings`
- **Инструментальные дорожки**: `/root/bootstrap_output/instrumental/` — есть, но нам не нужны
- **Вокальные дорожки**: удалены после бутстрапа (экономия места)
- **Оригинальные MP3**: удалены или отсутствуют

### Локально на `/home/croked/karaoke/`:
- **MP3 файлы**: пользователь предоставит в `m3_test/test_data/mp3_input/`
  - Имена файлов: `1.mp3`, `2.mp3`, `3.mp3`, `4.mp3`, `5.mp3` (по порядку из таблицы выше)
- **Conda-среда**: `source /home/croked/miniforge3/etc/profile.d/conda.sh && conda activate bootstrap`
  - Содержит: whisperx, torch, pyphen, audio-separator (возможно)

## План выполнения

### Шаг 1: Извлечь данные с сервера

Создать скрипт `m3_test/fetch_from_server.sh`, который:

1. Подключается к серверу по SSH: `ssh root@155.212.182.210`
2. Выполняет SQL-запросы к БД `/root/bootstrap_output/karaoke.db`
3. Скачивает результаты на локальную машину

SQL-запросы для поиска треков (выполнить на сервере через `sqlite3`):

```bash
SSH_HOST="root@155.212.182.210"
DB_PATH="/root/bootstrap_output/karaoke.db"

# Массив треков для поиска (artist|title)
TRACKS=(
  "Слава КПСС|Владимир Путин"
  "Григорий Лепс|Орлы или вороны"
  "Red Hot Chili Peppers|Bullet Proof"
  "Дима Билан|Я просто люблю тебя"
  "Король и Шут|Помнят с горечью древляне"
)
```

Для каждого трека выполнить на сервере:
```sql
SELECT id, artist, title, lyrics_text, syllable_timings, language
FROM tracks
WHERE artist LIKE '%<artist>%' AND title LIKE '%<title>%' AND status = 'ready'
LIMIT 1;
```

**ВАЖНО**: Поиск регистронезависимый через `LIKE`. Если точное совпадение не найдено —
попробовать FTS:
```sql
SELECT t.id, t.artist, t.title, t.lyrics_text, t.syllable_timings, t.language
FROM tracks_fts fts
JOIN tracks t ON t.id = fts.id
WHERE tracks_fts MATCH '<artist> <title>'
AND t.status = 'ready'
LIMIT 1;
```

Если и FTS не нашёл — вывести предупреждение и продолжить с остальными треками.

### Шаг 2: Сохранить данные с сервера локально

Для каждого найденного трека создать:

1. `m3_test/test_data/{N}/meta.json`:
```json
{
  "artist": "Слава КПСС",
  "title": "Владимир Путин",
  "language": "ru",
  "track_id": "<id из БД>"
}
```

2. `m3_test/test_data/{N}/lyrics.txt` — содержимое поля `lyrics_text` из БД

3. `m3_test/test_data/{N}/reference_timings.json` — содержимое поля `syllable_timings` из БД
   (это уже JSON-строка, нужно просто сохранить как файл)

**Формат reference_timings.json** (для справки):
```json
[
  {"syllable": "Она", "start": 12.34, "end": 12.78},
  {"syllable": " рас", "start": 12.78, "end": 13.01},
  {"syllable": "тёт", "start": 13.01, "end": 13.35},
  ...
]
```
Пробел перед слогом = начало нового слова. `\n` перед слогом = начало новой строки.

### Шаг 3: Дождаться MP3 от пользователя

После выполнения шагов 1-2, скрипт должен:
1. Создать директорию `m3_test/test_data/mp3_input/`
2. Напечатать инструкцию:
```
=== ОЖИДАНИЕ MP3 ФАЙЛОВ ===
Поместите MP3 файлы в: /home/croked/karaoke/m3_test/test_data/mp3_input/

Ожидаемые файлы:
  1.mp3 — Слава КПСС - Владимир Путин
  2.mp3 — Григорий Лепс - Орлы или вороны
  3.mp3 — Red Hot Chili Peppers - Bullet Proof
  4.mp3 — Дима Билан - Я просто люблю тебя
  5.mp3 — Король и Шут - Помнят с горечью древляне
```
3. Дождаться подтверждения пользователя (или проверить наличие файлов)
4. Скопировать каждый `mp3_input/{N}.mp3` → `test_data/{N}/original.mp3`

### Шаг 4: UVR-сепарация (получение вокала)

Для каждого трека нужно отделить вокал от инструментала.

**Вариант A — через audio-separator (предпочтительный)**:

```bash
source /home/croked/miniforge3/etc/profile.d/conda.sh
conda activate bootstrap
pip install audio-separator  # если не установлен
```

```python
from audio_separator.separator import Separator

separator = Separator(output_dir="./output", output_format="WAV")
separator.load_model(model_filename="UVR-MDX-NET-Voc_FT.onnx")

for n in range(1, 6):
    mp3_path = f"m3_test/test_data/{n}/original.mp3"
    output_files = separator.separate(mp3_path)
    # Найти vocals файл и переместить в test_data/{n}/vocals.wav
```

Модель скачается автоматически при первом запуске (~100MB).

**Вариант B — через Docker-контейнер воркера на сервере** (если локально не получится):

Загрузить MP3 на сервер, запустить сепарацию через worker-контейнер:
```bash
# На сервере уже есть модель в /root/models/
docker run --rm \
  -v /root/models:/data/models \
  -v /tmp/test_uvr:/data/media \
  -v /tmp/test_mp3:/input \
  karaoke-worker python -c "
from app.pipeline.uvr_separator import UVRSeparator
sep = UVRSeparator('/data/models', '/data/media', model_name='2_HP-UVR.pth')
sep.separate('/input/1.mp3')
"
```

**Вариант C — через demucs** (fallback, если ничего не работает):
```bash
pip install demucs
python -m demucs --two-stems vocals original.mp3
```

Для теста подойдёт любой вариант — нам нужен просто вокал для WhisperX и Sonoix.
Качество сепарации для этих целей не критично.

### Шаг 5: Проверка комплектности

После всех шагов проверить, что для каждого трека (1-5) существуют все 5 файлов:
```
m3_test/test_data/{N}/original.mp3
m3_test/test_data/{N}/vocals.wav
m3_test/test_data/{N}/lyrics.txt
m3_test/test_data/{N}/reference_timings.json
m3_test/test_data/{N}/meta.json
```

Также проверить:
- `lyrics.txt` не пустой
- `reference_timings.json` — валидный JSON-массив с полями `syllable`, `start`, `end`
- `vocals.wav` — корректный аудиофайл (длительность > 30с)
- `meta.json` — содержит все поля

Вывести итоговый отчёт:
```
Трек 1: Слава КПСС - Владимир Путин .......... OK (lyrics: 45 lines, timings: 312 syllables, vocals: 3:42)
Трек 2: Григорий Лепс - Орлы или вороны ...... OK (lyrics: 38 lines, timings: 287 syllables, vocals: 4:15)
...
```

## Реализация

Предпочтительно реализовать как **один Python-скрипт** `m3_test/prepare_test_data.py`,
который выполняет шаги 1-5 последовательно. SSH-команды выполнять через `subprocess.run`.

Скрипт должен быть **идемпотентным**: если часть данных уже скачана — не перезатирать,
а пропускать. Это позволит перезапускать скрипт после добавления MP3.

## Зависимости
- Python 3.10+
- ssh доступ к `root@155.212.182.210` (ключ уже настроен)
- `audio-separator` pip-пакет (для UVR)
- Conda-среда `bootstrap` (или любая с Python 3.10+)
