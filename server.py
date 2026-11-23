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
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

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


def _config() -> dict:
    """User copy in ~/.paprika-mood/moods.json (created from the bundled default)."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text((HERE / "moods.json").read_text())
    return json.loads(CONFIG_PATH.read_text())


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
    cfg_mtime = int(CONFIG_PATH.stat().st_mtime) if CONFIG_PATH.exists() else 0
    cfg = _config()
    if not _cached or _cached[0] != store.version or _cached[1] != cfg_mtime:
        _cached = (store.version, cfg_mtime,
                   Finder(store.active_recipes(), store.categories, store.meals, cfg))
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


@mcp.tool()
def list_moods_and_seasons() -> dict:
    """The mood, season and occasion definitions used for matching, plus the
    path of the file the user can edit to change them."""
    cfg = _config()
    return {"config_file": str(CONFIG_PATH),
            "moods": cfg.get("moods", {}), "seasons": cfg.get("seasons", {}),
            "occasions": cfg.get("occasions", [])}


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
