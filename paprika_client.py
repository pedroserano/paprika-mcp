"""Minimal client for Paprika 3's (unofficial) cloud sync API, with an
incremental, resumable, rate-limit-friendly local cache: only recipes whose
hash changed are downloaded, and progress is saved as it goes."""

from __future__ import annotations

import base64
import fcntl
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_URL = os.environ.get("PAPRIKA_BASE_URL", "https://www.paprikaapp.com").rstrip("/")
CACHE_DIR = Path(os.environ.get("PAPRIKA_CACHE_DIR", Path.home() / ".paprika-mood"))
USER_AGENT = "paprika-mcp/1.1"
# Fields we keep per recipe (keeps the cache small for big libraries).
KEEP = ("uid", "hash", "name", "ingredients", "directions", "description", "notes",
        "nutritional_info", "servings", "difficulty", "prep_time", "cook_time",
        "total_time", "source", "source_url", "image_url", "photo_url", "categories",
        "rating", "in_trash", "on_favorites", "created")


class PaprikaError(RuntimeError):
    pass


class RateLimited(PaprikaError):
    def __init__(self, retry_after: float | None):
        super().__init__("Paprika asked us to slow down (HTTP 429)")
        self.retry_after = retry_after


class _FallBack(PaprikaError):
    pass


class PaprikaClient:
    def __init__(self, email: str, password: str):
        if not email or not password:
            raise PaprikaError("Set PAPRIKA_EMAIL and PAPRIKA_PASSWORD.")
        self.email = email
        self.password = password
        self.token: str | None = None
        self.mode = "v2"  # falls back to v1 basic auth if v2 login fails

    def _request(self, path: str, data: dict | None = None, auth: bool = True,
                 raw_body: bytes | None = None, content_type: str | None = None):
        url = f"{BASE_URL}{path}"
        body = urllib.parse.urlencode(data).encode() if data is not None else raw_body
        req = urllib.request.Request(url, data=body,
                                     method="POST" if body is not None else "GET")
        if content_type:
            req.add_header("Content-Type", content_type)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept-Encoding", "gzip")
        if auth:
            if self.mode == "v2":
                req.add_header("Authorization", f"Bearer {self._get_token()}")
            else:
                cred = base64.b64encode(f"{self.email}:{self.password}".encode()).decode()
                req.add_header("Authorization", f"Basic {cred}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    ra = float(ra) if ra else None
                except ValueError:
                    ra = None
                raise RateLimited(ra) from e
            if e.code == 401 and auth and self.mode == "v2" and self.token:
                self.token = None  # expired token: log in again once
                return self._request(path, data, auth, raw_body, content_type)
            if e.code == 401:
                raise PaprikaError("Paprika rejected the email/password (HTTP 401).") from e
            raise PaprikaError(f"Paprika returned HTTP {e.code} for {path}") from e
        except urllib.error.URLError as e:
            raise PaprikaError(f"Could not reach Paprika: {e.reason}") from e
        except TimeoutError as e:
            raise PaprikaError("Paprika timed out") from e
        payload = json.loads(raw)
        if isinstance(payload, dict) and "error" in payload:
            raise PaprikaError(f"Paprika error: {payload['error']}")
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    def _get_token(self) -> str:
        if self.token:
            return self.token
        try:
            result = self._request("/api/v2/account/login/",
                                   {"email": self.email, "password": self.password},
                                   auth=False)
            self.token = result["token"]
        except RateLimited:
            raise
        except (PaprikaError, KeyError, TypeError):
            self.mode = "v1"
            raise _FallBack()
        return self.token

    def _api(self, endpoint: str):
        try:
            return self._request(f"/api/{self.mode}/sync/{endpoint}/")
        except _FallBack:
            return self._request(f"/api/v1/sync/{endpoint}/")

    def recipe_index(self) -> list[dict]:
        return self._api("recipes")

    def recipe(self, uid: str) -> dict:
        return self._api(f"recipe/{uid}")

    def categories(self) -> list[dict]:
        return self._api("categories")

    def meals(self) -> list[dict]:
        return self._api("meals")

    def meal_types(self) -> list[dict]:
        return self._api("mealtypes")

    def _upload(self, endpoint: str, obj) -> object:
        """POST a sync object the way the Paprika apps do: gzipped JSON sent as
        a multipart file field named 'data'."""
        boundary = f"----paprika-mcp-{uuid.uuid4().hex}"
        blob = gzip.compress(json.dumps(obj).encode())
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="data"; filename="file"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode() \
            + blob + f"\r\n--{boundary}--\r\n".encode()
        ctype = f"multipart/form-data; boundary={boundary}"
        try:
            return self._request(f"/api/{self.mode}/sync/{endpoint}/",
                                 raw_body=body, content_type=ctype)
        except _FallBack:
            return self._request(f"/api/v1/sync/{endpoint}/",
                                 raw_body=body, content_type=ctype)

    def _notify(self):
        try:  # tell the user's devices to pull the change now
            self._request(f"/api/{self.mode}/sync/notify/", raw_body=b"")
        except PaprikaError:
            pass

    def save_meals(self, meals: list[dict]) -> object:
        result = self._upload("meals", meals)
        self._notify()
        return result

    def grocery_lists(self) -> list[dict]:
        return self._api("grocerylists")

    def groceries(self) -> list[dict]:
        return self._api("groceries")

    def grocery_aisles(self) -> list[dict]:
        return self._api("groceryaisles")

    def grocery_ingredients(self) -> list[dict]:
        """Paprika's remembered ingredient -> aisle assignments."""
        return self._api("groceryingredients")

    def save_groceries(self, items: list[dict]) -> object:
        result = self._upload("groceries", items)
        self._notify()
        return result


class Pacer:
    """Polite request pacing: speeds up slowly, backs off hard on 429."""

    def __init__(self, delay: float = 0.4, min_delay: float = 0.15, max_delay: float = 10.0):
        self.delay, self.min, self.max = delay, min_delay, max_delay
        self.streak = 0
        self.backoff = 15.0

    def ok(self):
        self.streak += 1
        self.backoff = 15.0
        if self.streak >= 50:
            self.delay = max(self.min, self.delay * 0.9)
            self.streak = 0
        time.sleep(self.delay)

    def limited(self, retry_after: float | None) -> float:
        self.streak = 0
        self.delay = min(self.max, self.delay * 1.6 + 0.1)
        wait = retry_after if retry_after else self.backoff
        self.backoff = min(self.backoff * 2, 300.0)
        return wait


class RecipeStore:
    """Local mirror of the Paprika library, refreshed incrementally."""

    def __init__(self, client: PaprikaClient, max_age_seconds: int = 300):
        self.client = client
        self.max_age = max_age_seconds
        self.path = CACHE_DIR / "cache.json"
        self.lock = threading.RLock()
        self.recipes: dict[str, dict] = {}
        self.categories: dict[str, dict] = {}
        self.meals: list[dict] = []
        self.last_sync = 0.0
        self.version = 0
        self.status: dict = {"state": "idle"}
        self._thread: threading.Thread | None = None
        self._meal_types: list[dict] | None = None
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.recipes = data.get("recipes", {})
                self.categories = data.get("categories", {})
                self.meals = data.get("meals", [])
                self.last_sync = data.get("last_sync", 0.0)
                self.version += 1
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            blob = json.dumps({"recipes": self.recipes, "categories": self.categories,
                               "meals": self.meals, "last_sync": self.last_sync})
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ---- syncing -----------------------------------------------------------
    def is_stale(self) -> bool:
        return time.time() - self.last_sync >= self.max_age

    def sync_in_background(self) -> dict:
        """Start a sync thread if one isn't running; never blocks."""
        with self.lock:
            if self._thread and self._thread.is_alive():
                return self.status
            self._thread = threading.Thread(target=self._bg_sync, daemon=True)
            self.status = {"state": "starting"}
            self._thread.start()
        return self.status

    def _bg_sync(self):
        try:
            self.sync()
        except Exception as e:  # keep serving cached data
            self.status = {"state": "error", "error": str(e)}

    def sync(self, progress=None) -> dict:
        """Blocking sync. progress(done, total, note) is called as recipes download.
        Safe to interrupt: progress is saved every 200 recipes and on exit."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        lockf = open(CACHE_DIR / "sync.lock", "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lockf.close()
            self.status = {"state": "busy", "note": "another sync (e.g. the installer) is running"}
            return self.status
        pacer = Pacer()
        done = 0
        try:
            self.status = {"state": "checking"}
            index = {r["uid"]: r["hash"] for r in self._retry(self.client.recipe_index, pacer, progress)}
            with self.lock:
                changed = [u for u, h in index.items() if self.recipes.get(u, {}).get("hash") != h]
                removed = [u for u in self.recipes if u not in index]
                for u in removed:
                    del self.recipes[u]
            # Categories and meals first: they're one request each, and recipes only
            # carry category uids, so without the names nothing matches by category
            # until a long first download finishes.
            cats = self._retry(self.client.categories, pacer, progress)
            try:
                meals = self._retry(self.client.meals, pacer, progress)
            except PaprikaError:
                meals = self.meals
            with self.lock:
                self.categories = {c["uid"]: c for c in cats}
                self.meals = meals
                self.version += 1
            self._save()
            total = len(changed)
            for uid in changed:
                r = self._retry(lambda: self.client.recipe(uid), pacer, progress, done, total)
                with self.lock:
                    self.recipes[uid] = {k: r.get(k) for k in KEEP if k in r}
                    self.version += 1
                done += 1
                self.status = {"state": "downloading", "done": done, "total": total}
                if progress:
                    progress(done, total, None)
                if done % 200 == 0:
                    self._save()
            with self.lock:
                self.last_sync = time.time()
                self.version += 1
            self.status = {"state": "idle", "last_result": {
                "updated": total, "removed": len(removed), "total": len(index)}}
            return self.status["last_result"]
        finally:
            if done:
                self._save()
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()

    def _retry(self, fn, pacer: Pacer, progress=None, done=0, total=0, attempts=12):
        for _ in range(attempts):
            try:
                result = fn()
                pacer.ok()
                return result
            except RateLimited as e:
                wait = pacer.limited(e.retry_after)
                self.status = {"state": "waiting", "seconds": int(wait), "done": done, "total": total}
                if progress:
                    progress(done, total, f"Paprika asked us to slow down; waiting {int(wait)}s")
                time.sleep(wait)
            except PaprikaError as e:
                if "401" in str(e):
                    raise
                time.sleep(5)  # transient network hiccup
        raise PaprikaError("Paprika kept refusing requests; run the sync again later to resume.")

    def meal_types(self) -> list[dict]:
        """Meal types (Breakfast, Lunch, Dinner, Snacks, plus any custom ones),
        fetched once."""
        if self._meal_types is None:
            try:
                self._meal_types = self.client.meal_types() or []
            except PaprikaError:
                self._meal_types = []
        return self._meal_types

    def add_meal(self, recipe: dict, day: str, type_index: int,
                 type_uid: str | None) -> dict:
        """Put a recipe on the meal plan for day (YYYY-MM-DD)."""
        with self.lock:
            order = sum(1 for m in self.meals
                        if (m.get("date") or "")[:10] == day and m.get("type") == type_index)
        meal = {"uid": str(uuid.uuid4()).upper(), "recipe_uid": recipe["uid"],
                "date": f"{day} 00:00:00", "type": type_index,
                "name": recipe.get("name", ""), "order_flag": order}
        if type_uid:
            meal["type_uid"] = type_uid
        self.client.save_meals([meal])
        with self.lock:
            self.meals = self.meals + [meal]
            self.version += 1
        self._save()
        return meal

    def active_recipes(self) -> list[dict]:
        with self.lock:
            return [r for r in self.recipes.values() if not r.get("in_trash")]
