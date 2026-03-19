# Руководство по развёртыванию рекомендаций v2

## Предусловия

- Docker Compose запущен, все 4 контейнера healthy (qdrant, backend, worker, frontend)
- Каталог из ~20000 треков уже в SQLite (`tracks` со статусом `ready`)
- Аудио-фичи и лирические эмбеддинги уже в QDrant (коллекции `audio_features`, `lyrics_embeddings`)
- Z-score нормализация уже применена (`reindex_audio_features.py`)

Если каталог ещё не бутстрапнут — сначала выполните бутстрап (см. Phase 16-17 в PROJECT_LOG.md).

---

## Шаг 1: Применение миграций БД

Миграции применяются **автоматически** при старте backend-контейнера (`main.py` lifespan). Новые столбцы:
- `tracks.popularity_category`, `tracks.chart_count`, `tracks.chart_last_seen`, `tracks.catalog_cluster_id`
- Таблицы: `catalog_clusters`, `mood_tags`, `artists`, `api_costs`

Если контейнеры уже запущены — **перезапустите backend**:
```bash
docker restart karaoke_backend
docker logs -f --tail=20 karaoke_backend
# Ищите строки: migration_applied
```

### Проверка:
```bash
docker exec karaoke_backend python -c "
import sqlite3
conn = sqlite3.connect('/data/sqlite/karaoke.db')
# Проверить новые столбцы
row = conn.execute('PRAGMA table_info(tracks)').fetchall()
cols = [r[1] for r in row]
assert 'popularity_category' in cols, 'FAIL: popularity_category'
assert 'catalog_cluster_id' in cols, 'FAIL: catalog_cluster_id'
# Проверить новые таблицы
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
assert 'catalog_clusters' in tables, 'FAIL: catalog_clusters'
assert 'mood_tags' in tables, 'FAIL: mood_tags'
assert 'artists' in tables, 'FAIL: artists'
print('OK: все миграции применены')
conn.close()
"
```

---

## Шаг 2: Popularity scoring (категоризация треков)

Присваивает каждому треку категорию: `eternal_hit`, `current_hit`, `former_hit`, `artist_best`, `regular`.

```bash
# Сначала dry-run — посмотреть статистику без записи в БД
docker exec karaoke_backend python /app/scripts/parse_karaoke_charts.py \
  --db /data/sqlite/karaoke.db \
  --dry-run

# Если результат устраивает — запуск с записью
docker exec karaoke_backend python /app/scripts/parse_karaoke_charts.py \
  --db /data/sqlite/karaoke.db
```

**Ожидаемый результат:**
```
  eternal_hit: 30-70 (треки из хардкод-списка)
  current_hit: 0-50 (зависит от доступности чартов)
  artist_best: 500-2000
  former_hit: 0-100
  regular: 17000-19000
```

> **Примечание:** Парсинг чартов зависит от доступности сайтов (karaopa2.ru, hitlist.ru, hitmotop.com). Если сайты недоступны — будут только `eternal_hit` из хардкод-списка + `artist_best`. Это нормально.

### Проверка:
```bash
docker exec karaoke_backend python -c "
import sqlite3
conn = sqlite3.connect('/data/sqlite/karaoke.db')
for cat in ['eternal_hit', 'current_hit', 'former_hit', 'artist_best', 'regular']:
    n = conn.execute('SELECT COUNT(*) FROM tracks WHERE popularity_category = ?', (cat,)).fetchone()[0]
    print(f'  {cat}: {n}')
conn.close()
"
```

**Время:** ~5-20 минут (зависит от fuzzy matching 20k × chart entries).

---

## Шаг 3: Кластеризация каталога

Разбивает 20k треков на 15-20 вайб-кластеров по аудио+лирика векторам.

```bash
# Dry-run — посмотреть кластеры без записи
docker exec karaoke_backend python /app/scripts/cluster_catalog.py \
  --db /data/sqlite/karaoke.db \
  --qdrant-host qdrant \
  --qdrant-port 6333 \
  --n-clusters 15 \
  --dry-run

# Если кластеры выглядят осмысленно — запуск с записью
docker exec karaoke_backend python /app/scripts/cluster_catalog.py \
  --db /data/sqlite/karaoke.db \
  --qdrant-host qdrant \
  --qdrant-port 6333 \
  --n-clusters 15
```

> **Подбор числа кластеров:** начните с 15. Посмотрите silhouette score и содержимое кластеров. Если кластеры слишком "размытые" (рок и попс в одном) — увеличьте до 18-20. Если слишком мелкие — уменьшите до 12.

**Ожидаемый вывод:**
```
Loading audio vectors...
  20000 audio vectors
Loading lyrics vectors...
  19500 lyrics vectors

19500 tracks with both vectors

Running K-Means with k=15...
  Silhouette score: 0.15-0.25 (нормально для музыки)

============================================================
 Cluster   Size   Top tracks
============================================================
      0    2100   Кино — Группа крови; ДДТ — Что такое осень; ...
      1    1800   Eminem — Lose Yourself; 50 Cent — In Da Club; ...
      ...
```

### Проверка:
```bash
docker exec karaoke_backend python -c "
import sqlite3
conn = sqlite3.connect('/data/sqlite/karaoke.db')
clusters = conn.execute('SELECT id, track_count FROM catalog_clusters ORDER BY id').fetchall()
print(f'Кластеров: {len(clusters)}')
for cid, count in clusters:
    print(f'  cluster {cid}: {count} треков')
assigned = conn.execute('SELECT COUNT(*) FROM tracks WHERE catalog_cluster_id IS NOT NULL').fetchone()[0]
total = conn.execute(\"SELECT COUNT(*) FROM tracks WHERE status = 'ready'\").fetchone()[0]
print(f'Назначено: {assigned}/{total}')
conn.close()
"
```

**Время:** ~2-5 минут (scroll QDrant + K-Means).

---

## Шаг 4: Создание тегов настроения

Привязывает креативные названия к кластерам. Сначала нужно **посмотреть** что в кластерах.

### 4a. Просмотр содержимого кластеров:
```bash
docker exec karaoke_backend python /app/scripts/create_mood_tags.py \
  --db /data/sqlite/karaoke.db \
  --show-clusters
```

Для каждого кластера покажет топ-15 треков. По ним вы поймёте "вайб" кластера.

### 4b. Создание тегов:

**Вариант A — Быстрый тест (placeholder-теги):**
```bash
docker exec karaoke_backend python /app/scripts/create_mood_tags.py \
  --db /data/sqlite/karaoke.db \
  --clear \
  --example
```
Вставит тестовые теги для кластеров 1-10.

**Вариант B — Полноценные теги (рекомендуется):**

1. Откройте вывод `--show-clusters`
2. Для каждого кластера придумайте 5-10 тегов, отражающих настроение/ситуацию
3. Отредактируйте `EXAMPLE_TAGS` в `scripts/create_mood_tags.py`
4. Запустите:
```bash
docker exec karaoke_backend python /app/scripts/create_mood_tags.py \
  --db /data/sqlite/karaoke.db \
  --clear \
  --example
```

### Проверка:
```bash
docker exec karaoke_backend python -c "
import sqlite3
conn = sqlite3.connect('/data/sqlite/karaoke.db')
tags = conn.execute('SELECT mt.name, mt.cluster_id, cc.track_count FROM mood_tags mt JOIN catalog_clusters cc ON mt.cluster_id = cc.id ORDER BY mt.cluster_id').fetchall()
print(f'Тегов: {len(tags)}')
for name, cid, count in tags:
    print(f'  [{cid}] {name} ({count} треков)')
conn.close()
"
```

---

## Шаг 5: Фото артистов (опционально)

Скачивает фото артистов из Spotify. Требует Spotify API credentials.

```bash
# Dry-run — посмотреть сколько артистов без фото
docker exec karaoke_backend python /app/scripts/fetch_artist_images.py \
  --db /data/sqlite/karaoke.db \
  --dry-run

# Запуск (нужны Spotify credentials)
docker exec karaoke_backend python /app/scripts/fetch_artist_images.py \
  --db /data/sqlite/karaoke.db \
  --output-dir /data/media/artists \
  --spotify-client-id YOUR_CLIENT_ID \
  --spotify-client-secret YOUR_CLIENT_SECRET \
  --limit 500
```

> **Совет:** Начните с `--limit 500` (самые популярные артисты). Потом можно повторить для оставшихся. Spotify API имеет rate limit ~180 req/30sec.

> **Без Spotify:** Пропустите этот шаг. Карточки рекомендаций покажут gradient-placeholder вместо фото.

### Проверка:
```bash
docker exec karaoke_backend python -c "
import sqlite3
conn = sqlite3.connect('/data/sqlite/karaoke.db')
total = conn.execute('SELECT COUNT(DISTINCT artist) FROM tracks WHERE status = \"ready\"').fetchone()[0]
with_img = conn.execute('SELECT COUNT(*) FROM artists WHERE image_path IS NOT NULL').fetchone()[0]
print(f'Артистов: {total}, с фото: {with_img}')
conn.close()
"
```

**Время:** ~30-60 минут на 500 артистов (с rate limiting).

---

## Шаг 6: Финальная проверка через API

### 6a. Health check:
```bash
make health
```

### 6b. Рекомендации (POPULAR — без истории):
```bash
# Создать сессию
SESSION=$(curl -s http://localhost/api/v1/sessions -X POST \
  -H 'Content-Type: application/json' \
  -d '{"room_id":"test"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "Session: $SESSION"

# Получить рекомендации
curl -s "http://localhost/api/v1/recommendations?session_id=$SESSION&limit=5" | python3 -m json.tool
```

**Ожидаемый ответ:**
```json
{
  "strategy": "popular",
  "tracks": [
    {"id": "...", "artist": "Кино", "title": "Группа крови", "similarity_score": 0.0, "artist_image_url": "/api/v1/media/artists/abc123.jpg"},
    ...
  ]
}
```

### 6c. Теги настроения:
```bash
curl -s "http://localhost/api/v1/tags?session_id=$SESSION" | python3 -m json.tool
```

**Ожидаемый ответ:**
```json
[
  {"id": 1, "name": "Костёр на даче"},
  {"id": 2, "name": "Шальная императрица"},
  ...
]
```

### 6d. Рекомендации по тегу:
```bash
# Используйте id тега из предыдущего ответа
curl -s "http://localhost/api/v1/recommendations?session_id=$SESSION&tag_id=1&limit=5" | python3 -m json.tool
```

**Ожидаемый ответ:**
```json
{
  "strategy": "cluster",
  "tracks": [...]
}
```

### 6e. Рекомендации с фильтром языка:
```bash
curl -s "http://localhost/api/v1/recommendations?session_id=$SESSION&language=ru&limit=5" | python3 -m json.tool
```

### 6f. Полный цикл (добавить участника → трек → finish → рекомендации CLUSTER):
```bash
# Добавить участника
PARTICIPANT=$(curl -s "http://localhost/api/v1/sessions/$SESSION/participants" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"name":"Тестер"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Добавить трек в очередь (используйте id трека из рекомендаций)
TRACK_ID="<id трека из шага 6b>"
ENTRY=$(curl -s http://localhost/api/v1/queue -X POST \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SESSION\",\"participant_id\":\"$PARTICIPANT\",\"track_id\":\"$TRACK_ID\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Начать воспроизведение
curl -s "http://localhost/api/v1/queue/$ENTRY/start" -X POST | python3 -m json.tool

# Завершить воспроизведение
curl -s "http://localhost/api/v1/queue/$ENTRY/finish" -X POST | python3 -m json.tool

# Теперь рекомендации должны быть CLUSTER (не POPULAR)
curl -s "http://localhost/api/v1/recommendations?session_id=$SESSION&limit=5" | python3 -m json.tool
# strategy должен быть "cluster", треки — похожие на спетый
```

### 6g. Проверка тегов после прослушивания:
```bash
# Теги должны исключить кластер спетого трека
curl -s "http://localhost/api/v1/tags?session_id=$SESSION" | python3 -m json.tool
# Количество тегов может уменьшиться (покрытый кластер скрыт)
```

---

## Порядок запуска (cheatsheet)

```bash
# 1. Перезапустить backend (применить миграции)
docker restart karaoke_backend

# 2. Popularity scoring
docker exec karaoke_backend python /app/scripts/parse_karaoke_charts.py --db /data/sqlite/karaoke.db

# 3. Кластеризация
docker exec karaoke_backend python /app/scripts/cluster_catalog.py --db /data/sqlite/karaoke.db --qdrant-host qdrant --n-clusters 15

# 4. Теги
docker exec karaoke_backend python /app/scripts/create_mood_tags.py --db /data/sqlite/karaoke.db --show-clusters
# (просмотрите, отредактируйте EXAMPLE_TAGS, запустите --clear --example)

# 5. Фото (опционально)
docker exec karaoke_backend python /app/scripts/fetch_artist_images.py --db /data/sqlite/karaoke.db --output-dir /data/media/artists --spotify-client-id ... --spotify-client-secret ... --limit 500
```

---

## Повторные запуски (крон)

| Скрипт | Частота | Что делает |
|--------|---------|------------|
| `parse_karaoke_charts.py` | Раз в неделю | Обновляет чарты, пересчитывает категории |
| `cluster_catalog.py` | Раз в сутки | Пересчитывает кластеры (учитывает новые треки). **Удаляет mood_tags** — после запуска нужно пересоздать теги |
| `create_mood_tags.py` | После каждого cluster_catalog | Пересоздаёт теги |
| `fetch_artist_images.py` | По необходимости | Скачивает фото для новых артистов |

---

## Troubleshooting

**Кластеры пустые / 0 треков назначено:**
- Проверьте что QDrant доступен: `curl http://qdrant:6333/healthz`
- Проверьте что обе коллекции существуют: `curl http://qdrant:6333/collections`
- Проверьте что треки имеют и audio, и lyrics вектора

**Рекомендации всегда POPULAR:**
- Проверьте play_history: `SELECT COUNT(*) FROM play_history WHERE session_id = '...'`
- CLUSTER включается только после `finish_playing` (не после добавления в очередь)

**Теги не показываются:**
- Проверьте что mood_tags не пуста: `SELECT COUNT(*) FROM mood_tags`
- Проверьте что cluster_id в mood_tags совпадают с id в catalog_clusters

**Фото не отображаются:**
- Проверьте что файлы в `/data/media/artists/`
- Проверьте что nginx маппит `/api/v1/media/artists/` на правильную директорию
