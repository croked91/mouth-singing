# FigGPT Prompt — Screen 02: Session Setup / Add Participants

## PROMPT START

Design a **karaoke session setup screen** for adding participants, for tablet/monitor (1920x1080px frame). Dark glassmorphism aesthetic. The mood is social and playful — friends gathering before the fun begins.

---

## BACKGROUND

Same cosmic background as the landing screen:
- Deep space gradient: `#0D0B2B` top-left → `#1A1060` center → `#0A1628` bottom-right
- Radial glows: magenta-violet `#7C2FD5` at 25% opacity (top-right), electric blue `#2563EB` at 20% opacity (bottom-left)
- Star dots scattered (~50 dots, 1-2px, white 30-60% opacity)
- Subtle noise texture overlay at 3% opacity

---

## LAYOUT STRUCTURE

Full screen. Top navigation bar + centered main content card.

```
[TOP BAR: Logo left | Screen title center | (empty) right]
              |
[MAIN CONTENT CARD — glassmorphism, centered, ~780px wide, ~auto height]
   |
   ├── [SECTION TITLE: "Who's Singing Tonight?"]
   |
   ├── [PARTICIPANTS LIST — existing participants as avatar chips]
   |
   ├── [INPUT ROW — text field + nickname generator button]
   |
   ├── [ADD PARTICIPANT BUTTON]
   |
   └── [START KARAOKE BUTTON — primary, appears when ≥1 participant]
```

---

## COMPONENTS

### Top Navigation Bar
- Position: top, full width, height 72px
- Background: `rgba(13,11,43,0.8)`, backdrop-filter: blur(20px)
- Border-bottom: 1px solid `rgba(255,255,255,0.08)`
- Left: app logo (microphone icon 32px + "KARAOKE" text, Inter 700 18px, gradient fill `#E0C3FC`→`#8EC5FC`)
- Center: screen label "Новая сессия" — Inter 600, 16px, `rgba(255,255,255,0.6)`, letter-spacing 0.05em, uppercase

### Main Content Card (Glassmorphism)
- Width: 780px, centered horizontally
- Top margin from nav bar: 60px
- Padding: 48px
- Background: `rgba(255,255,255,0.05)`
- Border: 1px solid `rgba(255,255,255,0.12)`
- Border-radius: 24px
- Backdrop-filter: blur(24px)
- Box-shadow: `0 8px 64px rgba(124,58,237,0.15), 0 2px 0 rgba(255,255,255,0.06) inset`

### Section Title
- Text: "Кто поёт сегодня?"
- Font: Inter, weight 700, size 32px
- Color: white `#FFFFFF`
- Text-shadow: `0 0 30px rgba(168,85,247,0.4)`
- Subtitle below: "Добавьте всех, кто будет петь"
- Subtitle font: Inter, weight 400, size 16px, color `rgba(255,255,255,0.5)`
- Spacing: 8px between title and subtitle

### Participants List Area
- Minimum height: 100px (empty state shown when 0 participants)
- Layout: horizontal flex-wrap row, gap 12px
- Margin top from section title: 32px

#### Empty State (0 participants):
- Centered placeholder text: "Участников пока нет — добавьте первого певца"
- Font: Inter, weight 400, size 14px, color `rgba(255,255,255,0.3)`
- Dashed border container: 1px dashed `rgba(255,255,255,0.12)`, border-radius 12px, padding 24px 16px

#### Participant Chip (filled state, show 2-3 examples):
- MUI component: `<Chip>` custom styled
- Height: 52px, padding: 8px 16px
- Background: `rgba(124,58,237,0.25)`
- Border: 1px solid `rgba(167,139,250,0.4)`
- Border-radius: 26px
- Backdrop-filter: blur(8px)
- Contents (left to right):
  1. Avatar circle: 36px diameter, gradient background (unique per user — cycle through: `#7C3AED`→`#EC4899`, `#2563EB`→`#06B6D4`, `#059669`→`#10B981`), white initial letter, Inter 700 16px
  2. Nickname text: Inter, weight 600, size 15px, white
  3. Remove button (×): 20px circle, `rgba(255,255,255,0.1)`, white × icon 10px, hover state: `rgba(239,68,68,0.3)` background
- Gap between avatar and text: 10px, between text and ×: 8px
- Example chips to show: "Вася" (purple gradient), "Маша" (blue-cyan gradient), "Капитан Солнце" (green gradient)

### Input Row
- Margin top from participants list: 24px
- Layout: horizontal flex row, gap 12px

#### Text Input Field (MUI `<TextField>`)
- Flex: 1 (takes remaining width)
- Height: 56px
- Background: `rgba(255,255,255,0.06)`
- Border: 1.5px solid `rgba(255,255,255,0.15)`
- Border-radius: 16px
- Focused border: 1.5px solid `#7C3AED`
- Focused box-shadow: `0 0 0 3px rgba(124,58,237,0.25)`
- Placeholder text: "Введите никнейм..." — color `rgba(255,255,255,0.3)`, Inter 400 16px
- Input text: Inter 500, 16px, white
- Padding: 0 20px

#### Generate Nickname Button (MUI `<Button variant="outlined">`)
- Width: 200px, height: 56px
- Border-radius: 16px
- Border: 1.5px solid `rgba(167,139,250,0.5)`
- Background: `rgba(124,58,237,0.15)`
- Backdrop-filter: blur(8px)
- Text: "Сгенерировать" — Inter, weight 600, size 14px, color `#A78BFA`
- Left icon: MUI `AutoFixHighIcon` or dice/sparkle icon, 18px, `#A78BFA`
- Hover state: border color `#A78BFA`, background `rgba(124,58,237,0.25)`
- Active/loading state: spinner icon replaces sparkle icon

### Add Participant Button (MUI `<Button variant="contained">`)
- Width: 100%, height: 56px
- Margin top from input row: 16px
- Border-radius: 16px
- Background: `rgba(255,255,255,0.07)`
- Border: 1px solid `rgba(255,255,255,0.12)`
- Backdrop-filter: blur(8px)
- Text: "+ ДОБАВИТЬ" — Inter, weight 600, size 14px, letter-spacing 0.05em, color `rgba(255,255,255,0.7)`
- Hover: background `rgba(255,255,255,0.12)`, text color white

### Start Karaoke Button (Primary CTA — shown when ≥1 participant)
- Width: 100%, height: 64px
- Margin top from add button: 12px
- Border-radius: 16px
- Background: linear gradient 135deg `#7C3AED` → `#2563EB`
- Box-shadow: `0 0 40px rgba(124,58,237,0.5), 0 8px 32px rgba(37,99,235,0.3)`
- Text: "ПОЕХАЛИ!" — Inter, weight 700, size 18px, letter-spacing 0.06em, white
- Left icon: MUI `PlayArrowIcon` filled, 22px, white
- Show as disabled/greyed version in the design to illustrate 0-participant state:
  - Disabled: background `rgba(255,255,255,0.05)`, text `rgba(255,255,255,0.25)`, no glow

---

## ADDITIONAL DESIGN NOTES

- Show the filled state (2-3 chips already added) as the primary illustration
- Show a second artboard or annotation for empty state
- The card should feel warm and social, not clinical
- Avatar initials use uppercase first letter, font weight 800
- Nickname generation should feel like a "magic" interaction — the sparkle icon is key

## PROMPT END
