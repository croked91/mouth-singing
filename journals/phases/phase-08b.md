## Фаза 8b: Рекомендательная система

### Входные артефакты
- Результат Фазы 8a (векторы в QDrant)
- `journals/ARCHITECTURE.md` — раздел 3.5 «RecommendationService», раздел 6.4 «Получение рекомендаций», раздел 10 «API-контракт» (Recommendations)
- `journals/ADR.md` — ADR-005 (без регистрации, портрет в рамках сессии)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `ml-sota-expert` задачу на реализацию рекомендательной системы. Это одна из ключевых фич приложения (Core domain по DDD). Рекомендации строятся без user ID — только на основе истории исполнения участника в текущей сессии. После реализации — `polyglot-test-engineer`.

#### Подагент `ml-sota-expert`
Реализует рекомендательную систему:

1. **RecommendationService** (`backend/app/services/recommendation_service.py`):
   - `get_recommendations(participant_id, session_id, limit=10) -> RecommendationResponse`
   - Автоматический выбор стратегии по количеству исполненных треков участника:
     - 0 треков → strategy=`popular`: `SELECT ... FROM tracks WHERE status='ready' ORDER BY play_count DESC LIMIT 10`
     - 1 трек → strategy=`last`: KNN по вектору последнего трека из `audio_features` коллекции QDrant
     - 2 трека → strategy=`last_two_avg`: KNN по среднему арифметическому векторов двух последних треков
     - 3+ треков → strategy=`session_avg`: KNN по `portrait_vector` участника (скользящее среднее всех его треков)
   - Фильтрация: исключить уже исполненные участником треки, только status=ready
   - Возвращает: strategy name + list of tracks с similarity_score

2. **Portrait vector обновление**:
   - При `POST /queue/{entry_id}/finish` (уже реализован в Фазе 5) → добавить:
     - Получить audio_features вектор текущего трека из QDrant
     - Пересчитать `portrait_vector` участника: `new = (old * (n-1) + current) / n` (скользящее среднее)
     - Сохранить обновлённый `portrait_vector` в SQLite (participants)

3. **Граф переходов** (коллаборативная составляющая):
   - При `POST /queue/{entry_id}/finish` → если у участника есть предыдущий трек:
     - Upsert в QDrant коллекцию `transitions`: `{from_track_id, to_track_id, weight++}`
   - В рекомендациях: к content-based результатам подмешиваются «после песни X часто поют Y»

4. **API-роутер** (`backend/app/api/v1/recommendations.py`):
   - `GET /recommendations?participant_id=X&session_id=Y&limit=10` → 200
   - Ответ: `{strategy: "last_two_avg", tracks: [{id, artist, title, duration_sec, similarity_score}]}`

#### Подагент `polyglot-test-engineer`
Тесты рекомендательной системы:
- Участник без истории → strategy=popular → 10 треков по play_count
- Участник с 1 треком → strategy=last → похожие треки из QDrant
- Участник с 2 треками → strategy=last_two_avg
- Участник с 3+ → strategy=session_avg
- finish обновляет portrait_vector и transitions
- Рекомендации не включают уже исполненные участником треки

#### Пользователь
Проверяет рекомендации в разных сценариях (новый участник, после 1-2-3 треков). Подтверждает или вносит замечания.

### Выходные артефакты
- `RecommendationService` с 4 стратегиями
- Portrait vector обновление при finish
- Граф переходов в QDrant
- API `/recommendations`
- Тесты рекомендаций
- Коммит

