"""Turn planned meals' recipe ingredients into Paprika grocery-list items."""

from __future__ import annotations

import re
import uuid
from collections import Counter

_FRACTIONS = "¼½¾⅓⅔⅛⅜⅝⅞⅕⅖⅗⅘⅙⅚"
_NUM = rf"(?:\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[{_FRACTIONS}])"
_QTY = re.compile(
    rf"^\s*((?:{_NUM}(?:\s*{_NUM})?)(?:\s*(?:-|–|to)\s*{_NUM}(?:\s*{_NUM})?)?)\s*(.*)$")
_UNITS = {
    "c", "cup", "cups", "tbsp", "tbs", "tbl", "tablespoon", "tablespoons", "tsp",
    "teaspoon", "teaspoons", "oz", "ounce", "ounces", "fl oz", "lb", "lbs", "pound",
    "pounds", "g", "gram", "grams", "kg", "kilogram", "kilograms", "ml", "milliliter",
    "milliliters", "l", "liter", "liters", "litre", "litres", "pint", "pints", "quart",
    "quarts", "gallon", "gallons", "pinch", "pinches", "dash", "dashes", "clove",
    "cloves", "can", "cans", "jar", "jars", "package", "packages", "pkg", "bunch",
    "bunches", "head", "heads", "stick", "sticks", "slice", "slices", "sprig", "sprigs",
    "handful", "handfuls", "piece", "pieces",
}
# "1 (14 oz) can tomatoes": keep the size note with the quantity.
_PAREN = re.compile(r"^(\([^)]*\))\s*(.*)$")


def ingredient_lines(text: str | None) -> list[str]:
    """Non-empty ingredient lines, skipping section headers like 'For the sauce:'."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•*·").strip()
        if not line or line.endswith(":") or (line.isupper() and not any(c.isdigit() for c in line)):
            continue
        out.append(line)
    return out


def parse_line(line: str) -> dict:
    """Split '2 cups flour, sifted' into quantity '2 cups', ingredient 'flour',
    instruction 'sifted'. Lines without a quantity keep it empty."""
    quantity, rest = "", line
    m = _QTY.match(line)
    if m and m.group(2):
        quantity, rest = m.group(1).strip(), m.group(2)
        p = _PAREN.match(rest)
        if p:
            quantity, rest = f"{quantity} {p.group(1)}", p.group(2)
        words = rest.split()
        for n in (2, 1):  # two-word units first ("fl oz")
            if len(words) > n and " ".join(words[:n]).lower().rstrip(".") in _UNITS:
                quantity += " " + " ".join(words[:n])
                rest = " ".join(words[n:])
                break
        rest = re.sub(r"^of\s+", "", rest)
    ingredient, _, instruction = rest.partition(",")
    return {"quantity": quantity, "ingredient": ingredient.strip(),
            "instruction": instruction.strip()}


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def build_items(meals: list[dict], recipes: dict[str, dict], existing: list[dict],
                list_uid: str | None, aisles_by_ingredient: dict[str, tuple[str, str]],
                skip: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    """Grocery items for each planned meal's ingredients.

    A recipe planned twice adds its ingredients twice. Lines already on the list
    (not yet bought, same recipe) count toward that, so running it again doesn't
    duplicate anything. Returns (new items, skipped lines, meals without a recipe)."""
    have = Counter((m.get("recipe_uid"), _key(m.get("name", ""))) for m in existing
                   if not m.get("purchased") and (not list_uid or m.get("list_uid") == list_uid))
    skip_words = [_key(w) for w in skip if w.strip()]
    order = max((m.get("order_flag") or 0 for m in existing), default=0)
    items, skipped, missing = [], [], []
    for meal in meals:
        r = recipes.get(meal.get("recipe_uid") or "")
        if not r:
            missing.append(meal.get("name") or "(unnamed meal)")
            continue
        for line in ingredient_lines(r.get("ingredients")):
            parsed = parse_line(line)
            name_key = _key(parsed["ingredient"] or line)
            if any(re.search(rf"\b{re.escape(w)}\b", name_key) for w in skip_words):
                skipped.append({"recipe": r.get("name"), "line": line, "why": "skip list"})
                continue
            k = (r["uid"], _key(line))
            if have[k] > 0:
                have[k] -= 1
                skipped.append({"recipe": r.get("name"), "line": line, "why": "already on list"})
                continue
            order += 1
            aisle_uid, aisle = aisles_by_ingredient.get(name_key, (None, ""))
            item = {"uid": str(uuid.uuid4()).upper(), "recipe_uid": r["uid"],
                    "recipe": r.get("name", ""), "name": line,
                    "ingredient": parsed["ingredient"] or line,
                    "quantity": parsed["quantity"], "instruction": parsed["instruction"],
                    "aisle": aisle, "order_flag": order, "purchased": False,
                    "separate": False}
            if aisle_uid:
                item["aisle_uid"] = aisle_uid
            if list_uid:
                item["list_uid"] = list_uid
            items.append(item)
    return items, skipped, missing
