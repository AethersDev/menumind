"""Recommendation and grounded rendering.

Hard constraints (allergens, diet) filter in code; the model never sees a violating dish as a
recommendable option. Soft preferences rank what is left. The reply model refers to dishes
only by ID tokens, and code fills in names and prices from the menu:

    [[rec:M3]]   a recommendation; must be a dish that passes the user's hard constraints
    [[item:M5]]  any dish, e.g. when answering a question; a dish the user must avoid is
                 rendered with a warning written by code

check_reply() rejects unknown IDs, recommendations that violate a hard constraint, and any
dish name or price typed outside a token.
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


def check_reply(raw: str, menu: Menu, recs: Recommendations) -> list[str]:
    errors = []
    for kind, item_id in TOKEN.findall(raw):
        if menu.get(item_id) is None:
            errors.append(f"unknown item id {item_id}")
        elif kind == "rec" and item_id not in recs.allowed_ids():
            why = ", ".join(recs.excluded.get(item_id, ["not allowed"]))
            errors.append(f"recommended {item_id}, which the customer must avoid ({why})")
    text = TOKEN.sub(" ", raw)
    if TYPED_PRICE.search(text):
        errors.append("typed a price; prices must come from tokens")
    lowered = text.lower()
    for item in menu.items:
        if item.name.lower() in lowered:
            errors.append(f"typed the dish name {item.name!r}; use a token")
    return errors


def _price(p: float) -> str:
    return f"${p:.2f}"


def render(raw: str, menu: Menu, recs: Recommendations, budget) -> tuple[str, list[dict]]:
    """Replace tokens with names and prices from the menu. Call only after check_reply()."""
    mentioned: list[dict] = []
    over_ids = {c.item.id for c in recs.over_budget}

    def sub(m: re.Match) -> str:
        kind, item_id = m.group(1), m.group(2)
        item = menu.get(item_id)
        warnings = list(recs.excluded.get(item_id, []))
        if item_id in over_ids and budget is not None:
            warnings.append(f"over your {_price(budget)} budget")
        mentioned.append({"id": item.id, "name": item.name, "price": item.price,
                          "kind": kind, "warnings": warnings})
        label = f"**{item.name}** ({_price(item.price)})"
        if item_id in recs.excluded:
            return f"{label} [not suitable for you: {', '.join(warnings)}]"
        if warnings:
            return f"{label} [{', '.join(warnings)}]"
        return label

    return TOKEN.sub(sub, raw), mentioned
