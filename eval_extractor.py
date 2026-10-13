"""Score the extractor against extractor_cases.json and the reply classifier against
confirmation_cases.json (SPEC §11).

Each case is run through the real Conversation twice from the same starting profile: once
with the labeled claims (expected run E) and once with the predicted claims (P). The runs are
compared on what the user would experience:
  - the resulting profiles, per layer (active = enforced now, durable = remembered);
  - every confirmation question proposed, with its full content (attribute, subject,
    operation, value, scope), including any a later question replaced;
  - whether a clarifying question was asked.
Every restriction is counted individually.

Usage:
  python eval_extractor.py                                   # default model
  python eval_extractor.py --model claude-sonnet-5-5 --repeats 3
  python eval_extractor.py --price-in 1.0 --price-out 5.0    # USD per million tokens
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from memory import (DIET_ORDER, Claim, Conversation, Draft, EventStore, Op, Profile, Scope,
                    Source, SpecError, _hard_effect)

HERE = Path(__file__).parent
LAYERS = ("active", "durable")
Unit = tuple[str, str, str]           # (layer, attribute, value-or-subject)
Proposal = tuple                      # (attribute, subject, op, value, scope)


@dataclass(frozen=True)
class Outcome:
    profile: Profile
    proposals: tuple[Proposal, ...]   # every CONFIRM raised, in order
    clarified: bool


def _seed(profile: dict) -> list[Draft]:
    out = []
    for attr, v in profile.items():
        if isinstance(v, list):
            out += [Draft(attr, Op.SET, Scope.DURABLE, Source.STATED, subject=s) for s in v]
        else:
            out.append(Draft(attr, Op.SET, Scope.DURABLE, Source.STATED, value=v))
    return out


def _proposal(d: Draft) -> Proposal:
    return (d.attribute, d.subject, d.op.value, d.value, d.scope.value)


def simulate(profile: dict, claims: list[Claim]) -> Outcome:
    store = EventStore(":memory:")
    try:
        store.append("eval", "s", _seed(profile))
        conv = Conversation(store, "eval", "s")
        decisions = conv.handle(claims)
        return Outcome(conv.profile(),
                       tuple(_proposal(d.proposal) for d in decisions if d.proposal),
                       any(d.outcome == "clarify" for d in decisions))
    finally:
        store.close()


def _rank(diet: str) -> int:
    return DIET_ORDER.index(diet)


def restriction_units(start: Profile, final: Profile) -> list[Unit]:
    """Hard restrictions `final` has that `start` lacked, one unit per allergen per layer."""
    units: list[Unit] = []
    for layer in LAYERS:
        s, f = getattr(start, layer), getattr(final, layer)
        units += [(layer, "allergen", m) for m in sorted(f["allergen"] - s["allergen"])]
        if _rank(f["diet"]) > _rank(s["diet"]):
            units.append((layer, "diet", f["diet"]))
    return units


def covered(unit: Unit, p: Profile) -> bool:
    layer, attr, v = unit
    state = getattr(p, layer)
    return v in state["allergen"] if attr == "allergen" else _rank(state["diet"]) >= _rank(v)


def unsafe_loosenings(start: Profile, e: Profile, p: Profile) -> list[Unit]:
    """Restrictions held before and still expected after, but loosened in the prediction."""
    out: list[Unit] = []
    for layer in LAYERS:
        s, ex, pr = getattr(start, layer), getattr(e, layer), getattr(p, layer)
        out += [(layer, "allergen", m) for m in sorted((s["allergen"] & ex["allergen"]) - pr["allergen"])]
        if _rank(pr["diet"]) < min(_rank(s["diet"]), _rank(ex["diet"])):
            out.append((layer, "diet", pr["diet"]))
    return out


def unsafe_proposals(start: Profile, e: Outcome, p: Outcome) -> list[Proposal]:
    """Loosening questions the prediction raised that the labels did not. Nothing has changed
    yet, but a 'yes' would loosen a restriction the user never asked to loosen."""
    extra = Counter(p.proposals) - Counter(e.proposals)
    out = []
    for prop in extra.elements():
        attr, subject, op, value, scope = prop
        state = start.durable if scope == Scope.DURABLE.value else start.active
        if _hard_effect(attr, Op(op), subject, value, state) == "loosen":
            out.append(prop)
    return out


def spurious_restrictions(start: Profile, e: Profile, p: Profile) -> list[Unit]:
    """Hard restrictions the prediction added that neither the start nor the labels have."""
    out: list[Unit] = []
    for layer in LAYERS:
        s, ex, pr = getattr(start, layer), getattr(e, layer), getattr(p, layer)
        out += [(layer, "allergen", m) for m in sorted(pr["allergen"] - s["allergen"] - ex["allergen"])]
        if _rank(pr["diet"]) > max(_rank(s["diet"]), _rank(ex["diet"])):
            out.append((layer, "diet", pr["diet"]))
    return out


def _norm(c: Claim) -> tuple:
    v = float(c.value) if isinstance(c.value, (int, float)) else c.value
    return (c.attribute, c.polarity.value, c.subject, v, c.temporal.value, c.modality.value)


def score_case(case: dict, claims: list[Claim], error: Optional[str],
               stats: Optional[dict] = None) -> dict:
    expected = [Claim.from_dict(c) for c in case["expected_claims"]]
    base = simulate(case["profile"], []).profile
    e = simulate(case["profile"], expected)
    try:
        p = simulate(case["profile"], claims)
    except SpecError as exc:  # should not happen after parse_claims, but never crash a run
        error, claims, p = error or f"SpecError: {exc}", [], simulate(case["profile"], [])

    units = restriction_units(base, e.profile)
    stats = stats or {}
    return {
        "id": case["id"],
        "utterance": case["utterance"],
        "valid": error is None,
        "error": error,
        "exact": Counter(map(_norm, expected)) == Counter(map(_norm, claims)),
        "decision_match": (p.profile == e.profile
                           and Counter(p.proposals) == Counter(e.proposals)
                           and p.clarified == e.clarified),
        "units": [list(u) for u in units],
        "missed": [list(u) for u in units if not covered(u, p.profile)],
        "unsafe_applied": [list(u) for u in unsafe_loosenings(base, e.profile, p.profile)],
        "unsafe_proposed": [list(x) for x in unsafe_proposals(base, e, p)],
        "spurious": [list(u) for u in spurious_restrictions(base, e.profile, p.profile)],
        "proposals": {"expected": [list(x) for x in e.proposals],
                      "predicted": [list(x) for x in p.proposals]},
        "predicted": [dict(zip(("attribute", "polarity", "subject", "value", "temporal",
                                "modality"), _norm(c))) for c in claims],
        "latency_s": stats.get("latency_s"),
        "input_tokens": stats.get("input_tokens", 0),
        "output_tokens": stats.get("output_tokens", 0),
    }


def _latency(values: list[float]) -> Optional[dict]:
    if not values:
        return None
    q = sorted(values)
    return {"mean": round(statistics.fmean(q), 3), "p50": round(q[len(q) // 2], 3),
            "p95": round(q[min(len(q) - 1, int(0.95 * len(q)))], 3)}


def summarize(rows: list[dict]) -> dict:
    n = len(rows)

    def layer_recall(layer: str) -> dict:
        total = sum(1 for r in rows for u in r["units"] if u[0] == layer)
        miss = sum(1 for r in rows for u in r["missed"] if u[0] == layer)
        return {"covered": total - miss, "total": total,
                "recall": round((total - miss) / total, 3) if total else None}

    def listing(key: str) -> list:
        return [[r["id"], *u] for r in rows for u in r[key]]

    return {
        "runs": n,
        "restriction_recall": {layer: layer_recall(layer) for layer in LAYERS},
        "unsafe_loosening_errors": {"applied": sum(len(r["unsafe_applied"]) for r in rows),
                                    "proposed": sum(len(r["unsafe_proposed"]) for r in rows)},
        "spurious_restrictions": sum(len(r["spurious"]) for r in rows),
        "decision_accuracy": round(sum(r["decision_match"] for r in rows) / n, 3),
        "exact_claim_accuracy": round(sum(r["exact"] for r in rows) / n, 3),
        "invalid_output_rate": round(sum(not r["valid"] for r in rows) / n, 3),
        "latency_s": _latency([r["latency_s"] for r in rows if r["latency_s"] is not None]),
        "tokens": {"input": sum(r["input_tokens"] for r in rows),
                   "output": sum(r["output_tokens"] for r in rows)},
        "missed": listing("missed"),
        "unsafe_applied": listing("unsafe_applied"),
        "unsafe_proposed": listing("unsafe_proposed"),
        "spurious": listing("spurious"),
        "decision_mismatches": [r["id"] for r in rows if not r["decision_match"]],
    }


# A predictor returns (claims, error) or (claims, error, stats).
Predictor = Callable[[str], tuple]


def evaluate_extractor(cases: list[dict], predict: Predictor, repeats: int = 1) -> dict:
    rows = []
    for rep in range(repeats):
        for case in cases:
            out = predict(case["utterance"])
            claims, error, stats = (*out, None) if len(out) == 2 else out
            rows.append({**score_case(case, claims, error, stats), "repeat": rep})
    return {"summary": summarize(rows), "rows": rows}


def evaluate_confirmations(data: dict, classify: Callable[[str, str], object]) -> dict:
    """`classify` returns an answer string, or (answer, stats)."""
    rows = []
    for case in data["cases"]:
        out = classify(data["questions"][case["question"]], case["reply"])
        got, stats = out if isinstance(out, tuple) else (out, {})
        rows.append({"id": case["id"], "reply": case["reply"], "expected": case["expected"],
                     "predicted": got, "latency_s": stats.get("latency_s"),
                     "input_tokens": stats.get("input_tokens", 0),
                     "output_tokens": stats.get("output_tokens", 0)})
    n = len(rows)
    false_yes = [r["id"] for r in rows if r["predicted"] == "yes" and r["expected"] != "yes"]
    return {"summary": {
        "cases": n,
        "accuracy": round(sum(r["predicted"] == r["expected"] for r in rows) / n, 3),
        "false_yes": len(false_yes), "false_yes_ids": false_yes,
        "confusion": dict(Counter(f"{r['expected']}->{r['predicted']}" for r in rows)),
        "latency_s": _latency([r["latency_s"] for r in rows if r["latency_s"] is not None]),
        "tokens": {"input": sum(r["input_tokens"] for r in rows),
                   "output": sum(r["output_tokens"] for r in rows)}},
        "rows": rows}


def cost_usd(tokens: dict, price_in: Optional[float], price_out: Optional[float]) -> Optional[float]:
    if price_in is None or price_out is None:
        return None
    return round((tokens["input"] * price_in + tokens["output"] * price_out) / 1e6, 4)


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text())


def render(report: dict) -> str:
    s, c = report["extractor"]["summary"], report["confirmations"]["summary"]
    rr, ul = s["restriction_recall"], s["unsafe_loosening_errors"]
    lat = s["latency_s"] or {}
    lines = [
        f"model {report['model']}   runs {s['runs']}",
        "",
        "SAFETY (counts, not rates: the sample is small)",
        f"  hard restrictions enforced now     {rr['active']['covered']}/{rr['active']['total']}",
        f"  hard restrictions remembered       {rr['durable']['covered']}/{rr['durable']['total']}",
        f"  unsafe loosenings applied          {ul['applied']}",
        f"  unsafe loosenings proposed         {ul['proposed']}",
        f"  reply classifier false yes         {c['false_yes']}/{c['cases']}",
        "",
        "QUALITY",
        f"  spurious restrictions              {s['spurious_restrictions']}",
        f"  decision accuracy                  {s['decision_accuracy']:.3f}",
        f"  exact claim accuracy               {s['exact_claim_accuracy']:.3f}",
        f"  invalid output rate                {s['invalid_output_rate']:.3f}",
        f"  reply classifier accuracy          {c['accuracy']:.3f}",
        "",
        "COST",
        f"  extractor latency s (mean/p50/p95) {lat.get('mean')}/{lat.get('p50')}/{lat.get('p95')}",
        f"  tokens in/out (extractor+replies)  {report['tokens']['input']}/{report['tokens']['output']}",
        f"  cost USD                           {report['cost_usd'] if report['cost_usd'] is not None else 'n/a (pass --price-in/--price-out)'}",
        "",
        "Perfect counts here show the model handles these 32 phrasings, not that it will "
        "catch every restriction in unfamiliar conversation.",
    ]
    for label in ("missed", "unsafe_applied", "unsafe_proposed", "spurious"):
        lines += [f"  {label}: {item}" for item in s[label]]
    lines += [f"  false yes: {rid}" for rid in c["false_yes_ids"]]
    return "\n".join(lines)


def main() -> None:
    from extractor import DEFAULT_MODEL, classify_reply_with_stats, extract

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--price-in", type=float, help="USD per million input tokens")
    ap.add_argument("--price-out", type=float, help="USD per million output tokens")
    ap.add_argument("--out", default=None, help="report path (default eval_report_<model>.json)")
    args = ap.parse_args()

    import anthropic
    client = anthropic.Anthropic()

    def predict(utterance: str):
        x = extract(utterance, client=client, model=args.model)
        return x.claims, x.error, x.stats

    ext = evaluate_extractor(load("extractor_cases.json")["cases"], predict, args.repeats)
    conf = evaluate_confirmations(
        load("confirmation_cases.json"),
        lambda q, r: classify_reply_with_stats(q, r, client=client, model=args.model))
    tokens = {k: ext["summary"]["tokens"][k] + conf["summary"]["tokens"][k]
              for k in ("input", "output")}
    report = {"model": args.model, "tokens": tokens,
              "cost_usd": cost_usd(tokens, args.price_in, args.price_out),
              "extractor": ext, "confirmations": conf}
    out = args.out or f"eval_report_{args.model}.json"
    Path(out).write_text(json.dumps(report, indent=2, default=str))
    print(render(report))
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
