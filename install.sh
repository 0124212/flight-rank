#!/usr/bin/env bash
# flight-rank installer — Linux/Mac, idempotent. No secrets printed.
# Usage: ./install.sh [--dry-run]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
SKILL_SRC="$REPO_DIR/.opencode/skills/flight-rank/SKILL.md"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { echo "[flight-rank] $*"; }

# 0. python 3.10+ check (uses match/X|Y syntax-free code, but 3.10 is the floor)
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  log "ERROR: python3 >= 3.10 required (found $(python3 --version 2>&1))"
  exit 1
fi

# 1. python deps
if [ "$DRY" = 1 ]; then
  log "would: pip install -r $REPO_DIR/requirements.txt"
else
  log "installing python deps…"
  python3 -m pip install -q --break-system-packages -r "$REPO_DIR/requirements.txt" 2>/dev/null \
    || python3 -m pip install -q -r "$REPO_DIR/requirements.txt"
fi

# 2. merge MCP entry into opencode.json (python stdlib only, idempotent)
if [ "$DRY" = 1 ]; then
  log "would: merge flight-rank MCP into $CFG"
else
  mkdir -p "$(dirname "$CFG")"
  [ -f "$CFG" ] && cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)" && log "backed up $CFG"
  REPO_DIR="$REPO_DIR" CFG="$CFG" python3 - <<'EOF'
import json, os
cfg = os.environ["CFG"]; repo = os.environ["REPO_DIR"]
d = {}
if os.path.exists(cfg):
    with open(cfg) as f:
        try: d = json.load(f)
        except Exception: d = {}
d.setdefault("mcp", {})["flight-rank"] = {
    "type": "stdio",
    "command": ["python3", os.path.join(repo, "mcp_server/server.py")],
}
with open(cfg, "w") as f:
    json.dump(d, f, indent=2)
print("mcp entry merged")
EOF
fi

# 3. bootstrap .env from .env.example when missing (never overwrite)
if [ ! -f "$REPO_DIR/.env" ] && [ -f "$REPO_DIR/.env.example" ]; then
  if [ "$DRY" = 1 ]; then
    log "would: cp .env.example .env"
  else
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    log ".env bootstrapped from .env.example (fill in only keys you have)"
  fi
fi

# 4. install skill (new + legacy paths)
for dest in "$HOME/.config/opencode/skills/flight-rank/SKILL.md" \
            "$HOME/.config/opencode/skill/flight-rank.md"; do
  if [ "$DRY" = 1 ]; then
    log "would: copy SKILL.md -> $dest"
  else
    mkdir -p "$(dirname "$dest")"
    cp "$SKILL_SRC" "$dest"
    log "skill -> $dest"
  fi
done

log "done. Optional: export SERPAPI_KEY=<key> for fallback (250 free/mo)."
