#!/usr/bin/env bash
# flight-rank installer — Linux/Mac, idempotent. No secrets printed.
# Usage: ./install.sh [--yes] [--wizard] [--dry-run]   (default: interactive prompts; --yes = no prompts)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
SKILL_SRC="$REPO_DIR/.opencode/skills/flight-rank/SKILL.md"
DRY=0; YES=0; SEEN=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1; SEEN=1 ;;
    --yes|-y) YES=1; SEEN=1 ;;
    --wizard) YES=0; SEEN=1 ;; # explicit click-through (default already interactive)
  esac
done
# Piped/CI stdin has no tty to prompt on -> fall back to non-interactive (unless explicit flag).
[ "$SEEN" = 1 ] || { [ -t 0 ] || YES=1; }

log() { echo "[flight-rank] $*"; }
# ask VAR "prompt" : Enter keeps existing value (or skips when empty)
ask() { local var="$1" prompt="$2" def="${!1:-}" ans; printf '%s [%s]: ' "$prompt" "${def:+*** set, Enter keeps}${def:-skip = Enter}"; read -r ans || ans=""; [ -n "$ans" ] && printf -v "$var" '%s' "$ans"; return 0; }
# y/n with default Yes: returns 0 on yes
confirm() { local ans; printf '%s [Y/n]: ' "$1"; read -r ans || ans=""; case "$ans" in [Nn]*) return 1;; *) return 0;; esac; }

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
  python3 -m pip install -q --break-system-packages -r "$REPO_DIR/requirements.txt" </dev/null 2>/dev/null \
    || python3 -m pip install -q -r "$REPO_DIR/requirements.txt" </dev/null
fi

# 2. merge MCP entry into opencode.json (python stdlib only, idempotent)
MERGE=1
if [ "$DRY" = 0 ] && [ "$YES" = 0 ]; then
  confirm "Merge flight-rank MCP entry into $CFG?" || MERGE=0
fi
if [ "$DRY" = 1 ]; then
  log "would: merge flight-rank MCP into $CFG"
elif [ "$MERGE" = 1 ]; then
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
  log "restart opencode serve to pick up the new MCP (e.g. restart the serve session)"
fi

# 3. bootstrap .env from prompts (never overwrite set values; --yes keeps old bare copy)
ENV="$REPO_DIR/.env"
if [ -f "$ENV" ]; then
  # shellcheck disable=SC1090: load existing values as prompt defaults (no secrets printed)
  set -a; . "$ENV" 2>/dev/null || true; set +a
fi
if [ "$DRY" = 1 ]; then
  log "would: prompt 5 optional keys -> $ENV"
elif [ "$YES" = 1 ] && [ ! -f "$ENV" ] && [ -f "$REPO_DIR/.env.example" ]; then
  cp "$REPO_DIR/.env.example" "$ENV"
  log ".env bootstrapped from .env.example (fill in only keys you have)"
elif [ "$YES" = 0 ]; then
  log "optional keys — Enter skips, all work keyless:"
  ask SEATS_AERO_API_KEY "seats.aero Pro API key (optional)"
  ask SERPAPI_KEY "SerpAPI key, 250 free/mo (optional)"
  ask DUFFEL_API_KEY_LIVE "Duffel live key (optional)"
  ask DELTA_CURL_FILE "delta.com curl file path (optional)"
  ask SEARXNG_URL "self-host SearXNG URL (optional)"
  { for k in SEATS_AERO_API_KEY SERPAPI_KEY DUFFEL_API_KEY_LIVE DELTA_CURL_FILE SEARXNG_URL; do
      v="${!k:-}"; [ -n "$v" ] && echo "$k=$v" || echo "# $k="
    done; } > "$ENV"
  log ".env written (set values uncommented, skipped stay commented)"
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

# 5. keyless smoke test (default Yes)
if [ "$DRY" = 0 ] && { [ "$YES" = 1 ] || confirm "Run keyless smoke test?"; }; then
  python3 -m py_compile "$REPO_DIR/mcp_server/server.py" \
    && python3 -m pytest "$REPO_DIR/tests/test_no_key.py" -q </dev/null
fi
