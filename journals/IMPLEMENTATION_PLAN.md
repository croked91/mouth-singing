# План реализации: Караоке-приложение

**Версия:** 2.0
**Дата:** 2026-02-22
**Статус:** Ожидает согласования

---

## Общие принципы

- Каждая фаза — законченная мини-задача, оформляемая коммитом
- Промпты из `design/prompts/` используются напрямую как спецификация для фронтенд-агента
- `shared/` пакет создаётся сразу, чтобы исключить дублирование кода между backend, worker, bootstrap
- Структурированное логирование (structlog) внедряется с первой фазы
- Фазы нарезаны так, чтобы каждая помещалась в контекст одного подагента

### Карта подагентов

| Подагент | Тип (subagent_type) | Когда используется |
|---|---|---|
| Эксперт Python | `python-developer` | Бэкенд, воркер, CLI — любой Python-код |
| Фронтендер | `frontend-web-client` | React + TypeScript + MUI |
| ML-эксперт | `ml-sota-expert` | Извлечение фич, эмбеддинги, рекомендации |
| Тестировщик | `polyglot-test-engineer` | Unit-, интеграционные, E2E тесты |
| Архитектор | `software-architect` | Ревью кода, архитектурные решения |
| UI-директор | `ui-design-director` | Ревью фронтенда на соответствие дизайну |

### Ключевые ссылки

| Документ | Путь | Что содержит |
|---|---|---|
| Архитектура | `journals/ARCHITECTURE.md` | C4 модель, модули, даталогическая модель, API-контракт, Docker Compose, структура проекта |
| ADR | `journals/ADR.md` | 9 принятых архитектурных решений |
| Дизайн-система | `design/prompts/00_design_system.md` | Цвета, типографика, glassmorphism, анимации, MUI маппинг |
| Дизайн экранов | `design/prompts/01-07_*.md` | 7 экранов: Landing, Participants, Queue, Search, Upload, Player, Admin |
| Журнал проекта | `journals/PROJECT_LOG.md` | Хронология и статусы |
| Фазы | `journals/PHASES.md` | Таблица статусов фаз |
| Детали фаз | `journals/phases/` | Подробное описание каждой фазы (phase-03.md … phase-15.md) |

---

## Диаграмма зависимостей

```
Фаза 3 (scaffold)
    │
    ▼
Фаза 4a (модели + репозитории)
    │
    ▼
Фаза 4b (unit-тесты)
    │
    ├─────────────────┐
    ▼                 ▼
Фаза 5 (sessions)   Фаза 6 (tracks+search)
    │                 │
    │    ┌────────────┘
    ▼    ▼
Фаза 7a (JobService + UVR)
    │
    ▼
Фаза 7b (Sonoix + VideoGen + SSE)
    │
    ▼
Фаза 8a (extractors + pipeline)
    │
    ▼
Фаза 8b (recommendations)
    │
    ├─── Фаза 13 (bootstrap CLI) ────────┐
    │                                      │
    ▼                                      │
Фаза 9 (FE: scaffold + landing)          │
    │                                      │
    ▼                                      │
Фаза 10a (FE: queue page)               │
    │                                      │
    ▼                                      │
Фаза 10b (FE: search + upload)          │
    │                                      │
    ▼                                      │
Фаза 11 (FE: karaoke player)            │
    │                                      │
    ▼                                      │
Фаза 12 (FE: admin + polish)            │
    │                                      │
    ├──────────────────────────────────────┘
    ▼
Фаза 14 (Docker Compose + Nginx)
    │
    ▼
Фаза 15 (E2E testing + hardening)
```

**Примечание:** Фазы 5 и 6 могут выполняться параллельно. Фаза 13 (Bootstrap) может начаться после Фазы 8b, параллельно с фронтендом.

---

## Сводная таблица

| Фаза | Название | Подагенты |
|---|---|---|
| [Фаза 3](phases/phase-03.md) | Скаффолдинг + инфраструктура | python-developer, polyglot-test-engineer |
| [Фаза 4a](phases/phase-04a.md) | Модели + репозитории | python-developer, software-architect |
| [Фаза 4b](phases/phase-04b.md) | Unit-тесты слоя данных | polyglot-test-engineer |
| [Фаза 5](phases/phase-05.md) | Сессии + участники + очередь | python-developer, polyglot-test-engineer |
| [Фаза 6](phases/phase-06.md) | Каталог треков + поиск | python-developer, polyglot-test-engineer |
| [Фаза 7a](phases/phase-07a.md) | Worker + UVR | python-developer, polyglot-test-engineer |
| [Фаза 7b](phases/phase-07b.md) | Sonoix + VideoGen + SSE | python-developer, polyglot-test-engineer |
| [Фаза 8a](phases/phase-08a.md) | Feature extraction + embeddings | ml-sota-expert, polyglot-test-engineer |
| [Фаза 8b](phases/phase-08b.md) | Рекомендательная система | ml-sota-expert, polyglot-test-engineer |
| [Фаза 9](phases/phase-09.md) | FE: scaffold + тема + Landing + Sessions | frontend-web-client, ui-design-director |
| [Фаза 10a](phases/phase-10a.md) | FE: QueuePage + рекомендации | frontend-web-client, ui-design-director |
| [Фаза 10b](phases/phase-10b.md) | FE: Поиск + Загрузка | frontend-web-client, ui-design-director |
| [Фаза 11](phases/phase-11.md) | FE: Караоке-плеер | frontend-web-client, ui-design-director |
| [Фаза 12](phases/phase-12.md) | FE: Админка + UX polish | frontend-web-client, ui-design-director |
| [Фаза 13](phases/phase-13.md) | Bootstrap CLI | python-developer, ml-sota-expert, polyglot-test-engineer |
| [Фаза 14](phases/phase-14.md) | Docker Compose + Nginx | python-developer, polyglot-test-engineer |
| [Фаза 15](phases/phase-15.md) | E2E тестирование + hardening | polyglot-test-engineer, python-developer, frontend-web-client, software-architect |

**Итого: 17 фаз реализации** (нумерация 3-15, с подфазами a/b)
