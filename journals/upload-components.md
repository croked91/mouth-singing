# Схема взаимодействия компонентов: загрузка и обработка MP3

```mermaid
flowchart TB
    %% ============ STYLES ============
    classDef user fill:#f9f,stroke:#333,stroke-width:2px
    classDef frontend fill:#4FC3F7,stroke:#0288D1,color:#000
    classDef nginx fill:#90CAF9,stroke:#1565C0,color:#000
    classDef backend fill:#81C784,stroke:#388E3C,color:#000
    classDef worker fill:#FFB74D,stroke:#F57C00,color:#000
    classDef recsvc fill:#FFAB91,stroke:#D84315,color:#000
    classDef broker fill:#B39DDB,stroke:#4527A0,color:#000
    classDef storage fill:#B0BEC5,stroke:#546E7A,color:#000
    classDef external fill:#CE93D8,stroke:#7B1FA2,color:#000
    classDef pipeline fill:#FFF9C4,stroke:#F9A825,color:#000

    %% ============ USER ============
    USER((Пользователь)):::user

    %% ============ FRONTEND ============
    subgraph FRONT["Frontend (React + Vite)"]
        direction TB
        UPLOAD_TAB[UploadTab<br/>Форма загрузки MP3]:::frontend
        SSE_CLIENT[SSE Client<br/>EventSource]:::frontend
        ZUSTAND[Zustand Store<br/>Состояние прогресса]:::frontend
    end

    %% ============ NGINX ============
    NGINX["Nginx<br/>Reverse Proxy<br/>порт 80"]:::nginx

    %% ============ BACKEND ============
    subgraph BACK["Backend (FastAPI, порт 8000)"]
        direction TB
        TRACKS_API["POST /tracks/upload<br/>Приём файла, валидация"]:::backend
        SSE_ENDPOINT["GET /jobs/{id}/status<br/>SSE-стрим прогресса"]:::backend
        TRACK_SVC[TrackService<br/>Бизнес-логика]:::backend
    end

    %% ============ RABBITMQ ============
    subgraph BROKER["RabbitMQ"]
        direction TB
        EX_JOBS["Exchange 'jobs'<br/>(direct, durable)"]:::broker
        EX_PROGRESS["Exchange 'job.progress'<br/>(fanout)"]:::broker
        EX_REC["Exchange 'rec'<br/>(direct, durable)"]:::broker
        Q_PROCESS["Queue 'jobs.process'<br/>prefetch_count=1"]:::broker
        Q_REC["Queue 'rec.index'<br/>prefetch_count=1"]:::broker
        Q_DLQ["DLQ<br/>jobs.dlq / rec.dlq"]:::broker
        EX_JOBS --> Q_PROCESS
        EX_REC --> Q_REC
    end

    %% ============ WORKER ============
    subgraph WORK["Worker (GPU)"]
        direction TB
        CONSUMER[Consumer<br/>basic_consume]:::worker
        subgraph PIPE["Pipeline (7 шагов)"]
            direction TB
            S1["1. Разделение вокала<br/>UVR BS-Roformer (GPU)"]:::pipeline
            S23["2+3. VAD + ASR<br/>Whisper транскрипция"]:::pipeline
            S4["4. Поиск текста<br/>DeepSeek + Yandex Search"]:::pipeline
            S56["5+6. CTC-выравнивание<br/>Послоговая синхронизация"]:::pipeline
            S7["7. Разбиение на строки<br/>Line break detection"]:::pipeline
            FIN["Финализация<br/>INSERT track, publish rec"]:::pipeline

            S1 --> S23 --> S4 --> S56 --> S7 --> FIN
        end
    end

    %% ============ REC SERVICE ============
    subgraph RECSVC["Rec Service (Микросервис рекомендаций)"]
        direction TB
        REC_CONSUMER[Consumer<br/>basic_consume]:::recsvc
        REC_FE["Feature Extraction<br/>librosa → 45-d вектор"]:::recsvc
        REC_EMB["Lyric Embedding<br/>sentence-transformer → 384-d"]:::recsvc
        REC_SYNC["QDrant Sync<br/>upsert в 2 коллекции"]:::recsvc

        REC_CONSUMER --> REC_FE --> REC_EMB --> REC_SYNC
    end

    %% ============ STORAGE ============
    subgraph STORE["Хранилища"]
        direction TB
        POSTGRES[(PostgreSQL)]:::storage
        QDRANT[(QDrant<br/>порт 6333)]:::storage
        S3[("S3<br/>uploads/ instrumentals/")]:::storage
    end

    %% ============ EXTERNAL ============
    subgraph EXT["Внешние сервисы"]
        direction TB
        DEEPSEEK["DeepSeek API<br/>Анализ текста"]:::external
        YANDEX["Yandex Search API<br/>Поиск текста песни"]:::external
    end

    %% ============ CONNECTIONS: UPLOAD ============
    USER -- "MP3 + метаданные" --> UPLOAD_TAB
    UPLOAD_TAB -- "POST multipart/form-data" --> NGINX
    NGINX -- "proxy_pass" --> TRACKS_API
    TRACKS_API --> TRACK_SVC
    TRACK_SVC -- "PUT uploads/{job_id}.mp3" --> S3
    TRACK_SVC -- "INSERT job_queue (pending)<br/>mp3_key, artist_hint, title_hint" --> POSTGRES
    TRACK_SVC -- "publish {job_id, mp3_key}" --> EX_JOBS
    TRACKS_API -- "202 {job_id}" --> NGINX
    NGINX -- "202 Accepted" --> UPLOAD_TAB

    %% ============ CONNECTIONS: SSE ============
    UPLOAD_TAB --> SSE_CLIENT
    SSE_CLIENT -- "EventSource" --> NGINX
    NGINX -- "proxy_pass<br/>proxy_buffering off" --> SSE_ENDPOINT
    EX_PROGRESS -- "consume (fanout)" --> SSE_ENDPOINT
    SSE_CLIENT -- "Обновление UI" --> ZUSTAND

    %% ============ CONNECTIONS: WORKER ============
    Q_PROCESS -- "deliver {job_id, mp3_key}" --> CONSUMER
    CONSUMER -- "UPDATE job_queue<br/>SET status='running'" --> POSTGRES
    CONSUMER --> S1

    %% ============ CONNECTIONS: PIPELINE → STORAGE ============
    S1 -- "GET uploads/{job_id}.mp3" --> S3
    S1 -- "PUT instrumentals/{job_id}.wav" --> S3
    S23 -- "Читает vocals.wav<br/>из /tmp" --> S1
    S56 -- "Читает vocals.wav<br/>из /tmp" --> S1

    %% ============ CONNECTIONS: PIPELINE → DB ============
    S1 -- "mark_step + data.instrumental_key" ----> POSTGRES
    S4 -- "data.artist, title, lyrics, language" ----> POSTGRES
    S7 -- "data.syllable_timings" ----> POSTGRES
    FIN -- "INSERT tracks (ready, qdrant_synced=0)<br/>UPDATE job_queue (completed)" ----> POSTGRES

    %% ============ CONNECTIONS: PIPELINE → BROKER ============
    S1 -- "step: separating" --> EX_PROGRESS
    S23 -- "step: transcribing" --> EX_PROGRESS
    S4 -- "step: searching_lyrics" --> EX_PROGRESS
    S56 -- "step: aligning" --> EX_PROGRESS
    FIN -- "status: completed" --> EX_PROGRESS
    FIN -- "{track_id, mp3_key, lyrics}" --> EX_REC
    CONSUMER -- "basic_ack" --> Q_PROCESS

    %% ============ CONNECTIONS: PIPELINE → EXTERNAL ============
    S4 --> DEEPSEEK
    S4 --> YANDEX

    %% ============ CONNECTIONS: PIPELINE → CLEANUP ============

    %% ============ CONNECTIONS: REC SERVICE ============
    Q_REC -- "deliver {track_id,<br/>mp3_key, lyrics}" --> REC_CONSUMER
    REC_FE -- "GET uploads/{job_id}.mp3" --> S3
    REC_SYNC -- "upsert audio_features (45-d)<br/>upsert lyrics_embeddings (384-d)" --> QDRANT
    REC_SYNC -- "UPDATE tracks<br/>SET qdrant_synced=1" --> POSTGRES
    REC_SYNC -- "DELETE uploads/{job_id}.mp3" --> S3
    REC_CONSUMER -- "basic_ack" --> Q_REC

    %% ============ CONNECTIONS: DLQ ============
    Q_PROCESS -. "max_attempts exceeded" .-> Q_DLQ
    Q_REC -. "max_attempts exceeded" .-> Q_DLQ

    %% ============ CONNECTIONS: PLAYBACK ============
    BACK -. "302 → presigned S3 URL" .-> S3
```

## Потоки данных между компонентами

| Откуда | Куда | Что передаётся |
|--------|------|----------------|
| Frontend → Nginx | `POST /tracks/upload` | MP3 файл (≤50MB), artist, title |
| Nginx → Backend | proxy_pass | Тело запроса без буферизации |
| Backend → S3 | PUT | `uploads/{job_id}.mp3` |
| Backend → PostgreSQL | INSERT | `job_queue` (pending, mp3_key, hints) |
| Backend → RabbitMQ | publish → "jobs" | `{job_id, mp3_key}` |
| Backend → Frontend | 202 response | `{job_id}` |
| RabbitMQ → SSE Endpoint | consume (fanout) | `{job_id, step, progress}` |
| SSE Endpoint → Frontend | SSE events | `status` / `completed` / `error` |
| RabbitMQ → Worker | deliver из "jobs.process" | `{job_id, mp3_key}` |
| Worker → PostgreSQL | UPDATE (каждый шаг) | `current_step`, `progress`, промежуточные данные в `job_queue.data` |
| Worker → PostgreSQL | INSERT (финализация) | Готовый трек в `tracks` (`qdrant_synced=0`) |
| Worker → S3 | GET / PUT / DELETE | Чтение MP3, запись instrumental, удаление оригинала |
| Worker → RabbitMQ | publish → "rec" | `{track_id, mp3_key, lyrics}` |
| Worker → RabbitMQ | basic_ack | Подтверждение обработки |
| RabbitMQ → Rec Service | deliver из "rec.index" | `{track_id, mp3_key, lyrics}` |
| Rec Service → S3 | GET + DELETE | `uploads/{job_id}.mp3` (скачать, после обработки удалить) |
| Rec Service → QDrant | upsert × 2 | `audio_features` (45-d) + `lyrics_embeddings` (384-d) |
| Rec Service → PostgreSQL | UPDATE | `tracks SET qdrant_synced=1` |
| Worker/Rec Service → DeepSeek | HTTP | ASR-текст → текст песни |
| Backend → браузер → S3 | 302 redirect | Presigned URL для аудио-стриминга |

## Поток обработки

```mermaid
flowchart LR
    classDef worker fill:#FFF3E0,stroke:#E65100
    classDef rec fill:#FFEBEE,stroke:#C62828
    classDef handoff fill:#E8F5E9,stroke:#2E7D32

    SEP["1. Separation"]:::worker
    VAD["2+3. VAD + ASR"]:::worker
    LYR["4. Lyrics"]:::worker
    CTC["5+6. CTC Align"]:::worker
    LB["7. Line Breaks"]:::worker
    FIN["Финализация<br/>INSERT track<br/>publish → rec"]:::handoff
    FE["Feature Extraction<br/>librosa → 45-d"]:::rec
    EMB["Lyric Embedding<br/>384-d"]:::rec
    QD["QDrant Sync"]:::rec

    SEP --> VAD --> LYR --> CTC --> LB --> FIN
    FIN -- "RabbitMQ" --> FE --> EMB --> QD
```

**Worker** (оранжевый) обрабатывает аудио и текст → создаёт готовый трек.
**Rec Service** (красный) индексирует фичи и эмбеддинги фоново — не блокирует пользователя.
Точка передачи (зелёный) — финализация воркера: INSERT трека + publish в exchange `rec`.
