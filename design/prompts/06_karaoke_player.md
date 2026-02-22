# FigGPT Prompt — Screen 06: Karaoke Player (Full-Screen Playback)

## PROMPT START

Design the **karaoke player full-screen playback screen** — the most immersive view in the app. Tablet/monitor (1920x1080px). This is what everyone in the room sees while someone is singing. Maximum impact, cinema-quality visuals. Near-black background with neon lyric highlighting is the core experience.

---

## BACKGROUND

DIFFERENT from other screens — this is the "performance stage" mode:
- Base: near-black `#050508`
- Full-screen abstract background: large blurred abstract shapes suggesting atmosphere
  - Two massive radial gradient blobs visible at edges (not center — center is clear for text):
    - Left edge blob: `#4C1D95` (deep violet) at 40% opacity, very large ~800px radius, blurred heavily
    - Right edge blob: `#1E3A5F` (deep navy) at 35% opacity, similar size
  - Very subtle moving particle field annotation: ~30 tiny dots, white 15-25% opacity
- Central area (where lyrics display): kept very dark — black to near-black, maximum contrast for readability
- Top and bottom thirds can have subtle gradient bleed from the side blobs

---

## LAYOUT STRUCTURE (1920x1080)

```
┌─────────────────────────────────────────────────────────────────────┐
│  TRACK INFO BAR (top)                              [ЗАВЕРШИТЬ]  64px│
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│                                                                      │
│            CURRENT LINE (just sung / sung so far)         ~180px    │
│            (dimmed/teal — sung syllables)                           │
│                                                                      │
│  ═══════════════════════════════════════════════════════════════     │
│                                                                      │
│  CURRENT ACTIVE LINE (highlighted, large)                  ~320px   │
│  Word by word: [sung/dim] [ACTIVE SYLLABLE] [unsung/white]          │
│                                                                      │
│  ═══════════════════════════════════════════════════════════════     │
│                                                                      │
│            NEXT LINE (upcoming text)                       ~180px   │
│            (white, slightly dimmed)                                 │
│                                                                      │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│  CONTROLS BAR (bottom)                                         80px │
└─────────────────────────────────────────────────────────────────────┘
```

---

## COMPONENTS

### Top Track Info Bar
- Height: 64px, full width
- Background: linear gradient top `rgba(5,5,8,0.95)` → bottom transparent
- Padding: 0 48px
- Layout: horizontal flex, space-between, align center

Left section:
- Track title: Inter 700 20px, white — e.g., "Shallow"
- Separator: `·` `rgba(255,255,255,0.3)`
- Artist name: Inter 400 18px, `rgba(255,255,255,0.55)` — e.g., "Lady Gaga"
- Small gap, then singer label: "Поёт: Вася" with 24px avatar circle (gradient)

Right section (End button):
- Button: "ЗАВЕРШИТЬ" — pill shape, height 40px, padding 0 20px
- Background: `rgba(239,68,68,0.15)`
- Border: 1px solid `rgba(239,68,68,0.4)`
- Border-radius: 20px
- Icon: `StopIcon` (MUI) 16px, `#F87171`
- Text: "ЗАВЕРШИТЬ" — Inter 700 13px, letter-spacing 0.06em, `#F87171`
- Hover: background `rgba(239,68,68,0.3)`, border `#F87171`

---

### Lyric Display Zone (center content)
- Full remaining height between top bar and controls bar
- Padding: 0 120px (generous side padding)
- Vertical flex column, justify-content: center, gap: 40px

#### Previous / Sung Line (above active line)
- Text: previously sung line or first half of current verse
- Font: Inter, weight 500, size 36px
- Color: `rgba(6,182,212,0.5)` (teal/cyan, faded — "already sang this")
- Blur: filter blur(1px) — slightly defocused
- Letter spacing: 0.02em
- Line height: 1.3
- Text-align: left
- Example text: "Tell me something, boy..."

#### Active Line (current line being sung — THE HERO ELEMENT)
- This is the most important element in the entire app
- Font: Inter, weight 800 (ExtraBold), size 72px
- Text-align: left
- Letter spacing: -0.01em
- Line height: 1.2

Syllable coloring system (show on example line "Are you happy in this modern world?"):
- **Sung syllables** (before current position): color `rgba(255,255,255,0.3)`, no glow
- **Active syllable** (currently being sung): color `#FFD700` (bright gold/yellow) OR `#F0ABFC` (bright pink-purple)
  - Font-weight: 900
  - Text-shadow: `0 0 20px rgba(240,171,252,0.9), 0 0 40px rgba(167,85,247,0.6), 0 0 80px rgba(124,58,237,0.4)` — the neon glow effect
  - Scale: slightly larger (1.05x) than surrounding text (annotation)
- **Unsung syllables** (ahead): color `rgba(255,255,255,0.9)`, subtle text-shadow `0 0 8px rgba(255,255,255,0.15)`

Layout of the active line (example):
```
[Are] [you] [hap-][py] [in] [this] [MOD]-[ern] [world?]
dim   dim   sung  sung dim  dim    ACTIVE glow  white  white
```

Show the full word grouping with clear visual distinction between the three states.

Add a thin progress indicator line below the active text:
- Width: fills proportionally with song progress across the width of the text
- Height: 3px
- Color: gradient `#F0ABFC` → `#7C3AED`
- Glow: `0 0 8px rgba(240,171,252,0.8)`

#### Next Line (upcoming)
- Font: Inter, weight 500, size 36px
- Color: `rgba(255,255,255,0.45)`
- Letter spacing: 0.01em
- Filter: none (sharp, readable)
- Example text: "Or do you need more?"

---

### Bottom Controls Bar
- Height: 80px, full width
- Background: linear gradient bottom `rgba(5,5,8,0.97)` → top transparent (height 160px total, controls in bottom 80px)
- Padding: 0 48px
- Layout: horizontal flex, align center, gap: 24px

#### Progress/Timeline Slider (MUI `<Slider>` styled)
- Flex: 1 (takes most of the width)
- Track height: 4px
- Track background: `rgba(255,255,255,0.12)`
- Fill color: gradient `#7C3AED` → `#06B6D4`
- Filled track glow: `0 0 8px rgba(124,58,237,0.5)`
- Thumb: 16px circle, white, box-shadow `0 0 0 4px rgba(124,58,237,0.4)`
- Thumb hover: 20px, stronger glow
- Time label left: "1:23" — Inter 500 14px, `rgba(255,255,255,0.5)`, min-width 40px
- Time label right: "3:36" — same style

#### Play/Pause Button (MUI `<IconButton>`)
- Size: 56px circle
- Background: `rgba(255,255,255,0.1)`
- Border: 1px solid `rgba(255,255,255,0.2)`
- Icon: `PauseIcon` (currently playing state) 28px, white
- Hover: background `rgba(255,255,255,0.18)`

#### Rewind Button (-15s)
- Size: 44px circle
- Background: `rgba(255,255,255,0.06)`
- Border: 1px solid `rgba(255,255,255,0.1)`
- Icon: `Replay15Icon` (MUI) 22px, `rgba(255,255,255,0.55)`

#### Fast Forward Button (+15s)
- Size: 44px circle
- Same style as rewind
- Icon: `Forward15Icon` (MUI) 22px, `rgba(255,255,255,0.55)`

#### Volume Control
- Speaker icon: `VolumeUpIcon` 22px, `rgba(255,255,255,0.5)`
- Mini horizontal slider: width 100px, same track style but thinner (3px)
- Slider fill: `rgba(255,255,255,0.6)`
- Current level shown at ~75%

---

## LYRIC STATES ILLUSTRATION

Show 3 annotation boxes or small sub-artboards to illustrate syllable states clearly:

Box 1 — "SUNG": `rgba(255,255,255,0.25)` color, no shadow — "these words are done"
Box 2 — "ACTIVE": `#F0ABFC` + neon glow shadow — "this syllable right now"
Box 3 — "UPCOMING": `rgba(255,255,255,0.85)` color, subtle glow — "sing this next"

---

## VISUAL PHILOSOPHY FOR THIS SCREEN

- The lyric zone must have maximum contrast — black background, bright glowing text
- Everything else (controls, info bar) should recede into darkness
- The glowing active syllable is the ONE thing that demands attention
- Think concert big-screen karaoke aesthetic — it IS the show
- Font size 72px ensures readability from 3-4 meters distance (club environment)
- No cards, no glass panels in the center — pure text on black for maximum legibility
- The purple/pink neon glow on active syllable references the reference screenshot's neon aesthetic

## PROMPT END
