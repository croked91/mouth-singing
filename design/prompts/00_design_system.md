# Design System Reference — Karaoke App

This file defines the shared design tokens used across ALL 7 screens.
Reference this in every FigGPT prompt for consistency.

---

## COLOR PALETTE

### Background Colors
| Token | Hex | Usage |
|---|---|---|
| `bg-deep` | `#050508` | Karaoke player screen |
| `bg-primary` | `#0D0B2B` | Main background (all screens) |
| `bg-center` | `#1A1060` | Gradient center |
| `bg-navy` | `#0A1628` | Gradient bottom-right |

### Glow / Radial Blobs
| Token | Hex | Opacity | Usage |
|---|---|---|---|
| `glow-violet` | `#7C2FD5` | 25-35% | Top-right ambient glow |
| `glow-blue` | `#2563EB` | 20-30% | Bottom-left ambient glow |
| `glow-cyan` | `#06B6D4` | 15-20% | Accent glow (upload screens) |
| `glow-magenta` | `#A855F7` | 30-40% | Active session emphasis |

### Primary Accent Colors
| Token | Hex | Usage |
|---|---|---|
| `accent-violet` | `#7C3AED` | Primary CTA, active elements |
| `accent-blue` | `#2563EB` | Gradient partner to violet |
| `accent-cyan` | `#06B6D4` | Upload, secondary actions |
| `accent-purple-light` | `#A78BFA` | Text accents, icons |
| `accent-pink` | `#EC4899` | Avatar gradients, alt accents |

### Lyric Highlight Colors (Player screen)
| Token | Hex | Usage |
|---|---|---|
| `lyric-active` | `#F0ABFC` | Currently sung syllable (neon pink-purple) |
| `lyric-active-alt` | `#FFD700` | Alternative active syllable (gold) |
| `lyric-sung` | `rgba(6,182,212,0.5)` | Already sung words |
| `lyric-upcoming` | `rgba(255,255,255,0.9)` | Words not yet sung |

### Semantic Colors
| Token | Hex | Usage |
|---|---|---|
| `success` | `#10B981` | Success states, online indicators |
| `error` | `#F87171` | Error states, destructive actions |
| `error-bg` | `rgba(239,68,68,0.15)` | Error button backgrounds |
| `warning` | `#FBBF24` | Warning states |

### Glass / Surface Colors
| Token | Usage |
|---|---|
| `rgba(255,255,255,0.04-0.08)` | Card backgrounds (glassmorphism) |
| `rgba(255,255,255,0.10-0.15)` | Borders (glassmorphism) |
| `rgba(255,255,255,0.06-0.12)` | Interactive button backgrounds |
| `rgba(13,11,43,0.8-0.95)` | Navigation bars |

---

## GRADIENT DEFINITIONS

### Primary CTA Gradient
```
linear-gradient(135deg, #7C3AED, #2563EB)
```

### Upload Action Gradient
```
linear-gradient(135deg, #06B6D4, #7C3AED)
```

### Success Gradient
```
linear-gradient(135deg, #10B981, #06B6D4)
```

### Logo Text Gradient
```
linear-gradient(90deg, #E0C3FC, #8EC5FC)
```

### Avatar Gradients (cycle through 4)
1. `#7C3AED` → `#EC4899` (violet to pink)
2. `#2563EB` → `#06B6D4` (blue to cyan)
3. `#059669` → `#10B981` (dark green to green)
4. `#D97706` → `#F59E0B` (amber tones)

---

## TYPOGRAPHY

**Font Family:** Inter (Google Fonts) — only one family for the entire app

| Level | Size | Weight | Tracking | Usage |
|---|---|---|---|---|
| Display | 72px | 800 | -0.01em | Karaoke lyrics (active line) |
| H1 | 56px | 700 | -0.02em | Landing headline |
| H2 | 40px | 700 | -0.01em | — |
| H3 | 32px | 700 | 0 | Section titles |
| H4 | 26-28px | 700 | 0 | Card titles |
| Body Large | 20px | 400 | 0.01em | Subtitles |
| Body | 16px | 400-500 | 0 | General text |
| Small | 13-14px | 400-600 | 0 | Secondary labels |
| Caption / Label | 11-12px | 600 | 0.10-0.12em | ALL-CAPS section labels |

**All-caps labels** always use: weight 600, letter-spacing 0.10-0.12em, color `rgba(255,255,255,0.3-0.4)`

---

## SPACING SYSTEM

Base unit: 4px

| Scale | Value | Usage |
|---|---|---|
| 1 | 4px | Minimal gaps |
| 2 | 8px | Icon-to-text gap, tight spacing |
| 3 | 12px | Card internal gaps |
| 4 | 16px | Standard component gap |
| 5 | 20px | Card padding small |
| 6 | 24px | Section spacing small |
| 7 | 28px | — |
| 8 | 32px | Section spacing |
| 10 | 40px | Large section spacing |
| 12 | 48px | Card padding large |
| 16 | 64px | — |

---

## BORDER RADIUS TOKENS

| Token | Value | Usage |
|---|---|---|
| `rounded-sm` | 8px | Minimum radius |
| `rounded-md` | 12-14px | Small cards, chips |
| `rounded-lg` | 16-20px | Standard cards, inputs |
| `rounded-xl` | 24px | Main content cards |
| `rounded-2xl` | 28px | Modal cards |
| `rounded-pill` | 32px / 50% | Buttons, chips, avatars |

---

## GLASSMORPHISM RECIPE

Standard card:
```
background: rgba(255,255,255,0.05-0.08)
border: 1px solid rgba(255,255,255,0.10-0.15)
backdrop-filter: blur(16-24px)
border-radius: 20-24px
```

Elevated card (with glow):
```
+ box-shadow: 0 8px 64px rgba(124,58,237,0.15)
+ box-shadow (inset): 0 1px 0 rgba(255,255,255,0.06)
```

Navigation bar:
```
background: rgba(13,11,43,0.8-0.9)
backdrop-filter: blur(20px)
border-bottom: 1px solid rgba(255,255,255,0.08)
```

---

## MUI COMPONENT MAPPING

| UI Element | MUI Component |
|---|---|
| Primary CTA button | `<Button variant="contained">` |
| Secondary button | `<Button variant="outlined">` |
| Ghost button | `<Button variant="text">` |
| Text input | `<TextField>` |
| Search input | `<TextField InputProps={{ startAdornment: SearchIcon }}>` |
| Tag/chip | `<Chip>` |
| Tabs | `<Tabs>` / `<Tab>` |
| Progress bar | `<LinearProgress>` |
| Slider | `<Slider>` |
| Modal overlay | `<Dialog>` |
| Icon button | `<IconButton>` |

---

## ICON LIBRARY

Use **Material UI Icons (MUI)** exclusively. Style: Rounded variant preferred.

Key icons used:
- `MicIcon` / `MicExternalOnIcon` — app logo/branding
- `PlayArrowIcon`, `PauseIcon` — playback
- `Forward15Icon`, `Replay15Icon` — seek
- `VolumeUpIcon`, `VolumeMuteIcon` — volume
- `SearchIcon`, `SearchOffIcon` — search
- `CloudUploadIcon` — upload
- `AutoAwesomeIcon`, `AutoFixHighIcon` — AI/magic
- `QueueMusicIcon` — queue
- `LockIcon`, `LockOpenIcon` — admin
- `ArrowBackIcon` — navigation
- `CloseIcon` — dismiss
- `CheckIcon`, `CheckCircleIcon` — success
- `PowerSettingsNewIcon` — end session
- `WarningAmberIcon` — warnings
- `BackspaceIcon` — PIN keypad
- `PersonIcon` — users
- `MusicNoteIcon` — tracks

---

## ANIMATION PRINCIPLES

| Type | Duration | Easing | Usage |
|---|---|---|---|
| Micro-interaction | 150ms | ease-out | Button press, chip remove |
| State transition | 250ms | ease-in-out | Tab switch, modal open |
| Entrance | 300ms | cubic-bezier(0.34, 1.56, 0.64, 1) | Modal appear (spring) |
| Glow pulse | 2000ms | ease-in-out infinite | Active singer indicator |
| Shimmer | 1500ms | linear infinite | Loading skeletons |
| Lyric highlight | Real-time | Linear | Syllable progression in player |

---

## SCREEN RESOLUTIONS

| Target | Size | Notes |
|---|---|---|
| Primary (monitors) | 1920x1080 | All artboards use this |
| Secondary (tablets) | 1280x800 | Scale down font sizes ~15% |
| Secondary (tablets) | 1024x768 | Minimum supported |
