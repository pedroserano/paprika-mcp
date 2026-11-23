"""Safe, validated edits to the user's moods.json, so Claude can change mood,
season and occasion definitions (e.g. from a phone) without hand-editing JSON.

Every change is checked, the previous file is kept as moods.json.bak, and the
write is atomic, so a bad edit can't leave a broken or half-written file."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date
from pathlib import Path

SEASONS = ("winter", "spring", "summer", "fall")
MAX_NAME, MAX_ITEM, MAX_ITEMS = 40, 60, 150
_lock = threading.Lock()


class MoodEditError(ValueError):
    pass


# ---- validation ------------------------------------------------------------
def _clean_name(name: str, what: str) -> str:
    n = (name or "").strip().lower()
    if not n or len(n) > MAX_NAME or not re.fullmatch(r"[\w][\w '&-]*", n):
        raise MoodEditError(f"{what} name must be 1-{MAX_NAME} letters/digits/spaces/-/'/&, got {name!r}.")
    return n


def _clean_items(items, field: str) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    out = []
    for it in items:
        if not isinstance(it, str) or not it.strip():
            raise MoodEditError(f"{field}: every entry must be non-empty text.")
        it = " ".join(it.split())
        if len(it) > MAX_ITEM:
            raise MoodEditError(f"{field}: {it[:20]!r}... is longer than {MAX_ITEM} characters.")
        out.append(it)
    return out


def _check_mmdd(v: str, field: str) -> str:
    v = (v or "").strip()
    m = re.fullmatch(r"(\d{2})-(\d{2})", v)
    if not m:
        raise MoodEditError(f"{field} must be MM-DD (e.g. 11-01), got {v!r}.")
    try:
        date(2024, int(m[1]), int(m[2]))  # leap year, so 02-29 is allowed
    except ValueError:
        raise MoodEditError(f"{field} {v!r} isn't a real date.") from None
    return v


def validate(cfg: dict) -> None:
    """Raise MoodEditError if cfg isn't a config the finder can use."""
    if not isinstance(cfg, dict):
        raise MoodEditError("Config must be a JSON object.")
    if cfg.get("hemisphere", "north") not in ("north", "south"):
        raise MoodEditError("hemisphere must be 'north' or 'south'.")
    for section in ("moods", "seasons"):
        profiles = cfg.get(section, {})
        if not isinstance(profiles, dict):
            raise MoodEditError(f"'{section}' must be an object.")
        for name, p in profiles.items():
            if not isinstance(p, dict):
                raise MoodEditError(f"{section}.{name} must be an object.")
            for k in ("categories", "keywords", "produce", "avoid_keywords"):
                v = p.get(k, [])
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise MoodEditError(f"{section}.{name}.{k} must be a list of text.")
                if len(v) > MAX_ITEMS:
                    raise MoodEditError(f"{section}.{name}.{k} has more than {MAX_ITEMS} entries.")
            mm = p.get("max_minutes")
            if mm is not None and not (isinstance(mm, int) and 1 <= mm <= 1440):
                raise MoodEditError(f"{section}.{name}.max_minutes must be 1-1440.")
    occs = cfg.get("occasions", [])
    if not isinstance(occs, list):
        raise MoodEditError("'occasions' must be a list.")
    for o in occs:
        if not isinstance(o, dict) or not o.get("name"):
            raise MoodEditError("Each occasion needs a name.")
        _check_mmdd(o.get("from", ""), f"occasion {o['name']!r} from")
        _check_mmdd(o.get("to", ""), f"occasion {o['name']!r} to")


# ---- file I/O ----------------------------------------------------------------
def _write(path: Path, cfg: dict) -> None:
    validate(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(".json.bak")
        bak.write_text(path.read_text())
        os.chmod(bak, 0o600)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _merge(current: list, add: list[str], remove: list[str]) -> list[str]:
    """Add (no case-insensitive duplicates) and remove (case-insensitive)."""
    drop = {r.lower() for r in remove}
    out = [c for c in current if c.lower() not in drop]
    have = {c.lower() for c in out}
    for a in add:
        if a.lower() not in have and a.lower() not in drop:
            out.append(a)
            have.add(a.lower())
    return out


def _apply_lists(profile: dict, changes: dict[str, tuple[list, list]]) -> None:
    for field, (add, remove) in changes.items():
        add, remove = _clean_items(add, field), _clean_items(remove, field)
        if add or remove:
            merged = _merge(profile.get(field, []), add, remove)
            if merged:
                profile[field] = merged
            else:
                profile.pop(field, None)


def edit(path: Path, load, mutate) -> dict:
    """Load the config, apply mutate(cfg) -> summary, validate and save."""
    with _lock:
        cfg = load()
        summary = mutate(cfg)
        _write(path, cfg)
        return summary


def undo(path: Path) -> None:
    with _lock:
        bak = path.with_suffix(".json.bak")
        if not bak.exists():
            raise MoodEditError("There's no earlier version to restore.")
        prev = json.loads(bak.read_text())
        validate(prev)
        cur = path.read_text() if path.exists() else None
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(bak.read_text())
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        if cur is not None:  # so undo can itself be undone
            bak.write_text(cur)


# ---- the three kinds of edit -----------------------------------------------
def mood_mutation(name, add_categories, remove_categories, add_keywords, remove_keywords,
                  add_avoid_keywords, remove_avoid_keywords, max_minutes, prefer_never_cooked,
                  delete):
    key = _clean_name(name, "Mood")

    def mutate(cfg):
        moods = cfg.setdefault("moods", {})
        if delete:
            if key not in moods:
                raise MoodEditError(f"There's no mood called {key!r}.")
            del moods[key]
            return {"deleted_mood": key}
        created = key not in moods
        p = moods.setdefault(key, {})
        _apply_lists(p, {"categories": (add_categories, remove_categories),
                         "keywords": (add_keywords, remove_keywords),
                         "avoid_keywords": (add_avoid_keywords, remove_avoid_keywords)})
        if max_minutes is not None:
            if max_minutes == 0:
                p.pop("max_minutes", None)
            else:
                p["max_minutes"] = max_minutes
        if prefer_never_cooked is not None:
            if prefer_never_cooked:
                p["prefer_never_cooked"] = True
            else:
                p.pop("prefer_never_cooked", None)
        if created and not (p.get("categories") or p.get("keywords")):
            raise MoodEditError("A new mood needs at least one category or keyword.")
        return {"mood": key, "created": created, "definition": p}
    return mutate


def season_mutation(season, add_categories, remove_categories, add_produce, remove_produce,
                    add_avoid_keywords, remove_avoid_keywords, hemisphere):
    key = (season or "").strip().lower()
    key = "fall" if key == "autumn" else key
    if key not in SEASONS:
        raise MoodEditError(f"season must be one of {', '.join(SEASONS)}.")
    if hemisphere is not None and hemisphere not in ("north", "south"):
        raise MoodEditError("hemisphere must be 'north' or 'south'.")

    def mutate(cfg):
        p = cfg.setdefault("seasons", {}).setdefault(key, {})
        _apply_lists(p, {"categories": (add_categories, remove_categories),
                         "produce": (add_produce, remove_produce),
                         "avoid_keywords": (add_avoid_keywords, remove_avoid_keywords)})
        out = {"season": key, "definition": p}
        if hemisphere:
            cfg["hemisphere"] = hemisphere
            out["hemisphere"] = hemisphere
        return out
    return mutate


def occasion_mutation(name, from_date, to_date, add_categories, remove_categories,
                      add_keywords, remove_keywords, delete):
    label = " ".join((name or "").split())
    if not label or len(label) > MAX_NAME:
        raise MoodEditError(f"Occasion name must be 1-{MAX_NAME} characters.")

    def mutate(cfg):
        occs = cfg.setdefault("occasions", [])
        idx = next((i for i, o in enumerate(occs) if o.get("name", "").lower() == label.lower()), None)
        if delete:
            if idx is None:
                raise MoodEditError(f"There's no occasion called {label!r}.")
            occs.pop(idx)
            return {"deleted_occasion": label}
        created = idx is None
        if created:
            if not (from_date and to_date):
                raise MoodEditError("A new occasion needs from_date and to_date (MM-DD).")
            occs.append({"name": label})
            idx = len(occs) - 1
        o = occs[idx]
        if from_date:
            o["from"] = _check_mmdd(from_date, "from_date")
        if to_date:
            o["to"] = _check_mmdd(to_date, "to_date")
        _apply_lists(o, {"categories": (add_categories, remove_categories),
                         "keywords": (add_keywords, remove_keywords)})
        if created and not (o.get("categories") or o.get("keywords")):
            raise MoodEditError("A new occasion needs at least one category or keyword.")
        return {"occasion": o["name"], "created": created, "definition": o}
    return mutate
