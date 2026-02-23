## Фаза 8a: Извлечение фичей и эмбеддингов

### Входные артефакты
- Результат Фазы 7b (полный аудио-пайплайн)
- `journals/ARCHITECTURE.md` — раздел 3.7 «AudioPipeline» (шаги 4-6), раздел 4.2 «QDrant Collections»

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `ml-sota-expert` задачу на реализацию извлечения аудиофичей (librosa) и генерации лирических эмбеддингов (sentence-transformers). Эти компоненты добавляются в аудио-пайплайн параллельно (шаги 4+5 выполняются через asyncio.gather после шага 3). После реализации — `polyglot-test-engineer` для тестирования.

#### Подагент `ml-sota-expert`
Реализует ML-компоненты:

1. **FeatureExtractor** (`shared/karaoke_shared/ml/feature_extractor.py` — в shared, т.к. используется и worker, и bootstrap):
   - Извлекает 45-мерный вектор аудиофич из instrumental.wav через librosa:
     - MFCC (13 коэффициентов × mean) = 13 dim
     - Chroma (12 полутонов × mean) = 12 dim
     - Spectral Contrast (7 полос × mean) = 7 dim
     - Tonnetz (6 × mean) = 6 dim
     - Дополнительные: tempo (1), spectral_centroid mean (1), spectral_bandwidth mean (1), spectral_rolloff mean (1), zero_crossing_rate mean (1), rms mean (1), spectral_flatness mean (1) = 7 dim
     - **Итого: 13 + 12 + 7 + 6 + 7 = 45**
   - `extract(audio_path: str) -> list[float]` — всегда возвращает ровно 45 float
   - Нормализация: L2-нормализация вектора

2. **LyricEmbedder** (`shared/karaoke_shared/ml/lyric_embedder.py`):
   - Загружает модель `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (кэш в `MODEL_CACHE_DIR`)
   - `embed(text: str) -> list[float]` — всегда возвращает ровно 384 float
   - Для длинных текстов: разбиение на чанки по 256 токенов → среднее арифметическое эмбеддингов

3. **Интеграция в AudioPipeline** (шаги 4-6):
   - После шага 3 (VideoGenerator) параллельно запускаются:
     - Шаг 4: `FeatureExtractor.extract(instrumental.wav)` → `vector[45]`
     - Шаг 5: `LyricEmbedder.embed(full_text)` → `vector[384]`
   - Шаг 6: `QDrantRepository.upsert` для обеих коллекций (`audio_features`, `lyrics_embeddings`) + обновление `qdrant_synced=1` в SQLite

#### Подагент `polyglot-test-engineer`
Тесты:
- FeatureExtractor на разных аудиофайлах → всегда 45 float, нормализован
- LyricEmbedder на русском и английском тексте → всегда 384 float
- После обработки трека: QDrant содержит записи в обеих коллекциях
- Параллельность: шаги 4+5 выполняются через asyncio.gather (не последовательно)

#### Пользователь
Проверяет, что после upload MP3 в QDrant появляются векторы. Подтверждает или вносит замечания.

### Выходные артефакты
- `FeatureExtractor` (librosa → 45 dim) в shared/
- `LyricEmbedder` (sentence-transformers → 384 dim) в shared/
- AudioPipeline завершён: все 6 шагов работают
- Векторы записываются в QDrant после обработки
- Коммит

