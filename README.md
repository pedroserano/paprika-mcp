# Paprika connector for Claude

Lets Claude search your Paprika 3 recipes by **mood**, **season**, **occasion**, and **your own cooking history**. It stays live: before each search it checks Paprika's cloud sync (at most every 5 minutes) and downloads only the recipes that changed.

## What you can ask Claude
- "I'm wiped, something cozy under 45 minutes"
- "What's in season that I haven't made in a while?"
- "Light winter dinners rated 4 stars or more"
- "Which moods and seasons is my collection thin on?"
- "Plan 3 cozy dinners and 1 baking project for this week"

## Tools
| Tool | What it does |
|---|---|
| `find_recipes` | Mood, season, keyword, category, time, rating and "not cooked lately" filters. Results are ranked and each shows why it matched. |
| `whats_in_season` | Today's season, current occasions (e.g. Thanksgiving), and the best matching recipes |
| `get_recipe` | Full recipe, plus when you last cooked it and how many times |
| `list_categories` | Your category tree with recipe counts |
| `list_moods_and_seasons` | Shows the current mood and season definitions |
| `mood_season_gaps` | Mood × season count grid, to find thin spots |
| `edit_mood` / `edit_season` / `edit_occasion` | Ask Claude to change your mood, season or occasion definitions, e.g. "add my Braises category to cozy" or "make a date-night mood". Changes are checked before saving. |
| `undo_mood_edit` | Undoes the last mood, season or occasion change |
| `sync_now` | Syncs immediately |

## Setup (Claude Desktop on Mac)
Quick way: clone or download this repo to `~/paprika-mcp`, then run `bash ~/paprika-mcp/install.sh`. It asks for your Paprika login (the password is hidden as you type), adds the connector to Claude Desktop, and downloads your library.

Manual setup:
1. **Turn on sync in Paprika:** Settings → Sync, signed in with a Paprika Cloud Sync account.
2. **Install:** Use Python 3.10+. In this folder, run:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
   ```
3. **Add it to Claude Desktop:** Go to Settings → Developer → Edit Config. Add the following, using your own full paths:
   ```json
   {
     "mcpServers": {
       "Paprika": {
         "command": "/full/path/to/paprika-mcp/.venv/bin/python",
         "args": ["/full/path/to/paprika-mcp/server.py"],
         "env": {
           "PAPRIKA_EMAIL": "you@example.com",
           "PAPRIKA_PASSWORD": "your-paprika-password"
         }
       }
     }
   }
   ```
   On Windows, the command path is `...\.venv\Scripts\python.exe`.
4. **Restart Claude Desktop.** The first search downloads your whole library, so it may take a minute. After that, only changes are downloaded.

## Run on a server (TrueNAS, any Docker host): phone + claude.ai
Instead of each Mac keeping its own copy, run the connector on an always-on box. It syncs with Paprika on a timer, and you add it once as a **custom connector** in claude.ai, which makes it available in the Claude web, desktop and mobile apps.

**Trade-offs:** your recipes are always fresh, the first big download doesn't need your Mac awake, and your Paprika password lives only on the server. On the other hand, it needs the server and internet to be up, and `moods.json` lives on the server (`<dataset>/moods.json`), though you can simply ask Claude to change it (see below).

1. **Make a secret.** The connector URL contains a long random secret, which is what keeps others out. Treat the full URL like a password:
   ```
   openssl rand -hex 24
   ```
2. **Configure it.** Copy `.env.example` to `.env` and fill in your Paprika email and password, plus `PAPRIKA_URL_SECRET`.
3. **Start it.** You don't need to build anything: GitHub publishes a ready-made image, `ghcr.io/pedroserano/paprika-mcp:latest`, every time `main` changes.
   - **Any Docker host:** in a folder with `docker-compose.yml` and your `.env`, run `docker compose up -d`.
   - **TrueNAS SCALE (24.10+):** create a dataset such as `pool/apps/paprika` with the **Apps** preset. Then go to *Apps → Discover Apps → ⋮ → Install via YAML*, name it `paprika`, and paste the following, filling in your values:
     ```yaml
     services:
       paprika:
         image: ghcr.io/pedroserano/paprika-mcp:latest
         restart: unless-stopped
         environment:
           PAPRIKA_TRANSPORT: http
           PAPRIKA_EMAIL: you@example.com
           PAPRIKA_PASSWORD: your-paprika-password
           PAPRIKA_URL_SECRET: paste-your-secret
           PAPRIKA_SYNC_MINUTES: "5"
         volumes:
           - /mnt/pool/apps/paprika:/data
         ports:
           - "8080:8080"
     ```
   - **Updating:** on TrueNAS, use the app's *Update* / *Pull image* option. Elsewhere, run `docker compose pull && docker compose up -d`. Your cache and `moods.json` in the data folder are kept.

   For a big library, you can watch the first download with `docker exec -it paprika python sync.py` (on TrueNAS, the container name is shown by `docker ps`).
4. **Give it a public HTTPS address.** claude.ai reaches connectors from Anthropic's servers, not from your home network, so a LAN-only address won't work. Neither option below opens a router port:
   - **Tailscale Funnel** (no domain needed): with Tailscale on the NAS, run `tailscale funnel --bg 8080`. Your address is `https://<nas-name>.<tailnet>.ts.net`.
   - **Cloudflare Tunnel** (needs a domain on Cloudflare): create a tunnel, route a hostname to `http://paprika:8080`, put the token in `.env`, and uncomment the `cloudflared` service.
5. **Add the connector.** In claude.ai, go to *Settings → Connectors → Add custom connector*. Use the URL `https://<your-host>/<PAPRIKA_URL_SECRET>/mcp` and no authentication. It then shows up on your phone and in Claude Desktop too.
6. **Remove the local copy** to avoid duplicate tools: delete the `"Paprika"` entry from `~/Library/Application Support/Claude/claude_desktop_config.json` on your Mac(s), then restart Claude Desktop.

Check that it's up with `curl https://<your-host>/healthz`, which should print `ok`. Any other path without the secret returns 404. Request logging is off so the secret doesn't end up in logs.

## Make the moods yours (important for a heavily categorized library)
On first run, the connector copies `moods.json` to `~/.paprika-mood/moods.json`. Edit that copy:
- Each mood or season lists **your** category names. A parent category also covers its sub-categories, so `"Soups"` matches `Soups › Bean Soups`.
- `keywords` and `produce` are matched against the recipe name, ingredients, description and notes, by word start: `"strawberr"` matches "strawberries", but `"sage"` doesn't match "sausage".
- `avoid_keywords` push a recipe down the list. `max_minutes` sets a default time limit, as for `lazy`. `prefer_never_cooked` gives a boost to recipes you haven't cooked yet.
- If you have a category named after a mood or season (say "Cozy" or "Winter"), it is used automatically.
- Use `hemisphere` (`north` or `south`) to set which way the seasons run. Occasions are date windows written as `MM-DD`.

An easy way to set this up: ask Claude to "run list_categories and suggest moods.json mappings for my categories."

**Or just ask Claude**, which works from your phone too. For example: "add Braises and Short Ribs to cozy", "make lazy 20 minutes max", "add a date-night mood for steak, risotto and my Fancy category", "add a Lunar New Year occasion from 01-20 to 02-20 with dumplings". Claude uses `edit_mood`, `edit_season` and `edit_occasion`:
- Only the parts you mention change, and lists get added to or removed from, not replaced.
- Bad input is rejected with a clear error (an impossible date, a 5,000-minute limit), and the file is never left half-written.
- If you add a category that doesn't exist in Paprika yet, Claude warns you but keeps it, so it starts matching once you create that category.
- The previous version is saved as `moods.json.bak`, and "undo that" restores it.

## How ranking works
Points come from category matches (strongest), keyword or produce matches, current occasions, having cooked it in this month in past years (from your Paprika meal planner), rating, and favorites. Highly rated recipes you haven't made in over a year also get a small boost.

## Big libraries
The first download goes at a pace Paprika allows, and waits automatically if Paprika asks it to slow down. Thousands of recipes can take an hour or more. You can stop it with Ctrl+C and pick up where it left off:
```
~/paprika-mcp/.venv/bin/python ~/paprika-mcp/sync.py
```
You can also leave it to Claude, which continues the download in the background. You can search while it runs, and results fill in as recipes arrive.

## Notes
- This uses Paprika's **unofficial** sync API, the same one other community tools use. It is **read-only** and never changes your recipes. The only file it changes is your own `moods.json`, and only when you ask. If Paprika changes the API, it may stop working until updated.
- Your password stays in your local Claude config. The recipe cache is saved to `~/.paprika-mood/cache.json` and only your user account can read it. Delete that file to force a full re-download.
- Options (environment variables): `PAPRIKA_SYNC_MINUTES` (default 5), `PAPRIKA_CACHE_DIR`, and `PAPRIKA_MOODS_FILE`. Server mode also uses `PAPRIKA_TRANSPORT=http`, `PAPRIKA_URL_SECRET`, `PAPRIKA_HOST`, and `PAPRIKA_PORT` (default 8080).
