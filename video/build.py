"""Build the video: "The Spike" as the cold open, then the narrated walkthrough.

  video/.venv/bin/python video/build.py --url URL --repo URL --acceptance out.txt

Writes MenuMind_Walkthrough.mp4. The links go on the closing scene; out.txt is acceptance.py's
output against the real app, shown as it printed it (every check must pass).

Render the cold open first with spike_render.py. The narrator is a synthetic voice, Kokoro-82M
(af_heart), generated on this Mac with mlx-audio in video/.venv-tts; every section is then
transcribed back with Whisper and compared with the script, so a misread line stops the build. The walkthrough is a film page like the
Spike (walkthrough/index.html): a timeline laid out from sections.py and the narration's
measured length, seeked and captured frame by frame. App footage is the captured frames at
their real timing, except that each wait for the model is cut to under a second. The page
frames, zooms and points at them; it never alters what the app showed. The regions it points
at (the newest reply, the order ticket...) are found in the frames themselves, so a new
capture needs no hand edits. Writes narration.md and storyboard.md too.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

sys.path.insert(0, "/Users/malek/.claude/skills/system-video-production/scripts")
from vlib import FPS, H, SR, W, contact, dur, norm_vo  # noqa: E402

from sections import SECTIONS  # noqa: E402

VIDEO = Path(__file__).resolve().parent
sys.path.insert(0, str(VIDEO.parent))
from menu import HOUSE_RECIPES, INGREDIENTS, load_menu  # noqa: E402

RAW, CLIPS, ASSETS = VIDEO / "clips" / "raw", VIDEO / "clips", VIDEO / "assets"
SPIKE = VIDEO / "MenuMind_The_Spike.mp4"   # the cold open, from spike_render.py
PAGE = VIDEO / "walkthrough" / "index.html"
LEAD, TAIL = 0.3, 0.9   # seconds of quiet before and after each section's narration
END_HOLD = 3.0          # seconds the closing scene stays after the last word
WAIT_KEEP = 0.8         # seconds of "Thinking..." kept from each model call
SETTLE = 1.5            # seconds a section's last footage holds its last frame, so a late mark can be read
RECIPE_DISH = "M4"      # the menu-data scene: a dish whose sauce is a recipe inside a recipe
TTS = VIDEO / ".venv-tts" / "bin" / "python"   # mlx-audio, kept apart from the video tools
VOICE = ("mlx-community/Kokoro-82M-bf16", "af_heart", "a")   # model, voice, language
WHISPER = "mlx-community/whisper-large-v3-turbo-asr-fp16"
SAY = {"satay": "[satay](/sˈæteɪ/)"}   # how the narrator should say a word it gets wrong (heard() found it)


def ff(*args) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def narrate(s: dict) -> Path:
    """The section read by the narrator, cached by its text and voice, loudness-normalized."""
    spoken = re.sub(r"\b(" + "|".join(SAY) + r")\b", lambda m: SAY[m.group(1)], s["text"])
    key = hashlib.sha1(json.dumps([spoken, *VOICE]).encode()).hexdigest()[:8]  # new text, new reading
    folder = VIDEO / "vo"
    raw = folder / f"{s['id']}-{key}.wav"
    if not raw.exists():
        folder.mkdir(exist_ok=True)
        model, voice, lang = VOICE
        subprocess.run([str(TTS), "-m", "mlx_audio.tts.generate", "--model", model, "--voice", voice,
                        "--lang_code", lang, "--text", spoken, "--output_path", str(folder),
                        "--file_prefix", raw.stem, "--join_audio", "--audio_format", "wav"],
                       check=True, capture_output=True)
    return Path(norm_vo(str(raw), str(raw.with_name(f"{raw.stem}_n.wav"))))


NUMBER_WORDS = set("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                   "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy "
                   "eighty ninety hundred thousand".split())


def _words(text: str) -> list:
    """Words to compare: Whisper may write the script's spelled-out numbers as digits."""
    text = text.lower().replace("-", " ").replace("'", "").replace("\u2019", "")
    return [w for w in re.findall(r"[a-z]+", text) if w not in NUMBER_WORDS]


def heard(vos: dict) -> list:
    """Each section transcribed back, with how closely it matches the script (1.0 = word for
    word). Numbers are compared as the words the script spells out, so 196 is not a mismatch."""
    out = []
    for s in SECTIONS:
        folder = VIDEO / "vo"
        txt = vos[s["id"]].with_suffix(".heard")
        if not txt.with_suffix(".heard.txt").exists():
            subprocess.run([str(TTS), "-m", "mlx_audio.stt.generate", "--model", WHISPER, "--audio",
                            str(vos[s["id"]]), "--output-path", str(txt), "--format", "txt"],
                           check=True, capture_output=True, cwd=folder)
        said = txt.with_suffix(".heard.txt").read_text()
        ratio = difflib.SequenceMatcher(None, _words(s["text"]), _words(said)).ratio()
        out.append((s["id"], round(ratio, 3), said.strip()))
    return out


# ------------------------------------------------------------------ footage

def _runs(flags: list, min_len: int = 1) -> list:
    out, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    return out


def _lum(p) -> float:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def _contrast(path: Path) -> int:
    lo, hi = Image.open(path).convert("L").getextrema()
    return hi - lo


def _bbox(points: list, pad: int) -> list:
    return [min(x for x, _ in points) - pad, min(y for _, y in points) - pad,
            max(x for x, _ in points) + pad, max(y for _, y in points) + pad]


def regions(path: Path) -> dict:
    """Where things are in one captured frame, in its pixels, found by the app's own colors:
    a reply has a jade bar on its left, the order ticket is paper white, saved items have
    jade bullets and this conversation's amber ones, a waiting question is an amber box."""
    px = Image.open(path).convert("RGB").load()
    jade = lambda p: p[1] > p[0] + 40 and p[1] > p[2] + 10 and p[0] < 110  # noqa: E731
    amber = lambda p: p[0] > 190 and 120 < p[1] < 175 and p[2] < 90  # noqa: E731
    paper = lambda p: p[0] > 248 and p[1] > 247 and p[2] > 238  # noqa: E731
    soft = lambda p: p[0] > 238 and 215 < p[1] < 236 and 170 < p[2] < 205  # noqa: E731
    out = {}
    if _lum(px[60, 1000]) < 200:  # the menu is open: the page behind it is dimmed
        note = [(x, y) for y in range(80, 700, 2) for x in range(400, 1700, 4) if soft(px[x, y])]
        if note:
            out["note"] = _bbox(note, 6)
        return out
    bars = _runs([jade(px[291, y]) for y in range(H)], 12)
    if bars:
        y0, y1 = bars[-1]  # the newest reply
        out["reply"] = [296, y0 - 6, 1212, y1 + 6]
        out["reply_first"] = [296, y0 - 6, 1212, y0 + 32]
        rows = [min(_lum(px[x, y]) for x in range(300, 1205, 2)) for y in range(y0, y1 + 1)]
        lines = [(a + y0, min(rows[a:b + 1])) for a, b in _runs([r < 200 for r in rows])]
        # "How I read that" is set in the softer ink, and it ends the reply
        k = next((i for i in range(1, len(lines)) if all(m > 65 for _, m in lines[i:])), None)
        if k is not None:
            out["reply_trace"] = [296, lines[k][0] - 8, 1212, y1 + 6]
    col = _runs([paper(px[1626, y]) for y in range(H)], 60)
    if col:
        ty0, ty1 = max(col, key=lambda r: r[1] - r[0])
        out["ticket"] = [1250, ty0 - 4, 1642, ty1 + 4]
        band = lambda test: [y for y in range(ty0, ty1)  # noqa: E731
                             if any(test(px[x, y]) for x in range(1276, 1296))]
        saved, now = band(jade), band(amber)
        if saved:
            out["ticket_saved"] = [1256, min(saved) - 16, 1636, max(saved) + 16]
        if now:
            out["ticket_now"] = [1256, min(now) - 16, 1636, max(now) + 16]
        elif saved:  # "Nothing different from usual." under the saved items
            out["ticket_now"] = [1256, max(saved) + 20, 1636, ty1 - 10]
        waiting = [(x, y) for y in range(ty0, ty1, 2) for x in range(1262, 1630, 4) if soft(px[x, y])]
        if waiting:
            out["ticket_waiting"] = _bbox(waiting, 6)
    return out


def footage(beat: dict) -> dict:
    """A beat's frames on the shown clock (each model wait cut to WAIT_KEEP), the moments its
    replies land, and where things are on every frame once the app is on screen."""
    folder = RAW / beat["session"]
    frames = json.loads((folder / "frames.json").read_text())
    cuts = [(s + WAIT_KEEP, r) for s, r in beat["waits"] if r - s > WAIT_KEEP]

    def shown(t: float) -> float:
        return t - beat["start"] - sum(max(0.0, min(t, b) - a) for a, b in cuts)

    before = [f for f in frames if f[0] <= beat["start"]]
    inside = [f for f in frames if beat["start"] < f[0] <= beat["end"]]
    seq = ([[beat["start"], before[-1][1]]] if before else []) + inside
    length = shown(beat["end"])
    on = [(shown(t), f) for t, f in seq]
    # each reply lands as its model call returns; with no model call, at the end state
    lands = [shown(r) + 0.15 for _, r in beat["waits"]] or [max(0.0, length - 2.5)]
    # a page still loading is blank: the screen comes in with the first frame that shows the app
    blank = next((t for t, f in on if _contrast(folder / f) > 40), 0.0)
    return {"frames": [[round(t, 3), f"../clips/raw/{beat['session']}/{f}"] for t, f in on],
            "dur": round(length, 3), "lands": [round(t, 3) for t in lands], "blank": round(blank, 3),
            "states": [[round(t, 3), regions(folder / f)] for t, f in on if t >= blank]}


# ------------------------------------------------------------------ the film's data

def recipe(item_id: str) -> dict:
    """A dish down to its basic ingredients, with what each one carries (menu.py's data)."""
    def node(name: str) -> dict:
        if name in HOUSE_RECIPES:
            return {"name": name, "house": True, "parts": [node(p) for p in HOUSE_RECIPES[name]]}
        return {"name": name, "allergens": sorted(INGREDIENTS[name][1])}
    item = load_menu().get(item_id)
    return {"name": item.name, "allergens": sorted(item.allergens), "diet": item.diet,
            "parts": [node(i) for i in item.ingredients]}


def layout(vos: dict, beats: dict) -> list:
    """Each section lasts as long as its narration (or its footage, if longer); all visuals but
    the last take their share of the narration, and the last fills the rest."""
    out, t = [], 0.0
    for s in SECTIONS:
        need = LEAD + dur(str(vos[s["id"]])) + TAIL + (END_HOLD if s is SECTIONS[-1] else 0.0)
        visuals, spent = [], 0.0
        for i, v in enumerate(s["visual"]):
            natural = beats[v[1]]["dur"] if v[0] == "capture" else 0.0
            if i == len(s["visual"]) - 1:
                length = max(need - spent, natural + (SETTLE if natural else 0.0), 3.0)
            elif len(v) > 2 or natural:
                length = max(v[2] * need if len(v) > 2 else 0.0, natural)
            else:
                sys.exit(f"{s['id']}: {v[1]} needs a share of the narration (it is not the last visual)")
            visuals.append({"kind": v[0], "name": v[1], "t0": round(t + spent, 3), "dur": round(length, 3)})
            spent += length
        for cue, region, _ in s.get("marks", []):
            if cue not in s["text"]:
                sys.exit(f"{s['id']}: mark cue {cue!r} is not in the narration")
            if not any(region in st[1] for v in s["visual"] if v[0] == "capture" for st in beats[v[1]]["states"]):
                sys.exit(f"{s['id']}: {region!r} is not on screen in its footage; check the capture")
        out.append({"id": s["id"], "part": s["part"], "title": s["title"], "text": s["text"],
                    "t0": round(t, 3), "dur": round(spent, 3), "vo": round(dur(str(vos[s["id"]])), 3),
                    "visuals": visuals, "marks": s.get("marks", [])})
        t += spent
    return out


def write_data(sections: list, beats: dict, data: dict) -> None:
    trace = (VIDEO / "spike" / "trace.js").read_text()
    tests = re.search(r'"tests": "(\d+) passed', trace).group(1)  # recorded by spike_trace.py
    walk = {"fps": FPS, "lead": LEAD, "duration": round(sum(s["dur"] for s in sections), 3),
            "sections": sections, "beats": beats, "tests": int(tests),
            "menu": [i.public() for i in load_menu().items], "recipe": recipe(RECIPE_DISH), **data}
    (VIDEO / "walkthrough" / "data.js").write_text(f"window.WALK = {json.dumps(walk)};\n")


# ------------------------------------------------------------------ render

def render(total: float, out: Path) -> Path:
    """Every frame of the walkthrough page, seeked and captured, into a silent video."""
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-i", "-",
         "-c:v", "libx264", "-crf", "16", "-preset", "slow", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-an", str(out)], stdin=subprocess.PIPE)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(PAGE.as_uri())
        try:
            page.wait_for_selector("body[data-ready='1']", timeout=60000)
        except PlaywrightTimeout:
            sys.exit(f"the walkthrough page did not load: {errors or 'no error, it timed out'}")
        for i in range(round(total * FPS)):
            page.evaluate("t => window.__film.seek(t)", i / FPS)
            enc.stdin.write(page.screenshot(type="jpeg", quality=95))  # 5x faster than PNG, no visible loss
            if i % 600 == 0:
                print(f"walkthrough {i / FPS:6.1f}s / {total:.1f}s", flush=True)
        browser.close()
    enc.stdin.close()
    if enc.wait():
        sys.exit("ffmpeg failed while encoding the walkthrough")
    return out


def narration(sections: list, vos: dict, out: Path) -> Path:
    """One track: each section's voice after LEAD seconds, padded to the section's length."""
    parts = []
    for s in sections:
        part = CLIPS / "parts" / f"{s['id']}.wav"
        ff("-i", str(vos[s["id"]]), "-af", f"adelay={int(LEAD * 1000)}:all=1,apad",
           "-t", f"{s['dur']:.3f}", "-ar", str(SR), "-ac", "2", str(part))
        parts.append(part)
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out))
    return out


def join(walk: Path, voice: Path, out: Path, t: float = 0.5) -> Path:
    """The cold open, a short crossfade, then the walkthrough with its narration."""
    d = dur(str(SPIKE))
    graph = (f"[0:v][1:v]xfade=transition=fade:duration={t}:offset={d - t:.3f}[v];"
             f"[0:a][2:a]acrossfade=d={t},loudnorm=I=-16:TP=-1.5:LRA=11,aresample={SR}[a]")
    ff("-i", str(SPIKE), "-i", str(walk), "-i", str(voice), "-filter_complex", graph, "-map", "[v]",
       "-map", "[a]", "-c:a", "aac", "-ar", str(SR), "-b:a", "192k", "-c:v", "libx264", "-crf", "17",
       "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart", str(out))
    return out


# ------------------------------------------------------------------ docs

def stamp(t: float) -> str:
    t = round(t, 1)  # first, so 59.96 is 1:00.0, not 0:60.0
    return f"{int(t // 60)}:{t % 60:04.1f}"


def write_docs(sections: list, offset: float, total: float) -> None:
    nar = ["# MenuMind walkthrough: narration\n",
           f"Total {stamp(total)}, of which the cold open (The Spike) is {stamp(offset)}. Read by a "
           f"synthetic voice ({VOICE[1]}, Kokoro-82M). Generated by `build.py` from `sections.py`; "
           "edit the text there.\n"]
    board = ["# Storyboard\n", "Times from the current build. Captures are the real app on the live model; "
             "only each model wait is shortened to under a second.\n",
             "| ID | Time | Part | Section | On screen |", "|---|---|---|---|---|",
             "| Spike | 0:00.0 | Cold open | The Spike | spike/index.html, rendered by spike_render.py |"]
    for s in sections:
        a = offset + s["t0"]
        nar.append(f"## {s['id']} · {stamp(a)}–{stamp(a + s['dur'])} · {s['title']}\n\n{s['text']}\n")
        shown = " then ".join(f"{v['kind']}: {v['name']}" for v in s["visuals"])
        board.append(f"| {s['id']} | {stamp(a)} | {s['part']} | {s['title']} | {shown} |")
    (VIDEO / "narration.md").write_text("\n".join(nar))
    (VIDEO / "storyboard.md").write_text("\n".join(board) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="the live app")
    ap.add_argument("--repo", required=True, help="the repository")
    ap.add_argument("--acceptance", type=Path, required=True, help="output of acceptance.py against the real app")
    args = ap.parse_args()
    checks = [ln for ln in args.acceptance.read_text().splitlines() if re.match(r"(PASS|FAIL)\s", ln)]
    if not checks or any(ln.startswith("FAIL") for ln in checks):
        sys.exit(f"{args.acceptance}: not a passing acceptance.py run (it needs PASS lines and no FAIL)")
    if not SPIKE.exists():
        sys.exit(f"{SPIKE.name} is missing: render the cold open with video/spike_render.py first")
    data = json.loads((ASSETS / "capture_data.json").read_text())
    if data.get("preview"):
        sys.exit("the footage is from --preview (scripted model); capture the live app first")
    data.update(url=args.url, repo=args.repo, acceptance="\n".join(checks))
    (CLIPS / "parts").mkdir(parents=True, exist_ok=True)
    beats = {name: footage(b) for name, b in json.loads((RAW / "beats.json").read_text()).items()}
    vos = {s["id"]: narrate(s) for s in SECTIONS}
    misread = [(sid, r, said) for sid, r, said in heard(vos) if r < 0.9]
    if misread:
        sys.exit("the narrator misread a section; reword it in sections.py:\n"
                 + "\n".join(f"  {sid} ({r}): heard {said!r}" for sid, r, said in misread))
    sections = layout(vos, beats)
    write_data(sections, beats, data)
    total = sum(s["dur"] for s in sections)
    walk = render(total, CLIPS / "walkthrough.mp4")
    voice = narration(sections, vos, CLIPS / "narration.wav")
    out = VIDEO / "MenuMind_Walkthrough.mp4"
    join(walk, voice, out)
    offset = dur(str(SPIKE)) - 0.5
    sheet = contact(str(out), [(9.0, "Spike")] + [(offset + s["t0"] + s["dur"] / 2, s["id"]) for s in sections],
                    str(CLIPS / "check"), cols=5)
    write_docs(sections, offset, dur(str(out)))
    print(f"{out} · {stamp(dur(str(out)))}, walkthrough {stamp(total)}"
          + (" · OVER THE 6:00 LIMIT" if total > 360 else "") + f" · contact sheet {sheet}")


if __name__ == "__main__":
    main()
