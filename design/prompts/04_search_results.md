# FigGPT Prompt — Screen 04: Search Results Screen

## PROMPT START

Design a **track search results screen** for the karaoke app, tablet/monitor (1920x1080px). This screen appears when a user types in the search bar from the main queue screen. Dark glassmorphism aesthetic. The mood is focused and quick — users need to find and select their track fast.

---

## BACKGROUND

Same cosmic background:
- Deep gradient: `#0D0B2B` top → `#1A1060` center → `#0A1628` bottom-right
- Muted radial glows (less intense than main screen — user is in focus mode):
  - Violet `#6D28D9` at 20% opacity, top-right area
  - Blue `#1D4ED8` at 15% opacity, bottom-left
- Star dots (~40, subtle, white 20-40% opacity)

---

## LAYOUT STRUCTURE

```
┌─────────────────────────────────────────────────────────────────────┐
│  TOP BAR  [Назад]  [ПОЛЕ ПОИСКА — full width, active]  72px        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  РЕЗУЛЬТАТЫ             КОЛИЧЕСТВО                                  │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  TRACK CARD 1                                               │    │
│  ├─────────────────────────────────────────────────────────────┤    │
│  │  TRACK CARD 2                                               │    │
│  ├─────────────────────────────────────────────────────────────┤    │
│  │  TRACK CARD 3                                               │    │
│  ├─────────────────────────────────────────────────────────────┤    │
│  │  ... (scrollable list, up to ~8 visible without scroll)     │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## COMPONENTS

### Top Navigation Bar
- Height: 72px, full width
- Background: `rgba(13,11,43,0.9)`, backdrop-filter: blur(20px)
- Border-bottom: 1px solid `rgba(255,255,255,0.08)`
- Layout: horizontal flex, align center, padding: 0 32px, gap: 16px

#### Back Button
- Circle button: 44px diameter
- Background: `rgba(255,255,255,0.06)`
- Border: 1px solid `rgba(255,255,255,0.1)`
- Border-radius: 22px
- Icon: `ArrowBackIcon` (MUI), 20px, `rgba(255,255,255,0.7)`
- Hover: background `rgba(255,255,255,0.12)`, icon white

#### Search Field (Active State — MUI `<TextField>` styled)
- Flex: 1, max-width: 800px, height: 48px
- Background: `rgba(255,255,255,0.08)`
- Border: 1.5px solid `rgba(124,58,237,0.6)` (active/focused state)
- Border-radius: 24px
- Box-shadow: `0 0 0 3px rgba(124,58,237,0.2)` (focus ring)
- Left icon: `SearchIcon` 20px, `#A78BFA` (active color)
- Right icon: `CloseIcon` 18px, `rgba(255,255,255,0.3)` (clear button)
- Input text: Inter 500 16px, white
- Current search query shown: e.g., "shallow" — text `#FFFFFF`
- Padding: 0 48px

#### Current Singer Indicator (right of search field)
- Small pill: height 36px, padding 0 14px
- Background: `rgba(124,58,237,0.2)`
- Border: 1px solid `rgba(167,139,250,0.3)`
- Border-radius: 18px
- Small avatar: 24px circle, gradient, initial letter Inter 700 11px
- Text: "Вася" — Inter 500 13px, `rgba(255,255,255,0.7)`
- Gap between avatar and text: 6px

---

### Results Header Row
- Full width content area, padding: 24px 40px 16px 40px
- Left: label "РЕЗУЛЬТАТЫ" — Inter 600 11px, letter-spacing 0.12em, `rgba(255,255,255,0.35)`
- Right: results count "12 треков найдено" — Inter 400 13px, `rgba(255,255,255,0.4)`

---

### Track Result Cards List
- Padding: 0 40px
- Gap between cards: 8px
- Max visible without scroll: ~8 cards at 80px each

#### Individual Track Card (MUI `<Card>` styled)
- Width: 100%, height: 80px
- Background: `rgba(255,255,255,0.05)`
- Border: 1px solid `rgba(255,255,255,0.09)`
- Border-radius: 16px
- Backdrop-filter: blur(8px)
- Padding: 0 20px
- Layout: horizontal flex, align-items center, gap: 16px

Hover state annotation:
- Background: `rgba(124,58,237,0.12)`
- Border: 1px solid `rgba(167,139,250,0.35)`
- Left border accent: 3px solid gradient `#7C3AED`→`#06B6D4` (left side only, inset)

Card element (left to right):

1. **Track Number / Index** (optional, 28px wide):
   - Text: "01" — Inter 500 13px, `rgba(255,255,255,0.2)`

2. **Album Art Placeholder** (48x48px):
   - Border-radius: 10px
   - Gradient background (unique per track): 4 preset gradients cycling
   - Centered music note icon: `rgba(255,255,255,0.4)` 18px MUI `MusicNoteIcon`
   - OR show abstract geometric pattern as placeholder

3. **Track Info Block** (flex: 1):
   - Track title: Inter 600 16px, white, overflow ellipsis 1 line
   - Artist name: Inter 400 13px, `rgba(255,255,255,0.5)`, margin-top: 2px, overflow ellipsis
   - Lyric snippet below: Inter 400 italic 12px, `rgba(255,255,255,0.3)`, margin-top: 2px, 1 line overflow ellipsis

4. **Duration** (64px wide):
   - Text: "3:42" — Inter 500 13px, `rgba(255,255,255,0.35)`, right-aligned

5. **Choose Button** (MUI `<Button variant="contained">`):
   - Width: 100px, height: 40px
   - Border-radius: 20px
   - Background: linear gradient 135deg `#7C3AED` → `#2563EB`
   - Text: "ВЫБРАТЬ" — Inter 700 12px, letter-spacing 0.05em, white
   - Box-shadow: `0 0 16px rgba(124,58,237,0.4)`
   - Hover: larger glow, slightly lighter gradient

Show 8 example track cards with varied data:

| # | Title | Artist | Snippet | Duration |
|---|-------|--------|---------|----------|
| 01 | Shallow | Lady Gaga, Bradley Cooper | "Tell me something, boy..." | 3:36 |
| 02 | Нет | Земфира | "Мне не нравится..." | 4:12 |
| 03 | Shallow (Acoustic) | Lady Gaga | "In the sha-ha-sha-llow..." | 3:20 |
| 04 | Все идет по плану | Гражданская Оборона | "Все идет по плану..." | 3:55 |
| 05 | Du Hast | Rammstein | "Du. Du hast. Du hast mich..." | 3:52 |
| 06 | Bohemian Rhapsody | Queen | "Is this the real life?..." | 5:55 |
| 07 | Ночь | Сплин | "Я ждал тебя..." | 3:44 |
| 08 | Sweet Home Alabama | Lynyrd Skynyrd | "Big wheels keep on turning..." | 4:44 |

---

### Empty / No Results State
- Show as separate annotation layer or second artboard
- Centered in the content area below search bar
- Icon: `SearchOffIcon` (MUI) 64px, `rgba(255,255,255,0.15)`
- Primary text: "Ничего не найдено" — Inter 600 22px, `rgba(255,255,255,0.5)`
- Sub-text: "Попробуйте другой запрос или загрузите свой трек" — Inter 400 14px, `rgba(255,255,255,0.3)`
- Below sub-text: link-button "Загрузить свой трек →" — Inter 600 14px, `#A78BFA`, no background

---

### Loading State
- Show skeleton cards: 8 cards, same size as result cards
- Skeleton shimmer: `rgba(255,255,255,0.06)` base, shimmer `rgba(255,255,255,0.1)` moving left to right
- Animated with arrow annotation: "shimmer animation, 1.5s infinite"

---

## VISUAL NOTES

- This is a utility/functional screen, but should still feel premium
- Cards should be fast to scan — track title and artist are the visual hierarchy peaks
- The "ВЫБРАТЬ" button should feel like a confident action — it glows
- Vertical scroll inside the card list (after 8 items), not the whole page
- Search field stays sticky at the top at all times

## PROMPT END
