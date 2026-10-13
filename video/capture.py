"""Film the real MenuMind UI for the walkthrough.

Starts the app on a fresh database, drives Chrome with Playwright and records every repaint
with Chrome's screencast (crisp frames, real typing and real replies). Each beat checks that
the app did what the narration will say; if one fails, the whole session is retaken on a new
database, up to 3 times, so no footage has to be faked or hand-edited to look right.

  export OPENAI_API_KEY="$(security find-generic-password -s openai-api-key -w)"
  video/.venv/bin/python video/capture.py               # the real model (spends tokens)
  video/.venv/bin/python video/capture.py --preview     # scripted model: checks mechanics only

Writes video/clips/raw/ (frames + timings) and video/assets/capture_data.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "video" / "clips" / "raw"
DATA = ROOT / "video" / "assets" / "capture_data.json"
PORT = 8790
URL = f"http://127.0.0.1:{PORT}"
ZOOM = "1.25"     # the layout at 1536 x 864 CSS pixels, rendered at a full 1920 x 1080
ATTEMPTS = 3


class Retake(Exception):
    """A beat did not show what the narration says."""


class Recorder:
    """Chrome screencast of one page: a JPEG and a wall-clock timestamp for every repaint."""

    def __init__(self, ctx, page, name: str):
        self.name, self.frames = name, []
        self.dir = RAW / name
        self.dir.mkdir(parents=True)
        self.cdp = ctx.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._frame)
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 95,
                                               "maxWidth": 1920, "maxHeight": 1080})

    def _frame(self, p: dict) -> None:
        path = self.dir / f"{len(self.frames):06d}.jpg"
        path.write_bytes(base64.b64decode(p["data"]))
        self.frames.append([p["metadata"]["timestamp"], path.name])
        self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})

    def stop(self) -> None:
        self.cdp.send("Page.stopScreencast")
        (self.dir / "frames.json").write_text(json.dumps(self.frames))


class Shoot:
    def __init__(self, strict: bool):
        self.strict, self.beats, self.notes = strict, {}, []

    def check(self, beat: str, ok: bool, what: str) -> None:
        self.notes.append(f"{'ok  ' if ok else 'MISS'} {beat}: {what}")
        if not ok and self.strict:
            raise Retake(f"{beat}: {what}")

    def start(self, name: str, session: str) -> dict:
        print(f"  filming {name} ...", flush=True)  # a live run takes minutes; show it is working
        self.beats[name] = b = {"session": session, "start": time.time(), "waits": []}
        return b

    def end(self, name: str) -> None:
        self.beats[name]["end"] = time.time()


def say(page, beat: dict, text: str) -> dict:
    """Type like a person, send, and return the API's answer for checking."""
    page.click("#message")
    page.wait_for_timeout(300)
    page.type("#message", text, delay=45)
    page.wait_for_timeout(400)
    with page.expect_response(lambda r: r.url.endswith("/api/chat"), timeout=120_000) as resp:
        sent = time.time()
        page.keyboard.press("Enter")
    beat["waits"].append([sent, time.time()])  # trimmed in the edit, not hidden in the story
    if resp.value.status != 200:
        raise Retake(f"chat returned {resp.value.status}: {resp.value.text()[:200]}")
    page.wait_for_function("!document.querySelector('.msg.typing')")
    page.wait_for_timeout(1800)  # the reply scrolls in and the ticket settles
    return resp.value.json()


def picks(r: dict) -> list:
    return [m["id"] for m in r["mentioned"] if m["kind"] == "rec"]


def sign_up(page, name: str) -> None:
    page.goto(URL)
    page.wait_for_selector("#auth:not([hidden])")
    page.wait_for_timeout(900)
    page.type("#username", name, delay=90)
    page.type("#pin", "4826", delay=110)
    page.wait_for_timeout(500)
    page.click("button[data-action=register]")
    page.wait_for_selector("#app:not([hidden])")
    page.wait_for_function("document.querySelectorAll('.msg.agent').length >= 1")
    page.wait_for_timeout(2200)


def session(pw, shoot: Shoot, menu: dict, db: Path) -> dict:
    browser = pw.chromium.launch()
    data = {}
    try:
        def new_page(name):
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
            ctx.add_init_script(f"document.addEventListener('DOMContentLoaded', "
                                f"() => document.documentElement.style.zoom = '{ZOOM}')")
            page = ctx.new_page()
            return ctx, page, Recorder(ctx, page, name)

        ctx, page, rec = new_page("maya")
        b = shoot.start("signup", "maya")
        sign_up(page, "maya")
        shoot.end("signup")

        b = shoot.start("spicy", "maya")
        r = say(page, b, "I love spicy food, but I hate mushrooms.")
        shoot.check("spicy", {"spice: hot", "dislikes mushroom"} <= set(r["changes"]), f"changes {r['changes']}")
        shoot.check("spicy", bool(picks(r)), f"recommended {picks(r)}")
        page.locator(".msg.agent details.trace summary").last.click()
        page.wait_for_timeout(3500)
        shoot.end("spicy")

        b = shoot.start("allergy", "maya")
        r = say(page, b, "I'm allergic to peanuts. What main would you suggest?")
        shoot.check("allergy", "avoiding peanut (allergy)" in r["changes"], f"changes {r['changes']}")
        shoot.check("allergy", bool(picks(r)) and all(menu[i]["category"] == "main" for i in picks(r)),
                    f"recommended {picks(r)}")
        shoot.check("allergy", all("peanut" not in menu[i]["allergens"] for i in picks(r)), "no peanut")
        shoot.end("allergy")

        b = shoot.start("question", "maya")
        r = say(page, b, "Does the pad thai have peanuts?")
        shoot.check("question", r["changes"] == [], f"changes {r['changes']}")
        shoot.check("question", "not suitable for you" in r["reply"], "reply shows the app's warning")
        shoot.end("question")

        b = shoot.start("hedged", "maya")
        r = say(page, b, "I think I might be allergic to shellfish.")
        shoot.check("hedged", "avoiding shellfish (allergy) (this conversation)" in r["changes"],
                    f"changes {r['changes']}")
        shoot.check("hedged", "shellfish" in (r["question"] or ""), f"question {r['question']!r}")
        r = say(page, b, "No, just for tonight.")
        shoot.check("hedged", r["question"] is None, f"question {r['question']!r}")
        shoot.check("hedged", not any("shellfish" in x for x in r["profile"]["remembered"]), "not saved")
        shoot.check("hedged", any("shellfish" in x for x in r["profile"]["this_conversation"]),
                    f"tonight {r['profile']['this_conversation']}")
        shoot.end("hedged")

        b = shoot.start("tonight", "maya")
        r = say(page, b, "Nothing spicy tonight, and keep it under $15.")
        tonight = r["profile"]["this_conversation"]
        shoot.check("tonight", {"spice: none (this conversation)", "budget up to $15.00 (this conversation)"}
                    <= set(r["changes"]), f"changes {r['changes']}")
        shoot.check("tonight", {"Spice: none (saved: hot)", "Budget: up to $15.00 (saved: not set)"} <= set(tonight),
                    f"ticket {tonight}")
        shoot.end("tonight")

        b = shoot.start("return", "maya")
        page.wait_for_timeout(600)
        page.click("#logout")
        page.wait_for_selector("#auth:not([hidden])")
        page.wait_for_timeout(1200)
        page.fill("#username", "")
        page.type("#username", "maya", delay=90)
        page.type("#pin", "4826", delay=110)
        page.wait_for_timeout(400)
        page.click("button[data-action=login]")
        page.wait_for_function("document.querySelector('#log').textContent.includes('Welcome back')")
        page.wait_for_timeout(2500)
        page.click("#new-conv")
        page.wait_for_function("document.querySelector('#this-visit').textContent.includes('Nothing different')")
        page.wait_for_timeout(4000)
        saved = page.inner_text("#remembered")
        shoot.check("return", all(x in saved for x in ("Allergies: peanut", "Spice: hot", "Dislikes: mushroom")),
                    f"ticket {saved!r}")
        shoot.check("return", "shellfish" not in saved and "Budget" not in saved, "tonight's changes not saved")
        shoot.end("return")

        b = shoot.start("remove_ask", "maya")
        r = say(page, b, "I'm no longer allergic to peanuts.")
        shoot.check("remove_ask", r["question"] is not None, f"question {r['question']!r}")
        shoot.check("remove_ask", "Allergies: peanut" in r["profile"]["remembered"], "allergy kept until yes")
        shoot.check("remove_ask", r["reply"].endswith(f"**{r['question']}**"), "the reply ends with the app's question")
        shoot.check("remove_ask", [(c["attribute"], c["polarity"], c["subject"]) for c in r["trace"]["claims"]]
                    == [("allergen", "negate", "peanut")], f"claims {r['trace']['claims']}")
        shoot.check("remove_ask", [d["rule"] for d in r["trace"]["decisions"]] == ["R10"],
                    f"decisions {r['trace']['decisions']}")  # the deep-dive cards show this turn
        page.wait_for_timeout(1500)
        shoot.end("remove_ask")
        data["removal_turn"] = {"say": "I'm no longer allergic to peanuts.", "claims": r["trace"]["claims"],
                                "decisions": r["trace"]["decisions"], "question": r["question"]}

        b = shoot.start("remove_yes", "maya")
        r = say(page, b, "yes")
        shoot.check("remove_yes", all(not x.startswith("Allergies") for x in r["profile"]["remembered"]),
                    f"ticket {r['profile']['remembered']}")
        shoot.check("remove_yes", "Dislikes: mushroom" in r["profile"]["remembered"], "rest of memory kept")
        page.wait_for_timeout(1500)
        shoot.end("remove_yes")
        rec.stop()
        ctx.close()

        ctx, page, rec = new_page("sam")
        b = shoot.start("sam", "sam")
        sign_up(page, "sam")
        r = say(page, b, "What do you recommend?")
        shoot.check("sam", r["profile"] == {"remembered": [], "this_conversation": []}, f"ticket {r['profile']}")
        shoot.end("sam")

        b = shoot.start("menu_note", "sam")
        page.click("#open-menu")
        page.wait_for_selector("dialog#menu[open]")
        page.wait_for_timeout(3500)
        shoot.check("menu_note", "Cross-contact" in page.inner_text("dialog#menu"), "allergen note shown")
        shoot.end("menu_note")
        rec.stop()
        ctx.close()
    finally:
        browser.close()

    with sqlite3.connect(db) as con:  # the real event log behind Maya's ticket
        (uid,) = con.execute("SELECT user_id FROM users WHERE username = 'maya'").fetchone()
        rows = con.execute("SELECT sequence_number, attribute, subject, value, operation, scope, source, "
                           "evidence FROM events WHERE user_id = ? ORDER BY sequence_number", (uid,)).fetchall()
    data["events"] = [dict(zip(("seq", "attribute", "subject", "value", "operation", "scope", "source",
                                "evidence"), row)) for row in rows]
    return data


def serve(preview: bool, db: Path) -> subprocess.Popen:
    env = {**os.environ, "DB_PATH": str(db), "PORT": str(PORT)}
    cmd = ([str(ROOT / ".venv/bin/python"), "preview.py"] if preview else
           [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "server:app", "--port", str(PORT)])
    srv = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"{URL}/api/health", timeout=1).status_code == 200:
                return srv
        except httpx.TransportError:
            time.sleep(0.25)
    srv.terminate()
    sys.exit("the app did not start")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="scripted model; checks are reported, not enforced")
    args = ap.parse_args()
    if not args.preview and not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first (see the usage at the top of this file).")
    for attempt in range(1, ATTEMPTS + 1):
        shutil.rmtree(RAW, ignore_errors=True)
        RAW.mkdir(parents=True)
        db = RAW / "capture.db"
        srv = serve(args.preview, db)
        shoot = Shoot(strict=not args.preview)
        try:
            menu = {i["id"]: i for i in httpx.get(f"{URL}/api/menu").json()["items"]}
            with sync_playwright() as pw:
                data = session(pw, shoot, menu, db)
        except Retake as why:
            print(f"attempt {attempt}: retake ({why})")
            continue
        finally:
            srv.terminate()
            srv.wait()
            print("\n".join(shoot.notes))
        (RAW / "beats.json").write_text(json.dumps(shoot.beats, indent=1))
        DATA.write_text(json.dumps({**data, "preview": args.preview, "notes": shoot.notes}, indent=1))
        print(f"\ncaptured on attempt {attempt}: {RAW}")
        return
    sys.exit(f"no clean take in {ATTEMPTS} attempts; the notes above say which beat failed")


if __name__ == "__main__":
    main()
