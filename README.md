# Reel Kit, a Claude Code plugin

Give Claude your raw talking-head clips (phone videos, several takes, flubs and all) and get back a finished vertical reel for Instagram, TikTok or Shorts:

- **jump cuts** on every pause, with **punch-in zooms** that hide them
- **2–4 word captions** throughout
- **number titles** that fly into a running checklist
- **mock app cards** that build row by row as you talk
- a hopping **watermark**, a **"comment KEYWORD"** call to action, and an **end card**

Nothing is placed under Instagram's buttons, caption or header. That's checked automatically before every render.

## What you need

It runs on **your own computer** with Claude Code: the desktop app's **Code** tab in **Local** mode, the VS Code extension, or the terminal. Claude.ai chat, the phone app and cloud sessions can't run it, because it needs the tools below on your machine.

| Tool | Install |
|---|---|
| Python 3.10+ | [python.org](https://www.python.org/downloads/), then `pip install faster-whisper opencv-python numpy` |
| ffmpeg (with ffprobe) | [ffmpeg.org](https://ffmpeg.org/download.html), and make sure `ffmpeg -version` works in a new terminal |
| Node.js | [nodejs.org](https://nodejs.org) (the renderer downloads itself through `npx` on first use) |
| git | [git-scm.com](https://git-scm.com) (Claude Code uses it to download the plugin) |

The first video downloads a speech model (~1.5 GB) and the renderer, so it's slow once. After that, a 45-second reel renders in about 3 minutes. Rendering wants ~4 GB of free memory.

## Install

In a terminal:

```bash
claude plugin marketplace add https://github.com/akshaye2003/reel-kit.git
claude plugin install reel-kit@akshaye-plugins
```

Then restart Claude Code. (Inside a Claude Code session, `/plugin marketplace add https://github.com/akshaye2003/reel-kit.git` and then `/plugin install reel-kit@akshaye-plugins` do the same.)

**Updates:** run `claude plugin update reel-kit@akshaye-plugins`, or turn on auto-update in `/plugin` → **Marketplaces** → **akshaye-plugins** → **Enable auto-update**.

**Without the plugin system:** copy the `skills/reel-kit` folder into your skills folder (`C:\Users\<you>\.claude\skills\` on Windows, `~/.claude/skills/` on macOS and Linux) and restart Claude Code.

## Use

Just ask Claude, for example:

> Make a reel from the clips in `D:\shoots\oct-27\`. It's 5 ways to fix a broken AI app, ranked worst to best. My handle is @myhandle and the comment keyword is "dev".

Claude will:
1. transcribe the clips and pick the clean takes;
2. ask about anything it isn't sure of (brand names, your handle);
3. design the titles and cards;
4. show you previews;
5. render `reels/<name>/build/<name>.mp4`.

Small changes after that take a few minutes to re-render.

**Tips for better results**
- Record vertically and share the **original** files. Clips forwarded through WhatsApp are shrunk to about 576×1024, so punch-ins look soft and the voice loses its crispness.
- Keep your head in the top third of the frame, with empty space at chest height. That's where the cards go.
- Say each list item's name as its own phrase ("Number two: cold email."), so the title lands on it.
- On Windows, close the video in your player before asking for a re-render. Windows locks open files.

## What's inside

```
.claude-plugin/        plugin + marketplace manifests
skills/reel-kit/
  SKILL.md             the instructions Claude follows
  scripts/reelkit.py   the pipeline: transcribe → plan → edit → compose → audit → render
  references/          the format breakdown, project-file reference, example project
  assets/fonts/        Inter (SIL Open Font License 1.1, see OFL.txt)
```

## License

[MIT](LICENSE): use it, change it and share it freely; just keep the copyright notice. The bundled Inter font is under the SIL Open Font License 1.1.

Fetched at run time rather than bundled, each under its own license: the [HyperFrames](https://www.npmjs.com/package/hyperframes) CLI (via npx), [GSAP](https://gsap.com) (downloaded once from cdnjs and cached in `~/.cache/reel-kit/`) and the Whisper speech model (via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)).
