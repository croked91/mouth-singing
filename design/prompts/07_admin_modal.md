# FigGPT Prompt — Screen 07: Admin Modal (PIN Protection)

## PROMPT START

Design an **admin access modal** for the karaoke app, displayed as an overlay on top of any screen (use the main queue screen as the base layer). Tablet/monitor (1920x1080px). Dark glassmorphism. This modal provides PIN-protected admin controls. The tone is functional and slightly serious — access control for the venue staff.

---

## BASE LAYER (Background, blurred)

Use the main queue screen (Screen 03) as the visual base, heavily blurred:
- Entire background: blur(24px) applied to whatever screen was active
- Overlay dimming layer on top of blurred background: `rgba(5,5,8,0.7)`
- This creates a deep "focus trap" effect — everything behind is visible but inaccessible

---

## MODAL STRUCTURE

Centered modal card:
- Width: 460px, height: auto
- Position: absolute center (50% 50% transform -50% -50%)
- Background: `rgba(20,15,45,0.92)`
- Border: 1px solid `rgba(255,255,255,0.12)`
- Border-radius: 28px
- Backdrop-filter: blur(32px)
- Box-shadow: `0 32px 128px rgba(0,0,0,0.8), 0 0 0 1px rgba(255,255,255,0.05) inset, 0 0 80px rgba(124,58,237,0.1)`
- Padding: 48px 40px

---

## MODAL CONTENTS

### Header Section
- Centered alignment for all elements in this section

Lock icon:
- Container: 64px circle
- Background: `rgba(124,58,237,0.15)`
- Border: 1px solid `rgba(167,139,250,0.3)`
- Icon: `LockIcon` (MUI) 28px, `#A78BFA`
- Optional subtle glow behind container: `#7C3AED` at 20% opacity, blur 32px

Title text below icon:
- "Доступ администратора" — Inter 700 26px, white, margin-top 16px
- Letter-spacing: -0.01em

Subtitle:
- "Введите PIN для продолжения" — Inter 400 15px, `rgba(255,255,255,0.45)`, margin-top 8px

---

### PIN Input Display
- Margin-top: 32px
- Centered, horizontal flex row of 4 PIN dot indicators, gap: 16px

Each PIN digit indicator:
- 52x52px circle
- Empty state: background `rgba(255,255,255,0.06)`, border 2px solid `rgba(255,255,255,0.15)`
- Filled state: background gradient `#7C3AED` → `#2563EB`, border 2px solid `#A78BFA`, box-shadow `0 0 16px rgba(124,58,237,0.6)`
- Active (current) state: border 2px solid `#A78BFA` with pulsing glow animation annotation

Show in design: 2 filled dots + 1 active (being input) + 1 empty — represents PIN entry in progress.

Error state (show as annotation or separate state):
- All 4 dots: background `rgba(239,68,68,0.2)`, border `rgba(239,68,68,0.6)`, box-shadow `0 0 16px rgba(239,68,68,0.3)`
- Shake animation annotation: horizontal shake 3 times, 0.4s
- Error message appears below: "Неверный PIN" — Inter 500 13px, `#F87171`, margin-top 12px

---

### Numpad / Virtual Keyboard
- Margin-top: 28px
- 3x4 grid, gap: 10px
- Total width: 300px, centered

Key layout:
```
[1] [2] [3]
[4] [5] [6]
[7] [8] [9]
[←] [0] [✓]
```

Regular digit keys (1-9, 0):
- Size: 80x60px
- Background: `rgba(255,255,255,0.07)`
- Border: 1px solid `rgba(255,255,255,0.1)`
- Border-radius: 14px
- Text: Inter 700 24px, white
- Hover state: background `rgba(255,255,255,0.14)`, border `rgba(255,255,255,0.2)`
- Active/pressed: background `rgba(124,58,237,0.3)`, border `rgba(167,139,250,0.5)`, transform scale 0.96

Backspace key (←):
- Same size as regular key
- Background: `rgba(255,255,255,0.04)`
- Border: 1px solid `rgba(255,255,255,0.07)`
- Icon: `BackspaceIcon` (MUI) 22px, `rgba(255,255,255,0.4)`
- Hover: icon `rgba(255,255,255,0.7)`

Confirm key (✓):
- Same size, BUT background: `rgba(16,185,129,0.15)`, border: 1px solid `rgba(16,185,129,0.35)`
- Icon: `CheckIcon` (MUI) 22px, `#10B981`
- Only active when all 4 digits entered
- Disabled when <4 digits: background `rgba(255,255,255,0.03)`, icon `rgba(255,255,255,0.15)`

---

### Action Buttons (shown after correct PIN)
- Show as a separate state: overlay the numpad area with the admin action buttons
- Margin-top from PIN display: 24px

Section label: "УПРАВЛЕНИЕ" — Inter 600 11px, letter-spacing 0.12em, `rgba(255,255,255,0.3)`, margin-bottom 16px

**End Session Button:**
- Width: 100%, height: 56px
- Border-radius: 16px
- Background: `rgba(239,68,68,0.12)`
- Border: 1px solid `rgba(239,68,68,0.35)`
- Backdrop-filter: blur(8px)
- Text: "ЗАВЕРШИТЬ СЕССИЮ" — Inter 700 15px, letter-spacing 0.06em, `#F87171`
- Left icon: `PowerSettingsNewIcon` (MUI) 20px, `#F87171`
- Hover: background `rgba(239,68,68,0.25)`, border `rgba(239,68,68,0.6)`, glow `0 0 24px rgba(239,68,68,0.2)`

Gap: 10px

**Cancel/Close Button:**
- Width: 100%, height: 52px
- Border-radius: 16px
- Background: `rgba(255,255,255,0.05)`
- Border: 1px solid `rgba(255,255,255,0.08)`
- Text: "Отмена" — Inter 500 15px, `rgba(255,255,255,0.45)`
- Hover: text `rgba(255,255,255,0.7)`, background `rgba(255,255,255,0.09)`

---

### Confirmation Dialog (nested)
Show as a 3rd state: after pressing "ЗАВЕРШИТЬ СЕССИЮ", a confirmation prompt replaces the action buttons:

Confirmation card (inline, replaces button area):
- Background: `rgba(239,68,68,0.08)`
- Border: 1px solid `rgba(239,68,68,0.25)`
- Border-radius: 16px
- Padding: 20px

Warning icon: `WarningAmberIcon` (MUI) 32px, `#FBBF24` (amber), centered
Confirmation text: "Завершить сессию?" — Inter 700 18px, white, center, margin-top 12px
Sub-text: "Очередь будет очищена, приложение вернётся на стартовый экран. Это действие необратимо." — Inter 400 13px, `rgba(255,255,255,0.45)`, center, margin-top 6px

Two-button row (side by side), gap: 10px, margin-top 20px:

Left — "Отмена" button:
- Width: 50%, height: 48px
- Border-radius: 12px
- Background: `rgba(255,255,255,0.06)`
- Border: 1px solid `rgba(255,255,255,0.1)`
- Text: "Отмена" — Inter 600 14px, `rgba(255,255,255,0.6)`

Right — "Да, завершить" button:
- Width: 50%, height: 48px
- Border-radius: 12px
- Background: `#DC2626` (solid red — most decisive action)
- Box-shadow: `0 0 20px rgba(220,38,38,0.4)`
- Text: "Да, завершить" — Inter 700 14px, white
- Hover: `#EF4444`, larger shadow

---

### Close Button (modal dismiss)
- Top-right corner of modal card: 12px from edges
- Size: 36px circle
- Background: `rgba(255,255,255,0.06)`
- Border: 1px solid `rgba(255,255,255,0.08)`
- Icon: `CloseIcon` (MUI) 16px, `rgba(255,255,255,0.4)`
- Hover: icon `rgba(255,255,255,0.8)`, background `rgba(255,255,255,0.12)`

---

## ARTBOARD STATES SUMMARY

1. **State A: PIN Entry (in progress)** — 2 dots filled, 1 active, 1 empty. Numpad visible.
2. **State B: Wrong PIN** — 4 red dots, shake annotation, error message, numpad visible.
3. **State C: Correct PIN (Admin Unlocked)** — PIN dots all green checkmark, admin action buttons replace numpad.
4. **State D: End Session Confirmation** — warning card replaces action buttons.

Design all 4 states. Show State A as primary, others as additional artboards or annotation layers.

---

## VISUAL NOTES

- This modal should feel secure but not aggressive — it's a venue staff tool, not a security warning
- The PIN dots are the most delightful interaction — make them feel satisfying to tap
- The red "End Session" actions should clearly signal destructive intent without being alarming
- The backdrop blur ensures context awareness — staff can see what's happening behind the modal
- The numpad keys should be large enough for quick finger taps (80px wide on 1920px screen)

## PROMPT END
