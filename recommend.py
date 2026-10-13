"""Recommendation and grounded rendering.

Hard constraints (allergens, diet) filter in code; the model never sees a violating dish as a
recommendable option. Soft preferences rank what is left. The reply model returns a message
and, separately, the IDs of the dishes it recommends. Code writes every recommended dish
(name, price, the ranking's reasons) and accepts only IDs from the eligible set. In the
message, the model may mention a dish only as a token:

    [[item:M5]]  any dish, e.g. when answering a question; a dish the user must avoid is
                 rendered with a warning written by code

check_reply() rejects ineligible or unknown IDs and any full dish name or price typed in the
message. It cannot catch a dish described in other words ("the noodles with shrimp"), so the
message is held to the prompt there, not checked. Recommendations never depend on the message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from memory import DIET_ORDER, SPICE_ORDER
from menu import Menu, MenuItem


@dataclass(frozen=True)
class Candidate:
    item: MenuItem
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Recommendations:
    candidates: tuple[Candidate, ...]          # pass hard constraints and budget, best first
    over_budget: tuple[Candidate, ...]         # pass hard constraints, above budget_max
    excluded: dict                             # item id -> reasons it violates hard constraints

    def allowed_ids(self) -> set:
        return {c.item.id for c in self.candidates + self.over_budget}


def hard_violations(item: MenuItem, active: dict) -> list[str]:
    out = [f"contains {a.replace('_', ' ')}" for a in sorted(item.allergens & active["allergen"])]
    if DIET_ORDER.index(item.diet) < DIET_ORDER.index(active["diet"]):
        out.append(f"not {active['diet']}")
    return out


def soft_score(item: MenuItem, active: dict) -> tuple[float, tuple[str, ...]]:
    score, reasons = 0.0, []
    tol = active["spice_tolerance"]
    if tol is not None:
        diff = SPICE_ORDER.index(item.spice) - SPICE_ORDER.index(tol)
        if diff > 0:
            score -= 2.0 * diff
            reasons.append(f"spicier than you like ({item.spice})")
        elif diff == 0:
            score += 1.0
            reasons.append(f"matches your spice level ({item.spice})")
        else:
            score -= 0.25 * -diff
    for word in sorted(active["disliked_ingredient"]):
        if item.has_ingredient(word):
            score -= 5.0
            reasons.append(f"has {word.replace('_', ' ')}, which you dislike")
    for word in sorted(active["liked_ingredient"]):
        if item.has_ingredient(word):
            score += 1.5
            reasons.append(f"has {word.replace('_', ' ')}, which you like")
    lean = active["diet_lean"]
    if lean is not None and DIET_ORDER.index(item.diet) >= DIET_ORDER.index(lean):
        score += 1.0
        reasons.append(f"fits your {lean} lean")
    return score, tuple(reasons)


def recommend(menu: Menu, active: dict) -> Recommendations:
    ok, over, excluded = [], [], {}
    budget = active["budget_max"]
    for item in menu.items:
        if v := hard_violations(item, active):
            excluded[item.id] = v
            continue
        score, reasons = soft_score(item, active)
        (over if budget is not None and item.price > budget else ok).append(
            Candidate(item, score, reasons))
    order = lambda c: (-c.score, c.item.price, c.item.id)  # noqa: E731
    return Recommendations(tuple(sorted(ok, key=order)), tuple(sorted(over, key=order)), excluded)


# ------------------------------------------------------------------ grounded rendering

TOKEN = re.compile(r"\[\[(rec|item):([A-Za-z0-9_-]+)\]\]")
TYPED_PRICE = re.compile(r"\$\s?\d")
MAX_PICKS = 3


def check_reply(message: str, picks: list, menu: Menu, recs: Recommendations) -> list[str]:
    errors = []
    if len(picks) > MAX_PICKS or len(set(picks)) != len(picks):
        errors.append(f"recommend at most {MAX_PICKS} different dishes")
    allowed = recs.allowed_ids()
    for item_id in picks:
        if menu.get(item_id) is None:
            errors.append(f"unknown item id {item_id}")
        elif item_id not in allowed:
            why = ", ".join(recs.excluded[item_id])
            errors.append(f"recommended {item_id}, which the customer must avoid ({why})")
    for kind, item_id in TOKEN.findall(message):
        if kind == "rec":
            errors.append(f"[[rec:{item_id}]] in the message; put recommendations in \"recommend\"")
        elif menu.get(item_id) is None:
            errors.append(f"unknown item id {item_id}")
    text = TOKEN.sub(" ", message)
    if TYPED_PRICE.search(text):
        errors.append("typed a price; prices must come from tokens")
    lowered = text.lower()
    for item in menu.items:
        if item.name.lower() in lowered:
            errors.append(f"typed the dish name {item.name!r}; use a token")
    return errors


def _price(p: float) -> str:
    return f"${p:.2f}"


def render(message: str, picks: list, menu: Menu, recs: Recommendations,
           budget) -> tuple[str, list[dict]]:
    """Replace tokens with names and prices from the menu, then list the recommended dishes
    with their reasons. Call only after check_reply()."""
    mentioned: list[dict] = []
    over_ids = {c.item.id for c in recs.over_budget}  # non-empty only when budget is set
    reasons = {c.item.id: c.reasons for c in recs.candidates + recs.over_budget}

    def label(kind: str, item_id: str) -> str:
        item = menu.get(item_id)
        warnings = list(recs.excluded.get(item_id, []))
        if item_id in over_ids:
            warnings.append(f"over your {_price(budget)} budget")
        mentioned.append({"id": item.id, "name": item.name, "price": item.price,
                          "kind": kind, "warnings": warnings})
        text = f"**{item.name}** ({_price(item.price)})"
        if item_id in recs.excluded:
            return f"{text} [not suitable for you: {', '.join(warnings)}]"
        if warnings:
            return f"{text} [{', '.join(warnings)}]"
        return text

    text = TOKEN.sub(lambda m: label(m.group(1), m.group(2)), message)
    lines = [f"- {label('rec', i)}" + (f": {'; '.join(reasons[i])}" if reasons[i] else "")
             for i in picks]
    return ("\n\n".join([text, "\n".join(lines)]) if lines else text), mentioned
