## Фаза 6: Каталог треков и поиск

### Входные артефакты
- Результат Фаз 3, 4a (скелет + слой данных)
- `journals/ARCHITECTURE.md` — раздел 3.3 «TrackService», раздел 3.4 «SearchService», раздел 10 «API-контракт» (Tracks)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `python-developer` задачу на реализацию TrackService и SearchService. Это слой каталога — CRUD треков, полнотекстовый и семантический поиск, стриминг медиафайлов. После реализации запускаешь `polyglot-test-engineer`.

#### Подагент `python-developer`
Реализует каталог треков и поиск:

1. **TrackService** (`backend/app/services/track_service.py`):
   - `upload_mp3(file: UploadFile, artist: str | None, title: str | None) -> Track` — сохраняет файл в `MEDIA_ROOT/uploads/`, создаёт запись в SQLite с status=pending. Если artist/title не указаны → "Unknown Artist" / "Unknown Track".
   - `get_track(track_id) -> Track` — полная информация включая syllable_timings
   - `list_popular(limit=10) -> list[Track]` — top по play_count, только status=ready
   - `enqueue_processing(track_id) -> Job` — создаёт задачу в job_queue (для Фазы 7)

2. **SearchService** (`backend/app/services/search_service.py`):
   - `search(query, limit=20, offset=0) -> SearchResult` — гибридный поиск:
     1. Сначала FTS5 (`tracks_fts MATCH query`)
     2. Если FTS даёт < 5 результатов → fallback на семантический поиск через QDrant (коллекция `lyrics_embeddings`)
     3. Слияние и дедупликация по track_id, FTS-результаты приоритетнее
   - `suggest(query, limit=10) -> list[str]` — автокомплит (prefix search по artist+title)
   - Для семантического поиска: запрос эмбеддируется моделью `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (загрузка модели при старте, кэш)

3. **Playback** (`backend/app/api/v1/playback.py`):
   - `GET /tracks/{track_id}/stream` — HTTP Range Request для стриминга MP4/MP3
   - Поддержка заголовков `Range: bytes=N-M`, `Content-Range`, `Accept-Ranges`
   - `Content-Type: video/mp4` или `audio/mpeg`

4. **API-роутеры** (по API-контракту из ARCHITECTURE.md):
   - `backend/app/api/v1/tracks.py`:
     - `GET /tracks/search?q=...&limit=20&offset=0` → 200
     - `GET /tracks/search/suggest?q=...&limit=10` → 200
     - `GET /tracks/{track_id}` → 200
     - `POST /tracks/upload` → 202 (multipart/form-data, max 50MB)
     - `GET /tracks/popular?limit=10` → 200

#### Подагент `polyglot-test-engineer`
Тесты:
- Upload MP3 → трек появляется в каталоге со status=pending
- FTS поиск по artist, title, lyrics → корректные результаты
- Suggest → автокомплит
- Range Request стриминг: partial content, правильные заголовки
- list_popular → sorted by play_count desc, only status=ready
- Семантический fallback (когда FTS < 5 результатов)

#### Пользователь
Проверяет поиск и загрузку через curl/HTTP-клиент. Подтверждает или вносит замечания.

### Выходные артефакты
- `TrackService` и `SearchService`
- API-роутеры для `/tracks` с поиском, загрузкой, стримингом
- Гибридный поиск (FTS5 + семантический fallback)
- Тесты поиска и стриминга
- Коммит

