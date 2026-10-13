"""Menu loading and validation.

The menu is data, and the safety of every recommendation depends on its allergen and diet
fields being right. So the loader checks them against the ingredient lists: an ingredient
known to carry an allergen must list it, and an ingredient known to rule out a diet must not
be in a dish labeled with that diet. A menu that fails these checks does not load.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from memory import ALLERGENS, DIET_ORDER, SPICE_ORDER

CATEGORIES = ("starter", "main", "dessert")
DEFAULT_PATH = Path(__file__).parent / "menu.json"

# Ingredient -> allergens it always carries. Extend when the menu gains ingredients.
ALLERGEN_SOURCES: dict[str, frozenset] = {k: frozenset(v) for k, v in {
    "peanut": {"peanut"}, "peanut_sauce": {"peanut"},
    "cashew": {"tree_nut"}, "almond": {"tree_nut"}, "walnut": {"tree_nut"},
    "shrimp": {"shellfish"}, "crab": {"shellfish"},
    "fish_sauce": {"fish"}, "salmon": {"fish"},
    "egg": {"egg"},
    "butter": {"dairy"}, "cream": {"dairy"}, "cream_cheese": {"dairy"}, "paneer": {"dairy"},
    "milk": {"dairy"},
    "sesame_seed": {"sesame"}, "sesame_oil": {"sesame"},
    "tofu": {"soy"}, "edamame": {"soy"},
    "soy_sauce": {"soy", "gluten"}, "teriyaki_sauce": {"soy", "gluten"},
    "gochujang": {"soy", "gluten"}, "hoisin": {"soy", "gluten"},
    "wheat_wrapper": {"gluten"}, "bao_bun": {"gluten"}, "udon_noodle": {"gluten"},
    "flour": {"gluten"}, "graham_cracker": {"gluten"},
}.items()}

# Ingredient -> the strictest diet a dish containing it can still satisfy.
DIET_CEILING: dict[str, str] = {
    "chicken": "omnivore", "beef": "omnivore", "pork": "omnivore",
    "salmon": "pescatarian", "shrimp": "pescatarian", "crab": "pescatarian",
    "fish_sauce": "pescatarian",
    "egg": "vegetarian", "butter": "vegetarian", "cream": "vegetarian",
    "cream_cheese": "vegetarian", "paneer": "vegetarian", "milk": "vegetarian",
    "honey": "vegetarian",
}


class MenuError(ValueError):
    """The menu file is inconsistent; it is not loaded."""


@dataclass(frozen=True)
class MenuItem:
    id: str
    name: str
    category: str
    price: float
    diet: str
    spice: str
    ingredients: tuple[str, ...]
    allergens: frozenset
    description: str

    def has_ingredient(self, word: str) -> bool:
        """True if any ingredient is `word` or contains it as a whole word
        ('mushroom' matches 'shiitake_mushroom')."""
        return any(word == ing or word in ing.split("_") for ing in self.ingredients)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category,
                "price": self.price, "diet": self.diet, "spice": self.spice,
                "ingredients": list(self.ingredients), "allergens": sorted(self.allergens),
                "description": self.description}


@dataclass(frozen=True)
class Menu:
    restaurant: str
    items: tuple[MenuItem, ...]

    def get(self, item_id: str):
        return self._by_id().get(item_id)

    def _by_id(self) -> dict:
        return {i.id: i for i in self.items}


def _item(d: dict) -> MenuItem:
    ings = tuple(d["ingredients"])
    item = MenuItem(d["id"], d["name"], d["category"], float(d["price"]), d["diet"], d["spice"],
                    ings, frozenset(d["allergens"]), d["description"])
    problems = []
    if item.category not in CATEGORIES:
        problems.append(f"category {item.category!r}")
    if item.price <= 0:
        problems.append("price must be positive")
    if item.diet not in DIET_ORDER:
        problems.append(f"diet {item.diet!r}")
    if item.spice not in SPICE_ORDER:
        problems.append(f"spice {item.spice!r}")
    if unknown := item.allergens - set(ALLERGENS):
        problems.append(f"unknown allergens {sorted(unknown)}")
    for ing in ings:
        if ing != ing.lower() or " " in ing:
            problems.append(f"non-canonical ingredient {ing!r}")
        if missing := ALLERGEN_SOURCES.get(ing, frozenset()) - item.allergens:
            problems.append(f"{ing} carries {sorted(missing)} but allergens omit it")
        ceiling = DIET_CEILING.get(ing)
        if ceiling and DIET_ORDER.index(item.diet) > DIET_ORDER.index(ceiling):
            problems.append(f"labeled {item.diet} but contains {ing}")
    if problems:
        raise MenuError(f"{item.id} {item.name}: " + "; ".join(problems))
    return item


def parse_menu(data: dict) -> Menu:
    items = tuple(_item(d) for d in data["items"])
    for field in ("id", "name"):
        values = [getattr(i, field).lower() for i in items]
        if dupes := {v for v in values if values.count(v) > 1}:
            raise MenuError(f"duplicate {field}: {sorted(dupes)}")
    return Menu(data["restaurant"], items)


def load_menu(path: Path | str = DEFAULT_PATH) -> Menu:
    return parse_menu(json.loads(Path(path).read_text()))
