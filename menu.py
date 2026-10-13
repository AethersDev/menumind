"""Menu loading and validation.

The menu is data, and the safety of every recommendation depends on its allergen and diet
fields being right. So the loader derives them from the ingredients and rejects any dish
whose labels disagree. The ingredient vocabulary is closed: an ingredient that is not
classified fails the load instead of passing as allergen-free and vegan.

These checks cover declared ingredients only. They say nothing about cross-contact in a real
kitchen.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from memory import ALLERGENS, DIET_ORDER, SPICE_ORDER

CATEGORIES = ("starter", "main", "dessert")
DEFAULT_PATH = Path(__file__).parent / "menu.json"

def _classify(diet: str, names: str, *allergens: str) -> dict:
    return {n: (diet, frozenset(allergens)) for n in names.split()}


# Ingredient -> (the strictest diet a dish containing it can satisfy, allergens it carries).
# Every menu ingredient must be here or in HOUSE_RECIPES. Add new ingredients deliberately.
INGREDIENTS: dict[str, tuple[str, frozenset]] = {
    **_classify("omnivore", "chicken beef pork"),
    **_classify("pescatarian", "salmon fish_sauce", "fish"),
    **_classify("pescatarian", "shrimp crab", "shellfish"),
    **_classify("vegetarian", "egg", "egg"),
    **_classify("vegetarian", "butter cream cream_cheese paneer milk", "dairy"),
    **_classify("vegetarian", "honey"),
    **_classify("vegan", "peanut peanut_sauce", "peanut"),
    **_classify("vegan", "cashew almond walnut", "tree_nut"),
    **_classify("vegan", "sesame_seed sesame_oil", "sesame"),
    **_classify("vegan", "tofu edamame", "soy"),
    **_classify("vegan", "soy_sauce teriyaki_sauce gochujang hoisin", "soy", "gluten"),
    **_classify("vegan", "wheat_wrapper bao_bun udon_noodle flour graham_cracker", "gluten"),
    **_classify("vegan", "bamboo_shoot bean_sprout broccoli cabbage canola_oil carrot chickpea "
                "chili cocoa_butter cocoa_mass coconut_milk coriander cucumber cumin "
                "enoki_mushroom galangal garam_masala garlic glass_noodle glutinous_rice "
                "green_chili kombu lemongrass lime lime_zest mango matcha onion rice "
                "rice_noodle scallion sea_salt shallot shiitake_mushroom sugar tamarind "
                "thai_basil tomato water"),
}

# Compound ingredients made in house, as the restaurant declares them. A dish is judged by
# these components, so its labels hold for these recipes, not for every commercial version.
HOUSE_RECIPES: dict[str, tuple[str, ...]] = {
    "green_curry_paste": ("green_chili", "lemongrass", "galangal", "garlic", "shallot",
                          "lime_zest", "coriander", "cumin"),
    "chili_garlic_oil": ("chili", "garlic", "canola_oil"),
    "dark_chocolate": ("cocoa_mass", "cocoa_butter", "sugar"),
    "kombu_broth": ("kombu", "water"),
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
    labeled = DIET_ORDER.index(item.diet) if item.diet in DIET_ORDER else None
    carried, ceiling = set(), len(DIET_ORDER) - 1
    for ing in ings:
        if ing != ing.lower() or " " in ing:
            problems.append(f"non-canonical ingredient {ing!r}")
        for part in HOUSE_RECIPES.get(ing, (ing,)):
            if part not in INGREDIENTS:
                problems.append(f"unclassified ingredient {part!r}")
                continue
            diet, allergens = INGREDIENTS[part]
            if missing := allergens - item.allergens:
                problems.append(f"{ing} carries {sorted(missing)} but allergens omit it")
            if labeled is not None and labeled > DIET_ORDER.index(diet):
                problems.append(f"labeled {item.diet} but contains {ing}")
            carried |= allergens
            ceiling = min(ceiling, DIET_ORDER.index(diet))
    if extra := item.allergens - carried:
        problems.append(f"lists allergens {sorted(extra)} that no ingredient carries")
    if labeled is not None and labeled < ceiling:
        problems.append(f"labeled {item.diet} but its ingredients are {DIET_ORDER[ceiling]}")
    if problems:
        raise MenuError(f"{item.id} {item.name}: " + "; ".join(dict.fromkeys(problems)))
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
