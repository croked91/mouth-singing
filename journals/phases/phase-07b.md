## Фаза 7b: Sonoix + VideoGenerator + SSE

### Входные артефакты
- Результат Фазы 7a (Worker + UVR)
- `journals/ARCHITECTURE.md` — раздел 3.7 «AudioPipeline» (шаги 2-3), раздел 7.1 «Online Pipeline», раздел 10 «API-контракт» (Jobs SSE)
- `journals/ADR.md` — ADR-002 (Sonoix), ADR-008 (VPN для РФ)

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `python-developer` задачу на завершение аудио-пайплайна: интеграция Sonoix API для транскрипции, генерация караоке-видео через FFmpeg, и SSE endpoint для отслеживания прогресса на фронтенде. Важно: Sonoix API может быть недоступен из РФ напрямую, поэтому нужна поддержка HTTP-прокси.

#### Подагент `python-developer`
Завершает аудио-пайплайн:

1. **SonoixClient** (`worker/app/pipeline/sonoix_client.py`):
   - httpx async клиент к Sonoix API
   - `transcribe(vocals_path) -> TranscriptionResult` — отправляет WAV, получает JSON с послоговой разметкой
   - Формат ответа: `{syllables: [{text, start_ms, end_ms}], full_text, language}`
   - Поддержка HTTP_PROXY / HTTPS_PROXY env vars для работы через VPN из РФ (ADR-008)
   - Timeout: 60 сек, retry: 2 попытки
   - Конфигурация: `SONOIX_API_KEY`, `SONOIX_API_URL` из env

2. **Syllabifier** (`shared/karaoke_shared/utils/syllabifier.py`):
   - Простое деление слов на слоги через `pyphen` (ru_RU + en_US)
   - Если Sonoix возвращает word-level тайминги → дробим на слоги с пропорциональным делением времени
   - Если Sonoix возвращает syllable-level → используем как есть

3. **VideoGenerator** (`worker/app/pipeline/video_generator.py`):
   - Генерирует MP4 караоке-клип: инструментальная дорожка + субтитры с послоговой подсветкой
   - Используется FFmpeg с ASS subtitle filter (НЕ покадровый PIL — в 5-10 раз быстрее)
   - ASS формат: тёмный полупрозрачный фон, крупный шрифт (72px Inter), послоговая подсветка через `\k` тэги
   - Цвета из дизайн-системы (`design/prompts/06_karaoke_player.md`): спетые → dim, активный → neon glow (#F0ABFC), предстоящие → белый
   - Выход: `MEDIA_ROOT/clips/{track_id}.mp4` (H.264 + AAC)

4. **SSE endpoint** (`backend/app/api/v1/sse.py`):
   - `GET /jobs/{job_id}/status` → `Content-Type: text/event-stream`
   - Long-polling: каждые 2 сек читает статус из SQLite job_queue
   - События: `status` (step + progress), `completed` (clip_url), `error` (message)
   - Формат как в ARCHITECTURE.md раздел 10 (Jobs SSE)

5. **Интеграция в AudioPipeline**: заменить заглушки шагов 2-3 на реальные вызовы:
   - Шаг 2: SonoixClient.transcribe(vocals) → syllable_timings
   - Шаг 3: VideoGenerator.generate(instrumental, syllable_timings, artist, title) → clip.mp4
   - Сохранение `syllable_timings` (JSON) и `lyrics_text` в SQLite таблицу tracks
   - Обновление clip_path, instrumental_path, status=ready

6. **VPN конфигурация**: документация в `.env.example` для HTTP_PROXY переменных. Опциональный WireGuard-контейнер в `docker-compose.override.vpn.yml`.

#### Подагент `polyglot-test-engineer`
E2E тест:
- POST /tracks/upload (MP3) → Job создаётся
- SSE /jobs/{id}/status → получаем события: separating → transcribing → generating_video → completed
- Готовый MP4 существует, содержит аудио + субтитры
- syllable_timings сохранены в SQLite
- Весь процесс < 60 сек для 3-минутного трека

#### Пользователь
Проверяет полный цикл upload → clip на реальном MP3. Проверяет качество субтитров в клипе. Подтверждает или вносит замечания.

### Выходные артефакты
- `SonoixClient` с поддержкой VPN/прокси
- `Syllabifier` (pyphen ru_RU + en_US)
- `VideoGenerator` (FFmpeg + ASS)
- SSE endpoint `/jobs/{id}/status`
- Полный пайплайн: upload → UVR → Sonoix → FFmpeg → ready
- MP4 клип с послоговыми субтитрами
- Коммит

