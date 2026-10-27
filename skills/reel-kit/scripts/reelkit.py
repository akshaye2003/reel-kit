#!/usr/bin/env python
"""reelkit - turn raw talking-head takes into a "listicle explainer" reel.

Pipeline (see STYLE_GUIDE.md for the look it reproduces):
  transcribe  word-level Whisper transcript per source clip (cached)
  plan        align your script to the audio, cut out pauses, track the face,
              pick wide/tight framing per cut  -> build/plan.json
  edit        ffmpeg: trim + crop + scale + concat + grade + loudnorm -> build/public/edit.mp4
  compose     HyperFrames composition (captions, titles, tally, UI cards,
              watermark, CTA, end card) -> build/public/index.html
  render      npx hyperframes render -> build/<name>.mp4
  all         everything above in order

Usage:  python reelkit.py all projects/<name>/project.json
"""
import argparse
import difflib
import html
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

KIT = Path(__file__).resolve().parent
# assets/ sits next to this script (reel-kit folder) or one level up (packaged as a skill: scripts/reelkit.py)
ASSETS = next((d for d in (KIT / "assets", KIT.parent / "assets") if d.is_dir()), KIT / "assets")
HF = "hyperframes@0.7.90"   # the HyperFrames CLI version this pipeline was built and tested with
GSAP_URL = "https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"
W, H = 1080, 1920
SR = 16000          # analysis sample rate
HOP = 0.01          # energy frame (s)


# ───────────────────────────── helpers ─────────────────────────────

def sh(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd)[:240])
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def load_project(path):
    path = Path(path).resolve()
    p = json.loads(path.read_text(encoding="utf-8"))
    p["_dir"] = path.parent
    p["_build"] = path.parent / "build"
    p["_build"].mkdir(exist_ok=True)
    for t in p["takes"]:
        c = Path(t["clip"])
        t["clip"] = str(c if c.is_absolute() else (path.parent / c).resolve())
    p.setdefault("handle", "@yourhandle")
    p.setdefault("framing", {})
    f = p["framing"]
    f.setdefault("wide", 1.3)
    f.setdefault("tight", 1.55)
    f.setdefault("punch", 1.8)
    f.setdefault("face_y", 0.34)      # where the face centre sits, as fraction of frame height
    f.setdefault("min_hold", 1.6)     # don't change framing more often than this (s)
    p.setdefault("cuts", {})
    c = p["cuts"]
    c.setdefault("min_gap", 0.28)     # silences longer than this get cut
    c.setdefault("pad_in", 0.05)
    c.setdefault("pad_out", 0.09)
    c.setdefault("depth_db", 25)      # threshold = speech level minus this
    c.setdefault("max_seg", 3.2)      # longer shots get a mid-sentence punch-in
    c.setdefault("min_seg", 0.9)      # shorter fragments are folded into a neighbour
    p.setdefault("end_hold", 2.6)     # seconds of end card after the last word
    # Instagram no-go zones (1080x1920 px), measured from the Edits app's yellow guides:
    # the like/comment/share column on the right, the username/caption band at the bottom,
    # and the header at the top. Nothing is ever placed outside this box.
    p.setdefault("safe", {})
    s = p["safe"]
    s.setdefault("top", 250)          # top ~13%
    s.setdefault("bottom", 1595)      # bottom ~17%
    s.setdefault("right", 885)        # right ~18%, full height
    s.setdefault("left", 0)
    # the feed fills a tall phone's screen height and crops the 9:16 frame's sides: ~56 px each side
    # on an iPhone (measured from a screen recording of a posted reel), more on 20:9+ Androids
    s.setdefault("side", 100)
    s.setdefault("buffer", 20)        # keep this far from the yellow
    return p


def probe(clip):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
                          "-show_format", "-of", "json", clip], capture_output=True, text=True, check=True)
    j = json.loads(out.stdout)
    s = j["streams"][0]
    w, h = int(s["width"]), int(s["height"])
    rot = 0
    for sd in s.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    rot = rot or int(s.get("tags", {}).get("rotate", 0) or 0)
    if abs(rot) % 180 == 90:
        w, h = h, w
    num, den = s["r_frame_rate"].split("/")
    return {"w": w, "h": h, "fps": float(num) / float(den), "dur": float(j["format"]["duration"])}


def envelope_db(clip):
    """RMS level in dBFS per 10 ms frame."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", clip, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(SR * HOP)
    frames = a[: len(a) // n * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9)


def norm(tok):
    return re.sub(r"[^a-z0-9]", "", tok.lower())


# ───────────────────────────── transcribe ─────────────────────────────

def cmd_transcribe(p):
    tdir = p["_build"] / "transcripts"
    tdir.mkdir(exist_ok=True)
    wcfg = p.get("whisper", {})
    model = None
    for t in p["takes"]:
        out = transcript_path(p, t)
        if out.exists():
            continue
        if model is None:
            from faster_whisper import WhisperModel
            model = WhisperModel(wcfg.get("model", "medium.en"), device="cpu", compute_type="int8")
        lo, hi = t.get("in", 0.0), t.get("out")
        print(f"[transcribe] {Path(t['clip']).name} [{lo}-{hi or 'end'}]")
        # transcribe only the take's range: retakes inside one clip confuse Whisper less that way
        cmd = ["ffmpeg", "-v", "error", "-ss", str(lo)] + (["-to", str(hi)] if hi else []) + \
              ["-i", t["clip"], "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
        audio = np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, dtype=np.float32)
        segs, _ = model.transcribe(audio, word_timestamps=True, beam_size=5,
                                   condition_on_previous_text=False,
                                   hotwords=wcfg.get("hotwords") or None)
        words = [{"text": w.word.strip(), "start": round(w.start + lo, 3), "end": round(w.end + lo, 3)}
                 for s in segs for w in s.words]
        out.write_text(json.dumps(words, indent=1), encoding="utf-8")
        print("   ", " ".join(w["text"] for w in words))


def transcript_path(p, t):
    rng = f"_{t.get('in', 0):g}-{t.get('out', 'end')}"
    return p["_build"] / "transcripts" / (Path(t["clip"]).stem.replace(" ", "_") + rng + ".json")


def take_words(p, t):
    words = json.loads(transcript_path(p, t).read_text(encoding="utf-8"))
    lo, hi = t.get("in", 0), t.get("out", 1e9)
    return [w for w in words if w["end"] > lo and w["start"] < hi]


def align_script(script, words):
    """Give every token of the user's corrected script a start/end from the ASR words."""
    toks = script.split()
    a = [norm(x) for x in toks]
    b = [norm(w["text"]) for w in words]
    times = [None] * len(toks)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (words[j1 + k]["start"], words[j1 + k]["end"])
        elif tag == "replace":
            s, e = words[j1]["start"], words[j2 - 1]["end"]
            lens = [max(1, len(a[i])) for i in range(i1, i2)]
            tot, acc = sum(lens), 0
            for k, i in enumerate(range(i1, i2)):
                t0 = s + (e - s) * acc / tot
                acc += lens[k]
                times[i] = (t0, s + (e - s) * acc / tot)
    # tokens the ASR missed entirely: spread them between their neighbours
    i = 0
    while i < len(toks):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(toks) and times[j] is None:
            j += 1
        s = times[i - 1][1] if i > 0 else (times[j][0] - 0.3 * (j - i) if j < len(toks) else 0)
        e = times[j][0] if j < len(toks) else s + 0.3 * (j - i)
        for k in range(i, j):
            times[k] = (s + (e - s) * (k - i) / (j - i), s + (e - s) * (k - i + 1) / (j - i))
        i = j
    return [{"text": tok, "start": round(t[0], 3), "end": round(t[1], 3)} for tok, t in zip(toks, times)]


# ───────────────────────────── plan ─────────────────────────────

def speech_intervals(env, lo, hi, cfg):
    """Voiced intervals inside [lo, hi] (seconds), with short pauses bridged."""
    a, b = int(lo / HOP), min(len(env), int(hi / HOP))
    seg = env[a:b]
    # anchor to speech level, not the noise floor: phone audio often gates pauses to near digital silence
    loud = np.percentile(seg, 92)
    thr = float(np.clip(loud - cfg.get("depth_db", 25), -50, -32))
    voiced = seg > thr
    ivs, i = [], 0
    while i < len(voiced):
        if voiced[i]:
            j = i
            while j < len(voiced) and voiced[j]:
                j += 1
            ivs.append([i, j])
            i = j
        else:
            i += 1
    ivs = [iv for iv in ivs if iv[1] - iv[0] >= 8]   # clicks < 80 ms would split a pause into "short" gaps
    gap = int(cfg["min_gap"] / HOP)
    merged = []
    for s, e in ivs:
        if merged and s - merged[-1][1] < gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    # fold fragments shorter than min_seg into a neighbour (a cut every few words strobes) - but only
    # across a short breath: a short phrase between real pauses (a list label) keeps its own cut
    short = int(cfg.get("min_seg", 0.9) / HOP)
    fold_gap = int(cfg.get("fold_gap", 0.45) / HOP)
    k = 0
    while len(merged) > 1 and k < len(merged):
        if merged[k][1] - merged[k][0] < short:
            if k == 0:
                j = 1
            elif k == len(merged) - 1:
                j = k - 1
            else:   # join the side with the smaller pause
                j = k - 1 if merged[k][0] - merged[k - 1][1] <= merged[k + 1][0] - merged[k][1] else k + 1
            gap_frames = merged[max(k, j)][0] - merged[min(k, j)][1]
            if gap_frames > fold_gap:
                k += 1
                continue
            a, b = sorted((k, j))
            merged[a] = [merged[a][0], merged[b][1]]
            del merged[b]
            k = 0
        else:
            k += 1
    return [(lo + s * HOP, lo + e * HOP) for s, e in merged], thr


STARTERS = {"and", "but", "because", "so", "that", "which", "who", "with", "when", "if", "then", "they",
            "being", "for", "or", "while", "even", "just"}
# a caption chunk should not end on these (or on a bare number: "750 | million")
DANGLERS = {"the", "a", "an", "of", "to", "with", "in", "on", "and", "my", "your", "their", "its", "our", "is", "are",
            "for", "about", "which", "from", "at", "by", "as", "that"}
DETERMINERS = {"the", "a", "an", "this", "my", "your", "their", "our", "every"}
PARTICLES = {"out", "up", "off", "down", "them", "him", "her", "us"}


def split_long(segs, words, cfg, anchors=()):
    """Split shots longer than max_seg at a word boundary - preferably right where an overlay
    enters (anchors), else at punctuation / a clause starter / a pause, near the middle.
    No footage is removed - the framing change alone makes it a punch-in.
    Returns [start, end, continues_previous]."""
    out = []
    todo = [[s, e, False] for s, e in segs]
    while todo:
        s, e, cont = todo.pop(0)
        if e - s <= cfg.get("max_seg", 3.2):
            out.append([s, e, cont])
            continue
        best = None
        for k, (a, b) in enumerate(zip(words, words[1:])):
            t = (a["end"] + b["start"]) / 2
            if not (s + 1.2 <= t <= e - 1.2):
                continue
            score = (abs(t - (s + e) / 2) * 0.8
                     - (1.6 if (k + 1) in anchors else 0)
                     - (1.0 if re.search(r"[.,!?;:]$", a["text"]) else 0)
                     - (0.6 if norm(b["text"]) in STARTERS else 0)
                     - min(0.3, b["start"] - a["end"]) * 2)
            if best is None or score < best[0]:
                best = (score, t)
        if best is None:
            out.append([s, e, cont])
            continue
        todo[0:0] = [[s, best[1], cont], [best[1], e, True]]
    return out


def face_centers(clip, times, meta):
    import cv2
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(clip)
    pts = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        fh, fw = frame.shape[:2]
        if (fw > fh) != (meta["w"] > meta["h"]):               # backend ignored rotation metadata
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            fh, fw = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = casc.detectMultiScale(gray, 1.1, 6, minSize=(int(fw * 0.06), int(fw * 0.06)))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            pts.append(((x + w / 2) / fw, (y + h / 2) / fh))
    cap.release()
    if not pts:
        return None
    return float(np.median([q[0] for q in pts])), float(np.median([q[1] for q in pts]))


def crop_rect(meta, face, zoom, face_y):
    sw, sh_ = meta["w"], meta["h"]
    bw = min(sw, sh_ * W / H)
    bh = bw * H / W
    cw, ch = bw / zoom, bh / zoom
    fx, fy = (face or (0.5, 0.4))
    x = fx * sw - cw / 2
    y = fy * sh_ - face_y * ch
    x = min(max(0, x), sw - cw)
    y = min(max(0, y), sh_ - ch)
    ev = lambda v: int(round(v / 2) * 2)
    return {"w": ev(cw), "h": ev(ch), "x": ev(x), "y": ev(y)}


def find_phrase(words, phrase, after=0.0, missing_ok=False):
    q = [norm(x) for x in phrase.split() if norm(x)]
    toks = [norm(w["text"]) for w in words]
    for i in range(len(words) - len(q) + 1):
        if words[i]["start"] + 1e-6 < after:
            continue
        if toks[i:i + len(q)] == q:
            return i, i + len(q) - 1
    if missing_ok:
        return None
    raise SystemExit(f"phrase not found in script: {phrase!r}")


def cmd_plan(p):
    fps = p.get("fps") or round(probe(p["takes"][0]["clip"])["fps"])
    p["fps"] = fps
    fr = lambda v: round(v * fps) / fps
    cfg, fcfg = p["cuts"], p["framing"]
    segments, out_words, take_bounds = [], [], []
    t_out = 0.0
    envs = {}
    for ti, t in enumerate(p["takes"]):
        meta = probe(t["clip"])
        lo, hi = t.get("in", 0.0), min(t.get("out", meta["dur"]), meta["dur"])
        words = take_words(p, t)
        if t.get("script"):
            words = align_script(t["script"], words)
        if t["clip"] not in envs:
            envs[t["clip"]] = envelope_db(t["clip"])
        ivs, thr = speech_intervals(envs[t["clip"]], lo, hi, cfg)
        # keep only intervals that actually contain a word
        mids = [(w["start"] + w["end"]) / 2 for w in words]
        ivs = [iv for iv in ivs if any(iv[0] - 0.05 <= m <= iv[1] + 0.05 for m in mids)]
        # pad, keep inside the take, never overlap
        segs = []
        for k, (s, e) in enumerate(ivs):
            s = max(lo, s - cfg["pad_in"])
            e = min(hi, e + cfg["pad_out"])
            if segs and s < segs[-1][1]:
                mid = (segs[-1][1] + s) / 2
                segs[-1][1], s = mid, mid
            segs.append([s, e])
        anchors = set()
        for o in p.get("overlays", []):
            hit = find_phrase(words, o["at"], missing_ok=True)
            if hit:
                anchors.add(hit[0])
        segs = split_long(segs, words, cfg, anchors)
        if ti == len(p["takes"]) - 1 and segs:              # room for the end card
            segs[-1][1] = min(hi, segs[-1][1] + p["end_hold"])
        print(f"[plan] take {ti}: {Path(t['clip']).name} thr={thr:.1f}dB  {len(segs)} cuts")
        take_start = t_out
        for s, e, cont in segs:
            s, e = fr(s), fr(e)
            if e - s < 2 / fps:
                continue
            seg = {"take": ti, "clip": t["clip"], "src_in": s, "src_out": e, "out_in": round(t_out, 4),
                   "dur": round(e - s, 4), "meta": meta, "cont": cont}
            if cont and segments:                           # punch-in split: same footage continues
                segments[-1]["cont_next"] = True
            segments.append(seg)
            t_out += e - s
        # remap words into the output timeline
        for w in words:
            m = (w["start"] + w["end"]) / 2
            seg = min((sg for sg in segments if sg["take"] == ti),
                      key=lambda sg: 0 if sg["src_in"] <= m <= sg["src_out"] else min(abs(m - sg["src_in"]), abs(m - sg["src_out"])))
            st = min(max(w["start"], seg["src_in"]), seg["src_out"])
            en = min(max(w["end"], seg["src_in"]), seg["src_out"])
            out_words.append({"text": w["text"], "take": ti,
                              "start": round(seg["out_in"] + st - seg["src_in"], 3),
                              "end": round(seg["out_in"] + en - seg["src_in"], 3)})
        take_bounds.append([round(take_start, 4), round(t_out, 4)])
    # the last segment may be extended past the clip end -> freeze-frame pad
    last = segments[-1]
    want_end = out_words[-1]["end"] + p["end_hold"]
    pad = max(0.0, want_end - t_out)
    if pad > 0:
        last["freeze"] = round(pad, 3)
        t_out += pad
    duration = round(t_out, 4)

    # framing: every cut changes framing (that is what hides a jump cut); wide/tight alternate,
    # and the cut that starts a 'punch' phrase goes extra tight
    punch_spans = []
    for ph in p.get("punch", []):
        i, j = find_phrase(out_words, ph)
        punch_spans.append((out_words[i]["start"], out_words[j]["end"]))
    level, prev = "wide", None
    for k, sg in enumerate(segments):
        if k > 0:
            level = "tight" if prev["level"] == "wide" else "wide"
        lv = level
        if any(sg["out_in"] - 0.05 <= a < sg["out_in"] + min(sg["dur"], 1.0) for a, _ in punch_spans):
            lv = "punch"
        sg["level"] = lv
        times = [sg["src_in"] + sg["dur"] * f for f in (0.2, 0.5, 0.8)]
        face = face_centers(sg["clip"], times, sg["meta"])
        if face is None and prev is not None and prev["take"] == sg["take"]:
            face = prev["face"]
        # same take + same framing -> hold position unless the speaker really moved
        if prev is not None and prev["take"] == sg["take"] and prev["level"] == lv and face and prev["face"]:
            if abs(face[0] - prev["face"][0]) < 0.06 and abs(face[1] - prev["face"][1]) < 0.05:
                face = prev["face"]
        sg["face"] = face
        sg["crop"] = crop_rect(sg["meta"], face, fcfg[lv], fcfg["face_y"])
        prev = sg
    for sg in segments:
        sg.pop("meta")
    plan = {"fps": fps, "duration": duration, "segments": segments, "words": out_words, "takes": take_bounds}
    (p["_build"] / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    for sg in segments:
        a, b = sg["out_in"], sg["out_in"] + sg["dur"]
        said = " ".join(w["text"] for w in out_words if a <= (w["start"] + w["end"]) / 2 <= b)
        print(f"   {'+' if sg.get('cont') else ' '} {a:6.2f}-{b:6.2f}  {sg['level']:5}  {said}")
    src = sum(min(t.get("out", 1e9), probe(t["clip"])["dur"]) - t.get("in", 0) for t in p["takes"])
    print(f"[plan] {len(segments)} cuts, {duration:.2f}s output (from {src:.1f}s of selected takes)"
          "   ('+' = punch-in, no footage removed)")
    return plan


# ───────────────────────────── edit (ffmpeg) ─────────────────────────────

def cmd_edit(p):
    plan = json.loads((p["_build"] / "plan.json").read_text(encoding="utf-8"))
    pub = p["_build"] / "public"
    pub.mkdir(exist_ok=True)
    fps = plan["fps"]
    clips = list(dict.fromkeys(s["clip"] for s in plan["segments"]))
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for c in clips:
        args += ["-i", c]
    grade = p.get("grade", "eq=contrast=1.05:saturation=1.06:gamma=0.98")
    parts, labels = [], []
    for k, s in enumerate(plan["segments"]):
        i = clips.index(s["clip"])
        c = s["crop"]
        v = (f"[{i}:v]trim=start={s['src_in']}:end={s['src_out']},setpts=PTS-STARTPTS,"
             f"crop={c['w']}:{c['h']}:{c['x']}:{c['y']},scale={W}:{H}:flags=lanczos,setsar=1,fps={fps}")
        a = f"[{i}:a]atrim=start={s['src_in']}:end={s['src_out']},asetpts=PTS-STARTPTS"
        if not s.get("cont"):          # real cut: de-click; punch-in split: audio is continuous
            a += ",afade=t=in:d=0.012"
        if not s.get("cont_next"):
            a += f",afade=t=out:st={max(0, s['dur'] - 0.02):.3f}:d=0.02"
        if s.get("freeze"):
            v += f",tpad=stop_mode=clone:stop_duration={s['freeze']}"
            a += f",apad=pad_dur={s['freeze']}"
        parts += [v + f"[v{k}]", a + f"[a{k}]"]
        labels.append(f"[v{k}][a{k}]")
    n = len(plan["segments"])
    parts.append("".join(labels) + f"concat=n={n}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]{grade},unsharp=5:5:0.55:5:5:0[vo]")
    # audio leaves here untouched (also saved losslessly); the `voice` step enhances it
    parts.append("[ac]aresample=48000,asplit=2[ao][araw]")
    graph = p["_build"] / "edit_graph.txt"
    graph.write_text(";\n".join(parts), encoding="utf-8")
    t = f"{plan['duration']:.3f}"
    args += ["-filter_complex_script", str(graph),
             "-map", "[vo]", "-map", "[ao]", "-t", t,
             "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-g", str(fps), "-keyint_min", str(fps),
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(pub / "edit.mp4"),
             "-map", "[araw]", "-t", t, str(p["_build"] / "voice_raw.wav")]
    sh(args)
    print(f"[edit] {pub / 'edit.mp4'}")
    cmd_voice(p, swap_render=False)       # the old render no longer matches a new edit; render replaces it


# ───────────────────────────── voice ─────────────────────────────

# speech tone balance of the reference reel (dB per band, relative to the whole), measured on its voice
VOICE_BANDS = [(60, 150), (150, 400), (400, 1000), (1000, 2500), (2500, 5000), (5000, 8000), (8000, 12000)]
VOICE_TARGET = [-8, -5, -5, -7, -13, -21, -29]
VOICE_LEVELS = {"off": 0.0, "light": 0.6, "full": 1.0}


def voice_balance(wav):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1", "-ar", "44100", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.float32)
    n, hop = 2048, 1024
    fr = np.lib.stride_tricks.sliding_window_view(a, n)[::hop] * np.hanning(n)
    sp = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    fq = np.fft.rfftfreq(n, 1 / 44100)
    lvl = 10 * np.log10(sp.sum(axis=1) + 1e-12)
    S = sp[lvl > np.percentile(lvl, 60)].mean(axis=0)          # speech frames only
    band = lambda lo, hi: 10 * np.log10(S[(fq >= lo) & (fq < hi)].sum() + 1e-12)
    tot = band(60, 12000)
    return np.array([band(lo, hi) - tot for lo, hi in VOICE_BANDS])


def voice_chain(gains, exciter, strength):
    if strength == 0:
        return "highpass=f=75,acompressor=threshold=-21dB:ratio=3:attack=5:release=120,loudnorm=I=-14:TP=-1.5:LRA=9"
    eq = [f"equalizer=f={np.sqrt(lo * hi):.0f}:t=o:w=1.3:g={g:.1f}"
          for (lo, hi), g in zip(VOICE_BANDS, gains) if abs(g) >= 0.5]
    return ",".join(["highpass=f=85", "afftdn=nr=8:nf=-50:tn=1"] + eq + [
        f"aexciter=amount={exciter:.2f}:drive=6:freq=3000:blend=0:ceil=12000",   # regrows treble a codec cut off
        "deesser=i=0.35",
        f"acompressor=threshold=-26dB:ratio={3 + 2 * strength:.1f}:attack=4:release=90:makeup=2",
        "loudnorm=I=-14:TP=-1.0:LRA=7"])


def cmd_voice(p, swap_render=True, swap=True, level=None):
    """Enhance the voice: match-EQ the speech toward the reference reel's tone (measure -> EQ ->
    re-measure, twice), regrow lost treble with an exciter, light denoise, de-ess, compress, -14 LUFS.
    Then swap the audio into edit.mp4 and into the rendered video if there is one (no re-render)."""
    b = p["_build"]
    # default "off" = a light chain; "light"/"full" can sound overdone on heavily compressed phone audio
    level = level or p.get("voice", {}).get("enhance", "off")
    raw, out = b / "voice_raw.wav", b / ("voice.wav" if swap else f"voice_{level}.wav")
    strength = VOICE_LEVELS[level]
    run = lambda chain: subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af", chain,
                                        "-ar", "48000", str(out)], check=True)
    src = voice_balance(raw)
    target = np.array(VOICE_TARGET, dtype=float)
    dead = src < -40                                  # bands the codec emptied: EQ there only lifts hiss
    gains = np.clip((target - src) * strength, -6, 10)
    gains[0] = min(gains[0], 6)                       # never more than +6 dB of low end (boom, handling noise)
    gains = np.where(dead, np.minimum(gains, 0), gains)
    exciter = 1.2 * strength
    for _ in range(3 if strength else 0):             # closed loop on the real result
        run(voice_chain(gains, exciter, strength))
        got = voice_balance(out)
        resid = (target - got) * strength
        gains = np.clip(gains + 0.7 * np.where(dead, 0, resid), -8, 12)
        gains[0] = min(gains[0], 6)
        if dead[-1]:
            # treble the codec wiped out is the exciter's job alone (EQ there is capped) - steer it by the
            # top band. Where real treble survived, EQ corrects it; the exciter adapting too would double up.
            exciter = float(np.clip(exciter * 10 ** (0.7 * resid[-1] / 20), 0.2, 3.0))
    run(voice_chain(gains, exciter, strength))
    got = voice_balance(out)
    fmt = lambda v: " ".join(f"{x:+.0f}" for x in v)
    print(f"[voice] {level}: tone before [{fmt(src)}]  after [{fmt(got)}]  reference [{fmt(target)}]")
    if not swap:
        return out
    # swap the audio track in place - video stream copied, so this takes seconds
    final = b / f"{p.get('name', 'reel')}.mp4"
    targets = [b / "public" / "edit.mp4"] + ([final] if swap_render and final.exists() else [])
    for vid in targets:
        tmp = vid.with_name(vid.stem + ".voice-tmp.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(vid), "-i", str(out), "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                        str(tmp)], check=True)
        try:
            tmp.replace(vid)
            print(f"[voice] audio updated in {vid}")
        except PermissionError:
            print(f"[voice] {vid.name} is open in another program - new version saved as {tmp.name}")


def voice_samples(p, start=0.0, dur=15.0):
    """Same 15 s of voice at every level, for listening side by side."""
    d = p["_build"] / "voice-samples"
    d.mkdir(exist_ok=True)
    cut = lambda src, dst: subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(dur), "-i", str(src),
                                           str(dst)], check=True)
    for k, level in enumerate(["off", "light", "full"], 1):
        cut(cmd_voice(p, swap=False, level=level), d / f"{k}_{level}.wav")
    print(f"[voice] samples in {d}  (1_off = how it sounds now)")


# ───────────────────────────── compose (HyperFrames) ─────────────────────────────

ICONS = {
    "pin": '<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.4"/>',
    "people": '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17.2" cy="9" r="2.5"/><path d="M15.6 14.1c3 .2 5.4 2.7 5.4 5.9"/>',
    "play": '<path d="M8 5.5v13l11-6.5z" fill="currentColor" stroke="none"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "chart": '<path d="M3 20h18"/><path d="M5 16l4.5-5 3.5 3 6-7.5"/>',
    "home": '<path d="M3 11l9-7 9 7"/><path d="M5.5 9.5V20h13V9.5"/>',
    "network": '<circle cx="12" cy="12" r="2.6"/><circle cx="4.5" cy="5" r="1.9"/><circle cx="19.5" cy="5" r="1.9"/><circle cx="4.5" cy="19" r="1.9"/><circle cx="19.5" cy="19" r="1.9"/><path d="M6 6.4l4.1 4.1M18 6.4l-4.1 4.1M6 17.6l4.1-4.1M18 17.6l-4.1-4.1"/>',
    "megaphone": '<path d="M3 10v4h4l8 4.5v-13L7 10H3z"/><path d="M18 9a4 4 0 0 1 0 6"/>',
    "camera": '<rect x="3" y="6" width="18" height="13" rx="3"/><circle cx="12" cy="12.5" r="3.4"/><path d="M8.5 6l1.5-2.2h4L15.5 6"/>',
    "bell": '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "bolt": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "chat": '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
    "briefcase": '<rect x="3" y="7" width="18" height="13" rx="2.5"/><path d="M9 7V5h6v2M3 12.5h18"/>',
    "user": '<circle cx="12" cy="8" r="3.6"/><path d="M4.5 20.5c0-4 3.4-6.8 7.5-6.8s7.5 2.8 7.5 6.8"/>',
    "code": '<path d="M8.5 7L3.5 12l5 5M15.5 7l5 5-5 5"/>',
    "ig": '<rect x="2.6" y="2.6" width="18.8" height="18.8" rx="5.6"/><circle cx="12" cy="12" r="4.4"/><circle cx="17.5" cy="6.5" r="1.15" fill="currentColor" stroke="none"/>',
}
TONES = {"green": ("#e3f6e8", "#1f8a3b"), "orange": ("#fff1e0", "#c26a00"), "red": ("#fde7e7", "#d1242f"),
         "gray": ("#efeff2", "#5f6368"), "blue": ("#e6f0ff", "#1a5fd1")}
APP_COLORS = ["#111111", "#d6249f", "#ff0033", "#1d9bf0", "#0a84ff", "#34c759"]


def icon(name, size=40, stroke=2.0, color="currentColor"):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')


def esc(s):
    return html.escape(str(s))


def chunk_captions(words, hidden, max_chars=22, max_words=4):
    """Captions in 2-4 word chunks. Words are first grouped into clauses (break at punctuation,
    pauses, take changes, hidden spans), then each clause is cut into the fewest, most even chunks -
    so you get 'Think about / what just happened.' rather than 'Think about what just / happened.'"""
    from itertools import combinations

    def is_hidden(w):
        m = (w["start"] + w["end"]) / 2
        return any(a <= m <= b for a, b in hidden)
    clauses, cur = [], []
    for w in words:
        if is_hidden(w):
            if cur:
                clauses.append(cur)
                cur = []
            continue
        if cur and (w["start"] - cur[-1]["end"] > 0.45 or w["take"] != cur[-1]["take"]):
            clauses.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.,!?;:]$", w["text"]):
            clauses.append(cur)
            cur = []
    if cur:
        clauses.append(cur)
    out = []
    txt = lambda ws: " ".join(x["text"] for x in ws)
    # a chunk may hold max_words words, or one more if it is still short ("If your AI app breaks,")
    fits = lambda pt: len(pt) <= max_words or (len(pt) == max_words + 1 and len(txt(pt)) <= max_chars)
    def cost(pt, last):
        ln = len(txt(pt))
        c = 8 + max(0, ln - 18) * 1.0 + max(0, 9 - ln) * 0.8      # every chunk costs; ideal is 9-18 chars
        first = norm(pt[0]["text"])
        if first in DETERMINERS:
            c -= 2                                                 # "| the distribution system"
        if first in PARTICLES:
            c += 3                                                 # not "figured | out the"
        if not last and norm(pt[-1]["text"]) in DANGLERS:
            c += 5
        if not last and re.fullmatch(r"[\d,.+$%]+", pt[-1]["text"]):
            c += 9                                                 # "750 | million"
        return c

    for cl in clauses:
        n = len(cl)
        k0 = max(1, math.ceil(n / (max_words + 1)))
        best = None
        for k in range(k0, min(n, k0 + 3) + 1):
            for cuts in combinations(range(1, n), k - 1):
                parts = [cl[a:b] for a, b in zip((0,) + cuts, cuts + (n,))]
                if not all(fits(pt) for pt in parts):
                    continue
                score = sum(cost(pt, i == len(parts) - 1) for i, pt in enumerate(parts)) \
                    - sum(2 for pt in parts[1:] if norm(pt[0]["text"]) in STARTERS) \
                    + sum(6 for pt in parts if len(pt) == 1 and n > 1)
                if best is None or score < best[0]:
                    best = (score, parts)
        out.extend(best[1] if best else [cl])
    return out


class Comp:
    def __init__(self, p, plan):
        self.p, self.plan = p, plan
        self.fps = plan["fps"]
        self.words = plan["words"]
        self.html, self.css, self.js = [], [], []
        self.uid = 0

    def q(self, t):
        return round(round(t * self.fps) / self.fps, 4)

    def span(self, phrase, after=0.0):
        i, j = find_phrase(self.words, phrase, after)
        return self.words[i]["start"], self.words[j]["end"]

    def tw(self, sel, frm, to, t, dur, ease="power2.out"):
        to = dict(to, duration=dur, ease=ease, immediateRender=False)
        self.js.append(f"tl.fromTo({json.dumps(sel)},{json.dumps(frm)},{json.dumps(to)},{self.q(t)});")

    def to(self, sel, to, t, dur, ease="power2.inOut"):
        to = dict(to, duration=dur, ease=ease)
        self.js.append(f"tl.to({json.dumps(sel)},{json.dumps(to)},{self.q(t)});")

    def set(self, sel, props, t):
        self.js.append(f"tl.set({json.dumps(sel)},{json.dumps(props)},{self.q(t)});")

    def next_id(self, prefix):
        self.uid += 1
        return f"{prefix}{self.uid}"


def card_header(h, cid, comp):
    pill = ""
    if h.get("pill"):
        pl = h["pill"]
        bg, fg = TONES[pl.get("tone", "gray")]
        pid = f"{cid}-pill"
        pill = f'<div class="pill" id="{pid}" style="background:{bg};color:{fg}">{esc(pl["text"])}</div>'
        if pl.get("at"):
            comp.pill_reveals.append((pid, pl["at"]))
    sub = f'<div class="h-sub">{esc(h["subtitle"])}</div>' if h.get("subtitle") else ""
    return (f'<div class="c-head"><div class="h-icon">{icon(h.get("icon", "bolt"), 30, 2.2)}</div>'
            f'<div class="h-txt"><div class="h-title">{esc(h["title"])}</div>{sub}</div>{pill}</div>')


def build_card(o, cid, comp, t0, t1):
    """Returns inner HTML of a card and schedules its internal animations."""
    kind = o.get("kind", "ui")
    comp.pill_reveals = []
    body = card_header(o.get("header", {"title": ""}), cid, comp)
    after = t0 - 0.5
    if kind == "ui":
        for r, row in enumerate(o.get("rows", [])):
            rid = f"{cid}-r{r}"
            if row.get("type") == "avatars":
                n_show = 7
                av = "".join(f'<div class="av" style="background:{APP_COLORS[(k + 2) % len(APP_COLORS)]};'
                             f'left:{k * 46}px">{chr(65 + (k * 7) % 26)}</div>' for k in range(n_show))
                more = f'<div class="av more" style="left:{n_show * 46}px">+{row["count"] - n_show}</div>'
                body += (f'<div class="row avrow" id="{rid}"><div class="row-in"><div class="avs">{av}{more}</div>'
                         f'<div class="r-val">{esc(row.get("label", ""))}</div></div></div>')
            else:
                val = f'<div class="r-val">{esc(row["value"])}</div>' if row.get("value") is not None else ""
                if row.get("check"):
                    val += f'<div class="r-check" id="{rid}-ck">{icon("check", 30, 2.6, "#1f8a3b")}</div>'
                elif row.get("cross"):
                    val += f'<div class="r-check bad" id="{rid}-ck">{icon("x", 26, 2.8, "#d1242f")}</div>'
                val = f'<div class="r-right">{val}</div>' if val else ""
                bar = ""
                if row.get("progress") is not None:
                    bar = f'<div class="bar"><div class="fill" id="{rid}-fill"></div></div>'
                body += (f'<div class="row" id="{rid}"><div class="row-in"><div class="r-main"><div class="r-label">'
                         f'<span class="strike" id="{rid}-st"></span>{esc(row["label"])}</div>{val}</div>{bar}</div></div>')
            at = comp.span(row["at"], after)[0] if row.get("at") else t0 + 0.25 + 0.25 * r
            at = max(at, t0 + 0.2)
            # rows start collapsed (height 0, spacing lives on the static .row-in), so the card grows as
            # each one is mentioned
            comp.tw(f"#{rid}", {"autoAlpha": 0, "height": 0, "filter": "blur(6px)"},
                    {"autoAlpha": 1, "height": "auto", "filter": "blur(0px)"}, at, 0.32, "power3.out")
            if row.get("progress") is not None:
                comp.tw(f"#{rid}-fill", {"width": "0%"}, {"width": f"{row['progress'] * 100:.0f}%"}, at + 0.1, 0.7, "power3.out")
            if row.get("check") or row.get("cross"):
                comp.tw(f"#{rid}-ck", {"scale": 0, "autoAlpha": 0}, {"scale": 1, "autoAlpha": 1}, at + 0.15, 0.3, "back.out(2)")
            if row.get("strike_at"):
                st = max(comp.span(row["strike_at"], after)[0], at + 0.2)
                comp.tw(f"#{rid}-st", {"width": "0%"}, {"width": "100%"}, st, 0.35, "power2.inOut")
                comp.to(f"#{rid}", {"opacity": 0.45}, st + 0.2, 0.3)
    elif kind == "stat":
        vid = f"{cid}-num"
        final = f"{o.get('prefix', '')}{o['value']:,}{o.get('suffix', '')}" if o.get("format", ",d") == ",d" \
            else f"{o.get('prefix', '')}{o['value']}{o.get('suffix', '')}"
        size = min(104, int((comp.SW - 68) / (0.62 * len(final))))     # the counted-up number must fit the card
        body += (f'<div class="stat"><div class="stat-num" id="{vid}" style="font-size:{size}px">0</div>'
                 f'<div class="stat-label">{esc(o.get("label", ""))}</div></div>')
        if o.get("spark", True):
            body += (f'<svg class="spark" viewBox="0 0 800 120" preserveAspectRatio="none">'
                     f'<path id="{cid}-sp" pathLength="1" d="M0 112 C120 108 190 100 260 92 S380 84 450 66 '
                     f'S560 50 620 34 S730 12 800 6" fill="none" stroke="#1f8a3b" stroke-width="5" stroke-linecap="round"/></svg>')
            comp.tw(f"#{cid}-sp", {"strokeDasharray": 1, "strokeDashoffset": 1}, {"strokeDashoffset": 0}, t0 + 0.3, 1.2, "power2.inOut")
        comp.countups.append((vid, o.get("from", 0), o["value"], t0 + 0.25, o.get("count_dur", 1.3), o.get("format", ",d"),
                              o.get("prefix", ""), o.get("suffix", "")))
    elif kind == "grid":
        vid = f"{cid}-num"
        tiles = ""
        for k in range(o.get("tiles", 6)):
            c1, c2 = [("#ff5f6d", "#ffc371"), ("#5b86e5", "#36d1dc"), ("#8e2de2", "#ff6a88"),
                      ("#11998e", "#38ef7d"), ("#f7971e", "#ffd200"), ("#ee0979", "#ff6a00")][k % 6]
            tiles += (f'<div class="tile" id="{cid}-t{k}" style="background:linear-gradient(160deg,{c1},{c2})">'
                      f'<div class="t-play">{icon("play", 34)}</div><div class="t-bar"></div><div class="t-bar s"></div></div>')
            comp.tw(f"#{cid}-t{k}", {"autoAlpha": 0, "scale": 0.8, "y": 16}, {"autoAlpha": 1, "scale": 1, "y": 0},
                    t0 + 0.2 + 0.09 * k, 0.35, "back.out(1.7)")
        body += (f'<div class="grid-top"><div class="stat-num sm" id="{vid}">0</div>'
                 f'<div class="stat-label">{esc(o.get("label", ""))}</div></div><div class="tiles">{tiles}</div>')
        comp.countups.append((vid, 0, o["value"], t0 + 0.2, o.get("count_dur", 1.4), o.get("format", ",d"), "", ""))
    elif kind == "network":
        n = o.get("nodes", 10)
        cx, cy, rx, ry = 400, 190, 330, 150
        lines, nodes = "", ""
        for k in range(n):
            ang = -math.pi / 2 + 2 * math.pi * k / n
            x, y = cx + rx * math.cos(ang), cy + ry * math.sin(ang)
            lines += f'<path id="{cid}-l{k}" pathLength="1" d="M{cx} {cy} L{x:.1f} {y:.1f}" stroke="#c7c7cc" stroke-width="3" fill="none"/>'
            col = APP_COLORS[k % len(APP_COLORS)]
            nodes += (f'<g class="nd" id="{cid}-n{k}">'
                      f'<circle cx="{x:.1f}" cy="{y:.1f}" r="27" fill="{col}"/>'
                      f'<path transform="translate({x - 10:.1f} {y - 11:.1f}) scale(0.9)" d="M8 5.5v13l11-6.5z" fill="#fff"/></g>')
            tt = t0 + 0.25 + 0.07 * k
            comp.tw(f"#{cid}-l{k}", {"strokeDasharray": 1, "strokeDashoffset": 1}, {"strokeDashoffset": 0}, tt, 0.4)
            comp.tw(f"#{cid}-n{k}", {"autoAlpha": 0, "scale": 0.3, "transformOrigin": "50% 50%"},
                    {"autoAlpha": 1, "scale": 1}, tt + 0.2, 0.35, "back.out(2)")
        center = (f'<g><rect x="{cx - 92}" y="{cy - 34}" width="184" height="68" rx="34" fill="#111"/>'
                  f'<text x="{cx}" y="{cy + 11}" text-anchor="middle" font-family="Inter" font-weight="700" '
                  f'font-size="30" fill="#fff">{esc(o.get("center", "Brand"))}</text></g>')
        body += f'<svg class="net" viewBox="0 0 800 380">{lines}{nodes}{center}</svg>'
    elif kind == "notifs":
        body = ""                           # notifications have no card chrome
        for k, it in enumerate(o["items"]):
            nid = f"{cid}-n{k}"
            col = it.get("color", APP_COLORS[k % len(APP_COLORS)])
            body += (f'<div class="notif" id="{nid}"><div class="n-icon" style="background:{col}">'
                     f'{icon(it.get("icon", "play"), 34, 2.2, "#fff")}</div><div class="n-txt">'
                     f'<div class="n-app">{esc(it["app"])}<span>now</span></div>'
                     f'<div class="n-title">{esc(it["title"])}</div><div class="n-body">{esc(it["body"])}</div></div></div>')
            at = comp.span(it["at"], after)[0] if it.get("at") else t0 + 0.6 * k
            comp.tw(f"#{nid}", {"autoAlpha": 0, "y": -40, "scale": 0.96, "filter": "blur(8px)"},
                    {"autoAlpha": 1, "y": 0, "scale": 1, "filter": "blur(0px)"}, max(at, t0), 0.4, "power3.out")
    for pid, phrase in comp.pill_reveals:
        at = comp.span(phrase, after)[0]
        comp.tw(f"#{pid}", {"autoAlpha": 0, "scale": 0.7}, {"autoAlpha": 1, "scale": 1}, at, 0.3, "back.out(2)")
    return body, kind


def ensure_gsap(dst):
    """GSAP ships under its own license, so it isn't bundled: use a local copy if there is one,
    otherwise download the pinned build once and cache it."""
    cache = Path.home() / ".cache" / "reel-kit" / "gsap.min.js"
    for src in (ASSETS / "vendor" / "gsap.min.js", cache):
        if src.exists():
            shutil.copy(src, dst)
            return
    import urllib.request
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"[compose] downloading GSAP once from {GSAP_URL}")
    urllib.request.urlretrieve(GSAP_URL, cache)
    shutil.copy(cache, dst)


def cmd_compose(p):
    plan = json.loads((p["_build"] / "plan.json").read_text(encoding="utf-8"))
    pub = p["_build"] / "public"
    (pub / "fonts").mkdir(parents=True, exist_ok=True)
    (pub / "vendor").mkdir(exist_ok=True)
    shutil.copy(ASSETS / "fonts" / "Inter-var-latin.woff2", pub / "fonts/")
    ensure_gsap(pub / "vendor" / "gsap.min.js")
    c = Comp(p, plan)
    c.countups = []
    D = plan["duration"]
    words = plan["words"]
    last_word_end = words[-1]["end"]

    # every position below is derived from the safe box (see load_project)
    sf = p["safe"]
    X0 = max(sf["left"], sf["side"]) + sf["buffer"]
    X1 = min(sf["right"], W - sf["side"]) - sf["buffer"]
    Y0 = sf["top"] + sf["buffer"]
    Y1 = sf["bottom"] - sf["buffer"]
    SW = X1 - X0
    c.SW = SW
    CAP_Y, CAP_Y_CARD, TITLE_Y = 1268, min(1490, Y1 - 85), 1268
    TALLY_X, TALLY_Y, TALLY_DY = X0, Y0 + 4, 38
    TITLE_X = X0 + 8
    CARD_TOP = 900
    WM_W = 260
    WM_SPOTS = [(X1 - WM_W, Y0 + 20, "flex-end"), (X0, 760, "flex-start")]   # right-upper, left-middle

    hidden, blackout, card_windows, tally_n = [], [], [], 0   # hidden: words never captioned; blackout: no captions on screen
    els = []
    # titles and cards share the screen centre: a title leaves before the next card, and a card
    # waits for a title that cannot leave sooner (its own words are still being said)
    card_starts = sorted(c.span(o["at"])[0] - 0.1 for o in p.get("overlays", []) if o["type"] == "card")
    last_title_exit = -1.0

    # ── titles, cards, CTA ──
    for o in p.get("overlays", []):
        typ = o["type"]
        if typ == "title":
            s, e = c.span(o["at"])
            tid = c.next_id("title")
            t0 = s - 0.12
            hold = max(e + 0.25, t0 + o.get("min_hold", 1.1))
            nxt_card = next((t for t in card_starts if t > t0), 1e9)
            hold = max(e + 0.05, min(hold, nxt_card - 0.6))
            last_title_exit = hold
            hidden.append((s - 0.02, e + 0.02))
            num = f'<span class="t-num">{esc(o["number"])}</span>' if o.get("number") else ""
            big = "" if o.get("number") else " big"              # no number -> the label is the hero
            ic = f'<span class="t-icon">{icon(o["icon"], 50 if num else 72, 2.3)}</span>' if o.get("icon") else ""
            label = re.sub(r"(\S+-\S+)", r'<span class="nw">\1</span>', esc(o["label"]))   # never break "co-founder"
            # a number-less title is sized to fit the safe width (≈0.56 em per character in Inter 600)
            fit = f' style="font-size:{min(92, int((X1 - TITLE_X - 94) / (0.56 * len(o["label"]))))}px"' if big else ""
            els.append(f'<div class="title{big}" id="{tid}">{num}{ic}<span class="t-label"{fit}>{label}</span></div>')
            c.tw(f"#{tid}", {"autoAlpha": 0, "x": -24, "filter": "blur(18px)"},
                 {"autoAlpha": 1, "x": 0, "filter": "blur(0px)"}, t0, 0.38, "power3.out")
            c.tw(f"#{tid} .t-label", {"autoAlpha": 0, "x": -16}, {"autoAlpha": 1, "x": 0}, t0 + 0.12, 0.35, "power3.out")
            blackout.append((t0, hold + 0.15))
            if o.get("tally"):
                ty = TALLY_Y + tally_n * TALLY_DY + 14
                c.to(f"#{tid}", {"x": TALLY_X - TITLE_X, "y": ty - TITLE_Y, "scale": 0.22}, hold, 0.55, "power3.inOut")
                c.to(f"#{tid}", {"autoAlpha": 0, "filter": "blur(4px)"}, hold + 0.3, 0.25, "power1.in")
                c.tally_add = getattr(c, "tally_add", []) + [(o["tally"], hold + 0.42)]
                tally_n += 1
            else:
                c.to(f"#{tid}", {"autoAlpha": 0, "filter": "blur(14px)"}, hold, 0.3, "power2.in")
        elif typ == "card":
            s, _ = c.span(o["at"])
            _, e = c.span(o["until"], s) if o.get("until") else (None, s + 3)
            cid = c.next_id("card")
            t0, t1 = max(s - 0.1, last_title_exit + 0.35), e + o.get("tail", 0.3)
            inner, kind = build_card(o, cid, c, t0, t1)
            cls = "card notifs" if kind == "notifs" else "card"
            els.append(f'<div class="{cls}" id="{cid}">{inner}</div>')
            c.tw(f"#{cid}", {"autoAlpha": 0, "scale": 0.94, "y": 24, "filter": "blur(16px)"},
                 {"autoAlpha": 1, "scale": 1, "y": 0, "filter": "blur(0px)"}, t0, 0.42, "power3.out")
            c.to(f"#{cid}", {"autoAlpha": 0, "scale": 0.97, "filter": "blur(14px)"}, t1 - 0.28, 0.28, "power2.in")
            card_windows.append((t0, t1))
            if o.get("tally"):
                c.tally_add = getattr(c, "tally_add", []) + [(o["tally"], t1 - 0.1)]
                tally_n += 1
        elif typ == "cta":
            s, e = c.span(o["at"])
            hidden.append((s - 0.02, e + 0.02))
            kw = o["keyword"]
            size = min(140, int((SW - 20) / (0.58 * (len(kw) + 2))))      # keyword + quotes fit the safe width
            els.append(f'<div id="cta"><div class="cta-small">{esc(o.get("verb", "comment"))}</div>'
                       f'<div class="cta-kw" style="font-size:{size}px">“{esc(kw)}”</div></div>')
            c.tw("#cta", {"autoAlpha": 0, "scale": 1.08, "filter": "blur(16px)"},
                 {"autoAlpha": 1, "scale": 1, "filter": "blur(0px)"}, s - 0.05, 0.45, "power3.out")
            c.cta_t = s - 0.05                       # the tally clears when the CTA lands

    # ── running tally (top-left) ──
    tally_html = ""
    for k, (txt, t) in enumerate(getattr(c, "tally_add", [])):
        m = re.match(r"^(\S+)\s+(.*)$", txt)
        inner = f"<b>{esc(m.group(1))}</b> {esc(m.group(2))}" if m and re.search(r"\d", m.group(1)) else esc(txt)
        tally_html += f'<div class="tl-item" id="tally{k}" style="top:{TALLY_Y + k * TALLY_DY}px">{inner}</div>'
        c.tw(f"#tally{k}", {"autoAlpha": 0, "x": -10, "filter": "blur(6px)"},
             {"autoAlpha": 0.95, "x": 0, "filter": "blur(0px)"}, t, 0.3)

    # ── captions ──
    end_card_t = last_word_end + 0.35
    chunks = chunk_captions(words, hidden)
    cap_html = []
    prev_e = -1.0
    for k, ch in enumerate(chunks):
        # chained: a caption pushed late by a title pushes the next one too, so none gets squeezed
        s = max(ch[0]["start"] - 0.04, prev_e)
        for a, b in blackout:                        # never share the screen with a title
            if a <= s < b:
                s = b
        nxt = chunks[k + 1][0]["start"] - 0.04 if k + 1 < len(chunks) else 1e9
        e = max(min(nxt, ch[-1]["end"] + 0.5), s + 0.35)
        for a, b in blackout:
            if s < a < e:
                e = a
        e = min(e, end_card_t)
        if e - s < 0.3:
            continue
        prev_e = e
        on_card = any(min(e, b) - max(s, a) > 0.1 for a, b in card_windows)   # any real overlap
        y = CAP_Y_CARD if on_card else CAP_Y
        text = " ".join(w["text"] for w in ch)
        cap_html.append(f'<div class="cap" id="cap{k}" style="top:{y}px">{esc(text)}</div>')
        # the blur-in must finish before the hide, or the tween's end state re-shows the caption
        c.tw(f"#cap{k}", {"autoAlpha": 0, "filter": "blur(10px)", "y": 8},
             {"autoAlpha": 1, "filter": "blur(0px)", "y": 0}, s, min(0.16, (e - s) * 0.4))
        c.set(f"#cap{k}", {"autoAlpha": 0}, e)

    # ── count-ups ──
    for vid, a, b, t, dur, fmt, pre, suf in c.countups:
        c.js.append(
            f"(function(){{var o={{v:{a}}};var el=document.getElementById('{vid}');"
            f"tl.to(o,{{v:{b},duration:{dur},ease:'power3.out',onUpdate:function(){{el.textContent='{pre}'+__fmt(o.v,'{fmt}')+'{suf}';}}}},{c.q(t)});}})();")

    # ── opening push-in, watermark hops ──
    segs = plan["segments"]
    first_cut = segs[1]["out_in"] if len(segs) > 1 else D
    c.tw("#video-wrap", {"scale": 1.0}, {"scale": 1.07}, 0, min(first_cut, 2.0), "power1.inOut")
    c.set("#video-wrap", {"scale": 1.0}, first_cut)
    c.tw("#wm", {"autoAlpha": 0}, {"autoAlpha": 1}, 0.15, 0.3)
    # watermark hops between a right-upper and a left spot every ~7 s (anti-crop, like the reference)
    # (the CTA takes over the top of the safe box, so the watermark bows out when it lands)
    wm_end = min(getattr(c, "cta_t", end_card_t), end_card_t) - 0.1
    t, spot = 7.0, 1
    while t < wm_end - 2:
        x, y, align = WM_SPOTS[spot]
        c.set("#wm", {"left": x, "top": y, "alignItems": align}, t)
        c.tw("#wm", {"autoAlpha": 0, "filter": "blur(8px)"}, {"autoAlpha": 1, "filter": "blur(0px)"}, t, 0.3)
        c.tw("#wm .ig-col", {"autoAlpha": 1}, {"autoAlpha": 0}, t + 0.1, 0.8, "power1.in")
        spot, t = 1 - spot, t + 7.0
    c.tw("#wm .ig-col", {"autoAlpha": 1}, {"autoAlpha": 0}, 0.4, 1.0, "power1.in")

    # ── end card ──
    c.tw("#wm", {"autoAlpha": 1}, {"autoAlpha": 0}, wm_end, 0.25)
    c.tw("#tally", {"autoAlpha": 1, "filter": "blur(0px)"}, {"autoAlpha": 0, "filter": "blur(8px)"},
         min(getattr(c, "cta_t", end_card_t), end_card_t) - 0.1, 0.3)
    c.tw("#dim", {"autoAlpha": 0}, {"autoAlpha": 1}, end_card_t, 0.55, "power2.inOut")
    c.tw("#endcard", {"autoAlpha": 0, "scale": 0.85}, {"autoAlpha": 1, "scale": 1}, end_card_t + 0.25, 0.5, "back.out(1.6)")
    c.tw("#endcard .ig-col", {"autoAlpha": 1}, {"autoAlpha": 0}, end_card_t + 1.6, 0.6, "power1.inOut")

    handle = esc(p["handle"].upper())
    ig_white = icon("ig", 52, 1.9, "#fff")
    ig_grad = ('<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="url(#igg{n})" stroke-width="{w}" '
               'stroke-linecap="round" stroke-linejoin="round"><defs><linearGradient id="igg{n}" x1="0" y1="1" x2="1" y2="0">'
               '<stop offset="0" stop-color="#feda75"/><stop offset=".35" stop-color="#fa7e1e"/><stop offset=".6" stop-color="#d62976"/>'
               '<stop offset=".85" stop-color="#962fbf"/><stop offset="1" stop-color="#4f5bd5"/></linearGradient></defs>'
               + ICONS["ig"].replace('fill="currentColor"', 'fill="#d62976"') + '</svg>')
    fps = plan["fps"]
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<style>
@font-face {{ font-family: "Inter"; src: url("fonts/Inter-var-latin.woff2") format("woff2"); font-weight: 100 900; font-display: block; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: {W}px; height: {H}px; overflow: hidden; background: #000; font-family: "Inter", sans-serif; }}
#stage {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; background: #000; }}
#video-wrap {{ position: absolute; inset: 0; transform-origin: 50% 34%; }}
#video-wrap video {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
#grad {{ position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(to top, rgba(0,0,0,.62) 0%, rgba(0,0,0,.32) 22%, rgba(0,0,0,0) 46%),
              linear-gradient(to bottom, rgba(0,0,0,.28) 0%, rgba(0,0,0,0) 14%); }}
.cap {{ position: absolute; left: {X0}px; width: {SW}px; text-align: center; color: #fff; font-weight: 600;
  font-size: 56px; letter-spacing: -0.012em; line-height: 1.15; transform: translateY(-50%);
  text-shadow: 0 2px 14px rgba(0,0,0,.55), 0 1px 3px rgba(0,0,0,.4); visibility: hidden; opacity: 0; }}
.title {{ position: absolute; left: {TITLE_X}px; top: {TITLE_Y - 90}px; height: 180px; max-width: {X1 - TITLE_X}px;
  display: flex; align-items: center; gap: 20px;
  color: #fff; transform-origin: 0% 50%; visibility: hidden; opacity: 0;
  text-shadow: 0 3px 18px rgba(0,0,0,.5); }}
.t-num {{ font-size: 144px; font-weight: 600; letter-spacing: -0.05em; line-height: 1; flex: none; }}
.t-icon {{ flex: none; }}
.t-icon svg {{ display: block; filter: drop-shadow(0 2px 8px rgba(0,0,0,.4)); }}
.t-label {{ font-size: 58px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.04; flex: 0 1 auto; min-width: 0; }}
.title.big .t-label {{ letter-spacing: -0.035em; }}
#tally {{ position: absolute; inset: 0; pointer-events: none; }}
.tl-item {{ position: absolute; left: {TALLY_X}px; color: #fff; font-size: 26px; font-weight: 500; letter-spacing: -0.005em;
  text-shadow: 0 1px 8px rgba(0,0,0,.6); visibility: hidden; opacity: 0; white-space: nowrap; max-width: {SW}px; overflow: hidden; }}
.tl-item b {{ font-weight: 700; }}
.card {{ position: absolute; left: {X0}px; top: {CARD_TOP}px; width: {SW}px; background: rgba(255,255,255,.97);
  border-radius: 34px; padding: 30px 34px 30px; box-shadow: 0 24px 70px rgba(0,0,0,.38), 0 2px 8px rgba(0,0,0,.18);
  color: #111; visibility: hidden; opacity: 0; transform-origin: 50% 50%; }}
.card.notifs {{ background: none; box-shadow: none; padding: 0; display: flex; flex-direction: column; gap: 16px; }}
.c-head {{ display: flex; align-items: center; gap: 18px; }}
.h-icon {{ width: 58px; height: 58px; border-radius: 15px; background: #111; color: #fff; display: grid; place-items: center; flex: none; }}
.h-txt {{ flex: 1; min-width: 0; }}
.h-title {{ font-size: 34px; font-weight: 650; letter-spacing: -0.02em; }}
.h-sub {{ font-size: 24px; color: #8e8e93; font-weight: 500; margin-top: 2px; }}
.pill {{ font-size: 23px; font-weight: 650; padding: 9px 18px; border-radius: 999px; white-space: nowrap; }}
.row {{ height: 0; overflow: hidden; opacity: 0; visibility: hidden; }}
.row-in {{ margin-top: 22px; padding-top: 22px; border-top: 2px solid #f0f0f3; }}
.nw {{ white-space: nowrap; }}
.r-main {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
.r-label {{ position: relative; font-size: 30px; font-weight: 550; letter-spacing: -0.01em; }}
.strike {{ position: absolute; left: -4px; top: 52%; height: 3px; width: 0; background: #d1242f; border-radius: 2px; }}
.r-val {{ font-size: 28px; color: #6b6b70; font-weight: 600; }}
.r-check {{ width: 46px; height: 46px; border-radius: 50%; background: #e3f6e8; display: grid; place-items: center; flex: none; }}
.r-check.bad {{ background: #fde7e7; }}
.r-right {{ display: flex; align-items: center; gap: 14px; flex: none; }}
.bar {{ margin-top: 14px; height: 10px; border-radius: 6px; background: #ececf0; overflow: hidden; }}
.fill {{ height: 100%; width: 0; background: #111; border-radius: 6px; }}
.avrow .row-in {{ display: flex; align-items: center; justify-content: space-between; }}
.avs {{ position: relative; height: 62px; width: 440px; }}
.av {{ position: absolute; top: 0; width: 62px; height: 62px; border-radius: 50%; border: 4px solid #fff; color: #fff;
  font-size: 24px; font-weight: 700; display: grid; place-items: center; }}
.av.more {{ background: #f0f0f3; color: #111; width: 86px; border-radius: 31px; font-size: 22px; }}
.stat {{ margin-top: 18px; }}
.stat-num {{ font-size: 104px; font-weight: 700; letter-spacing: -0.045em; line-height: 1; font-variant-numeric: tabular-nums; }}
.stat-num.sm {{ font-size: 84px; }}
.stat-label {{ font-size: 28px; color: #8e8e93; font-weight: 550; margin-top: 6px; }}
.spark {{ display: block; width: 100%; height: 110px; margin-top: 18px; }}
.grid-top {{ margin-top: 18px; }}
.tiles {{ display: flex; gap: 12px; margin-top: 22px; }}
.tile {{ position: relative; flex: 1; aspect-ratio: 9/16; border-radius: 18px; overflow: hidden; color: #fff; opacity: 0; visibility: hidden; }}
.t-play {{ position: absolute; inset: 0; display: grid; place-items: center; filter: drop-shadow(0 2px 6px rgba(0,0,0,.3)); }}
.t-bar {{ position: absolute; left: 12px; bottom: 30px; width: 70%; height: 8px; border-radius: 4px; background: rgba(255,255,255,.85); }}
.t-bar.s {{ bottom: 14px; width: 45%; background: rgba(255,255,255,.6); }}
.net {{ display: block; width: 100%; margin-top: 10px; }}
.net .nd {{ opacity: 0; visibility: hidden; }}
.notif {{ display: flex; gap: 20px; align-items: center; background: rgba(246,246,248,.96); border-radius: 30px; padding: 22px 26px;
  box-shadow: 0 16px 50px rgba(0,0,0,.35); opacity: 0; visibility: hidden; }}
.n-icon {{ width: 70px; height: 70px; border-radius: 17px; display: grid; place-items: center; flex: none; color: #fff; }}
.n-txt {{ flex: 1; min-width: 0; }}
.n-app {{ font-size: 21px; color: #8e8e93; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; display: flex; justify-content: space-between; }}
.n-app span {{ text-transform: none; letter-spacing: 0; font-weight: 500; }}
.n-title {{ font-size: 29px; font-weight: 650; letter-spacing: -0.015em; margin-top: 4px; }}
.n-body {{ font-size: 26px; color: #3a3a3c; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
#wm {{ position: absolute; left: {WM_SPOTS[0][0]}px; top: {WM_SPOTS[0][1]}px; width: {WM_W}px; display: flex; flex-direction: column; align-items: flex-end;
  gap: 8px; color: #fff; visibility: hidden; opacity: 0; }}
#wm .ig {{ position: relative; width: 52px; height: 52px; filter: drop-shadow(0 2px 6px rgba(0,0,0,.45)); }}
#wm .ig > div, #endcard .ig > div {{ position: absolute; inset: 0; }}
#wm .handle {{ font-size: 19px; font-weight: 750; letter-spacing: .05em; text-shadow: 0 1px 6px rgba(0,0,0,.6); }}
#cta {{ position: absolute; left: {X0}px; width: {SW}px; top: {Y0}px; text-align: center; color: #fff; visibility: hidden; opacity: 0;
  text-shadow: 0 4px 24px rgba(0,0,0,.45); }}
.cta-small {{ font-size: 44px; font-weight: 600; letter-spacing: -0.01em; }}
.cta-kw {{ font-weight: 750; letter-spacing: -0.035em; line-height: 1.05; margin-top: 2px; }}
#dim {{ position: absolute; inset: 0; background: rgba(0,0,0,.72); visibility: hidden; opacity: 0; }}
#endcard {{ position: absolute; left: {X0}px; top: 820px; width: {SW}px; display: flex; flex-direction: column; align-items: center; gap: 26px;
  color: #fff; visibility: hidden; opacity: 0; }}
#endcard .ig {{ position: relative; width: 150px; height: 150px; }}
#endcard .handle {{ font-size: 36px; font-weight: 750; letter-spacing: .05em; }}
</style></head>
<body>
<div id="stage" data-composition-id="reel" data-start="0" data-duration="{D}" data-fps="{fps}" data-width="{W}" data-height="{H}">
  <div id="video-wrap"><video id="v" src="edit.mp4" muted playsinline data-start="0" data-duration="{D}" data-track-index="1"></video></div>
  <audio id="a" src="edit.mp4" data-start="0" data-duration="{D}" data-track-index="10" data-volume="1"></audio>
  <div id="grad"></div>
  <div id="wm"><div class="ig"><div>{ig_white}</div><div class="ig-col">{ig_grad.format(s=52, w=1.9, n=1)}</div></div><div class="handle">{handle}</div></div>
  <div id="tally">{tally_html}</div>
  {''.join(els)}
  {''.join(cap_html)}
  <div id="dim"></div>
  <div id="endcard"><div class="ig"><div>{icon("ig", 150, 1.7, "#fff")}</div><div class="ig-col">{ig_grad.format(s=150, w=1.7, n=2)}</div></div><div class="handle">{handle}</div></div>
  <script src="vendor/gsap.min.js"></script>
  <script>
  (function () {{
    window.__fmt = function (v, f) {{
      if (/^\\.[0-9]+f$/.test(f)) return Number(v).toFixed(Number(f.slice(1, -1)));
      if (f === ",d") return Math.round(v).toLocaleString("en-US");
      return String(Math.round(v));
    }};
    var tl = gsap.timeline({{ paused: true }});
    {chr(10).join('    ' + j for j in c.js).strip()}
    window.__timelines = window.__timelines || {{}};
    window.__timelines["reel"] = tl;
  }})();
  </script>
</div>
</body></html>
"""
    # move the #cta element so it sits under the dim layer but above captions (already in els)
    (pub / "index.html").write_text(doc, encoding="utf-8")
    print(f"[compose] {pub / 'index.html'}  ({len(cap_html)} captions, {len(els)} overlays, {D:.2f}s)")


# ───────────────────────────── audit ─────────────────────────────

def cmd_audit(p, step=0.25):
    """Prove nothing lands in Instagram's no-go zones: snapshot the overlay layer on its own
    (video, gradient and dim hidden, black background) every `step` seconds, then look for bright
    pixels inside the zones. Returns True when clean; writes build/audit/violations.jpg otherwise."""
    import cv2
    plan = json.loads((p["_build"] / "plan.json").read_text(encoding="utf-8"))
    pub, aud = p["_build"] / "public", p["_build"] / "audit"
    if aud.exists():
        shutil.rmtree(aud)
    aud.mkdir()
    shutil.copytree(pub / "fonts", aud / "fonts")
    shutil.copytree(pub / "vendor", aud / "vendor")
    doc = (pub / "index.html").read_text(encoding="utf-8")
    doc = re.sub(r"<video[^>]*></video>|<audio[^>]*></audio>", "", doc)
    doc = doc.replace("</style>", "#grad, #dim { display: none !important; }\n</style>", 1)
    (aud / "index.html").write_text(doc, encoding="utf-8")
    times = [round(k * step, 2) for k in range(int(plan["duration"] / step) + 1)]
    snaps = aud / "snaps"
    npx = shutil.which("npx") or "npx"
    print(f"[audit] snapshotting the overlay layer at {len(times)} moments...")
    subprocess.run([npx, "-y", HF, "snapshot", str(aud), "--at", ",".join(f"{t:g}" for t in times),
                    "--no-end", "-o", str(snaps), "--describe", "false"], check=True, capture_output=True)
    sf = p["safe"]
    zones = {"right": (min(sf["right"], W - sf["side"]), 0, W, H), "bottom": (0, sf["bottom"], W, H),
             "top": (0, 0, W, sf["top"])}
    if max(sf["left"], sf["side"]) > 0:
        zones["left"] = (0, 0, max(sf["left"], sf["side"]), H)      # cropped off by the feed on tall phones
    bad, frames = [], sorted(snaps.glob("frame-*.png"))
    for f in frames:
        t = float(re.search(r"at-([\d.]+)s", f.name).group(1))
        img = cv2.imread(str(f))
        if img.shape[:2] != (H, W):
            img = cv2.resize(img, (W, H))
        bright = img.max(axis=2) > 90                 # text / cards / icons; soft shadows stay dark
        for name, (x0, y0, x1, y1) in zones.items():
            m = bright[y0:y1, x0:x1]
            if m.sum() > 40:
                ys, xs = np.nonzero(m)
                bad.append((t, name, int(m.sum()), (x0 + xs.min(), y0 + ys.min(), x0 + xs.max(), y0 + ys.max()), f))
    if len(frames) < len(times) * 0.9:
        raise SystemExit(f"[audit] only {len(frames)}/{len(times)} snapshots were produced - cannot vouch for the layout")
    if not bad:
        print(f"[audit] clean: nothing in the no-go zones across {len(frames)} frames")
        return True
    for t, name, n, box, _ in bad:
        print(f"[audit] {t:6.2f}s  {name:6} zone  {n:6d} px  bbox={box}")
    tiles = []
    for t, name, n, box, f in bad[:8]:
        img = cv2.imread(str(f))
        img = cv2.resize(img, (W, H)) if img.shape[:2] != (H, W) else img
        ov = img.copy()
        for x0, y0, x1, y1 in zones.values():
            cv2.rectangle(ov, (x0, y0), (x1, y1), (0, 215, 255), -1)
        img = cv2.addWeighted(ov, 0.35, img, 0.65, 0)
        cv2.rectangle(img, box[:2], box[2:], (0, 0, 255), 4)
        cv2.putText(img, f"{t:.2f}s {name}", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 255), 5)
        tiles.append(cv2.resize(img, (360, 640)))
    cv2.imwrite(str(aud / "violations.jpg"), np.hstack(tiles))
    print(f"[audit] {len(bad)} violation(s) - see {aud / 'violations.jpg'}")
    return False


# ───────────────────────────── render ─────────────────────────────

def cmd_render(p, quality="standard", workers=None):
    pub = p["_build"] / "public"
    out = p["_build"] / f"{p.get('name', 'reel')}.mp4"
    npx = shutil.which("npx") or "npx"
    cmd = [npx, "-y", HF, "render", str(pub), "-o", str(out), "-q", quality]
    if workers:
        cmd += ["-w", str(workers)]
    sh(cmd)
    print(f"[render] {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["transcribe", "plan", "edit", "voice", "compose", "audit", "render", "all"])
    ap.add_argument("project")
    ap.add_argument("--quality", default="standard")
    ap.add_argument("--workers", default=None)
    ap.add_argument("--skip-audit", action="store_true", help="render even if overlays touch the no-go zones")
    ap.add_argument("--samples", action="store_true",
                    help="voice: write a 15 s before/after sample per level to build/voice-samples/ instead of swapping")
    a = ap.parse_args()
    p = load_project(a.project)
    steps = ["transcribe", "plan", "edit", "compose", "render"] if a.step == "all" else [a.step]
    for s in steps:
        if s == "transcribe":
            cmd_transcribe(p)
        elif s == "plan":
            cmd_plan(p)
        elif s == "edit":
            cmd_edit(p)
        elif s == "voice" and a.samples:
            voice_samples(p)
        elif s == "voice":
            cmd_voice(p)
        elif s == "compose":
            cmd_compose(p)
        elif s == "audit":
            sys.exit(0 if cmd_audit(p) else 1)
        elif s == "render":
            # never ship a video with text under Instagram's buttons / caption / header
            if not a.skip_audit and not cmd_audit(p):
                raise SystemExit("[render] stopped: fix the overlays above (or pass --skip-audit)")
            cmd_render(p, a.quality, a.workers)


if __name__ == "__main__":
    main()
