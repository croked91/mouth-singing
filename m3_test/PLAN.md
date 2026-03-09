# Тест pipeline на Mac M3

Цель: прогнать полный pipeline обработки трека на Mac M3 и замерить время каждого этапа.
На выходе: instrumental, syllable timings, audio features (45d), lyric embeddings (384d).

---

## Подготовка

### 1. Создать структуру директорий

```bash
cd /path/to/m3_test   # папка с этим файлом
mkdir -p input output/instrumental output/vocals models
```

### 2. Положить входные данные в `input/`

- `input/song.mp3` — любой трек ~3-4 минуты
- `input/lyrics.txt` — текст песни (plain text, одна строка = одна строка в тексте)

Формат `lyrics.txt`:
```
Группа крови на рукаве
Мой порядковый номер на рукаве
Пожелай мне удачи в бою
Пожелай мне
```

### 3. Создать venv и установить зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate

# PyTorch для Apple Silicon (MPS backend)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu

# audio-separator (обёртка над UVR моделями, включая BS-Roformer)
pip install "audio-separator[cpu]>=0.20"

# WhisperX (force-alignment)
pip install whisperx

# Audio features + lyric embeddings
pip install "librosa>=0.10" "numpy>=1.24" "sentence-transformers>=2.2" "pyphen>=0.16"
```

> Если `whisperx` не ставится через pip, установить из git:
> ```bash
> pip install git+https://github.com/m-bain/whisperX.git
> ```

### 4. Проверить что MPS доступен

```bash
python3 -c "import torch; print('MPS available:', torch.backends.mps.is_available())"
```

Ожидаемый вывод: `MPS available: True`

---

## Этап 1: Разделение аудио (BS-Roformer)

Это главный этап для бенчмарка — ради него и затеяли тест на M3.

```bash
python3 -c "
import time
from audio_separator.separator import Separator

sep = Separator(
    output_dir='output/instrumental',
    model_file_dir='models',
    output_format='WAV',
)

# BS-Roformer — SOTA модель (SDR 12.9)
# При первом запуске скачает ~639 MB модели в ./models/
model = 'model_bs_roformer_ep_317_sdr_12.9755.ckpt'
sep.load_model(model_filename=model)

start = time.time()
result = sep.separate('input/song.mp3')
elapsed = time.time() - start

print(f'BS-Roformer: {elapsed:.1f} сек')
print(f'Output files: {result}')
"
```

**Запиши время!** Это ключевая метрика.

Результат: в `output/instrumental/` появятся 2 файла:
- `*_(Vocals)_*.wav` — вокал (нужен для force-alignment)
- `*_(Instrumental)_*.wav` — инструментал (финальный результат)

Переименуй/перемести для удобства:
```bash
# Посмотри имена файлов
ls output/instrumental/

# Переименуй (подставь реальные имена)
mv output/instrumental/*Vocals*.wav output/vocals/vocals.wav
mv output/instrumental/*Instrumental*.wav output/instrumental/instrumental.wav
```

---

## Этап 2: WhisperX Force-Alignment

Берём текст из `input/lyrics.txt` и выравниваем по `vocals.wav`.

```python
# save as: run_alignment.py
import json
import time
import whisperx
import torch

DEVICE = "cpu"  # MPS пока не поддерживается whisperx; попробуй "mps" — если упадёт, используй "cpu"
AUDIO_PATH = "output/vocals/vocals.wav"
LYRICS_PATH = "input/lyrics.txt"
OUTPUT_PATH = "output/syllable_timings.json"

# --- Загрузка текста ---
with open(LYRICS_PATH, "r", encoding="utf-8") as f:
    lines = [line.strip() for line in f if line.strip()]

print(f"Loaded {len(lines)} lines from lyrics")

# --- Подготовка segments для force-align ---
# WhisperX force_align ожидает segments с text.
# Без реальных таймкодов из LRC мы сначала прогоним transcribe,
# потом align. Это стандартный подход WhisperX.

# Загрузка ASR модели
print("Loading Whisper model...")
model = whisperx.load_model("medium", DEVICE, compute_type="int8", language="ru")

# Транскрибируем (нужно для получения сегментов с таймингами)
print("Transcribing...")
start = time.time()
audio = whisperx.load_audio(AUDIO_PATH)
result = model.transcribe(audio, batch_size=4, language="ru")
transcribe_time = time.time() - start
print(f"Transcribe: {transcribe_time:.1f} сек")

# Удаляем ASR модель из памяти
del model
import gc; gc.collect()
if hasattr(torch.backends, "mps"):
    torch.mps.empty_cache() if hasattr(torch.mps, "empty_cache") else None

# --- Force-align с НАШИМ текстом ---
# Подменяем транскрипцию нашим текстом, сохраняя структуру сегментов.
# Вариант 1: Используем сегменты от Whisper (автоматические таймкоды) + наш текст
# Это даёт лучшее качество, т.к. таймкоды сегментов уже примерно верные.

# Заменяем текст в сегментах на наш
segments = result["segments"]
# Распределяем наши строки по сегментам Whisper (по порядку)
for i, seg in enumerate(segments):
    if i < len(lines):
        seg["text"] = lines[i]

print("Loading alignment model...")
align_model, align_metadata = whisperx.load_align_model(
    language_code="ru", device=DEVICE
)

print("Running force-alignment...")
start = time.time()
aligned = whisperx.align(
    segments, align_model, align_metadata, audio, DEVICE,
    return_char_alignments=True  # нужно для syllable-level
)
align_time = time.time() - start
print(f"Force-align: {align_time:.1f} сек")

# --- Формируем syllable timings ---
# Из aligned["segments"] достаём word-level timings
syllable_timings = []
for seg in aligned["segments"]:
    for word_info in seg.get("words", []):
        word = word_info.get("word", "")
        start_t = word_info.get("start")
        end_t = word_info.get("end")
        if start_t is not None and end_t is not None:
            syllable_timings.append({
                "syllable": word,
                "start": round(start_t, 3),
                "end": round(end_t, 3),
            })

with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(syllable_timings, f, ensure_ascii=False, indent=2)

print(f"Saved {len(syllable_timings)} word timings to {OUTPUT_PATH}")
print(f"\nИТОГО: transcribe={transcribe_time:.1f}s, align={align_time:.1f}s")
```

Запуск:
```bash
python3 run_alignment.py
```

**Запиши время transcribe и align отдельно.**

> Примечание: в реальном bootstrap мы используем LRC таймкоды (из lrclib) как
> входные segments для force_align, что точнее. Здесь мы используем Whisper
> транскрипцию как fallback, т.к. у тебя нет lrclib dump на Mac.

---

## Этап 3: Аудио-фичи (45d вектор)

```python
# save as: run_features.py
import json
import time
import numpy as np
import librosa

AUDIO_PATH = "input/song.mp3"  # фичи из ОРИГИНАЛА (не из instrumental)
OUTPUT_PATH = "output/audio_features.json"

def extract_features(audio_path: str) -> list[float]:
    """Extract 45-dimensional audio feature vector (same as FeatureExtractor)."""
    y, sr = librosa.load(audio_path, sr=None)

    # 13 MFCC
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_mean = np.mean(mfcc, axis=1)

    # 12 Chroma
    chroma = librosa.feature.chroma_stft(y=y, sr=sr)
    chroma_mean = np.mean(chroma, axis=1)

    # 7 Spectral Contrast
    contrast = librosa.feature.spectral_contrast(y=y, sr=sr)
    contrast_mean = np.mean(contrast, axis=1)

    # 6 Tonnetz
    tonnetz = librosa.feature.tonnetz(y=y, sr=sr)
    tonnetz_mean = np.mean(tonnetz, axis=1)

    # 1 Tempo
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    if isinstance(tempo, np.ndarray):
        tempo = float(tempo[0])

    # 1 Spectral Centroid
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
    centroid_mean = float(np.mean(centroid))

    # 1 Spectral Bandwidth
    bandwidth = librosa.feature.spectral_bandwidth(y=y, sr=sr)
    bandwidth_mean = float(np.mean(bandwidth))

    # 1 Spectral Rolloff
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr)
    rolloff_mean = float(np.mean(rolloff))

    # 1 Zero-Crossing Rate
    zcr = librosa.feature.zero_crossing_rate(y)
    zcr_mean = float(np.mean(zcr))

    # 1 RMS Energy
    rms = librosa.feature.rms(y=y)
    rms_mean = float(np.mean(rms))

    # 1 Spectral Flatness
    flatness = librosa.feature.spectral_flatness(y=y)
    flatness_mean = float(np.mean(flatness))

    # Concat all → 45 dimensions
    raw = np.concatenate([
        mfcc_mean,          # 13
        chroma_mean,        # 12
        contrast_mean,      # 7
        tonnetz_mean,       # 6
        [tempo],            # 1
        [centroid_mean],    # 1
        [bandwidth_mean],   # 1
        [rolloff_mean],     # 1
        [zcr_mean],         # 1
        [rms_mean],         # 1
        [flatness_mean],    # 1
    ])  # total: 45

    # L2-normalize
    norm = np.linalg.norm(raw)
    if norm > 0:
        raw = raw / norm

    return raw.tolist()


start = time.time()
features = extract_features(AUDIO_PATH)
elapsed = time.time() - start

with open(OUTPUT_PATH, "w") as f:
    json.dump({"dimensions": len(features), "vector": features}, f, indent=2)

print(f"Audio features ({len(features)}d): {elapsed:.1f} сек")
print(f"Saved to {OUTPUT_PATH}")
```

Запуск:
```bash
python3 run_features.py
```

---

## Этап 4: Lyric Embeddings (384d вектор)

```python
# save as: run_embeddings.py
import json
import time
from sentence_transformers import SentenceTransformer

LYRICS_PATH = "input/lyrics.txt"
OUTPUT_PATH = "output/lyric_embedding.json"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Модель скачается в текущую директорию при первом запуске (~420 MB)
print("Loading sentence-transformer model...")
model = SentenceTransformer(MODEL_NAME, cache_folder="models")

with open(LYRICS_PATH, "r", encoding="utf-8") as f:
    text = f.read().strip()

print(f"Text length: {len(text)} chars")

start = time.time()
embedding = model.encode(text).tolist()
elapsed = time.time() - start

with open(OUTPUT_PATH, "w") as f:
    json.dump({"dimensions": len(embedding), "vector": embedding}, f, indent=2)

print(f"Lyric embedding ({len(embedding)}d): {elapsed:.1f} сек")
print(f"Saved to {OUTPUT_PATH}")
```

Запуск:
```bash
python3 run_embeddings.py
```

---

## Сбор результатов

После всех этапов в `output/` должны быть:

```
output/
  instrumental/instrumental.wav   # инструментал для караоке
  vocals/vocals.wav               # вокал (промежуточный, можно удалить)
  syllable_timings.json           # таймкоды слов/слогов
  audio_features.json             # 45d вектор
  lyric_embedding.json            # 384d вектор
```

### Таблица замеров

Заполни по ходу выполнения:

| Этап | Время (сек) | Заметки |
|------|-------------|---------|
| BS-Roformer separation | | Ключевая метрика |
| Whisper transcribe | | |
| Force-alignment | | |
| Audio features (librosa) | | Обычно быстро |
| Lyric embedding | | Обычно быстро |
| **ИТОГО** | | |

---

## Бонус: тест других моделей разделения

Если хочешь сравнить BS-Roformer с другими моделями на M3:

```python
import time
from audio_separator.separator import Separator

models = [
    "model_bs_roformer_ep_317_sdr_12.9755.ckpt",  # BS-Roformer (SOTA)
    "UVR-MDX-NET-Voc_FT.onnx",                     # MDX-NET
    "2_HP-UVR.pth",                                 # HP-UVR (lightweight)
]

for model_name in models:
    sep = Separator(
        output_dir=f"output/bench_{model_name}",
        model_file_dir="models",
        output_format="WAV",
    )
    sep.load_model(model_filename=model_name)

    start = time.time()
    sep.separate("input/song.mp3")
    elapsed = time.time() - start

    print(f"{model_name}: {elapsed:.1f} сек")
```

---

## Что дальше

Если результаты хорошие (separation < 60 сек):
1. Арендовать Mac M4 у облачного провайдера
2. Развернуть worker на нём (Docker или native)
3. Worker принимает задачи от backend по HTTP/очереди
4. Backend на текущем VPS, worker на Mac — split architecture
