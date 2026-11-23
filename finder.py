"""Mood / season scoring over a Paprika recipe library."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime

NORTH_SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
                 6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall", 11: "fall"}
SOUTH_FLIP = {"winter": "summer", "summer": "winter", "spring": "fall", "fall": "spring"}


def season_for(d: date, hemisphere: str = "north") -> str:
    s = NORTH_SEASONS[d.month]
    return SOUTH_FLIP[s] if hemisphere.lower().startswith("s") else s


def parse_minutes(text: str | None) -> int | None:
    """'1 hr 30 mins', '1:15', '45 minutes', '2 hours' -> minutes."""
    if not text:
        return None
    t = text.lower().strip()
    m = re.fullmatch(r"(\d+):(\d{2})", t)
    if m:
        return int(m[1]) * 60 + int(m[2])
    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", t)
    mins = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", t)
    if hours or mins:
        return int(float(hours[1]) * 60 if hours else 0) + (int(mins[1]) if mins else 0)
    m = re.fullmatch(r"(\d+)", t)
    return int(m[1]) if m else None


def _has(text: str, word: str) -> bool:
    """Match at a word start, so 'sage' doesn't hit 'sausage' but 'strawberr' hits 'strawberries'."""
    return re.search(r"(?<![a-z])" + re.escape(word.lower()), text) is not None


def recipe_minutes(r: dict) -> int | None:
    total = parse_minutes(r.get("total_time"))
    if total:
        return total
    parts = [parse_minutes(r.get("prep_time")), parse_minutes(r.get("cook_time"))]
    parts = [p for p in parts if p]
    return sum(parts) if parts else None


class Finder:
    def __init__(self, recipes: list[dict], categories: dict[str, dict],
                 meals: list[dict], config: dict, today: date | None = None):
        self.recipes = recipes
        self.categories = categories
        self.config = config
        self.today = today or date.today()
        self.hemisphere = config.get("hemisphere", "north")
        self._build_category_paths()
        self._build_history(meals)
        # precomputed per-recipe search data (fast with very large libraries)
        self._idx = [(r, self.recipe_category_names(r), self._text(r), recipe_minutes(r))
                     for r in recipes]

    # ---- indexes ---------------------------------------------------------
    def _build_category_paths(self):
        """uid -> [own name, parent name, grandparent name, ...] (lowercased)."""
        self.cat_lineage: dict[str, list[str]] = {}
        for uid in self.categories:
            names, seen, cur = [], set(), uid
            while cur and cur in self.categories and cur not in seen:
                seen.add(cur)
                names.append(self.categories[cur].get("name", "").lower())
                cur = self.categories[cur].get("parent_uid")
            self.cat_lineage[uid] = names

    def _build_history(self, meals: list[dict]):
        self.last_cooked: dict[str, date] = {}
        self.cook_months: dict[str, Counter] = {}
        self.cook_count: Counter = Counter()
        for m in meals or []:
            uid, raw = m.get("recipe_uid"), (m.get("date") or "")[:10]
            if not uid or not raw:
                continue
            try:
                d = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d > self.today:
                continue  # planned, not cooked yet
            self.cook_count[uid] += 1
            self.cook_months.setdefault(uid, Counter())[d.month] += 1
            if uid not in self.last_cooked or d > self.last_cooked[uid]:
                self.last_cooked[uid] = d

    def recipe_category_names(self, r: dict, with_ancestors: bool = True) -> list[str]:
        out: list[str] = []
        for uid in r.get("categories") or []:
            lineage = self.cat_lineage.get(uid, [])
            out.extend(lineage if with_ancestors else lineage[:1])
        return list(dict.fromkeys(n for n in out if n))

    def display_categories(self, r: dict) -> list[str]:
        return [self.categories[u]["name"] for u in (r.get("categories") or []) if u in self.categories]

    @staticmethod
    def _text(r: dict) -> str:
        return " ".join(str(r.get(k) or "") for k in
                        ("name", "ingredients", "description", "notes")).lower()

    # ---- context ---------------------------------------------------------
    def current_occasions(self, d: date | None = None) -> list[dict]:
        d = d or self.today
        key = d.strftime("%m-%d")
        out = []
        for occ in self.config.get("occasions", []):
            lo, hi = occ["from"], occ["to"]
            if (lo <= key <= hi) if lo <= hi else (key >= lo or key <= hi):
                out.append(occ)
        return out

    # ---- scoring ---------------------------------------------------------
    def _profile_score(self, r, cats, text, label, profile, weight_cat=3.0, kw_field="keywords"):
        score, reasons = 0.0, []
        wanted = {c.lower() for c in profile.get("categories", [])} | {label.lower()}
        hits = [c for c in cats if c in wanted]
        if hits:
            score += weight_cat
            reasons.append(f"{label}: category {hits[0].title()}")
        kws = [k for k in profile.get(kw_field, []) if _has(text, k)]
        if kws:
            score += min(len(kws), 3) * 1.0
            reasons.append(f"{label}: {', '.join(kws[:3])}")
        bad = [k for k in profile.get("avoid_keywords", []) if _has(text, k)]
        if bad:
            score -= 1.5 * len(bad)
        return score, reasons

    def search(self, mood: str | None = None, season: str | None = None,
               query: str | None = None, categories: list[str] | None = None,
               max_minutes: int | None = None, min_rating: int | None = None,
               favorites_only: bool = False, not_cooked_in_days: int | None = None,
               include_occasions: bool = True, require_season: bool = False,
               limit: int = 10) -> dict:
        moods = self.config.get("moods", {})
        seasons = self.config.get("seasons", {})
        context: dict = {}

        mood_profile = None
        if mood:
            key = mood.lower().strip()
            mood_profile = moods.get(key) or {"categories": [key], "keywords": [key]}
            context["mood"] = key if key in moods else f"{key} (not in moods.json; matched by name)"
            if mood_profile.get("max_minutes") and not max_minutes:
                max_minutes = mood_profile["max_minutes"]

        season_key = None
        if season:
            season_key = season.lower().strip()
            if season_key in ("now", "current", "today", "in season"):
                season_key = season_for(self.today, self.hemisphere)
            if season_key == "autumn":
                season_key = "fall"
            context["season"] = season_key
        is_now = season_key == season_for(self.today, self.hemisphere)
        occasions = self.current_occasions() if (include_occasions and is_now) else []
        if occasions:
            context["occasions"] = [o["name"] for o in occasions]

        want_cats = [c.lower() for c in categories or []]
        q_terms = [t for t in re.split(r"\s+", (query or "").lower()) if t]

        results = []
        for r, cats, text, mins in self._idx:
            uid = r.get("uid")

            # hard filters
            if want_cats and not all(any(w in c for c in cats) for w in want_cats):
                continue
            if q_terms and not all(t in text or any(t in c for c in cats) for t in q_terms):
                continue
            if max_minutes and (mins is None or mins > max_minutes):
                continue
            rating = r.get("rating") or 0
            if min_rating and rating < min_rating:
                continue
            if favorites_only and not r.get("on_favorites"):
                continue
            last = self.last_cooked.get(uid)
            if not_cooked_in_days and last and (self.today - last).days < not_cooked_in_days:
                continue

            score, reasons = 0.0, []
            if mood_profile:
                s, why = self._profile_score(r, cats, text, mood.lower(), mood_profile)
                if s <= 0:
                    continue
                score += s
                reasons += why
                if mood_profile.get("prefer_never_cooked") and uid not in self.cook_count:
                    score += 1
                    reasons.append("never cooked yet")
            if season_key:
                prof = seasons.get(season_key, {"categories": [season_key]})
                s, why = self._profile_score(r, cats, text, season_key, prof, kw_field="produce")
                for occ in occasions:
                    s2, why2 = self._profile_score(r, cats, text, occ["name"], occ)
                    s += s2
                    why += why2
                # your own history: cooked in this calendar month before
                month_hits = self.cook_months.get(uid, Counter())[self.today.month]
                if month_hits and is_now:
                    s += 1.5 + min(month_hits, 3) * 0.5
                    why.append(f"you've cooked it in {self.today.strftime('%B')} before")
                if s <= 0 and (require_season or not mood_profile):
                    continue
                score += s
                reasons += why

            # tie-breakers
            score += rating * 0.4
            if r.get("on_favorites"):
                score += 0.5
                reasons.append("favorite")
            if rating >= 4:
                reasons.append(f"rated {rating}★")
            if last and (self.today - last).days > 365 and rating >= 4:
                score += 0.5
                reasons.append("haven't made it in over a year")

            results.append((score, r, reasons, mins, last))

        results.sort(key=lambda x: (-x[0], x[1].get("name", "")))
        return {
            "context": context,
            "matches": len(results),
            "recipes": [{
                "uid": r.get("uid"),
                "name": r.get("name"),
                "categories": self.display_categories(r),
                "minutes": mins,
                "rating": r.get("rating") or 0,
                "last_cooked": last.isoformat() if last else None,
                "why": reasons[:5],
                "score": round(score, 1),
            } for score, r, reasons, mins, last in results[:limit]],
        }

    def gaps(self) -> dict:
        """Count recipes for every mood x season pair to spot thin spots."""
        moods = list(self.config.get("moods", {}))
        seasons = list(self.config.get("seasons", {}))
        grid = {}
        for m in moods:
            grid[m] = {}
            for s in seasons:
                res = self.search(mood=m, season=s, include_occasions=False,
                                  require_season=True, limit=0)
                grid[m][s] = res["matches"]
        return grid
