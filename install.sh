#!/bin/bash
# Installs the Paprika connector for Claude Desktop (macOS).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Installing the Paprika connector from $DIR"

# Find Python 3.10+
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 \
         /opt/homebrew/bin/python3 "$HOME/.homebrew/bin/python3" /usr/local/bin/python3; do
  p=$(command -v "$c" 2>/dev/null || true)
  if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$p"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed. Install it with:  brew install python"
  echo "(or download from python.org), then run this script again."
  exit 1
fi
echo "Using $("$PY" --version) at $PY"

"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
echo "Dependencies installed."

read -r -p "Paprika email: " PAPRIKA_EMAIL
read -r -s -p "Paprika password (hidden): " PAPRIKA_PASSWORD; echo

CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
mkdir -p "$(dirname "$CONFIG")"
[ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG.bak-$(date +%Y%m%d%H%M%S)"

PAPRIKA_EMAIL="$PAPRIKA_EMAIL" PAPRIKA_PASSWORD="$PAPRIKA_PASSWORD" \
CONFIG="$CONFIG" DIR="$DIR" "$PY" - <<'EOF'
import json, os
path, d = os.environ["CONFIG"], os.environ["DIR"]
try:
    cfg = json.load(open(path))
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
servers = cfg.setdefault("mcpServers", {})
servers.pop("paprika-mood", None)  # older name
servers["Paprika"] = {
    "command": f"{d}/.venv/bin/python",
    "args": [f"{d}/server.py"],
    "env": {"PAPRIKA_EMAIL": os.environ["PAPRIKA_EMAIL"],
            "PAPRIKA_PASSWORD": os.environ["PAPRIKA_PASSWORD"]},
}
json.dump(cfg, open(path, "w"), indent=2)
os.chmod(path, 0o600)
EOF

echo "Checking your Paprika login..."
if ! DIR="$DIR" PAPRIKA_EMAIL="$PAPRIKA_EMAIL" PAPRIKA_PASSWORD="$PAPRIKA_PASSWORD" \
   "$DIR/.venv/bin/python" -c "
import os, sys; sys.path.insert(0, os.environ['DIR'])
from paprika_client import PaprikaClient
n = len(PaprikaClient(os.environ['PAPRIKA_EMAIL'].strip(), os.environ['PAPRIKA_PASSWORD']).recipe_index())
print(f'Login OK - {n} recipes in your Paprika library.')"; then
  echo
  echo "Couldn't log in. Make sure Cloud Sync is on in Paprika and the"
  echo "email/password are right, then run this script again."
  exit 1
fi

echo
echo "Downloading your library. Big libraries take a while because Paprika limits"
echo "how fast we can ask. You can stop anytime with Ctrl+C and resume later with:"
echo "   $DIR/.venv/bin/python $DIR/sync.py"
echo
"$DIR/.venv/bin/python" "$DIR/sync.py" || true
echo
echo "Connector installed. Quit Claude completely (Cmd+Q) and reopen it."
echo "If the download didn't finish, Claude will keep going in the background."
