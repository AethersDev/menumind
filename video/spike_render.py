"""Render "The Spike" (spike/index.html) to MenuMind_The_Spike.mp4, every frame in 3D.

    .venv/bin/python video/spike_trace.py          first, after any change to the app
    video/.venv/bin/python video/spike_render.py

1. The film's timeline is played back frame by frame and its state exported (film.json): the
   camera, how dark the pass is, the printer's light, where every piece of paper is.
2. Every piece of paper is cut from the page as a texture, once per state of its stamps.
3. Blender renders every frame from that state (spike3d.py, EEVEE on the Metal GPU). Frames
   already on disk are kept, so an interrupted run resumes; after spike_trace.py records new
   data, delete clips/spike3d.
4. The page's drawn graphics are captured on transparent plates and laid over each frame:
   "world" (her words, the slips, the menu, the threads), darkened as the film darkens the pass
   and hidden while the 3D camera is its own; then "screen" (captions and titles). The score is
   muxed last, so its cues land where they were composed.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

VIDEO = Path(__file__).resolve().parent
PAGE = (VIDEO / "spike" / "index.html").as_uri()
OUT3D = VIDEO / "clips" / "spike3d"
OUT = VIDEO / "MenuMind_The_Spike.mp4"
BLENDER = "/opt/homebrew/bin/blender"
W, H, FPS = 1920, 1080, 30


def ready(page, query: str = "") -> None:
    page.goto(PAGE + "?render=1" + query)
    page.wait_for_selector("body[data-ready='1']", timeout=60000)


def windows(cues: list) -> dict:
    """The stretches where the 3D camera is its own, timed from the film's lamp cues: the
    opening, until the lamp first flickers on; and "three days later", from the lamp going off
    to it flickering back."""
    on = [c["t"] for c in cues if c["type"] == "lamp_on"]
    off = [c["t"] for c in cues if c["type"] == "lamp_off"]
    if len(on) != 2 or len(off) != 1:
        sys.exit("expected two lamp_on cues and one lamp_off cue in the film")
    return {"open": on[0], "later": [off[0], on[1]]}


def own_camera(t: float, win: dict) -> float:
    """How much the 3D camera is its own at t (as spike3d.py blends it): the film's graphics only
    line up with the film's camera, so the world plate fades out by this much."""
    smooth = lambda x: (lambda c: c * c * (3 - 2 * c))(min(1.0, max(0.0, x)))  # noqa: E731
    start, flicker = win["later"]
    later = smooth((t - start) / 1.2) * (1 - smooth((t - flicker) / 0.9)) if start <= t else 0.0
    return 1.0 if t < win["open"] else later


def export(browser) -> tuple[Path, dict]:
    page = browser.new_page(viewport={"width": W, "height": H})
    ready(page)
    win = windows(page.evaluate("window.__film.cues"))
    frames = round(page.evaluate("window.__film.duration") * FPS)
    film = [page.evaluate("t => window.__film.frame(t)", i / FPS) for i in range(frames)]
    page.close()
    path = OUT3D / "film.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(film))
    return path, win


def textures(browser) -> Path:
    """Each piece of paper as printed, on a transparent background: once without stamps, then
    with each stamp in turn added (a torn half keeps the stamps it was torn with)."""
    out = OUT3D / "papers"
    out.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=3)
    ready(page)
    page.evaluate("window.__film.seek(window.__film.duration)")  # every stamp in its final state
    for p in page.evaluate("window.__film.papers()"):
        variants = [[True, True]] if p["kind"] == "half" else [[j < i for j in range(2)] for i in range(p["stamps"] + 1)]
        for i, shown in enumerate(variants):
            page.evaluate("([id, shown]) => window.__film.texture(id, shown)", [p["id"], shown])
            page.locator("#tex").screenshot(path=str(out / f"{p['id']}-{i}.png"), omit_background=True)
    page.close()
    return out


def render3d(film: Path, papers: Path, win: dict, frames: int) -> Path:
    out = OUT3D / "frames"
    if len(list(out.glob("*.jpg"))) < frames:
        subprocess.run([BLENDER, "--background", "--python-exit-code", "1", "--python", str(VIDEO / "spike3d.py"), "--", str(film),
                        str(papers), str(out), json.dumps(win)], check=True)
    return out


def composite(browser, film: list, frames3d: Path, win: dict) -> None:
    world, screen = (browser.new_page(viewport={"width": W, "height": H}) for _ in range(2))
    part = OUT.with_suffix(".part")  # becomes OUT only once every frame is encoded
    ready(world, "&plate=world")
    ready(screen, "&plate=screen")
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-i", str(VIDEO / "spike" / "menumind-score.mp3"),
         "-map", "0:v", "-map", "1:a", "-vf", "noise=alls=3:allf=t", "-c:v", "libx264", "-crf", "16",
         "-preset", "slow", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
         "-shortest", "-movflags", "+faststart", "-f", "mp4", str(part)], stdin=subprocess.PIPE)
    for i, f in enumerate(film):
        t = i / FPS
        img = Image.open(frames3d / f"{i:04d}.jpg").convert("RGBA")
        keep = (1 - f["dark"]) * (1 - own_camera(t, win))  # the pass is dark, or the camera is our own
        if keep > 0.004:
            world.evaluate("t => window.__film.seek(t)", t)
            layer = Image.open(io.BytesIO(world.screenshot(omit_background=True))).convert("RGBA")
            if keep < 0.996:
                layer.putalpha(layer.getchannel("A").point(lambda a: int(a * keep)))
            img.alpha_composite(layer)
        screen.evaluate("t => window.__film.seek(t)", t)
        img.alpha_composite(Image.open(io.BytesIO(screen.screenshot(omit_background=True))).convert("RGBA"))
        enc.stdin.write(img.convert("RGB").tobytes())
        if i % 300 == 0:
            print(f"film {t:6.1f}s / {len(film) / FPS:.1f}s", flush=True)
    enc.stdin.close()
    if enc.wait():
        sys.exit("ffmpeg failed")
    part.replace(OUT)


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        film_path, win = export(browser)
        papers = textures(browser)
        browser.close()
    film = json.loads(film_path.read_text())
    frames3d = render3d(film_path, papers, win, len(film))  # the browser is closed: 16 GB is shared
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        composite(browser, film, frames3d, win)
        browser.close()
    print(OUT, json.dumps(win))


if __name__ == "__main__":
    main()
