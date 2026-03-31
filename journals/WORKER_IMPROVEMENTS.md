# Worker Pipeline: Предложения по ускорению

Базовые замеры (трек ~3:18, русский язык, 2026-03-30): **175.66с** общее время.
После оптимизации CTC (2026-03-31): **~99с** общее время (**-44%**).

Критический путь (текущий):
```
UVR (26.6с) → VAD+Whisper (28.5с) → Lyrics Search (38.5с) → CTC (0.75с) → остальное (~0.1с)
Load/cleanup overhead: ~4.2с
```

Шаг 5 (Lyrics Search) — **39% всего времени**, основное узкое место.

---

## 1. Кэш/локальный поиск lyrics

**Сложность:** средняя | **Потенциал:** -30-50с

Lyrics Search (38-60с) — самый долгий шаг. Каждый раз вызывается DeepSeek + Yandex, даже если трек уже есть в каталоге (17K треков).

Варианты:
- **Кэш по audio fingerprint** — Chromaprint fingerprint загруженного трека → поиск в SQLite. Если трек уже обрабатывался, пропустить поиск.
- **Поиск по QDrant lyrics embeddings** — embed Whisper text через MiniLM → nearest neighbor в `lyrics_embeddings`. Если cosine similarity > порога, взять lyrics из SQLite. Агент нужен только для новых/неизвестных треков.
- **Локальная база текстов** — импортировать тексты из внешних баз (musixmatch dump, etc.) и искать сначала локально.
- Основная проблема не в количестве итераций, а в latency DeepSeek API — каждый LLM-вызов занимает 5-10с. При 5 итерациях это ~30-40с только на ожидание LLM, плюс ~15-20с на tool calls.

## 2. ✅ CTC Alignment на GPU (реализовано 2026-03-31)

**Статус:** реализовано | **Результат:** 27с → 0.75с (**36x ускорение**)

### Хронология экспериментов

**Попытка 1: ONNX CUDA EP** — провалилась.
1. cuDNN 9 отсутствовал в образе (`cuda:12.1.1-cudnn8`) — тихий fallback на CPU.
2. После установки `nvidia-cudnn-cu12` CUDA EP активировался, но:
   - `batch_size≥8` → OOM 6.2 ГБ (wav2vec2 feature extractor обрабатывает все 30с-окна одним батчем).
   - `batch_size=4` → OOM нет, но **24 Memcpy-узла** в графе (неподдерживаемые CUDA EP операции).
     GPU-Util ~0%, время **43.8с vs 27с на CPU**.
3. Вывод: wav2vec2 ONNX граф несовместим с CUDA Execution Provider.

**Попытка 2: torchaudio MMS_FA (95M params)** — работает, но качество хуже.
- `torchaudio.pipelines.MMS_FA` + `torchaudio.functional.forced_align()` — нативные CUDA кернелы.
- Alignment: 1.4с (первый подход с chunking) → 1.06с (без chunking).
- Качество выравнивания хуже ONNX baseline — MMS_FA base слишком маленькая модель.

**Попытка 3: MMS-300M (315M params) — финальное решение.**
- `MahmoudAshraf/mms-300m-1130-forced-aligner` через HuggingFace + torchaudio forced_align.
- 1130 языков (включая русский), 315M параметров, fp16 inference.
- Alignment: **0.75с**, качество на уровне ONNX baseline.

### Итоговая реализация

- **Файл:** `worker/gpu/torch_ctc_aligner.py` — `TorchCTCAligner` класс.
- **Модель:** `MahmoudAshraf/mms-300m-1130-forced-aligner` (HuggingFace, fp16).
- **Runtime:** PyTorch CUDA, in-process (без subprocess изоляции).
- **VRAM:** ~3.4 ГБ при загрузке, lazy load + cleanup после каждого трека.
- **Тесты:** `tests/worker/test_torch_ctc_aligner.py` (19 тестов).
- **Docker:** torchaudio обновлён до 2.11.0+cu130, audio loading через librosa (TorchCodec workaround).
- **API mode:** по-прежнему использует старый `CTCAligner` (ONNX CPU).

### Изменённые файлы

| Файл | Изменение |
|---|---|
| `worker/gpu/torch_ctc_aligner.py` | Новый — GPU CTC aligner |
| `worker/gpu/gpu_pipeline.py` | Интеграция TorchCTCAligner, load/cleanup, permanently_failed |
| `worker/app/main.py` | GPU mode → TorchCTCAligner, cleanup при shutdown |
| `docker/worker-gpu.Dockerfile` | torch + torchaudio cu130 |
| `worker/app/config.py` | Обновлены docstrings |
| `tests/worker/test_torch_ctc_aligner.py` | 19 unit-тестов |

## 3. Feature Extraction: сэмплирование вместо полного файла

**Сложность:** низкая | **Потенциал:** -15-20с

Feature extraction (24.78с) неожиданно долгий. Причины:
- `librosa.load(sr=None)` декодирует весь MP3 в нативном sample rate.
- DSP-фичи (MFCC, chroma, tonnetz и др.) вычисляются по всему файлу.

Что сделать:
- Загружать только 30-60 секунд из середины трека: `librosa.load(audio_path, sr=22050, offset=60, duration=60)`.
- Зафиксировать `sr=22050` вместо `sr=None` — меньше сэмплов, быстрее FFT.
- Для рекомендаций 30-секундный фрагмент даёт почти такое же качество фич, как полный файл.

## 4. ~~Совместить UVR и Whisper на GPU~~

**Статус:** исследовано, отклонено (2026-03-31)

Держать все модели (UVR + Whisper + CTC + Embedder) резидентно в VRAM не оправдано:
- 2 воркера × ~6.1 ГБ = 12.2 ГБ + десктоп = ~12.9 ГБ из 16 ГБ.
- Остаётся ~3.4 ГБ для UVR inference тензоров — при параллельной обработке 2 треков будет OOM.
- Суммарный оверхед load/cleanup всех моделей — **~4.2с** за пайплайн (4.3%).
- Не стоит риска OOM ради 4с экономии.

## 5. Уменьшить итерации Lyrics Agent

**Сложность:** низкая | **Потенциал:** -10-20с

`max_iterations=15` — это верхний предел агентного цикла. На практике большинство треков находятся за 3-5 итераций. Можно:
- Добавить early stopping: если агент нашёл JSON с `lyrics` длиной > 100 символов, остановиться.
- Уменьшить `max_iterations` до 8-10.
- Параллельно запускать 2-3 поисковых запроса (разные формулировки) и брать первый успешный.

---

## Сводка

| #   | Оптимизация                            | Статус       | Результат        |
|:----|:---------------------------------------|:-------------|:-----------------|
| 2   | CTC на GPU (torchaudio + MMS-300M)     | ✅ Готово     | 27с → 0.75с      |
| 4   | Все модели резидентно в VRAM           | ❌ Отклонено  | Риск OOM > 4с    |
| 3   | Feature extraction: сэмплирование      | ⏳ Планируется | -15-20с          |
| 5   | Уменьшить итерации lyrics agent        | ⏳ Планируется | -10-20с          |
| 1   | Кэш/локальный поиск lyrics             | ⏳ Планируется | -30-50с          |

Текущее время: **~99с**. С оптимизациями 3+5+1: потенциал **~50-70с**.
