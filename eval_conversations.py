"""Live end-to-end evaluation: scripted conversations through the real agent and model.

Each case in conversation_cases.json is a short conversation with one fresh customer. After
every turn the script checks what the app did, not how the model worded it: the saved and
enforced profiles, whether a yes/no question is waiting, and the recommended dishes. Every
recommended dish is also checked against the customer's allergies and diet. The replies are
kept in the report for reading.

Usage (spends model tokens):
  python eval_conversations.py                          # default model, one run
  python eval_conversations.py --model gpt-5.4-nano --repeats 3
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from agent import Agent
from llm import DEFAULT_MODEL, PROVIDER, make_client
from memory import EventStore
from menu import Menu, load_menu
from recommend import hard_violations

HERE = Path(__file__).parent


def check(expect: dict, result, profile, menu: Menu) -> list[str]:
    """Unmet expectations for one turn. Recommended dishes are always checked for safety."""
    failed = []
    for layer in ("durable", "active"):
        state = getattr(profile, layer)
        for attr, want in expect.get(layer, {}).items():
            got = sorted(state[attr]) if isinstance(state[attr], frozenset) else state[attr]
            if got != want:
                failed.append(f"{layer} {attr} is {got!r}, expected {want!r}")
    if "asks" in expect and (result.question is not None) != expect["asks"]:
        failed.append(f"pending question {result.question!r}, expected asks={expect['asks']}")
    picks = [menu.get(m["id"]) for m in result.mentioned if m["kind"] == "rec"]
    ids = [i.id for i in picks]
    if expect.get("recommends") and not picks:
        failed.append("recommended nothing")
    if "category" in expect and any(i.category != expect["category"] for i in picks):
        failed.append(f"recommended {ids}, expected only {expect['category']}s")
    if "max_price" in expect and any(i.price > expect["max_price"] for i in picks):
        failed.append(f"recommended {ids}, expected nothing over ${expect['max_price']}")
    for i in picks:
        if why := hard_violations(i, profile.active):
            failed.append(f"UNSAFE: recommended {i.id} ({', '.join(why)})")
    return failed


def run_case(case: dict, client, model: str, menu: Menu) -> list[dict]:
    db = str(Path(tempfile.mkdtemp()) / "eval.db")

    def new_agent() -> Agent:
        return Agent(EventStore(db), menu, client, extract_model=model, reply_model=model)

    agent, convs, turns = new_agent(), {}, []
    for t in case["turns"]:
        if t.get("restart"):  # a new process on the same database
            agent.store.close()
            agent, convs = new_agent(), {}
        user = t.get("user", "customer")
        if t.get("new_conversation") or user not in convs:
            convs[user] = agent.start_conversation(user)
        t0 = time.perf_counter()
        r = agent.turn(user, convs[user], t["say"])
        latency = round(time.perf_counter() - t0, 3)
        turns.append({
            "say": t["say"], "reply": r.reply, "latency_s": latency,
            "failed": check(t.get("expect", {}), r, agent.profile(user, convs[user]), menu),
            "changes": r.changes, "question": r.question, "claims": r.trace["claims"],
            "decisions": r.trace.get("decisions", []), "extract_error": r.trace["extract_error"],
            "fallback": r.trace.get("fallback", False), "reply_errors": r.trace.get("reply_errors", [])})
    agent.store.close()
    return turns


def summarize(rows: list[dict]) -> dict:
    turns = [t for r in rows for t in r["turns"]]
    lat = sorted(t["latency_s"] for t in turns)
    return {
        "unsafe_recommendations": sum(f.startswith("UNSAFE") for t in turns for f in t["failed"]),
        "cases_correct": sum(all(not t["failed"] for t in r["turns"]) for r in rows),
        "cases": len(rows),
        "turns_correct": sum(not t["failed"] for t in turns),
        "turns": len(turns),
        "extraction_failures": sum(t["extract_error"] is not None for t in turns),
        "fallback_replies": sum(t["fallback"] for t in turns),
        "reply_rejected_at_least_once": sum(bool(t["reply_errors"]) for t in turns),
        "latency_s": {"mean": round(statistics.fmean(lat), 2), "max": lat[-1],
                      "p95": lat[min(len(lat) - 1, int(0.95 * len(lat)))]},
        "failures": [[r["id"], r["repeat"], t["say"], f]
                     for r in rows for t in r["turns"] for f in t["failed"]],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default=None, help="report path (default eval_report_conversations_<model>.json)")
    args = ap.parse_args()

    client, menu = make_client(PROVIDER), load_menu()
    cases = json.loads((HERE / "conversation_cases.json").read_text())["cases"]
    rows = [{"id": c["id"], "repeat": k, "turns": run_case(c, client, args.model, menu)}
            for k in range(args.repeats) for c in cases]
    s = summarize(rows)
    out = args.out or f"eval_report_conversations_{args.model}.json"
    Path(out).write_text(json.dumps({"model": args.model, "summary": s, "rows": rows}, indent=2))
    lat = s["latency_s"]
    print(f"model {args.model}   repeats {args.repeats}\n"
          f"  unsafe recommendations          {s['unsafe_recommendations']}\n"
          f"  cases fully correct             {s['cases_correct']}/{s['cases']}\n"
          f"  turns fully correct             {s['turns_correct']}/{s['turns']}\n"
          f"  extraction failures             {s['extraction_failures']}\n"
          f"  fallback replies                {s['fallback_replies']}\n"
          f"  model reply rejected at least once  {s['reply_rejected_at_least_once']}\n"
          f"  turn latency s (mean/p95/max)   {lat['mean']}/{lat['p95']}/{lat['max']}")
    for f in s["failures"]:
        print("  failed:", " | ".join(map(str, f)))
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
