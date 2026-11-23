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

    def _request(self, path: str, data: dict | None = None, auth: bool = True):
        url = f"{BASE_URL}{path}"
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
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
                return self._request(path, data, auth)
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
            cats = self._retry(self.client.categories, pacer, progress)
            try:
                meals = self._retry(self.client.meals, pacer, progress)
            except PaprikaError:
                meals = self.meals
            with self.lock:
                self.categories = {c["uid"]: c for c in cats}
                self.meals = meals
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

    def active_recipes(self) -> list[dict]:
        with self.lock:
            return [r for r in self.recipes.values() if not r.get("in_trash")]
