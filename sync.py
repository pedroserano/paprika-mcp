"""Download / update your Paprika library with progress. Safe to stop (Ctrl+C)
and re-run: it picks up where it left off.

Reads PAPRIKA_EMAIL / PAPRIKA_PASSWORD from the environment, or from the
Claude Desktop config the installer wrote."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from paprika_client import PaprikaClient, PaprikaError, RecipeStore  # noqa: E402


def creds():
    e, p = os.environ.get("PAPRIKA_EMAIL"), os.environ.get("PAPRIKA_PASSWORD")
    if e and p:
        return e, p
    cfg = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    servers = json.loads(cfg.read_text())["mcpServers"]
    env = (servers.get("Paprika") or servers["paprika-mood"])["env"]
    return env["PAPRIKA_EMAIL"], env["PAPRIKA_PASSWORD"]


def main():
    email, pw = creds()
    store = RecipeStore(PaprikaClient(email.strip(), pw), 0)
    have = len(store.recipes)
    if have:
        print(f"Already saved: {have} recipes. Checking for the rest / changes...")
    start = time.time()

    def progress(done, total, note):
        if note:
            print(f"\r  {done} / {total}  - {note}            ", end="", flush=True)
            return
        rate = done / max(time.time() - start, 1)
        left = (total - done) / rate if rate else 0
        eta = f"{int(left // 3600)}h {int(left % 3600 // 60)}m" if left >= 3600 else f"{int(left // 60)}m {int(left % 60)}s"
        print(f"\r  {done} / {total} recipes   ~{eta} left            ", end="", flush=True)

    try:
        r = store.sync(progress=progress)
    except KeyboardInterrupt:
        print(f"\nStopped. {len(store.recipes)} recipes saved; run this again to resume.")
        return 0
    except PaprikaError as e:
        print(f"\n{e}\n{len(store.recipes)} recipes saved; run this again to resume.")
        return 1
    if r.get("state") == "busy":
        print("Another sync is already running (probably Claude in the background). Try again later.")
        return 0
    print(f"\nAll set: {r['total']} recipes ({r['updated']} downloaded this run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
