# FigGPT Prompt — Screen 03: Main Queue Screen (Core Karaoke Interface)

## PROMPT START

Design the **main karaoke queue management screen** — the primary interface used throughout the entire session. Tablet/monitor (1920x1080px). Dark glassmorphism. This is the most complex and most-used screen — it must feel vibrant, live, and energetic.

---

## BACKGROUND

Same cosmic background system:
- Deep gradient: `#0D0B2B` → `#1A1060` → `#0A1628`
- Stronger radial glows on this screen (active session feel):
  - Magenta blob `#A855F7` at 35% opacity, size ~600px, top-right area
  - Blue blob `#3B82F6` at 30% opacity, size ~500px, left side
  - Cyan accent `#06B6D4` at 20% opacity, bottom-center
- Star dots (~70, white, 1-2px, varying opacity)

---

## LAYOUT STRUCTURE (1920x1080)

```
┌─────────────────────────────────────────────────────────────────────┐
│  TOP BAR  [Logo]    [СЕЙЧАС ПОЁТ: NAME]    [ПРОПУСТИТЬ]  [АДМИН]  72px│
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  LEFT PANEL (480px)          │  RIGHT PANEL (flex: 1)               │
│  ┌─────────────────────┐     │  ┌───────────────────────────────┐   │
│  │  CURRENT SINGER     │     │  │TAB BAR: Поиск | Рекомендации | Загрузить|   │
│  │  (large avatar +    │     │  ├───────────────────────────────┤   │
│  │   name + status)    │     │  │                               │   │
│  └─────────────────────┘     │  │  ACTIVE TAB CONTENT           │   │
│                              │  │  (Search / Recommendations    │   │
│  QUEUE STRIP (horizontal     │  │   / Upload)                   │   │
│  scrollable avatars)         │  │                               │   │
│                              │  └───────────────────────────────┘   │
│                              │                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## COMPONENTS

### Top Navigation Bar
- Height: 72px, full width
- Background: `rgba(13,11,43,0.9)`, backdrop-filter: blur(20px)
- Border-bottom: 1px solid `rgba(255,255,255,0.08)`

Left section (240px):
- App logo: microphone icon 28px + "KARAOKE" Inter 700 16px, gradient `#E0C3FC`→`#8EC5FC`

Center section (flex):
- Label: "СЕЙЧАС ПОЁТ:" — Inter 500 12px, letter-spacing 0.1em, `rgba(255,255,255,0.4)`, all-caps
- Singer name: "ВАСЯ" — Inter 800 22px, white, letter-spacing 0.05em
- Animated pulsing microphone icon next to name: `#A855F7`, size 20px, subtle 2s pulse animation note
- Separator: 1px vertical line `rgba(255,255,255,0.1)`, height 20px

Right section (240px):
- Skip button: "ПРОПУСТИТЬ" — pill shape, height 36px, padding 0 20px, border 1px solid `rgba(255,255,255,0.2)`, background `rgba(255,255,255,0.05)`, Inter 600 13px letter-spacing 0.05em `rgba(255,255,255,0.7)`, border-radius 18px
- Admin button (lock icon only): 36px square, icon `LockIcon` 18px, `rgba(255,255,255,0.3)`

---

### LEFT PANEL — Current Singer & Queue
- Width: 480px (fixed), height: 100% minus topbar
- Background: `rgba(255,255,255,0.03)`
- Border-right: 1px solid `rgba(255,255,255,0.07)`
- Padding: 32px 28px

#### Current Singer Card
- Glassmorphism card, full width, padding: 28px
- Background: `rgba(124,58,237,0.15)`
- Border: 1.5px solid `rgba(167,139,250,0.35)`
- Border-radius: 20px
- Backdrop-filter: blur(16px)
- Box-shadow: `0 0 40px rgba(124,58,237,0.2)`

Contents (centered):
- Avatar circle: 88px diameter
  - Gradient background: `#7C3AED` → `#EC4899`
  - White initial letter: Inter 800 40px
  - Outer ring: 3px gap + 2px ring, gradient `#7C3AED`→`#EC4899`, subtle rotation animation note
  - Status dot: 14px circle bottom-right, `#10B981` green with glow
- Singer name below avatar: Inter 700 28px, white, margin-top 16px
- Status label: "Ваша очередь выбирать!" — Inter 500 13px, `#A78BFA`, margin-top 4px
- Decorative sound-wave bars under status: 4 thin vertical bars animating height (illustration of "active")

#### Queue Section Title
- "СЛЕДУЮЩИЙ" — Inter 600 11px, letter-spacing 0.12em, `rgba(255,255,255,0.35)`, margin-top 28px, margin-bottom 16px

#### Queue Strip (horizontal scroll row)
- Width: 100%, height: 80px
- Overflow: scroll horizontal (hide scrollbar)
- Gap between items: 12px
- Each queue avatar item:
  - Circle: 56px, gradient background (each unique: cycle through 4 preset gradients)
  - White initial: Inter 700 22px
  - Number badge: 18px circle, background `rgba(13,11,43,0.9)`, border 1px solid `rgba(255,255,255,0.2)`, Inter 700 10px white, positioned top-right of avatar circle
  - Nickname below: Inter 500 11px, `rgba(255,255,255,0.55)`, max-width 56px, overflow ellipsis
- Show 4-5 example queue items

---

### RIGHT PANEL — Action Area
- Flex: 1, height: 100% minus topbar
- Padding: 32px 36px

#### Tab Bar (MUI `<Tabs>` styled)
- Full width, height: 52px
- Background: `rgba(255,255,255,0.04)`
- Border: 1px solid `rgba(255,255,255,0.08)`
- Border-radius: 16px
- Padding: 4px
- Gap between tabs: 4px

Each Tab:
- Flex: 1, height: 44px, border-radius: 12px
- Inactive: background transparent, text Inter 600 14px `rgba(255,255,255,0.45)`, icon `rgba(255,255,255,0.3)`
- Active: background `rgba(124,58,237,0.35)`, border 1px solid `rgba(167,139,250,0.4)`, text white, icon `#A78BFA`

Tab 1: MUI `SearchIcon` 18px + "Поиск трека"
Tab 2: MUI `AutoAwesomeIcon` 18px + "Рекомендации"
Tab 3: MUI `CloudUploadIcon` 18px + "Загрузить"

---

#### Active Tab: RECOMMENDATIONS (default/primary state to illustrate)

Label above cards: "В ВАШЕМ СТИЛЕ" — Inter 600 11px, letter-spacing 0.12em, `rgba(255,255,255,0.35)`, margin-bottom 16px

Cards Grid — 2 columns, gap 12px:

Each Recommendation Card:
- Height: 72px, full column width
- Background: `rgba(255,255,255,0.06)`
- Border: 1px solid `rgba(255,255,255,0.1)`
- Border-radius: 14px
- Backdrop-filter: blur(10px)
- Padding: 12px 16px
- Layout: horizontal flex
- Hover state annotation: border color `rgba(167,139,250,0.5)`, background `rgba(124,58,237,0.15)`

Card contents (left to right):
1. Track number or small album art placeholder: 44px square, border-radius 8px, background gradient (abstract, unique per card: `#7C3AED`→`#3B82F6`, or `#EC4899`→`#F59E0B`, etc.), centered music note icon `rgba(255,255,255,0.5)` 20px
2. Text block (flex: 1):
   - Track title: Inter 600 15px white, overflow ellipsis
   - Artist name: Inter 400 13px `rgba(255,255,255,0.5)`, margin-top 2px
   - Song fragment (lyric snippet): Inter 400 italic 12px `rgba(255,255,255,0.35)`, margin-top 2px, overflow ellipsis, max 1 line
3. Select button:
   - Pill, height 32px, padding 0 14px
   - Background: `rgba(124,58,237,0.4)`
   - Border: 1px solid `rgba(167,139,250,0.4)`
   - Border-radius: 16px
   - Text: "ВЫБРАТЬ" — Inter 700 11px, letter-spacing 0.06em, `#A78BFA`
   - Hover: background `rgba(124,58,237,0.7)`, text white

Show 6 example cards in the 2-column grid.

Example track data to illustrate:
- "Ночь" / Сплин / "Я ждал тебя..."
- "Shallow" / Lady Gaga / "Tell me something..."
- "Нет" / Земфира / "Мне не нравится..."
- "Bohemian Rhapsody" / Queen / "Is this the real..."
- "Du Hast" / Rammstein / "Du. Du hast. Du..."
- "Мы не ангелы" / Ёлка / "Мы с тобой не..."

---

#### Active Tab: SEARCH (second state — show as second artboard or annotation)

Search Input Field:
- Width: 100%, height: 56px
- Background: `rgba(255,255,255,0.07)`
- Border: 1.5px solid `rgba(255,255,255,0.15)`
- Border-radius: 16px
- Left icon: `SearchIcon` 20px, `rgba(255,255,255,0.35)`
- Placeholder: "Исполнитель, название, текст..." — Inter 400 16px, `rgba(255,255,255,0.3)`
- Focused: border `#7C3AED`, left icon `#A78BFA`
- Padding: 0 20px 0 52px

Below search bar when empty: same 2-column recommendation cards grid with label "ПОПУЛЯРНОЕ СЕГОДНЯ"

---

## VISUAL NOTES

- Left panel feels like a "stage side" showing who is performing
- Right panel feels like a "menu" with options
- The currently singing avatar should feel prominent and almost glowing
- Use neon cyan `#06B6D4` as occasional accent for interactive states (borders, icons)
- The overall screen should feel like you could build a session here for 30+ minutes without eye fatigue

## PROMPT END
