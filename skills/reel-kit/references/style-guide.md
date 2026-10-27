# The format: listicle explainer reel

Taken apart from a popular founder-education reel ("7 things you have to do before launching your app"): 43 s, 9:16. It's **a single seated take, cut tight, with a graphics layer that turns the talk into a checklist.** There's no B-roll and no screen recording; every visual besides the speaker is a clean, Apple-style mock UI card. That's why it's cheap to make and still looks premium.

All measurements are for a 1080×1920 canvas.

## 1. Script structure

| Beat | Time | Example | What it does |
|---|---|---|---|
| Hook | 0–3 s | "7 things you have to do before launching your app to real users." | Number + promise + who it's for. Opens on a physical action (e.g. closing a laptop). |
| Items | ~4.5 s each | "**Apple developer account.** Sign up early because approval can take a few days." | Say the **label** first (it becomes the title), then **one reason** (it becomes the proof card). |
| Escalation | 2–3 s | "And this is not even half the things every great app is implementing." | Makes the CTA feel like it unlocks *more*. |
| CTA | 4–5 s | "…comment **guide** and I'll send you my free launch guide." | One-word comment keyword, wired to an auto-DM. |
| End card | ~3 s | (silent) | Dimmed frame + Instagram logo + handle. |

- About 3.2 words/s, with no pause longer than about 0.15 s left in.
- Each item is a **noun-phrase label + one sentence**. That rhythm is what lets the graphics sync without any manual timing.

## 2. Edit

- **Jump cuts on every pause.** Silences over about 0.3 s are removed.
- **Framing changes on every cut.** It alternates wide and tight (about 1.15–1.2×); that's what hides the jump. Long sentences get a punch-in mid-sentence even with nothing removed.
- **Punchlines get the tightest crop** ("it's not what you think", "not even half").
- **Cuts land exactly when a graphic enters.** A title or card appears *on* a cut, never mid-shot.
- The first second is a slow push-in.
- Grade: a little contrast and saturation. The face sits at about a third of the frame height.

## 3. Instagram no-go zones (hard rule)

Instagram draws its own interface over a Reel; nothing of ours may sit there. Measured from the Edits app's yellow guide overlay:

| Zone | Covers | Area on 1080×1920 |
|---|---|---|
| Right column | like / comment / share / more / audio buttons | x ≥ 885 (right ~18%), full height |
| Bottom band | username, caption, audio line | y ≥ 1595 (bottom ~17%) |
| Top band | "Reels" header + camera icon | y < 250 (top ~13%) |

That leaves a safe box of **x 0–885, y 250–1595**. The kit keeps a further 20 px buffer and a 54 px left margin, so things are centred on the safe box (x≈460), not the frame (x=540). Many reference reels break this rule: a checklist in the top band, a watermark in the right column. Don't copy that.

## 4. Graphics layers (bottom to top)

| Layer | Look | Position | Motion |
|---|---|---|---|
| Bottom gradient | black 60% → 0 over the lower 45% | full frame | static |
| Running checklist | 26 px white, weight 500, numbers bold | top-left of the safe box, 38 px line step | each item fades in as its title flies into it |
| Watermark | Instagram glyph 52 px + handle 19 px caps | hops between top-right of the safe box and left-middle about every 7 s | blur-in at each hop; the glyph flashes the IG gradient then goes white. Hopping makes it hard to crop out of reposts. Clears when the CTA lands. |
| Section title | big number 144 px + 50 px line icon + label 58 px (wraps) | left of the safe box, centred on the caption line (y≈1268) | blur-in from the left; holds while spoken; then **shrinks and flies into the checklist** |
| Proof card | white, 34 px radius, soft shadow; black app-icon tile + title/subtitle + status pill; rows with hairline dividers, ✓/✗ badges, progress bars | x 54, y 900, w 811 (chest height) | blur + scale in; **grows a row on the word that mentions it**; blur out |
| Notifications | iOS banner stack, grey glass, coloured app tile | the card slot | drop in from above, one per phrase |
| Captions | 56 px Inter 600, white, soft shadow, 2–4 words, punctuation kept | y≈1268, or y≈1490 while a card is up | 0.16 s blur-in, hard cut out; hidden while a title is on screen |
| CTA | "comment" 44 px / keyword in curly quotes sized to fit (≤140 px) | top of the safe box | blur + scale in; checklist and watermark clear |
| End card | 72% black dim + 150 px IG glyph + handle | centre of the safe box | dim, logo pops |

The reference uses SF Pro; Inter, bundled under the OFL, is the closest free match.

## 5. Filming checklist (what makes the edit work)

1. Shoot vertical 1080×1920 or 4K and **share the original file**. Messaging apps shrink video to about 576×1024 and cut the voice's treble, so punch-ins look soft and the voice sounds dull.
2. Seat the speaker with the head in the top third and empty space at chest height. Cards and captions live there.
3. A background with depth (lights, plants, a window) slightly out of focus. A busy office works, but it competes with the cards.
4. A prop helps: a mic in hand, or a laptop to close as the hook action.
5. Read item by item. **Say each label as its own phrase** before explaining it.
6. Pause about a second between takes and re-read the whole sentence on a flub. The kit cuts the pauses, and you choose takes by time range.
7. Say the CTA keyword clearly, and set up the auto-DM for that keyword before posting.

## 6. Adapting it to content that isn't a list

- Numbered titles become **stat titles** ("60+ creators", "750M views"). The big number is the stat, not an index.
- The checklist becomes a **running tally** of the story's proof points.
- Proof cards become **mock UIs of each claim**: a view counter (`stat`), a short-video grid counting up (`grid`), a campaign card that gets struck through (`ui` + `strike_at`), a creator-network graph (`network`), a notification flood (`notifs`).
