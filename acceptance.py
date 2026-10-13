"""End-to-end acceptance test over HTTP, against a running server and its real model.

  python acceptance.py --url https://your-app.example.com    # restart the service when asked
  python acceptance.py --url http://127.0.0.1:8000 --no-restart

The workflow: customer A says they are allergic to peanuts and dislike mushrooms, and gets
peanut-free recommendations; a new conversation still has the allergy; the server restarts
and the allergy is still saved; A says the allergy is gone, the app asks first and keeps
avoiding peanuts until A answers yes; then the allergy is removed and the dislike remains.
Customer B signs in, sees none of A's memory and cannot open A's conversation.

Each check prints PASS or FAIL; the exit status is 1 if any failed. It creates two accounts
with random names and spends model tokens.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import time

import httpx

PIN = "4826"
EMPTY = {"remembered": [], "this_conversation": []}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--no-restart", dest="restart", action="store_false",
                    help="skip the restart check (it waits for the server to restart)")
    args = ap.parse_args()
    base, tag, results = args.url.rstrip("/"), secrets.token_hex(3), []

    def check(name: str, ok: bool, detail="") -> None:
        results.append(ok)
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n      got: {detail}"))

    def customer(name: str) -> httpx.Client:
        c = httpx.Client(base_url=base, timeout=90)
        c.post("/api/register", json={"username": f"accept-{name}-{tag}", "pin": PIN}).raise_for_status()
        return c

    def conversation(c: httpx.Client) -> str:
        return c.post("/api/conversations").raise_for_status().json()["conversation_id"]

    def say(c: httpx.Client, cid: str, text: str) -> dict:
        return c.post("/api/chat", json={"conversation_id": cid, "message": text}).raise_for_status().json()

    menu = {i["id"]: i for i in httpx.get(f"{base}/api/menu").raise_for_status().json()["items"]}

    def picks(r: dict) -> list:
        return [m["id"] for m in r["mentioned"] if m["kind"] == "rec"]

    def peanut_free(r: dict) -> bool:
        return all("peanut" not in menu[i]["allergens"] for i in picks(r))

    def has_allergy(profile: dict) -> bool:
        return "Allergies: peanut" in profile["remembered"]

    a = customer("a")
    cid = conversation(a)
    r = say(a, cid, "I'm allergic to peanuts.")
    check("A's peanut allergy is saved", has_allergy(r["profile"]), r["profile"])
    r = say(a, cid, "I love spicy food, but I hate mushrooms.")
    check("A's dislike of mushrooms is saved", "Dislikes: mushroom" in r["profile"]["remembered"],
          r["profile"])
    r = say(a, cid, "What do you recommend?")
    check("A gets recommendations", bool(picks(r)), r["reply"])
    check("none of them contains peanut", peanut_free(r), picks(r))

    cid = conversation(a)
    me = a.get("/api/me", params={"conversation_id": cid}).raise_for_status().json()
    check("a new conversation still has the allergy", has_allergy(me["profile"]), me["profile"])

    if args.restart:
        started = httpx.get(f"{base}/api/health").raise_for_status().json()["started_at"]
        print("\nRestart the server now. Waiting for it to come back ...", flush=True)
        while True:
            time.sleep(2)
            try:
                if httpx.get(f"{base}/api/health", timeout=5).json()["started_at"] != started:
                    break
            except httpx.TransportError:  # down while it restarts: keep waiting
                continue
        check("A is still signed in after the restart", a.get("/api/me").status_code == 200)
        if not results[-1]:
            sys.exit("Stopping: the sign-in did not survive the restart. Is the database on a "
                     "persistent disk (DB_PATH)?")
        cid = conversation(a)  # conversations end with the process; saved memory must not
        me = a.get("/api/me", params={"conversation_id": cid}).raise_for_status().json()
        check("the allergy survived the restart", has_allergy(me["profile"]), me["profile"])

    r = say(a, cid, "I'm no longer allergic to peanuts.")
    check("removing the allergy asks for confirmation", r["question"] is not None, r["reply"])
    check("the allergy stays until confirmed", has_allergy(r["profile"]), r["profile"])
    check("recommendations still avoid peanut", peanut_free(r), picks(r))
    r = say(a, cid, "yes")
    check("after yes the allergy is removed", not has_allergy(r["profile"]), r["profile"])
    check("the rest of A's memory is kept", "Dislikes: mushroom" in r["profile"]["remembered"],
          r["profile"])

    b = customer("b")
    me = b.get("/api/me").raise_for_status().json()
    check("B starts with none of A's memory", me["profile"] == EMPTY, me["profile"])
    check("B cannot open A's conversation",
          b.get("/api/me", params={"conversation_id": cid}).status_code == 404)
    r = say(b, conversation(b), "What do you recommend?")
    check("B's chat shows none of A's memory", r["profile"] == EMPTY, r["profile"])

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
