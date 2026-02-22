# FigGPT Prompt — Screen 05: Upload Track Screen

## PROMPT START

Design a **track upload screen** for the karaoke app, tablet/monitor (1920x1080px). Dark glassmorphism aesthetic. This screen allows users to drag & drop their own MP3 files to create a karaoke track. The mood is creative and empowering — "your song, your rules."

---

## BACKGROUND

Same cosmic background:
- Deep gradient: `#0D0B2B` → `#1A1060` → `#0A1628`
- Radial glows: violet `#7C2FD5` at 25% top-right, cyan `#06B6D4` at 20% bottom-left (emphasizes the upload/cloud theme)
- Star dots (~50, white, 1-2px, 25-55% opacity)

---

## LAYOUT STRUCTURE

Full screen. Top bar (same app nav) + centered main content card.

```
[TOP BAR: Назад | "Загрузка трека" title | Singer indicator]
              |
[MAIN CONTENT CARD — 900px wide, auto height, centered]
   |
   ├── [DRAG & DROP ZONE — large, prominent]
   |
   ├── [DIVIDER: "— ИЛИ —"]
   |
   ├── [ВЫБРАТЬ ФАЙЛ BUTTON]
   |
   ├── [OPTIONAL FIELDS: Исполнитель + Название]
   |
   └── [PRIMARY ACTION BUTTON: "Загрузить и создать караоке"]
```

---

## COMPONENTS

### Top Navigation Bar
- Height: 72px, full width
- Background: `rgba(13,11,43,0.9)`, backdrop-filter: blur(20px)
- Border-bottom: 1px solid `rgba(255,255,255,0.08)`
- Left: back arrow button (same style as search screen) + "KARAOKE" logo
- Center: "Загрузка трека" — Inter 600 18px, white, with `CloudUploadIcon` 20px to the left, `rgba(255,255,255,0.6)`
- Right: singer indicator pill (same style as search screen, showing current user)

---

### Main Content Card
- Width: 900px, centered
- Margin top from nav: 48px
- Padding: 48px
- Background: `rgba(255,255,255,0.04)`
- Border: 1px solid `rgba(255,255,255,0.1)`
- Border-radius: 24px
- Backdrop-filter: blur(24px)
- Box-shadow: `0 8px 64px rgba(6,182,212,0.1), 0 2px 0 rgba(255,255,255,0.05) inset`

---

### Drag & Drop Zone (DEFAULT STATE)
- Width: 100% (fills card), height: 240px
- Background: `rgba(6,182,212,0.04)`
- Border: 2px dashed `rgba(6,182,212,0.4)`
- Border-radius: 20px
- Backdrop-filter: blur(8px)
- Layout: vertically and horizontally centered flex column

Contents:
- Cloud upload icon (large): 64px, gradient fill `#06B6D4` → `#7C3AED`, use MUI `CloudUploadIcon` or custom vector
- Optional: subtle glow behind icon, `#06B6D4` at 30% opacity, blur 48px
- Primary text: "Перетащите MP3 сюда" — Inter 700 24px, white
- Secondary text: "MP3, WAV, M4A — до 50 МБ" — Inter 400 14px, `rgba(255,255,255,0.4)`
- Gap between icon and primary text: 16px
- Gap between primary and secondary text: 8px

#### Drag & Drop Zone — HOVER/DRAG-OVER STATE (show as annotation or second state):
- Background: `rgba(6,182,212,0.1)`
- Border: 2px dashed `#06B6D4` (brighter, solid)
- Box-shadow: `0 0 48px rgba(6,182,212,0.25)`
- Icon scales up slightly (annotation: transform scale 1.1)
- Primary text: "Отпустите здесь!" — same style but color changes to `#06B6D4`

---

### Divider Row
- Horizontal flex row: `<hr>` line + text + `<hr>` line
- Line: 1px solid `rgba(255,255,255,0.1)`, flex: 1
- Text: "ИЛИ" — Inter 500 13px, `rgba(255,255,255,0.3)`, padding: 0 16px
- Margin: 24px 0

---

### Choose File Button (MUI `<Button variant="outlined">`)
- Width: 220px, height: 52px, centered (margin: 0 auto)
- Border-radius: 16px
- Border: 1.5px solid `rgba(6,182,212,0.5)`
- Background: `rgba(6,182,212,0.08)`
- Backdrop-filter: blur(8px)
- Text: "ВЫБРАТЬ ФАЙЛ" — Inter 700 14px, letter-spacing 0.06em, `#06B6D4`
- Left icon: `FolderOpenIcon` (MUI) 18px, same cyan color
- Hover: border `#06B6D4`, background `rgba(6,182,212,0.15)`, box-shadow `0 0 20px rgba(6,182,212,0.3)`

---

### Divider Line
- 1px solid `rgba(255,255,255,0.06)`, margin: 32px 0

---

### Optional Metadata Fields Section

Section label: "ДЕТАЛИ ТРЕКА (необязательно)" — Inter 600 11px, letter-spacing 0.12em, `rgba(255,255,255,0.3)`, margin-bottom: 16px

Two-column row, gap: 16px:

Field 1 — Artist Name (MUI `<TextField>`):
- Width: 50% - 8px
- Height: 56px
- Label "Исполнитель" — floating label, Inter 500 14px, `rgba(255,255,255,0.4)` when unfocused, `#A78BFA` when focused
- Background: `rgba(255,255,255,0.06)`
- Border: 1.5px solid `rgba(255,255,255,0.12)`
- Border-radius: 14px
- Focused border: `#7C3AED`, glow `0 0 0 3px rgba(124,58,237,0.2)`
- Placeholder: "напр. Кино"
- Input text: Inter 400 16px, white

Field 2 — Track Title (MUI `<TextField>`):
- Same styling as Artist field
- Label: "Название"
- Placeholder: "напр. Группа крови"

---

### Primary Upload Button (MUI `<Button variant="contained">`)
- Width: 100%, height: 64px
- Margin-top: 24px
- Border-radius: 16px
- Background: linear gradient 135deg `#06B6D4` → `#7C3AED`
- Box-shadow: `0 0 40px rgba(6,182,212,0.4), 0 8px 32px rgba(124,58,237,0.3)`
- Text: "ЗАГРУЗИТЬ И СОЗДАТЬ КАРАОКЕ" — Inter 700 16px, letter-spacing 0.08em, white
- Left icon: `RocketLaunchIcon` (MUI) or `AutoAwesomeIcon` 20px, white
- Disabled state: background `rgba(255,255,255,0.05)`, text `rgba(255,255,255,0.2)`, no glow, no pointer events

---

## UPLOAD PROGRESS STATE (Second Artboard / Modal Overlay)

Show as a full-screen modal overlay or a separate artboard at the same 1920x1080 size.

Modal Overlay:
- Background: `rgba(13,11,43,0.85)`, backdrop-filter: blur(20px)
- Centered card: 520px wide, auto height
  - Background: `rgba(255,255,255,0.06)`
  - Border: 1px solid `rgba(255,255,255,0.12)`
  - Border-radius: 24px
  - Padding: 48px
  - Box-shadow: `0 8px 64px rgba(0,0,0,0.5)`

Processing Card Contents — show 3 sub-states as 3 annotations:

**State A: Uploading (0-50%)**
- Top icon: animated spinner ring, 56px, stroke `#06B6D4`, unfilled arc rotating
- Title: "Загрузка..." — Inter 700 24px, white
- Subtitle: "your-track.mp3 · 8.2 MB" — Inter 400 14px, `rgba(255,255,255,0.45)`
- Progress bar:
  - Width: 100%, height: 8px, border-radius: 4px
  - Background track: `rgba(255,255,255,0.08)`
  - Fill: gradient `#06B6D4` → `#7C3AED`, width at ~45% for illustration
  - Box-shadow on fill: `0 0 12px rgba(6,182,212,0.6)`
- Percentage text below: "45%" — Inter 600 16px, `#06B6D4`

**State B: Processing (50-100%)**
- Icon: sound wave / waveform icon 56px, pulsing cyan
- Title: "Создаём караоке..." — Inter 700 24px, white
- Subtitle: "ИИ отделяет вокал и синхронизирует текст" — Inter 400 14px, `rgba(255,255,255,0.45)`
- Progress bar: same style, width ~75%
- Percentage: "75%" — Inter 600 16px, `#A78BFA`
- Small note below: "Обычно это занимает 15-30 секунд" — Inter 400 12px, `rgba(255,255,255,0.3)`

**State C: Success (100%)**
- Icon: checkmark circle 56px, filled gradient `#10B981` → `#06B6D4`
- Optional: particle burst effect annotation around checkmark
- Title: "Готово к исполнению!" — Inter 700 24px, white
- Track info: "Hey Jude · The Beatles" — Inter 500 16px, `rgba(255,255,255,0.6)`
- Duration auto-detected: "7:08" — Inter 400 14px, `rgba(255,255,255,0.4)`
- CTA Button: "ПОЕХАЛИ" — same primary button style, gradient `#10B981`→`#06B6D4`, width 100% height 56px, icon `PlayArrowIcon`

---

## VISUAL NOTES

- The drag & drop zone is the hero element — it should feel spacious and inviting
- Cyan `#06B6D4` dominates this screen as the primary accent (cloud/upload theme)
- Progress states use animated elements — annotate all animations clearly for handoff
- The optional fields have a subtle dotted separator to signal "you don't need these"
- Overall card width 900px gives comfortable touch targets on large monitors

## PROMPT END
