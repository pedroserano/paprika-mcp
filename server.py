"""Paprika connector for Claude: mood & season recipe search.

Live-syncs your Paprika 3 library (re-checks Paprika at most every
PAPRIKA_SYNC_MINUTES, default 5, downloading only changed recipes) and lets
Claude search it by mood, season, occasion and cooking history.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import groceries
import moods_edit
from finder import Finder, recipe_minutes, season_for
from paprika_client import CACHE_DIR, PaprikaClient, PaprikaError, RecipeStore

HERE = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("PAPRIKA_MOODS_FILE", CACHE_DIR / "moods.json"))

# "stdio" (default: launched by Claude Desktop on this computer) or "http"
# (a long-running server, e.g. in Docker on a NAS, reached as a custom connector).
TRANSPORT = os.environ.get("PAPRIKA_TRANSPORT", "stdio").strip().lower()
URL_SECRET = os.environ.get("PAPRIKA_URL_SECRET", "").strip().strip("/")

if TRANSPORT == "http":
    # Bind to all interfaces; the secret URL path is the access control, so the
    # localhost-only Host-header check FastMCP applies by default must stay off.
    mcp = FastMCP("Paprika", host=os.environ.get("PAPRIKA_HOST", "0.0.0.0"),
                  port=int(os.environ.get("PAPRIKA_PORT", "8080")),
                  streamable_http_path=f"/{URL_SECRET}/mcp", stateless_http=True)
else:
    mcp = FastMCP("Paprika")
_store: RecipeStore | None = None


_config_problem: str | None = None  # why the user's moods.json couldn't be used


def _unreadable_message(e: Exception) -> str:
    msg = f"Can't read {CONFIG_PATH} ({e.strerror or e})."
    if isinstance(e, PermissionError):
        msg += (f" The server runs as uid {os.getuid()}, which isn't allowed to read it. "
                f"Fix it on the host, e.g. `chown {os.getuid()}:{os.getgid()} moods.json && "
                "chmod 600 moods.json` "
                "in the data folder.")
    return msg


def _config(strict: bool = False) -> dict:
    """User copy of moods.json (created from the bundled default).

    If the user's file can't be read (e.g. wrong owner on a NAS volume), searches
    fall back to the bundled defaults and report why, instead of failing. With
    strict=True (used for edits) the error is raised, so an edit never replaces
    the user's own file with the defaults."""
    global _config_problem
    try:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text((HERE / "moods.json").read_text())
        cfg = json.loads(CONFIG_PATH.read_text())
        _config_problem = None
        return cfg
    except OSError as e:
        problem = _unreadable_message(e)
    except json.JSONDecodeError as e:
        problem = f"{CONFIG_PATH} isn't valid JSON (line {e.lineno}): {e.msg}."
    if strict:
        raise moods_edit.MoodEditError(problem + " Not changed.")
    _config_problem = problem + " Using the built-in moods and seasons for now."
    return json.loads((HERE / "moods.json").read_text())


_cached: tuple[int, int, Finder] | None = None  # (store version, config mtime, finder)


def _get_store() -> RecipeStore:
    global _store
    if _store is None:
        client = PaprikaClient(os.environ.get("PAPRIKA_EMAIL", ""),
                               os.environ.get("PAPRIKA_PASSWORD", ""))
        minutes = int(os.environ.get("PAPRIKA_SYNC_MINUTES", "5"))
        _store = RecipeStore(client, max_age_seconds=minutes * 60)
    return _store


def _finder() -> tuple[Finder, dict | None]:
    """Searches always answer from the local copy; syncing runs in the background."""
    global _cached
    store = _get_store()
    info = None
    if store.is_stale():
        store.sync_in_background()
    if store.status.get("state") not in ("idle", None):
        info = dict(store.status)
        if not store.recipes:
            info["note"] = "First download still in progress; results will fill in as it continues."
        elif store.status.get("state") == "error":
            info["note"] = "Using the saved copy; the last sync failed."
    try:
        cfg_mtime = CONFIG_PATH.stat().st_mtime_ns if CONFIG_PATH.exists() else 0
    except OSError:
        cfg_mtime = -1
    if not _cached or _cached[0] != store.version or _cached[1] != cfg_mtime or _config_problem:
        cfg = _config()
        _cached = (store.version, cfg_mtime,
                   Finder(store.active_recipes(), store.categories, store.meals, cfg))
    if _config_problem:
        info = dict(info or {})
        info["moods_file_problem"] = _config_problem
    return _cached[2], info


def _with_sync(result: dict, sync_info: dict | None) -> dict:
    if sync_info:
        result["sync"] = sync_info
    return result


@mcp.tool()
def find_recipes(mood: str | None = None, season: str | None = None,
                 query: str | None = None, categories: list[str] | None = None,
                 max_minutes: int | None = None, min_rating: int | None = None,
                 favorites_only: bool = False, not_cooked_in_days: int | None = None,
                 limit: int = 10) -> dict:
    """Search the user's Paprika recipes by mood and/or season, ranked with reasons.

    mood: e.g. cozy, lazy, light, bright, indulgent, celebratory, adventurous,
      sick-day, baking (see list_moods_and_seasons; any other word is matched
      against category names and recipe text). Translate the user's feeling
      into the closest mood, and use max_minutes/query for extra constraints.
    season: winter/spring/summer/fall, or "now" for today's season. Also
      boosts current occasions (e.g. Thanksgiving) and recipes the user has
      cooked in this month in past years.
    query: words that must all appear (name, ingredients, notes, categories).
    categories: category names that must all match (sub-categories count).
    not_cooked_in_days: hide recipes cooked more recently than this.
    """
    f, s = _finder()
    return _with_sync(f.search(mood, season, query, categories, max_minutes, min_rating,
                               favorites_only, not_cooked_in_days, limit=limit), s)


@mcp.tool()
def whats_in_season(limit: int = 10, mood: str | None = None) -> dict:
    """Today's season, active occasions, and the best in-season recipes from the
    user's library (optionally blended with a mood)."""
    f, s = _finder()
    res = f.search(mood=mood, season="now", limit=limit)
    res["today"] = date.today().isoformat()
    res["season"] = season_for(date.today(), f.hemisphere)
    res["in_season_produce"] = f.config.get("seasons", {}).get(res["season"], {}).get("produce", [])
    return _with_sync(res, s)


@mcp.tool()
def get_recipe(uid_or_name: str) -> dict:
    """Full recipe (ingredients, directions, notes, times, categories) by uid or name."""
    f, s = _finder()
    key = uid_or_name.strip().lower()
    hit = next((r for r in f.recipes if r.get("uid", "").lower() == key), None) \
        or next((r for r in f.recipes if r.get("name", "").lower() == key), None) \
        or next((r for r in f.recipes if key in r.get("name", "").lower()), None)
    if not hit:
        return {"error": f"No recipe matching '{uid_or_name}'."}
    fields = ("uid", "name", "description", "ingredients", "directions", "notes",
              "servings", "prep_time", "cook_time", "total_time", "rating",
              "source", "source_url", "nutritional_info", "on_favorites")
    out = {k: hit.get(k) for k in fields if hit.get(k)}
    out["categories"] = f.display_categories(hit)
    out["minutes"] = recipe_minutes(hit)
    last = f.last_cooked.get(hit.get("uid"))
    out["last_cooked"] = last.isoformat() if last else None
    out["times_cooked"] = f.cook_count.get(hit.get("uid"), 0)
    return _with_sync(out, s)


@mcp.tool()
def list_categories() -> dict:
    """The user's Paprika category tree with recipe counts."""
    f, s = _finder()
    counts: dict[str, int] = {}
    for r in f.recipes:
        for uid in r.get("categories") or []:
            counts[uid] = counts.get(uid, 0) + 1
    children: dict[str | None, list] = {}
    for uid, c in f.categories.items():
        parent = c.get("parent_uid") if c.get("parent_uid") in f.categories else None
        children.setdefault(parent, []).append(uid)

    def node(uid):
        c = f.categories[uid]
        kids = sorted(children.get(uid, []), key=lambda u: f.categories[u].get("name", ""))
        n = {"name": c.get("name"), "recipes": counts.get(uid, 0)}
        if kids:
            n["children"] = [node(k) for k in kids]
        return n

    roots = sorted(children.get(None, []), key=lambda u: f.categories[u].get("name", ""))
    return _with_sync({"total_recipes": len(f.recipes), "categories": [node(u) for u in roots]}, s)


def _category_uid(f: Finder, name: str) -> str:
    """Category uid for a name, or 'Parent > Child' when two share a name."""
    parts = [p.strip().lower() for p in name.split(">")]
    hits = [u for u, lineage in f.cat_lineage.items()
            if lineage[:len(parts)] == parts[::-1]]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"No category called '{name}'. Use list_categories to see them; "
                         "this tool doesn't create new categories.")
    def path(u):
        names, cur = [], u
        while cur in f.categories and len(names) < 10:
            names.append(f.categories[cur].get("name", ""))
            cur = f.categories[cur].get("parent_uid")
        return " > ".join(reversed(names))
    paths = [path(u) for u in hits]
    raise ValueError(f"'{name}' matches {len(hits)} categories; name one as "
                     + " or ".join(f"'{p}'" for p in paths) + ".")


@mcp.tool()
def edit_recipe_categories(recipe: str, add: list[str] | None = None,
                           remove: list[str] | None = None) -> dict:
    """Add or remove categories on one of the user's Paprika recipes. The change
    syncs to their Paprika apps.

    recipe: uid (best) or name. If a name matches several recipes nothing
      changes and the matches are returned: ask which one.
    add / remove: existing category names, as list_categories shows them. If
      two categories share a name, write 'Parent > Child' (e.g. 'Region > Tiki').
      New categories aren't created; the user makes those in Paprika.
    """
    f, _ = _finder()
    hits = _match_recipes(f, recipe)
    if not hits:
        return {"error": f"No recipe matching '{recipe}'.", "changed": False}
    if len(hits) > 1:
        return {"error": f"'{recipe}' matches {len(hits)} recipes; say which one.",
                "changed": False,
                "matches": [{"uid": r["uid"], "name": r.get("name")} for r in hits[:15]]}
    if not add and not remove:
        return {"error": "Nothing to add or remove.", "changed": False}
    try:
        add_uids = [_category_uid(f, n) for n in add or []]
        remove_uids = {_category_uid(f, n) for n in remove or []}
    except ValueError as e:
        return {"error": str(e), "changed": False}
    target = hits[0]
    before = list(target.get("categories") or [])
    after = [u for u in before if u not in remove_uids]
    after += [u for u in dict.fromkeys(add_uids) if u not in after]
    if after == before:
        return {"changed": False, "recipe": target.get("name"),
                "categories": f.display_categories(target),
                "note": "It already had those categories."}

    def change(full: dict):
        cats = [u for u in (full.get("categories") or []) if u not in remove_uids]
        full["categories"] = cats + [u for u in dict.fromkeys(add_uids) if u not in cats]

    try:
        saved = _get_store().update_recipe(target["uid"], change)
    except PaprikaError as e:
        return {"error": f"Paprika didn't accept the change: {e}", "changed": False}
    names = lambda uids: [f.categories[u].get("name") for u in uids if u in f.categories]
    return {"changed": True, "recipe": saved.get("name"), "uid": target["uid"],
            "categories": names(saved.get("categories") or []),
            "added": names([u for u in add_uids if u not in before]),
            "removed": names([u for u in remove_uids if u in before])}


@mcp.tool()
def list_moods_and_seasons() -> dict:
    """The mood, season and occasion definitions used for matching, plus the
    path of the file the user can edit to change them."""
    cfg = _config()
    out = {"config_file": str(CONFIG_PATH),
           "moods": cfg.get("moods", {}), "seasons": cfg.get("seasons", {}),
           "occasions": cfg.get("occasions", [])}
    if _config_problem:
        out["problem"] = _config_problem
    return out


def _apply_edit(make_mutation, added_categories: list[str] | None) -> dict:
    """Run a moods.json edit; report errors plainly and flag unknown categories."""
    try:
        result = moods_edit.edit(CONFIG_PATH, lambda: _config(strict=True), make_mutation())
    except moods_edit.MoodEditError as e:
        return {"error": str(e), "changed": False}
    result["changed"] = True
    result["config_file"] = str(CONFIG_PATH)
    known = {c.get("name", "").lower() for c in _get_store().categories.values()}
    unknown = [c for c in (added_categories or []) if known and c.strip().lower() not in known]
    if unknown:
        result["warning"] = (f"Not (yet) a category in Paprika: {', '.join(unknown)}. "
                             "Kept anyway; it will match once a category by that name exists.")
    return result


@mcp.tool()
def edit_mood(name: str, add_categories: list[str] | None = None,
              remove_categories: list[str] | None = None,
              add_keywords: list[str] | None = None, remove_keywords: list[str] | None = None,
              add_avoid_keywords: list[str] | None = None,
              remove_avoid_keywords: list[str] | None = None,
              max_minutes: int | None = None, prefer_never_cooked: bool | None = None,
              delete: bool = False) -> dict:
    """Create, change or delete a mood in the user's moods.json. Only fields you
    pass change; lists are added to / removed from, not replaced.

    Confirm with the user before deleting a mood. categories are Paprika
    category names (a parent also covers its sub-categories); keywords match
    recipe name, ingredients, description and notes as whole words, allowing
    common endings (s, es, ed, ing, y, ies...), so "roast" hits "roasted" and
    "strawberr" hits "strawberries", but "pear" won't hit "pearl"; add compound
    words such as "applesauce" separately. avoid_keywords push recipes down. max_minutes sets a default
    time limit (0 removes it). prefer_never_cooked boosts untried recipes.
    The previous file is kept, so undo_mood_edit can revert the last change.
    """
    return _apply_edit(lambda: moods_edit.mood_mutation(
        name, add_categories, remove_categories, add_keywords, remove_keywords,
        add_avoid_keywords, remove_avoid_keywords, max_minutes, prefer_never_cooked,
        delete), add_categories)


@mcp.tool()
def edit_season(season: str, add_categories: list[str] | None = None,
                remove_categories: list[str] | None = None,
                add_produce: list[str] | None = None, remove_produce: list[str] | None = None,
                add_avoid_keywords: list[str] | None = None,
                remove_avoid_keywords: list[str] | None = None,
                hemisphere: str | None = None) -> dict:
    """Change a season (winter/spring/summer/fall) in moods.json: its categories,
    in-season produce words, and avoid words. hemisphere ("north"/"south")
    sets which way the seasons run. Lists are added to / removed from."""
    return _apply_edit(lambda: moods_edit.season_mutation(
        season, add_categories, remove_categories, add_produce, remove_produce,
        add_avoid_keywords, remove_avoid_keywords, hemisphere), add_categories)


@mcp.tool()
def edit_occasion(name: str, from_date: str | None = None, to_date: str | None = None,
                  add_categories: list[str] | None = None,
                  remove_categories: list[str] | None = None,
                  add_keywords: list[str] | None = None,
                  remove_keywords: list[str] | None = None, delete: bool = False) -> dict:
    """Create, change or delete an occasion (a yearly date window like
    Thanksgiving) in moods.json. Dates are MM-DD; a window may wrap the year
    end (12-20 to 01-05). Confirm with the user before deleting."""
    return _apply_edit(lambda: moods_edit.occasion_mutation(
        name, from_date, to_date, add_categories, remove_categories,
        add_keywords, remove_keywords, delete), add_categories)


@mcp.tool()
def undo_mood_edit() -> dict:
    """Restore moods.json to how it was before the last edit (calling it again
    re-applies that edit)."""
    try:
        moods_edit.undo(CONFIG_PATH)
    except (moods_edit.MoodEditError, ValueError) as e:
        return {"error": str(e), "changed": False}
    except OSError as e:
        return {"error": _unreadable_message(e) + " Not changed.", "changed": False}
    return {"changed": True, "config_file": str(CONFIG_PATH)}


_DEFAULT_MEALS = ["breakfast", "lunch", "dinner", "snack"]
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_day(when: str) -> date:
    """YYYY-MM-DD, today, tomorrow, or a weekday name (the next one, today included)."""
    w = when.strip().lower()
    today = date.today()
    if w in ("", "today", "tonight"):
        return today
    if w == "tomorrow":
        return today + timedelta(days=1)
    w = w.removeprefix("this ").removeprefix("next ")
    if w in _WEEKDAYS:
        return today + timedelta(days=(_WEEKDAYS.index(w) - today.weekday()) % 7)
    try:
        return date.fromisoformat(w)
    except ValueError:
        raise ValueError(f"Can't read the date '{when}'. Use YYYY-MM-DD, today, "
                         "tomorrow or a weekday name.") from None


def _meal_type(store: RecipeStore, meal: str) -> tuple[int, str | None, str]:
    """(type index, type uid if Paprika has one, display name) for a meal name."""
    want = meal.strip().lower().rstrip("s") or "dinner"
    types = sorted(store.meal_types(), key=lambda t: t.get("order_flag", 0))
    for i, t in enumerate(types):
        if (t.get("name") or "").strip().lower().rstrip("s") == want:
            idx = _DEFAULT_MEALS.index(want) if want in _DEFAULT_MEALS else i
            return idx, t.get("uid"), t.get("name")
    if want in _DEFAULT_MEALS:
        return _DEFAULT_MEALS.index(want), None, want.title()
    names = [t.get("name") for t in types] or [m.title() for m in _DEFAULT_MEALS]
    raise ValueError(f"No meal called '{meal}'. Choose one of: {', '.join(names)}.")


def _match_recipes(f: Finder, uid_or_name: str) -> list[dict]:
    """Exact uid, then exact name, then every name containing the text."""
    key = uid_or_name.strip().lower()
    for field in ("uid", "name"):
        exact = [r for r in f.recipes if r.get(field, "").lower() == key]
        if exact:
            return exact[:1]
    return [r for r in f.recipes if key in r.get("name", "").lower()]


@mcp.tool()
def add_to_meal_plan(recipe: str, day: str, meal: str = "dinner",
                     add_to_groceries: bool = False) -> dict:
    """Put a recipe on the user's Paprika meal plan; it shows up in the Paprika
    apps after they sync.

    recipe: uid (best, e.g. from find_recipes) or name. If a name matches
      several recipes nothing is added and the matches are returned: ask the
      user which one, then call again with its uid.
    day: YYYY-MM-DD, "today", "tomorrow", or a weekday name (the next one).
    meal: breakfast, lunch, dinner (default), snack, or a custom meal type.
    Each call adds one meal; call it once per recipe and day.
    add_to_groceries: also put the recipe's ingredients on the default grocery
      list (or use add_meal_plan_to_groceries afterwards for a whole week).
    """
    f, _ = _finder()
    hits = _match_recipes(f, recipe)
    if not hits:
        return {"error": f"No recipe matching '{recipe}'.", "added": False}
    if len(hits) > 1:
        return {"error": f"'{recipe}' matches {len(hits)} recipes; say which one.",
                "added": False,
                "matches": [{"uid": r["uid"], "name": r.get("name")} for r in hits[:15]]}
    store = _get_store()
    try:
        d = _parse_day(day)
        idx, type_uid, type_name = _meal_type(store, meal)
        saved = store.add_meal(hits[0], d.isoformat(), idx, type_uid)
    except ValueError as e:
        return {"error": str(e), "added": False}
    except PaprikaError as e:
        return {"error": f"Paprika didn't accept the meal: {e}", "added": False}
    result = {"added": True, "recipe": saved["name"], "recipe_uid": saved["recipe_uid"],
              "date": d.isoformat(), "weekday": d.strftime("%A"), "meal": type_name,
              "meal_uid": saved["uid"],
              "note": "Remove or move it in Paprika's meal planner if needed."}
    if add_to_groceries:
        result["groceries"] = _add_groceries(store, [saved], None, ["water"], False)
    return result


def _grocery_list(lists: list[dict], wanted: str | None) -> dict | None:
    """The named grocery list, else the one marked default, else the first."""
    lists = sorted(lists, key=lambda g: g.get("order_flag", 0))
    if wanted:
        for g in lists:
            if (g.get("name") or "").strip().lower() == wanted.strip().lower():
                return g
        raise ValueError(f"No grocery list called '{wanted}'. Lists: "
                         + ", ".join(g.get("name", "?") for g in lists))
    return next((g for g in lists if g.get("is_default")), lists[0] if lists else None)


def _aisle_map(store: RecipeStore) -> dict[str, tuple[str, str]]:
    """Ingredient name -> (aisle uid, aisle name), from what Paprika remembers."""
    try:
        aisles = {a["uid"]: a.get("name", "") for a in store.client.grocery_aisles()}
        return {g["name"].strip().lower(): (g["aisle_uid"], aisles.get(g["aisle_uid"], ""))
                for g in store.client.grocery_ingredients()
                if g.get("name") and g.get("aisle_uid")}
    except (PaprikaError, KeyError, TypeError):
        return {}


def _add_groceries(store: RecipeStore, meals: list[dict], list_name: str | None,
                   skip: list[str], preview: bool) -> dict:
    try:
        glist = _grocery_list(store.client.grocery_lists() or [], list_name)
        existing = store.client.groceries() or []
    except ValueError as e:
        return {"error": str(e), "added": 0}
    except PaprikaError as e:
        return {"error": f"Couldn't read the grocery list from Paprika: {e}", "added": 0}
    list_uid = glist.get("uid") if glist else None
    items, skipped, missing = groceries.build_items(
        meals, store.recipes, existing, list_uid, _aisle_map(store), skip)
    out = {"grocery_list": glist.get("name") if glist else "Groceries",
           "recipes": sorted({i["recipe"] for i in items}),
           "items": [{"item": i["name"], "recipe": i["recipe"],
                      **({"aisle": i["aisle"]} if i["aisle"] else {})} for i in items],
           "skipped": skipped}
    if missing:
        out["meals_without_recipe"] = missing
    if preview or not items:
        out.update(added=0, preview=preview)
        if not items:
            out["note"] = "Nothing new to add."
        return out
    try:
        store.client.save_groceries(items)
    except PaprikaError as e:
        return {**out, "error": f"Paprika didn't accept the groceries: {e}", "added": 0}
    out["added"] = len(items)
    return out


@mcp.tool()
def add_meal_plan_to_groceries(start: str = "today", days: int = 7,
                               recipes: list[str] | None = None,
                               skip: list[str] | None = None,
                               grocery_list: str | None = None,
                               preview: bool = False) -> dict:
    """Add the ingredients of the recipes on the user's Paprika meal plan to
    their grocery list (the default list unless grocery_list names another).

    start/days: which part of the plan, like list_meal_plan (default: the next 7 days).
    recipes: only these planned recipes (uids or names); default all in range.
    skip: ingredients to leave off, e.g. pantry staples the user has
      ("salt", "olive oil"). Default: ["water"].
    preview: true to show what would be added without changing anything;
      useful to confirm with the user first.
    Re-running is safe: lines already on the list (not yet bought) for the
    same recipe aren't added again. A recipe planned twice is added twice.
    """
    store = _get_store()
    try:
        first = _parse_day(start)
    except ValueError as e:
        return {"error": str(e), "added": 0}
    last = first + timedelta(days=max(1, min(days, 62)) - 1)
    try:  # the plan may have changed on the phone since the last sync
        fresh = store.client.meals()
        with store.lock:
            store.meals = fresh
            store.version += 1
    except PaprikaError:
        pass
    planned = []
    for m in store.meals:
        try:
            d = date.fromisoformat((m.get("date") or "")[:10])
        except ValueError:
            continue
        if first <= d <= last:
            planned.append(m)
    if recipes:
        f, _ = _finder()
        wanted = set()
        for r in recipes:
            hits = _match_recipes(f, r)
            if not hits:
                return {"error": f"No recipe matching '{r}'.", "added": 0}
            wanted.update(h["uid"] for h in hits)
        planned = [m for m in planned if m.get("recipe_uid") in wanted]
    if not planned:
        return {"from": first.isoformat(), "to": last.isoformat(), "added": 0,
                "note": "No planned meals with recipes in that range."}
    result = _add_groceries(store, planned, grocery_list,
                            ["water"] if skip is None else skip, preview)
    return {"from": first.isoformat(), "to": last.isoformat(), **result}


@mcp.tool()
def list_meal_plan(start: str = "today", days: int = 7) -> dict:
    """The user's Paprika meal plan from start (YYYY-MM-DD, today, tomorrow or a
    weekday) for the given number of days."""
    store = _get_store()
    try:
        first = _parse_day(start)
    except ValueError as e:
        return {"error": str(e)}
    last = first + timedelta(days=max(1, min(days, 62)) - 1)
    names = {i: m.title() for i, m in enumerate(_DEFAULT_MEALS)}
    for t in store.meal_types():
        if t.get("uid"):
            names[t["uid"]] = t.get("name")
    plan: dict[str, list] = {}
    for m in sorted(store.meals, key=lambda m: (m.get("date") or "", m.get("type", 0),
                                                m.get("order_flag", 0))):
        raw = (m.get("date") or "")[:10]
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        if first <= d <= last:
            plan.setdefault(f"{raw} ({d.strftime('%A')})", []).append({
                "meal": names.get(m.get("type_uid")) or names.get(m.get("type"), "Meal"),
                "recipe": m.get("name") or store.recipes.get(m.get("recipe_uid"), {}).get("name"),
                "recipe_uid": m.get("recipe_uid")})
    return {"from": first.isoformat(), "to": last.isoformat(), "plan": plan}


@mcp.tool()
def mood_season_gaps() -> dict:
    """Grid of recipe counts for every mood x season, to spot thin areas
    (e.g. few 'light' recipes for winter)."""
    f, s = _finder()
    return _with_sync({"grid": f.gaps()}, s)


@mcp.tool()
def sync_now() -> dict:
    """Start a sync with Paprika right away (normally automatic) and report progress.
    Call again to check on it."""
    store = _get_store()
    st = store.sync_in_background()
    return {"status": st, "recipes_saved": len(store.recipes)}


def _sync_forever():
    """HTTP mode: keep the cache fresh even when nobody is searching."""
    minutes = max(1, int(os.environ.get("PAPRIKA_SYNC_MINUTES", "5")))
    while True:
        try:
            store = _get_store()
            if store.is_stale():
                store.sync_in_background()
        except Exception as e:  # e.g. missing credentials; keep the server up
            print(f"scheduled sync failed: {e}", file=sys.stderr, flush=True)
        time.sleep(minutes * 60)


def _serve_http():
    if len(URL_SECRET) < 24 or not URL_SECRET.replace("-", "").replace("_", "").isalnum():
        sys.exit("PAPRIKA_URL_SECRET must be set to at least 24 letters/digits "
                 "(e.g. `openssl rand -hex 24`) to run in http mode.")
    import uvicorn
    from starlette.responses import PlainTextResponse

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return PlainTextResponse("ok")

    threading.Thread(target=_sync_forever, daemon=True).start()
    print(f"Paprika MCP listening on {mcp.settings.host}:{mcp.settings.port} "
          "at /<PAPRIKA_URL_SECRET>/mcp", file=sys.stderr, flush=True)
    # access_log off: request paths contain the secret.
    uvicorn.run(mcp.streamable_http_app(), host=mcp.settings.host,
                port=mcp.settings.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    if TRANSPORT == "http":
        _serve_http()
    elif TRANSPORT == "stdio":
        mcp.run()
    else:
        sys.exit(f"Unknown PAPRIKA_TRANSPORT {TRANSPORT!r}: use 'stdio' or 'http'.")
