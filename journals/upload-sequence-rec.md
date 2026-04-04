# Диаграмма последовательности: индексация рекомендаций (Rec Service)

```mermaid
sequenceDiagram
    participant DB as PostgreSQL
    participant S3 as S3<br/>(instrumentals/)
    participant RMQ as RabbitMQ
    participant RS as Rec Service<br/>(Consumer)
    participant QD as QDrant

    %% ============ TRIGGER ============
    Note over RMQ: Worker завершил обработку трека<br/>и опубликовал сообщение<br/>в exchange "rec"

    Note over RMQ: Очередь "rec.index"<br/>durable, prefetch_count=1
    RMQ -->> RS: basic_consume → deliver<br/>{track_id, mp3_key, lyrics}
    Note over RMQ,RS: manual ack: сообщение<br/>не удаляется из очереди,<br/>пока Rec Service не подтвердит

    %% ============ FEATURE EXTRACTION ============
    rect rgb(230, 245, 255)
        Note over RS: Шаг 1: Извлечение аудио-фич
        RS ->> S3: GET uploads/{job_id}.mp3<br/>→ скачать во /tmp
        RS ->> RS: FeatureExtractor.extract(mp3)<br/>(librosa: tempo, chroma, mfcc, spectral)<br/>→ 45-d вектор
    end

    %% ============ LYRIC EMBEDDING ============
    rect rgb(255, 240, 240)
        Note over RS: Шаг 2: Эмбеддинг текста
        RS ->> RS: LyricEmbedder.embed(lyrics)<br/>(sentence-transformer)<br/>→ 384-d вектор
    end

    %% ============ QDRANT SYNC ============
    rect rgb(245, 245, 230)
        Note over RS: Шаг 3: Синхронизация с QDrant
        RS ->> QD: upsert("audio_features",<br/>track_id, 45-d vector,<br/>{track_id, artist, title, status: "ready"})
        RS ->> QD: upsert("lyrics_embeddings",<br/>track_id, 384-d vector,<br/>{track_id, artist, title, status: "ready"})
    end

    %% ============ FINALIZATION ============
    rect rgb(230, 255, 230)
        Note over RS: Финализация
        RS ->> DB: UPDATE tracks<br/>SET qdrant_synced=1<br/>WHERE id=track_id
        RS ->> S3: DELETE uploads/{job_id}.mp3
        RS ->> RS: Очистить /tmp
        RS ->> RMQ: basic_ack<br/>(сообщение удалено из очереди)
    end

    %% ============ RESULT ============
    Note over DB,QD: Трек теперь доступен<br/>в поиске по похожести<br/>и в рекомендациях
```

## Ключевые детали

### Входное сообщение
Rec Service получает из очереди `rec.index`:
```json
{
  "track_id": "uuid",
  "mp3_key": "uploads/{job_id}.mp3",
  "lyrics": "полный текст песни"
}
```

### Шаги обработки
1. **Feature Extraction** — скачивает оригинальный MP3 из S3, извлекает 45-мерный вектор (librosa: tempo, chroma, mfcc, spectral centroid и т.д.)
2. **Lyric Embedding** — sentence-transformer генерирует 384-мерный вектор из текста
3. **QDrant Sync** — upsert в две коллекции: `audio_features` (45-d, COSINE) и `lyrics_embeddings` (384-d, COSINE)
4. **Финализация** — помечает трек `qdrant_synced=1` в PostgreSQL

### Связь с Worker
- Worker создаёт трек со `status='ready', qdrant_synced=0` и публикует в exchange `rec`
- Rec Service работает **асинхронно** — не блокирует пользователя
- До завершения Rec Service трек доступен для воспроизведения, но не появляется в рекомендациях «похожих треков»

### Идемпотентность
- QDrant upsert перезаписывает вектор по `track_id` — безопасно при повторной обработке
- `UPDATE tracks SET qdrant_synced=1` тоже идемпотентен
- Можно переиндексировать все треки, переопубликовав сообщения в `rec.index`

### Масштабирование
- `prefetch_count=1` — каждый инстанс берёт по одному треку
- Можно запустить N инстансов Rec Service без изменений кода
- CPU-bound задача (librosa + sentence-transformer) — не требует GPU

### Обработка ошибок
- `max_attempts=3` — при краше RabbitMQ делает requeue
- Исчерпание попыток → `basic_nack(requeue=false)` → DLQ (`rec.dlq`)
- При ошибке трек остаётся `qdrant_synced=0` — легко найти и переобработать: `SELECT * FROM tracks WHERE qdrant_synced=0`
