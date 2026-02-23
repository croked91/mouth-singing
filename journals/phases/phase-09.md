## Фаза 9: Фронтенд — скаффолдинг, тема, Landing + Sessions

### Входные артефакты
- Результат Фаз 3-8b (полностью работающий backend с API)
- `design/prompts/00_design_system.md` — дизайн-система: цвета, типографика, glassmorphism, анимации, MUI маппинг
- `design/prompts/01_landing.md` — экран Landing (стартовый, «Начать сессию»)
- `design/prompts/02_session_participants.md` — экран добавления участников
- `journals/ARCHITECTURE.md` — раздел 8 «Структура проекта» (frontend/), раздел 10 «API-контракт»

### Задачи фазы

#### Оркестратор (ты)
Передаёшь `frontend-web-client` задачу на создание React-приложения с нуля. Агент получает дизайн-спецификации из `design/prompts/` и использует их как прямую инструкцию для реализации UI. Интерфейс на русском языке. После создания запускаешь `ui-design-director` для ревью визуального соответствия дизайну.

#### Подагент `frontend-web-client`
Создаёт фронтенд-приложение:

1. **Инициализация**: Vite + React 18 + TypeScript + MUI 5
2. **Тёмная тема** (`src/theme/darkTheme.ts`) — строго по `00_design_system.md`:
   - Цвета: Background #050508, Surface `rgba(15,10,40,0.85)`, Primary gradient #7C3AED → #2563EB, Accent #F0ABFC
   - Типографика: Inter (Google Fonts), размеры и weight из спеки
   - Glassmorphism recipe: backdrop-filter blur(24px), border rgba(255,255,255,0.08), box-shadow
   - MUI overrides: Button, Card, TextField, Chip — все по спеке
3. **Zustand stores** (`src/store/`):
   - `sessionStore.ts`: session_id, participants, createSession(), addParticipant()
   - `queueStore.ts`: currentEntry, upcoming, addToQueue(), skipTurn()
   - `playerStore.ts`: isPlaying, currentTime, volume
4. **API client** (`src/services/api.ts`): axios, baseURL=/api/v1, типизированные методы
5. **SSE service** (`src/services/sseService.ts`): EventSource обёртка для /jobs/{id}/status
6. **Роутинг** (react-router-dom):
   - `/` → WelcomePage
   - `/session/:id` → SessionPage (участники)
   - `/session/:id/queue` → QueuePage (Фаза 10a)
   - `/session/:id/play/:entryId` → PlayerPage (Фаза 11)
   - `/admin` → AdminPage (Фаза 12)
7. **WelcomePage** (`src/pages/WelcomePage/`) — по спеке `01_landing.md`:
   - Полноэкранный тёмный фон с градиентом
   - Центрированная карточка: логотип/иконка, заголовок «Добро пожаловать в Караоке!»
   - Кнопка «Начать сессию» с gradient эффектом
   - POST /sessions → переход к SessionPage
8. **SessionPage** (`src/pages/SessionPage/`) — по спеке `02_session_participants.md`:
   - Список участников (chip-компоненты)
   - Кнопка «Добавить участника» → модалка с полем ввода имени + кнопка «Сгенерировать никнейм»
   - POST /sessions/{id}/participants → обновление списка
   - Кнопка «Начать караоке!» → переход к QueuePage
9. **frontend/Dockerfile**: multi-stage build (node → nginx, static files)

#### Подагент `ui-design-director`
Ревью: открывает приложение в браузере, проверяет соответствие дизайн-системе (цвета, glassmorphism, типографика, отступы). Даёт замечания или одобряет.

#### Пользователь
Открывает приложение в браузере, проверяет WelcomePage и SessionPage. Подтверждает или вносит замечания.

### Выходные артефакты
- React + TypeScript + MUI приложение с тёмной темой по дизайн-системе
- Zustand stores (session, queue, player)
- API client + SSE service
- WelcomePage: создание сессии
- SessionPage: добавление участников с русскоязычными никнеймами
- Роутинг между страницами
- `npm run build` без ошибок
- `docker build ./frontend` проходит
- Коммит

