"""Build the walkthrough from sections.py, the captured footage and the cards.

  video/.venv/bin/python video/build.py                 # draft: timed to a local scratch voice,
                                                        # exported without sound
  video/.venv/bin/python video/build.py --voice         # final: uses video/voice/<id>.(wav|m4a)
  video/.venv/bin/python video/build.py --url URL --repo URL   # links on the closing card
  video/.venv/bin/python video/build.py --acceptance out.txt    # real acceptance.py output

Each section lasts as long as its narration plus a short tail, so the picture is always timed
to the voice. In the footage only the wait for the model is shortened (to under a second);
everything shown happened in the app as shown. Writes narration.md, voice_cues.md and
storyboard.md from the same sections, with the measured timings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/Users/malek/.claude/skills/system-video-production/scripts")
from playwright.sync_api import sync_playwright  # noqa: E402
from speech import offline, speak  # noqa: E402
from vlib import FPS, H, SR, W, contact, dur, norm_vo, timeline, video_vo  # noqa: E402

from sections import SECTIONS  # noqa: E402

VIDEO = Path(__file__).resolve().parent
RAW, CLIPS, ASSETS = VIDEO / "clips" / "raw", VIDEO / "clips", VIDEO / "assets"
TAIL = 1.0         # seconds of picture after each narration ends
WAIT_KEEP = 0.8    # seconds of "Thinking..." kept from each model call
CRF = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-crf", "16", "-an"]


def ff(*args) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def render_cards(names: set, data: dict) -> dict:
    out = ASSETS / "cards"
    out.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
        ctx.add_init_script(f"window.DATA = {json.dumps(data)};")
        page = ctx.new_page()
        for name in sorted(names):
            page.goto(f"{(ASSETS / 'cards.html').as_uri()}?card={name}")
            page.wait_for_selector("body[data-ready='1']")
            page.wait_for_timeout(300)
            page.screenshot(path=str(out / f"{name}.png"))
        b.close()
    return {n: out / f"{n}.png" for n in names}


def beat_clip(name: str, beat: dict) -> Path:
    """The beat's real frames at their real timing, minus the dead time of each model call."""
    frames = json.loads((RAW / beat["session"] / "frames.json").read_text())
    cuts = [(s + WAIT_KEEP, r) for s, r in beat["waits"] if r - s > WAIT_KEEP]

    def shown(t: float) -> float:  # the time a viewer sees, with the cuts removed
        return t - sum(max(0.0, min(t, b) - a) for a, b in cuts)

    before = [f for f in frames if f[0] <= beat["start"]]
    inside = [f for f in frames if beat["start"] < f[0] <= beat["end"]]
    seq = ([[beat["start"], before[-1][1]]] if before else []) + inside
    times = [shown(t) for t, _ in seq] + [shown(beat["end"])]
    out = CLIPS / "beats" / f"{name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".txt")
    lines = [f"file '{RAW / beat['session'] / f}'\nduration {times[i + 1] - times[i]:.4f}"
             for i, (_, f) in enumerate(seq) if times[i + 1] - times[i] > 0.0005]
    listing.write_text("\n".join(lines) + f"\nfile '{RAW / beat['session'] / seq[-1][1]}'\n")
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-vf", f"fps={FPS},scale={W}:{H}", *CRF, str(out))
    return out


def hold(src: Path, seconds: float, out: Path, still: bool) -> Path:
    """`src` for exactly `seconds`: a card held, or footage whose last frame is held."""
    if still:
        ff("-loop", "1", "-framerate", str(FPS), "-t", f"{seconds:.3f}", "-i", str(src),
           "-vf", f"scale={W}:{H}:flags=lanczos", *CRF, "-t", f"{seconds:.3f}", str(out))
    else:
        ff("-i", str(src), "-vf", f"tpad=stop_mode=clone:stop_duration={seconds:.3f}",
           *CRF, "-t", f"{seconds:.3f}", str(out))
    return out


def voice_for(s: dict, use_voice: bool) -> Path:
    if use_voice:
        found = sorted((VIDEO / "voice").glob(f"{s['id']}.*"))
        if len(found) != 1:
            sys.exit(f"{s['id']}: need exactly one recording in video/voice/, found {[f.name for f in found]}")
        raw = found[0]
    else:
        assert offline("local")
        key = hashlib.sha1(s["text"].encode()).hexdigest()[:8]  # new text, new reading
        raw = VIDEO / "vo_scratch" / f"{s['id']}-{key}.wav"
        if not raw.exists():
            speak(s["text"], str(raw), provider="local")
    return Path(norm_vo(str(raw), str(raw.with_name(f"{s['id']}_n.wav"))))


def build_section(s: dict, vo: Path, cards: dict, beats: dict) -> object:
    need = dur(str(vo)) + TAIL
    parts, spent = [], 0.0
    for i, v in enumerate(s["visual"]):
        kind, name = v[0], v[1]
        src = cards[name] if kind == "card" else CLIPS / "beats" / f"{name}.mp4"
        natural = 0.0 if kind == "card" else dur(str(src))
        last = i == len(s["visual"]) - 1
        seconds = max(need - spent, natural, 3.0) if last else (v[2] if len(v) > 2 else natural)
        parts.append(hold(src, seconds, CLIPS / "parts" / f"{s['id']}_{i}.mp4", kind == "card"))
        spent += seconds
    joined = CLIPS / "parts" / f"{s['id']}.mp4"
    listing = joined.with_suffix(".txt")
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined))
    return video_vo(str(joined), str(vo), str(CLIPS / "segments" / f"{s['id']}.mp4"))


def assemble_clean(builts: list, out: Path, t: float = 0.5) -> Path:
    """vlib.assemble without its film finish: a vignette and grain darken a light UI and
    alter the real captures, so only crossfades, a fade in and out, and loudness are applied."""
    d = [dur(b.path) for b in builts]
    ins = [x for b in builts for x in ("-i", b.path)]
    parts, cur, acc = [], "[0:v]", d[0]
    for i in range(1, len(builts)):
        parts.append(f"{cur}[{i}:v]xfade=transition=fade:duration={t}:offset={acc - t:.3f}[v{i}]")
        cur, acc = f"[v{i}]", acc + d[i] - t
    parts.append(f"{cur}fade=t=in:st=0:d=0.8,fade=t=out:st={acc - 1.0:.3f}:d=1.0[vf]")
    cura = "[0:a]"
    for i in range(1, len(builts)):
        parts.append(f"{cura}[{i}:a]acrossfade=d={t}[a{i}]")
        cura = f"[a{i}]"
    parts.append(f"{cura}loudnorm=I=-16:TP=-1.5:LRA=11,aresample={SR}[af]")
    ff(*ins, "-filter_complex", ";".join(parts), "-map", "[vf]", "-map", "[af]", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-crf", "18", "-r", str(FPS), "-c:a", "aac", "-ar", str(SR),
       "-b:a", "192k", "-movflags", "+faststart", str(out))
    return out


def stamp(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def write_docs(builts: list, vos: dict, total: float) -> None:
    spans = timeline(builts)
    nar = ["# MenuMind walkthrough: narration\n",
           f"Total {stamp(total)}. Generated by `build.py` from `sections.py`; edit the text there.\n"]
    cues = ["# Voice cues\n", (
        "Record one file per section, named by its ID (`S01.wav`, `S02.m4a`, ...), into `video/voice/`.\n"
        "Read the text exactly as written; the picture is timed to your voice, so pace is yours.\n\n"
        "- Quiet room, soft furnishings; phone or Mac 20-30 cm from your mouth, the same place every take.\n"
        "- Half a second of silence before and after each section. No music, no effects.\n"
        "- Calm and plain, like explaining to a colleague. Breathe at the commas; don't rush the numbers.\n"
        "- A take with a stumble: record the section again over the same file. One file per ID.\n"
        "- Target length is the scratch reading at 160 words a minute; within about 20% either way is fine.\n")]
    board = ["# Storyboard\n", "Times from the current build. Captures are the real app on the live model; "
             "only each model wait is shortened to under a second.\n",
             "| ID | Time | Part | Section | On screen |", "|---|---|---|---|---|"]
    for s, (a, b) in zip(SECTIONS, spans):
        nar.append(f"## {s['id']} · {stamp(a)}–{stamp(b)} · {s['title']}\n\n{s['text']}\n")
        cues.append(f"## {s['id']} · {s['title']}\n\nFile `video/voice/{s['id']}.wav` · target "
                    f"~{dur(str(vos[s['id']])):.0f} s\n\n> {s['text']}\n")
        shown = " then ".join(f"{k}: {n}" for k, n, *_ in s["visual"])
        board.append(f"| {s['id']} | {stamp(a)} | {s['part']} | {s['title']} | {shown} |")
    (VIDEO / "narration.md").write_text("\n".join(nar))
    (VIDEO / "voice_cues.md").write_text("\n".join(cues))
    (VIDEO / "storyboard.md").write_text("\n".join(board) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", action="store_true", help="use the recordings in video/voice/")
    ap.add_argument("--url", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--acceptance", type=Path, help="output of acceptance.py against the real app")
    args = ap.parse_args()
    data = json.loads((ASSETS / "capture_data.json").read_text())
    data.update({k: v for k, v in (("url", args.url), ("repo", args.repo)) if v})
    if args.acceptance:
        data["acceptance"] = args.acceptance.read_text().strip()
    if data.get("preview") and args.voice:
        sys.exit("the footage is from --preview (scripted model); capture the live app first")
    beats = json.loads((RAW / "beats.json").read_text())
    for name, beat in beats.items():
        beat_clip(name, beat)
    for d in ("parts", "segments"):
        (CLIPS / d).mkdir(parents=True, exist_ok=True)
    cards = render_cards({n for s in SECTIONS for k, n, *_ in s["visual"] if k == "card"}, data)
    vos = {s["id"]: voice_for(s, args.voice) for s in SECTIONS}
    builts = [build_section(s, vos[s["id"]], cards, beats) for s in SECTIONS]
    master = assemble_clean(builts, CLIPS / "master.mp4")
    out = VIDEO / ("MenuMind_Walkthrough.mp4" if args.voice else "MenuMind_draft_no_voice.mp4")
    if args.voice:
        ff("-i", str(master), "-c", "copy", str(out))
    else:
        ff("-i", str(master), "-c:v", "copy", "-an", str(out))
    spans = timeline(builts)
    sheet = contact(str(out), [((a + b) / 2, s["id"]) for s, (a, b) in zip(SECTIONS, spans)],
                    str(CLIPS / "check"), cols=5)
    write_docs(builts, vos, dur(str(out)))
    print(f"{out} · {stamp(dur(str(out)))} · contact sheet {sheet}"
          + (" · FOOTAGE FROM --preview, NOT FOR SUBMISSION" if data.get("preview") else ""))


if __name__ == "__main__":
    main()
