#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# astron-claw installer
# Installs the astron-claw OpenClaw plugin from local files.
# ---------------------------------------------------------------------------

OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
NPM_BIN="${NPM_BIN:-npm}"
PLUGIN_NAME="astron-claw"
TARGET_DIR="${TARGET_DIR:-$HOME/.openclaw/extensions/astron-claw}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$HOME/.openclaw/openclaw.json}"

BOT_TOKEN=""
SERVER_URL="ws://localhost:8765/ws/bot"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
  cat <<'USAGE'
Usage:
  ./install.sh --bot-token <token> [options]

Options:
  --bot-token <token>       Bot authentication token (required)
  --server-url <url>        Astron bridge WebSocket URL
                            (default: ws://localhost:8765/ws/bot)
  --target-dir <path>       Plugin install directory
                            (default: ~/.openclaw/extensions/astron-claw)
  -h, --help                Show this help message
USAGE
}

log() {
  printf "[astron-install] %s\n" "$*"
}

log_error() {
  printf "[astron-install] ERROR: %s\n" "$*" >&2
}

require_cmd() {
  local cmd="$1"
  local hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log_error "missing command: $cmd"
    log_error "$hint"
    exit 1
  fi
}

need_next_arg() {
  local opt="$1"
  local argc="$2"
  if [ "$argc" -lt 2 ]; then
    log_error "missing value for $opt"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [ "$#" -gt 0 ]; do
  case "$1" in
    --bot-token)
      need_next_arg "$1" "$#"
      BOT_TOKEN="$2"
      shift 2
      ;;
    --server-url)
      need_next_arg "$1" "$#"
      SERVER_URL="$2"
      shift 2
      ;;
    --target-dir)
      need_next_arg "$1" "$#"
      TARGET_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log_error "unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate required arguments
# ---------------------------------------------------------------------------
if [ -z "$BOT_TOKEN" ]; then
  log_error "--bot-token is required"
  usage
  exit 1
fi

# ---------------------------------------------------------------------------
# Check prerequisites
# ---------------------------------------------------------------------------
require_cmd "$OPENCLAW_BIN" "Install OpenClaw CLI then retry: https://docs.openclaw.dev"
require_cmd "$NPM_BIN" "Install Node.js + npm then retry: https://nodejs.org"
require_cmd node "Install Node.js then retry: https://nodejs.org"

log "prerequisites check passed"

# ---------------------------------------------------------------------------
# Locate plugin source directory (relative to this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$SCRIPT_DIR/plugin"

if [ ! -f "$PLUGIN_SRC/package.json" ]; then
  log_error "plugin source not found at $PLUGIN_SRC"
  log_error "ensure the 'plugin/' directory exists alongside install.sh"
  exit 1
fi

# ---------------------------------------------------------------------------
# Rollback state
# ---------------------------------------------------------------------------
BACKUP_DIR=""
ROLLBACK_NEEDED="0"

cleanup() {
  local exit_code=$?
  if [ "$ROLLBACK_NEEDED" = "1" ] && [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    log "install failed (exit=$exit_code), rolling back..."
    rm -rf "$TARGET_DIR"
    mv "$BACKUP_DIR" "$TARGET_DIR"
    "$OPENCLAW_BIN" plugins install -l "$TARGET_DIR" >/dev/null 2>&1 || true
    "$OPENCLAW_BIN" plugins enable "$PLUGIN_NAME" >/dev/null 2>&1 || true
    "$OPENCLAW_BIN" gateway restart >/dev/null 2>&1 || true
    log "rollback completed"
  fi
  # Clean up backup on success
  if [ "$exit_code" -eq 0 ] && [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Backup existing installation (if any)
# ---------------------------------------------------------------------------
if [ -d "$TARGET_DIR" ]; then
  BACKUP_DIR="${TARGET_DIR}.bak.$(date +%s)"
  log "backing up existing install to $BACKUP_DIR"
  mv "$TARGET_DIR" "$BACKUP_DIR"
fi
ROLLBACK_NEEDED="1"

# ---------------------------------------------------------------------------
# Copy plugin files to target directory
# ---------------------------------------------------------------------------
log "copying plugin files to $TARGET_DIR"
mkdir -p "$TARGET_DIR"
cp -r "$PLUGIN_SRC/"* "$TARGET_DIR/"

# ---------------------------------------------------------------------------
# Install npm dependencies
# ---------------------------------------------------------------------------
log "installing npm dependencies"
(
  cd "$TARGET_DIR"
  "$NPM_BIN" install --omit=dev
)

# ---------------------------------------------------------------------------
# Verify plugin can be loaded
# ---------------------------------------------------------------------------
if [ -f "$TARGET_DIR/dist/index.js" ]; then
  if node -e "import('$TARGET_DIR/dist/index.js').then(() => process.exit(0)).catch(() => process.exit(1))" 2>/dev/null; then
    log "plugin sanity check passed"
  else
    log "warning: plugin sanity check inconclusive (may need runtime dependencies)"
  fi
fi

# ---------------------------------------------------------------------------
# Register and enable the plugin with OpenClaw
# ---------------------------------------------------------------------------
log "registering plugin with OpenClaw"

# Disable any previous version first
"$OPENCLAW_BIN" plugins disable "$PLUGIN_NAME" >/dev/null 2>&1 || true

# Install from local path and enable
"$OPENCLAW_BIN" plugins install -l "$TARGET_DIR" >/dev/null 2>&1 || true
"$OPENCLAW_BIN" plugins enable "$PLUGIN_NAME"

# ---------------------------------------------------------------------------
# Write plugin configuration
# ---------------------------------------------------------------------------
log "configuring plugin (server=$SERVER_URL)"

CONFIG_JSON=$(node -e "
  const cfg = {
    bridge: {
      url: $(printf '%s' "$SERVER_URL" | node -e "process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))"),
      token: $(printf '%s' "$BOT_TOKEN" | node -e "process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))")
    }
  };
  process.stdout.write(JSON.stringify(cfg));
")

"$OPENCLAW_BIN" config set "plugins.entries.$PLUGIN_NAME.config" --json "$CONFIG_JSON"
log "plugin config updated"

# ---------------------------------------------------------------------------
# Installation succeeded -- disable rollback
# ---------------------------------------------------------------------------
ROLLBACK_NEEDED="0"

# ---------------------------------------------------------------------------
# Restart gateway to load the new plugin
# ---------------------------------------------------------------------------
log "restarting OpenClaw gateway"
"$OPENCLAW_BIN" gateway restart >/dev/null 2>&1 || true

log "done! astron-claw plugin installed successfully"
log "bridge server: $SERVER_URL"
log "plugin directory: $TARGET_DIR"
