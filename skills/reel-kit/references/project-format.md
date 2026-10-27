# project.json reference

One `project.json` describes one reel: which takes, what was said, and which graphics go on which words. Everything the pipeline produces goes in `build/` beside it. Clip paths may be absolute or relative to the project file.

## Contents
- Steps
- Top-level fields
- Takes
- Overlays: title, card (ui / stat / grid / network / notifs), cta
- Icons and tones
- Tuning

## Steps

| Step | Does | Re-run when |
|---|---|---|
| `transcribe` | Whisper word timings per take range, cached in `build/transcripts/` | a take's clip or `in`/`out` changes (new ranges only) |
| `plan` | aligns your script to the audio, finds pauses, picks cuts and framing, tracks the face. Prints the shot list → `build/plan.json` | takes, `punch`, `framing`, `cuts` or overlay anchors change |
| `edit` | ffmpeg cut, crop, grade → `build/public/edit.mp4`, plus untouched audio `build/voice_raw.wav` | after `plan` |
| `voice` | voice processing (see `voice` below); `--samples` writes 15 s per level for listening | to change the voice level; it swaps audio into an existing render, with no re-render |
| `compose` | overlay composition → `build/public/index.html` | overlays, handle or styling change (takes seconds) |
| `audit` | snapshots the overlay layer every 0.25 s; fails if anything touches a no-go zone → `build/audit/violations.jpg` | runs automatically before `render` |
| `render` | audit, then HyperFrames → `build/<name>.mp4` (`--quality draft|standard|high`, `--workers N`) | after anything |
| `all` | transcribe → plan → edit → compose → render | first full build |

## Top-level fields

```jsonc
{
  "name": "my-reel",                       // output file name
  "handle": "@yourhandle",                 // watermark + end card. Ask the user; never reuse an example's
  "fps": 30,                               // optional; defaults to the first clip's fps
  "whisper": { "model": "medium.en", "hotwords": "BrandName ProductName" },
  "framing": { "wide": 1.3, "tight": 1.55, "punch": 1.8, "face_y": 0.34 },
  "punch": ["not what you think"],         // phrases whose cut gets the tightest crop
  "end_hold": 2.6,                         // seconds of end card after the last word
  "voice": { "enhance": "off" },           // "off" (default) | "light" | "full" (experimental)
  "safe": { "top": 250, "bottom": 1595, "right": 885, "side": 100 },   // Instagram no-go zones, 1080x1920 px; side = feed crop on tall phones
  "cuts": { "min_gap": 0.28, "max_seg": 3.2, "min_seg": 0.9, "depth_db": 25 },
  "takes": [ ... ],
  "overlays": [ ... ]
}
```

- `framing` values are zoom factors relative to the largest 9:16 crop of the source. A selfie that already fills the frame wants about `1.0 / 1.16 / 1.3`; a wide seated shot wants about `1.3 / 1.55 / 1.8`. `face_y` is where the face centre sits, as a fraction of frame height.

## Takes

In story order. A take is a range of one clip:

```json
{ "clip": "clips/IMG_0412.MOV", "in": 13.95, "out": 20.87,
  "script": "Exactly what was said, spelled the way captions should read." }
```

- `in`/`out` are optional (default: the whole clip). Use them to drop flubbed attempts and off-camera talk.
- `script` is optional (default: Whisper's words). It fixes ASR errors and sets caption spelling; it's aligned to the audio automatically.
- Several takes may come from the same clip.

## Overlays

Each overlay is timed by **phrases from the scripts**, never by seconds. Punctuation and case are ignored, and the words must appear in that order. A phrase matches its first occurrence; `until` and per-row `at` are searched after the overlay's `at`.

### title

Big number + icon + label in the caption slot. With `tally`, it shrinks and flies into the top-left checklist.

```json
{ "type": "title", "at": "Cold email.", "number": "2", "icon": "chat", "label": "Cold email",
  "tally": "2 Cold email" }
```

Leave out `number` for a big text-only title, which is auto-sized to fit. In a tally line, a leading token with a digit is shown bold.

### card

All card kinds take `at`, `until`, an optional `tail` (seconds after `until`, default 0.3), an optional `tally` (adds a checklist line when the card leaves), and `header`:

```json
"header": { "icon": "chart", "title": "Ad campaign", "subtitle": "Day 1",
            "pill": { "text": "Live", "tone": "green", "at": "start today" } }
```

The `pill.at` is optional; without it the pill shows with the card.

**`kind: "ui"`**: header + rows. The card grows a row on each row's `at`.

```json
"rows": [
  { "label": "Ready to launch", "check": true, "at": "start today" },
  { "label": "Budget used", "value": "80%", "progress": 0.8, "at": "burn money" },
  { "label": "Knows what works", "cross": true, "at": "what works" },
  { "label": "Hire influencers", "check": true, "at": "hire", "strike_at": "about the product" },
  { "type": "avatars", "count": 60, "label": "60+ creators", "at": "creators" }
]
```

`check` → green ✓, `cross` → red ✗, `value` → grey text on the right, `progress` (0–1) → animated bar, `strike_at` → red strike-through and fade.

**`kind: "stat"`**: big count-up number + sparkline. `value`, `format` (`",d"` thousands separators, `".1f"`), `label`, `prefix`, `suffix`, `from`, `count_dur`, `spark` (default true).

**`kind: "grid"`**: count-up + a row of short-video thumbnails popping in. `value`, `label`, `tiles` (default 6).

**`kind: "network"`**: a brand node with creators branching out. `center` (text in the middle), `nodes` (default 10).

**`kind: "notifs"`**: an iOS notification stack with no card chrome; each item drops in on its `at`.

```json
"items": [ { "app": "Mail", "title": "0 new replies", "body": "Inbox", "icon": "bell", "color": "#8e8e93", "at": "never reply" } ]
```

### cta

```json
{ "type": "cta", "at": "comment \"users\"", "keyword": "users", "verb": "comment" }
```

The words of `at` are left out of the captions while the CTA shows. The checklist and watermark clear, and the end card follows the last word.

## Icons and tones

Icons: `pin people user play eye chart home network megaphone camera bell check x bolt chat briefcase code`. To add one, put an SVG path (24×24 viewBox, stroke-based) in `ICONS` in `scripts/reelkit.py`.

Pill tones: `green orange red gray blue`.

## Tuning

- **Pauses not cut, or words clipped:** `cuts.min_gap` (shortest pause that gets removed) and `cuts.depth_db` (threshold = speech level minus this; higher cuts more). `cuts.pad_in` / `pad_out` set breathing room around each cut.
- **Too many or too few framing changes:** `cuts.max_seg` is how long a shot may run before a mid-sentence punch-in. `cuts.min_seg` folds shorter fragments into a neighbour, but only across short breaths (`cuts.fold_gap`, default 0.45 s), so a list label between real pauses keeps its own cut.
- **Face lost:** the crop falls back to the previous shot of the same take. Detection uses OpenCV's frontal-face model, so profile shots don't track.
- **Captions:** the chunker aims for 9–18 characters and 2–4 words (5 if short). It avoids ending on "the", "of", "for"… and never splits a number from its unit.
- **Voice:** `voice --samples` writes `1_off.wav`, `2_light.wav` and `3_full.wav` at equal loudness for listening; set `voice.enhance`, then run `voice` to swap it into the render.
