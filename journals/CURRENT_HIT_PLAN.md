# План реализации current_hit / former_hit

## Контекст

В каталоге 25k треков. Категории popularity: `eternal_hit` (992), `artist_best` (2880), `regular` (22706). Нужно добавить `current_hit` (сейчас в чартах) и `former_hit` (был в чартах, выпал).

## Источники чартов

5 коллекций с hitmotop:
- https://rus.hitmotop.com/collection/10567 — ТОП100 ВК
- https://rus.hitmotop.com/collection/10562 — Europa Plus
- https://rus.hitmotop.com/collection/10571 — ТОП100 Shazam
- https://rus.hitmotop.com/collection/10902 — ТОП100 USA
- https://rus.hitmotop.com/collection/10569 — Русское радио

## Архитектура

### Новая таблица: `chart_tracks`

```sql
CREATE TABLE IF NOT EXISTS chart_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,          -- 'vk_top100', 'europa_plus', 'shazam', 'usa_top100', 'russkoe_radio'
    fetched_at TEXT NOT NULL,      -- ISO timestamp когда спарсили
    UNIQUE(artist, title, source)
);
```

Это кэш текущих чартов. Перезаписывается полностью при каждом обновлении (DELETE + INSERT по source).

### Компонент 1: ChartService (`shared/karaoke_shared/services/chart_service.py`)

Класс с методами:

```python
class ChartService:
    SOURCES = {
        "vk_top100": "https://rus.hitmotop.com/collection/10567",
        "europa_plus": "https://rus.hitmotop.com/collection/10562",
        "shazam": "https://rus.hitmotop.com/collection/10571",
        "usa_top100": "https://rus.hitmotop.com/collection/10902",
        "russkoe_radio": "https://rus.hitmotop.com/collection/10569",
    }

    async def fetch_charts(self) -> dict[str, list[tuple[str, str]]]:
        """Парсит все 5 URL, возвращает {source: [(artist, title), ...]}."""
        # BeautifulSoup парсинг HTML
        # Каждая коллекция — список треков с artist и title
        # Graceful: если сайт недоступен — логируем warning, возвращаем пустой список для этого source
        ...

    async def refresh_cache(self):
        """Обновляет таблицу chart_tracks свежими данными."""
        charts = await self.fetch_charts()
        for source, tracks in charts.items():
            # DELETE FROM chart_tracks WHERE source = ?
            # INSERT новые
        ...

    async def is_in_charts(self, artist: str, title: str) -> bool:
        """Проверяет трек по кэшированным чартам (fuzzy match)."""
        # SELECT * FROM chart_tracks
        # Fuzzy match artist+title (SequenceMatcher, порог 0.85)
        ...

    async def update_popularity(self):
        """Обновляет popularity_category для всех треков на основе чартов."""
        # 1. Для каждого трека в chart_tracks — fuzzy match по tracks
        # 2. Найденные: popularity_category = 'current_hit', chart_count++, chart_last_seen = now()
        # 3. Не трогать eternal_hit (они выше по приоритету)
        ...
```

### Компонент 2: Проверка при загрузке трека (worker pipeline)

**Где:** `worker/gpu/gpu_pipeline.py` и `worker/api/api_pipeline.py`, после финализации трека.

**Как:**
```python
# В конце process(), после mark_completed:
if self.chart_service:
    is_hit = await self.chart_service.is_in_charts(track.artist, track.title)
    if is_hit:
        await self.repo.update_track(
            track_id,
            TrackUpdate(popularity_category="current_hit", chart_last_seen=now_iso())
        )
```

**Зависимости:**
- ChartService инжектится в pipeline при инициализации
- chart_tracks должна быть заполнена (иначе is_in_charts всегда False — ок, просто не размечает)

**Важно:** Не перезаписывать `eternal_hit` — проверять текущую категорию перед обновлением.

### Компонент 3: Крон-скрипт (`scripts/update_charts.py`)

Запускается ежедневно. Логика:

```
1. chart_service.refresh_cache()
   — парсит 5 URL
   — обновляет chart_tracks

2. chart_service.update_popularity()
   — матчит chart_tracks по каталогу
   — найденные → current_hit (кроме eternal_hit)
   — chart_count++, chart_last_seen = now()

3. Перевод в former_hit:
   — SELECT * FROM tracks WHERE popularity_category = 'current_hit'
     AND chart_last_seen < datetime('now', '-30 days')
   — UPDATE ... SET popularity_category = 'former_hit'
   — (30 дней — настраиваемый параметр)

4. Лог статистики:
   — Сколько треков в каждой категории
   — Сколько новых current_hit
   — Сколько переведено в former_hit
```

**Запуск:**
```bash
# Из docker-compose
docker exec karaoke_backend python /app/scripts/update_charts.py

# Или cron на хосте
0 6 * * * docker exec karaoke_backend python /app/scripts/update_charts.py >> /var/log/chart_update.log 2>&1
```

**Аргументы:**
- `--db` — путь к SQLite (default: /data/sqlite/karaoke.db)
- `--dry-run` — показать что будет, не писать
- `--stale-days 30` — через сколько дней current_hit → former_hit

## Приоритет категорий

При обновлении popularity_category соблюдать иерархию:
```
eternal_hit > current_hit > artist_best > former_hit > regular
```

Правила:
- `eternal_hit` — НИКОГДА не перезаписывается автоматически
- `current_hit` — может перезаписать `artist_best`, `former_hit`, `regular`
- Когда `current_hit` стареет → `former_hit` (но НЕ `regular`, потому что бывший хит ценнее)
- `artist_best` — выставляется один раз (LLM), не перезаписывается чартами
- Если трек был `artist_best` и попал в чарты → `current_hit`. Выпал из чартов → обратно `artist_best` (не `former_hit`)

Для этого нужно хранить "базовую" категорию отдельно или просто учитывать при переводе:
```python
# При переводе из current_hit:
if previous_category == 'artist_best':
    new_category = 'artist_best'  # возвращаем
else:
    new_category = 'former_hit'
```

Для этого добавить поле `base_popularity_category` в tracks — исходная категория до попадания в чарты. Или проще: хранить `was_artist_best` boolean.

**Решение:** добавить столбец `base_popularity_category TEXT` в tracks. При первой установке current_hit сохраняем текущую категорию в base. При снятии current_hit — восстанавливаем из base.

## Парсинг HTML hitmotop

Структура страницы коллекции (нужно проверить при реализации):
```html
<div class="track__info">
    <a class="track__title">НАЗВАНИЕ ТРЕКА</a>
    <a class="track__desc">ИСПОЛНИТЕЛЬ</a>
</div>
```

Fallback: если структура изменилась — логируем ошибку, возвращаем пустой список. Не крашим приложение.

## Файлы для создания/изменения

1. **Новый:** `shared/karaoke_shared/services/chart_service.py`
2. **Изменить:** `shared/karaoke_shared/repositories/sqlite_repository.py` — миграция chart_tracks + base_popularity_category
3. **Изменить:** `worker/gpu/gpu_pipeline.py` — вызов chart_service после финализации
4. **Изменить:** `worker/api/api_pipeline.py` — то же
5. **Изменить:** `worker/app/main.py` — инициализация ChartService, передача в pipeline
6. **Новый:** `scripts/update_charts.py` — крон-скрипт
7. **Изменить:** `backend/app/main.py` — миграция новых столбцов

## Порядок реализации

1. Миграция БД (chart_tracks таблица + base_popularity_category столбец)
2. ChartService (парсинг + кэш + match)
3. Крон-скрипт (update_charts.py)
4. Интеграция в worker pipeline
5. Тесты
6. Настройка крона на продакшне
