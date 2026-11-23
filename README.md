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

## Make the moods yours (important for a heavily categorized library)
On first run, the connector copies `moods.json` to `~/.paprika-mood/moods.json`. Edit that copy:
- Each mood or season lists **your** category names. A parent category also covers its sub-categories, so `"Soups"` matches `Soups › Bean Soups`.
- `keywords` and `produce` are matched against the recipe name, ingredients, description and notes, by word start: `"strawberr"` matches "strawberries", but `"sage"` doesn't match "sausage".
- `avoid_keywords` push a recipe down the list. `max_minutes` sets a default time limit, as for `lazy`. `prefer_never_cooked` gives a boost to recipes you haven't cooked yet.
- If you have a category named after a mood or season (say "Cozy" or "Winter"), it is used automatically.
- Use `hemisphere` (`north` or `south`) to set which way the seasons run. Occasions are date windows written as `MM-DD`.

An easy way to set this up: ask Claude to "run list_categories and suggest moods.json mappings for my categories."

## How ranking works
Points come from category matches (strongest), keyword or produce matches, current occasions, having cooked it in this month in past years (from your Paprika meal planner), rating, and favorites. Highly rated recipes you haven't made in over a year also get a small boost.

## Big libraries
The first download goes at a pace Paprika allows, and waits automatically if Paprika asks it to slow down. Thousands of recipes can take an hour or more. You can stop it with Ctrl+C and pick up where it left off:
```
~/paprika-mcp/.venv/bin/python ~/paprika-mcp/sync.py
```
You can also leave it to Claude, which continues the download in the background. You can search while it runs, and results fill in as recipes arrive.

## Notes
- This uses Paprika's **unofficial** sync API, the same one other community tools use. It is **read-only** and never changes your recipes. If Paprika changes the API, it may stop working until updated.
- Your password stays in your local Claude config. The recipe cache is saved to `~/.paprika-mood/cache.json` and only your user account can read it. Delete that file to force a full re-download.
- Options (environment variables): `PAPRIKA_SYNC_MINUTES` (default 5), `PAPRIKA_CACHE_DIR`, and `PAPRIKA_MOODS_FILE`.
