# FigGPT Prompt — Screen 01: Landing / Start Screen

## PROMPT START

Design a **karaoke club app landing screen** for tablet/monitor (1920x1080px frame). Dark mode. Glassmorphism aesthetic. The mood is atmospheric, cinematic, and inviting — like walking into an upscale karaoke lounge at night.

---

## BACKGROUND

Full-screen deep space gradient background:
- Top-left: `#0D0B2B` (near-black indigo)
- Center: `#1A1060` (deep violet-blue)
- Bottom-right: `#0A1628` (deep navy)
- Add 2-3 large radial glow blobs: one magenta-violet `#7C2FD5` at ~30% opacity (top-right area), one electric blue `#2563EB` at ~25% opacity (bottom-left), one soft cyan `#06B6D4` at ~15% opacity (center-bottom)
- Scatter ~60 tiny star dots (1-2px circles, white at 40-70% opacity) randomly across the background for a cosmic/club atmosphere
- Very subtle noise texture overlay at 3-5% opacity

---

## LAYOUT STRUCTURE

Vertically centered, single column, centered horizontally. Total content block ~600px wide.

```
[BACKGROUND with gradient + stars]
          |
    [LOGO BLOCK]         ← top ~35% of screen
          |
   [HEADLINE TEXT]       ← center
          |
  [SUBTITLE TEXT]        ← center
          |
 [START BUTTON]          ← prominent CTA
          |
[FEATURE PILLS ROW]      ← bottom ~25% of screen
          |
[ADMIN LINK]             ← bottom edge, subtle
```

---

## COMPONENTS

### Logo Block
- Custom icon: microphone with musical note or sound wave, rendered as a flat gradient icon
- Icon size: 72x72px
- Icon gradient fill: left `#A855F7` (purple) to right `#06B6D4` (cyan)
- Optional: soft glow halo behind icon, `#7C2FD5` at 40% opacity, blur radius 40px
- Below icon: app name text "KARAOKE" in all caps
  - Font: Inter or Plus Jakarta Sans, weight 800 (ExtraBold)
  - Letter spacing: 0.15em
  - Size: 48px
  - Fill: linear gradient left-to-right `#E0C3FC` → `#8EC5FC`
  - Spacing between icon and text: 16px

### Headline Text
- Text: "Пойте вместе сегодня"
- Font: Inter, weight 700 (Bold)
- Size: 56px on desktop / 40px on tablet
- Color: white `#FFFFFF`
- Letter spacing: -0.02em
- Line height: 1.1
- Text-shadow: 0px 0px 40px rgba(168, 85, 247, 0.5)
- Margin top from logo block: 32px

### Subtitle Text
- Text: "Выбирайте песни, вставайте в очередь и пусть ночь начнётся."
- Font: Inter, weight 400 (Regular)
- Size: 20px
- Color: `rgba(255,255,255,0.65)`
- Letter spacing: 0.01em
- Line height: 1.6
- Max width: 480px, center-aligned
- Margin top from headline: 16px

### Primary CTA Button — "Начать сессию"
- MUI component reference: `<Button variant="contained" size="large">`
- Width: 280px, height: 64px
- Border radius: 32px (fully rounded pill shape)
- Background: linear gradient 135deg `#7C3AED` (violet) → `#2563EB` (blue)
- Text: "НАЧАТЬ СЕССИЮ" — Inter, weight 700, size 18px, letter spacing 0.08em, white
- Box shadow: `0 0 30px rgba(124, 58, 237, 0.6), 0 8px 32px rgba(37, 99, 235, 0.4)`
- Inner glow: inset 0 1px 0 rgba(255,255,255,0.2)
- Hover state indication: slightly brighter gradient + larger glow (annotate as hover)
- Margin top from subtitle: 40px
- Icon inside button (left): Play triangle icon, white, 20px

### Feature Pills Row
- 3 horizontal pill chips, evenly spaced in a row
- Each pill: glassmorphism card
  - Background: `rgba(255,255,255,0.06)`
  - Border: 1px solid `rgba(255,255,255,0.12)`
  - Border radius: 24px
  - Backdrop filter: blur(12px)
  - Padding: 12px 24px
  - Height: 48px
- Pill 1: Icon (MUI `QueueMusicIcon`) + text "Очередь песен"
- Pill 2: Icon (MUI `CloudUploadIcon`) + text "Загрузите свой трек"
- Pill 3: Icon (MUI `AutoAwesomeIcon`) + text "ИИ-рекомендации"
- Icon size: 18px, color: `#A78BFA` (light purple)
- Text: Inter, weight 500, size 14px, color `rgba(255,255,255,0.75)`
- Gap between icon and text: 8px
- Row margin top from button: 48px

### Admin Access Link
- Position: fixed bottom-right corner, 24px from edges
- Text: "Админ"
- Font: Inter, weight 400, size 12px
- Color: `rgba(255,255,255,0.3)`
- No underline, small lock icon (14px) preceding text
- On hover: color changes to `rgba(255,255,255,0.6)`

---

## VISUAL EFFECTS NOTES

- The overall feel should be "premium dark club" — deep blacks, glowing purples/blues/cyans
- All text should feel like it's emitting a subtle glow against the dark background
- No harsh edges anywhere — everything uses rounded corners (minimum 8px)
- The glassmorphism on pills should look like frosted club glass
- Refer to Dribbble "Neon Beats Music Player" and "AI Music App Glassmorphism" aesthetic for mood

---

## TYPOGRAPHY SYSTEM (applies to all screens)

- Primary font: **Inter** (Google Fonts)
- H1: 56px / weight 700 / tracking -0.02em
- H2: 40px / weight 700 / tracking -0.01em
- H3: 28px / weight 600
- Body: 16px / weight 400 / line-height 1.6
- Small/Caption: 12-14px / weight 400-500
- All-caps labels: 11-13px / weight 600 / tracking 0.1em

## PROMPT END
