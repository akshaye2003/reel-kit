---
name: reel-kit
description: Turn raw talking-head clips (phone or WhatsApp videos, several takes, flubs and all) into a finished vertical Reel / TikTok / Short in the "listicle explainer" style. Pauses are cut with alternating punch-in zooms, and 2–4 word captions run throughout. Big number titles fly into a running checklist, Apple-style mock-UI cards build row by row as the speaker talks, a watermark hops around the frame, and it ends on a "comment KEYWORD" CTA and an end card. Everything is kept clear of Instagram's UI zones and checked automatically. Use this whenever someone wants to edit, cut, caption or "make a reel like this" from face-to-camera footage, add captions and graphics to a talking-head video, fix text hidden under Instagram's buttons or caption, or reproduce a reference reel's editing style, even if they never say "reel-kit".
compatibility: Claude Code on a desktop machine (Windows, macOS or Linux). Needs Python 3.10+ with faster-whisper, opencv-python and numpy, plus ffmpeg/ffprobe and Node.js (npx). Rendering wants ~4 GB of free RAM. Internet on first run (HyperFrames CLI, Whisper model, GSAP).
---

# reel-kit

Raw takes in, finished 1080×1920 reel out. One script, `scripts/reelkit.py`, does the mechanical work (transcription, cutting, framing, composition, safe-zone audit, render). Your job is the editorial work: pick the takes, fix the transcript, and decide which graphics show up on which words.

What the finished reel looks like, layer by layer, is in `references/style-guide.md`. Read it once before designing overlays. Every field of the project file and every card type is in `references/project-format.md`. `references/example-project.json` is a complete project to copy.

```
python "$SKILL_DIR/scripts/reelkit.py" <step> <path/to/project.json>
  steps: transcribe | plan | edit | voice | compose | audit | render | all
```

`$SKILL_DIR` is this skill's base directory. Everything a project produces lands in `build/` next to its `project.json`.

## 0. Check the machine

```bash
ffmpeg -version && ffprobe -version && node --version && npx --version
python -c "import faster_whisper, cv2, numpy"     # else: pip install faster-whisper opencv-python numpy
```

The first render downloads the pinned HyperFrames CLI and a headless Chrome through npx. The first transcription downloads the Whisper model (`medium.en`, about 1.5 GB). Say so before it happens, because both take a while.

## 1. Look at the footage before deciding anything

For each clip, run `ffprobe` for size, fps, duration and rotation. Phone clips often store 1024×576 with rotation metadata and are really portrait. Make a contact sheet (`ffmpeg ... -vf "fps=1,scale=...,tile=..."`) and look at it. You're checking framing (where the face sits), whether the speaker looks down at notes, and the quality. A WhatsApp-forwarded clip is around 576×1024 or smaller; it will look soft once punched in. Tell the user that and ask for the original files if they have them.

If the user shares a *reference* reel, contact-sheet it the same way and compare it with `references/style-guide.md`, so you know which devices it actually uses.

## 2. Create the project and transcribe

Make `reels/<name>/project.json` in the user's working folder, starting from `references/example-project.json`. Put one take per clip, covering the full clip, with no `script` yet. Then run:

```bash
python "$SKILL_DIR/scripts/reelkit.py" transcribe reels/<name>/project.json
```

Each take is transcribed separately, with word timings, into `build/transcripts/`. Read them all.

## 3. Choose takes and write the script

This step decides whether the video is any good, so take your time.

- **Retakes.** People repeat a sentence when they flub it. Use the *last clean* attempt. Find the restart point in the word timings and set that take's `in`/`out`. A clip can hold two or three attempts, and a restart inside a sentence ("If you want to know more, if you want to know more about…") counts as one. After changing `in`/`out`, re-run `transcribe`. It only redoes ranges it hasn't seen, and transcribing the exact range gives cleaner words.
- **Off-camera voices** ("okay… less smiling… and action") are a director talking. Cut them out. The take that follows is usually the one the director wanted.
- **Order.** Arrange the takes into the story: hook, then the points, then the CTA. The clips' file order isn't necessarily the story order.
- **`script`.** Write exactly what was said, spelled the way the captions should read: brand names, digits ("60+", "20,000"), punctuation. The script is matched to the audio automatically, so it can differ from Whisper's words. It just shouldn't add or remove whole phrases.
- **Unclear words, especially product and brand names.** Don't guess silently. Re-transcribe the snippet with the name in `whisper.hotwords`. If it's still ambiguous (a product name heard as ordinary words, "app" vs "ad"), ask the user. If you can't ask, use your best reading and flag it in the final report. A misspelled brand on screen is the mistake people notice most.

## 4. Check the cut

```bash
python "$SKILL_DIR/scripts/reelkit.py" plan reels/<name>/project.json
```

This prints every shot with its words and framing (`wide` / `tight` / `punch`; `+` = a punch-in with no footage removed). Check that:

- cuts land at sentence or clause boundaries;
- each list label or title phrase starts on a cut. The planner prefers this automatically when the phrase is an overlay `at` anchor, so write the overlays (step 5) before the final `plan`;
- no shot runs past ~3 s without a framing change.

If pauses survive or words get clipped, tune `cuts` (see project-format.md). Fast talkers have only 0.2–0.4 s gaps, so very little gets removed. That's expected.

## 5. Design the overlays

Read `references/style-guide.md` §4–6 first. In short:

- **A list** ("5 ways…", "worst to best") gets one `title` per item. Number it the way the speaker counts (a countdown is fine) and give each a `tally` line for the running checklist.
- **Not a list**: make the big number the *stat* the speaker says ("60+ creators in SF", "750M views"). The tally becomes a running list of proof points.
- **Every claim gets a proof `card`.** A mock app screen that shows the claim: a proposal, a dashboard, a notification flood, a network graph, a ✓/✗ checklist. Each row appears on the word that mentions it (`rows[].at`), so the card builds while the speaker talks.
- **End with a `cta`** using the keyword the speaker says ("comment dev").

Rules that matter:

- **Put only what the speaker says on a card.** Don't invent numbers, durations, prices or quotes. Illustrative UI is fine ("New proposal", "Tagged YourBrand"); fabricated facts are not.
- **Anchors are phrases from the script** (`at`, `until`, `rows[].at`). Punctuation and case are ignored, but the words must appear in that order. `until` and row anchors are searched after the card's `at`. A phrase that appears twice matches its first occurrence, so add a neighbouring word to make it unique.
- Aim for one title or card every ~4–6 s. Titles and cards never share the screen; the script keeps them apart.
- The handle (`handle`) goes on the watermark and the end card. Ask the user for it, and never reuse one from an example.

## 6. Build, look at it, then render

```bash
python "$SKILL_DIR/scripts/reelkit.py" edit reels/<name>/project.json      # cut + framing + grade → build/public/edit.mp4
python "$SKILL_DIR/scripts/reelkit.py" compose reels/<name>/project.json   # overlays → build/public/index.html
npx -y hyperframes@0.7.90 snapshot reels/<name>/build/public --at 3,9.5,15 --no-end -o snaps --describe false
```

Snapshot one moment per overlay, *open the PNGs*, and look. Check that:
- titles fit, and no caption shows a single orphaned word;
- every card row you expect is there and no card text is truncated;
- the CTA keyword fits;
- the face isn't hidden for long.

Fix `project.json` and re-run `compose`, which takes seconds. Then:

```bash
python "$SKILL_DIR/scripts/reelkit.py" render reels/<name>/project.json    # audit, then ~2–4 min of rendering
```

`render` first runs the **safe-zone audit**: it snapshots the overlay layer every 0.25 s and refuses to render if any text, card or icon touches an Instagram UI zone. It writes `build/audit/violations.jpg` showing where. Fix the layout and don't skip the audit. Text under Instagram's buttons or caption is the one flaw that makes a reel look amateur on the actual app.

On Windows, **close the video in any player before re-rendering**. Windows locks an open file, and the render then fails at the last step with `EPERM … rename`. Close it and run `render` again.

## 7. Instagram no-go zones (never place anything here)

On 1080×1920, measured from the Edits app's yellow guides:

- right column, x ≥ 885 (like/comment/share/audio buttons), full height;
- bottom band, y ≥ 1595 (username, caption, audio line);
- top band, y < 250 (header, camera icon);
- both side strips, x < 100 and x > 980. The feed fills a tall phone's screen height, so it **crops the sides of the 9:16 frame**: about 56 px each side on an iPhone, more on 20:9 Androids. Editor previews show the whole frame, so they won't warn you about this.

The composer lays everything out inside the remaining box (x 120–865, y 270–1575), with a 20 px buffer from the zones. That's why captions and cards are centred slightly left of the frame centre. That's correct, not a bug. The zones are configurable under `"safe"`, but change them only if Instagram's layout changes.

## 8. Report back

Give the output path (`build/<name>.mp4`) and a short list of what you chose and anything the user must check:
- which takes you used and why;
- transcript fixes you made;
- any name you weren't sure of;
- the handle, if it's a placeholder;
- soft image quality from compressed sources, and the fix: send the originals;
- the CTA only works with an auto-DM tool (e.g. ManyChat) set up for that keyword.

## Voice enhancer (experimental, off by default)

`voice` can match the speech tone to a crisp podcast-style target (EQ toward a reference balance, an exciter for treble lost to compression, denoise, de-ess, compression). On heavily compressed phone audio, users have found "light" and "full" overdone. So leave `"voice": {"enhance": "off"}` unless asked. If someone wants it, run `voice --samples` and let them *listen* to `build/voice-samples/1_off.wav`, `2_light.wav` and `3_full.wav` before choosing. `voice` swaps the new audio into an already-rendered video in seconds, with no re-render.
