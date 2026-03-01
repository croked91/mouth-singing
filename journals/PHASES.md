# Журнал фаз проекта

## Фаза 1: Анализ и проектирование
- **Статус:** Завершена (коммит 43bb7dc)
- **Начало:** 2026-02-22
- **Выходные артефакты:**
  - [x] journals/ADR.md — 9 принятых решений
  - [x] journals/ARCHITECTURE.md — полный архитектурный документ (1720+ строк)
  - [x] design/prompts/ — 8 промптов для FigGPT (дизайн-система + 7 экранов, UI на русском)

## Фаза 2: Пофазный план реализации
- **Статус:** Завершена (коммит 2694b95)
- **Начало:** 2026-02-22
- **Примечание:** Figma MCP не используем — промпты используются напрямую как спецификация
- **Выходные артефакты:**
  - [x] journals/IMPLEMENTATION_PLAN.md — 17 фаз реализации (3-15, с подфазами a/b)
  - [x] journals/phases/ — подробные описания каждой фазы в формате входные/задачи/выходные артефакты

## Фазы реализации (3-15)

| Фаза | Название | Файл | Статус |
|---|---|---|---|
| 3 | Скаффолдинг проекта и инфраструктура | [phase-03.md](phases/phase-03.md) | Завершена |
| 4a | Pydantic-модели и репозитории | [phase-04a.md](phases/phase-04a.md) | Завершена |
| 4b | Unit-тесты слоя данных | [phase-04b.md](phases/phase-04b.md) | Завершена |
| 5 | Сессии, участники, очередь | [phase-05.md](phases/phase-05.md) | Завершена |
| 6 | Каталог треков и поиск | [phase-06.md](phases/phase-06.md) | Завершена |
| 7a | Audio Worker — JobService + UVR | [phase-07a.md](phases/phase-07a.md) | Завершена |
| 7b | Sonoix + VideoGenerator + SSE | [phase-07b.md](phases/phase-07b.md) | Завершена |
| 8a | Извлечение фичей и эмбеддингов | [phase-08a.md](phases/phase-08a.md) | Завершена |
| 8b | Рекомендательная система | [phase-08b.md](phases/phase-08b.md) | Завершена |
| 9 | Фронтенд — скаффолдинг, тема, Landing + Sessions | [phase-09.md](phases/phase-09.md) | Завершена |
| 10a | Фронтенд — QueuePage + рекомендации | [phase-10a.md](phases/phase-10a.md) | Завершена |
| 10b | Фронтенд — Поиск + Загрузка | [phase-10b.md](phases/phase-10b.md) | Завершена |
| 11 | Фронтенд — Караоке-плеер | [phase-11.md](phases/phase-11.md) | Завершена |
| 12 | Фронтенд — Админка и UX polish | [phase-12.md](phases/phase-12.md) | Завершена |
| 13 | Bootstrap CLI | [phase-13.md](phases/phase-13.md) | Завершена |
| 14 | Docker Compose + Nginx + Deploy | [phase-14.md](phases/phase-14.md) | Завершена |
| 15 | E2E тестирование и hardening | [phase-15.md](phases/phase-15.md) | Завершена (d586571, a012830) |

## Фаза 16: Bootstrap pipeline v2 + массовый импорт треков

- **Статус:** В процессе (~71% каталога обработано)
- **Начало:** 2026-02-25
- **Ключевые решения:**
  - BS-Roformer (SDR 12.9 SOTA) для бутстрапа, MDX-NET для продакшна (ADR-013)
  - Multi-GPU `_run_local_gpu()` с atomic claiming и per-track QDrant flush (ADR-014)
  - 12 воркеров на 4×RTX 4090 (3 воркера/GPU) — GPU util 93-100%
- **Инфраструктура:**
  - GPU сервер: root@195.225.111.241 (philomena), 4×RTX 4090, 24 vCPU, 235GB RAM
  - Диск данных: 394GB (`/mnt/data`), мигрирован с VPS lainey
  - QDrant v1.8.0 binary, conda env на data disk
- **Прогресс:** ~3337/4726 треков, ETA ~5ч (к ~14:00 01.03.2026)

## ML-аудит и исправление рекомендательной системы (2026-03-01)

- **Статус:** Код готов, ожидает z-score reindex на сервере (после bootstrap)
- **Контекст:** ML-аудит (совместно с ml-sota-expert) выявил 9 проблем: scale dominance (tempo ~120 vs flatness ~0.001), transition weight всегда = 1, N+1 SQL запросы, no recency bias, portrait drift без L2-renorm, popularity feedback loop, transition graph не используется, tracks_played из len(history), нет payload index from_track_id.
- **Решение:** Все 9 проблем исправлены за один проход:
  - Post-hoc z-score + скрипт `v2/scripts/reindex_audio_features.py` (cost: $0, ~30 сек)
  - EMA portrait (alpha=0.3) + L2-renormalization
  - Transition weight increment (read-modify-write)
  - Transition candidates в LAST стратегии
  - Batch SQLite (get_tracks_by_ids)
  - Popular: 70% top + 30% random
  - QDrant payload index from_track_id
- **Тесты:** 85/85 pass (27 feature extractor + 58 recommendation service)
- **Файлы:** feature_extractor.py, recommendation_service.py, qdrant_repository.py, sqlite_repository.py, main.py + скрипт reindex + тесты
- **TODO:** Запустить `reindex_audio_features.py` на GPU-сервере после завершения bootstrap

## Итог

Все 17 фаз (1–15, включая подфазы a/b) завершены. Фаза 16 (массовый импорт каталога) в процессе:
- **540+ unit/integration тестов** — все pass (включая 85 новых для рекомендательной системы)
- **Browser E2E** (Playwright через Docker) — все потоки проверены
- **Архитектурное ревью** — 2 критических бага и 5 предупреждений найдены и исправлены
- **ML-аудит рекомендательной системы** — 9 проблем найдены и исправлены
- **Bootstrap:** ~3337/4726 треков обработано, 12 воркеров на 4×RTX 4090
