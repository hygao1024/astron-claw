#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# astron-claw installer
# Installs the astron-claw OpenClaw channel plugin and optionally the Python
# bridge server.
# Supports both local (plugin/ directory present) and remote (GitHub Release
# download) modes, so it works equally well from a git clone or via:
#
#   curl -fsSL https://raw.githubusercontent.com/hygao1024/astron-claw/master/install.sh | bash -s -- \
#     --bot-token <token> --server-url ws://server:8765/bridge/bot
#
# Wrapped in main() so `curl ... | bash` reads the entire script before
# executing — prevents sub-commands from consuming stdin.
# ---------------------------------------------------------------------------

main() {

GITHUB_REPO="hygao1024/astron-claw"
TARBALL_NAME="astron-claw-plugin.tar.gz"

OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
PLUGIN_NAME="astron-claw"
TARGET_DIR="${TARGET_DIR:-$HOME/.openclaw/extensions/astron-claw}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$HOME/.openclaw/openclaw.json}"

BOT_TOKEN=""
SERVER_URL="ws://localhost:8765/bridge/bot"
ACCOUNT_NAME="AstronClaw"
VERSION="latest"
WITH_SERVER="0"
SERVER_DIR="${SERVER_DIR:-$HOME/.openclaw/astron-claw-server}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
  cat <<'USAGE'
Usage:
  install.sh --bot-token <token> [options]

Options:
  --bot-token <token>       Bot authentication token (required)
  --server-url <url>        Astron bridge WebSocket URL
                            (default: ws://localhost:8765/bridge/bot)
  --name <name>             Display name for the channel account
                            (default: AstronClaw)
  --target-dir <path>       Plugin install directory
                            (default: ~/.openclaw/extensions/astron-claw)
  --version <tag>           Release version to download (default: latest)
                            Only used in remote mode (no local plugin/ dir)
  --with-server             Also install the bridge server component
  --server-dir <path>       Server install directory
                            (default: ~/.openclaw/astron-claw-server)
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
    --name)
      need_next_arg "$1" "$#"
      ACCOUNT_NAME="$2"
      shift 2
      ;;
    --target-dir)
      need_next_arg "$1" "$#"
      TARGET_DIR="$2"
      shift 2
      ;;
    --version)
      need_next_arg "$1" "$#"
      VERSION="$2"
      shift 2
      ;;
    --with-server)
      WITH_SERVER="1"
      shift
      ;;
    --server-dir)
      need_next_arg "$1" "$#"
      SERVER_DIR="$2"
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
require_cmd node "Install Node.js then retry: https://nodejs.org"

if [ "$WITH_SERVER" = "1" ]; then
  require_cmd python3 "Install Python 3 then retry: https://python.org"
  if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
    log_error "pip not found. Install pip then retry."
    exit 1
  fi
fi

log "prerequisites check passed"

# ---------------------------------------------------------------------------
# Determine plugin source: local directory or remote tarball
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
PLUGIN_SRC="$SCRIPT_DIR/plugin"
TMP_DIR=""
USE_LOCAL="0"

if [ -d "$PLUGIN_SRC" ] && [ -f "$PLUGIN_SRC/package.json" ]; then
  USE_LOCAL="1"
  log "detected local plugin directory: $PLUGIN_SRC"
else
  log "no local plugin/ directory found, will download from GitHub Release"
  require_cmd curl "Install curl then retry"
  require_cmd tar  "Install tar then retry"

  TMP_DIR="$(mktemp -d)"

  # Build download URL
  if [ "$VERSION" = "latest" ]; then
    DOWNLOAD_URL="https://github.com/${GITHUB_REPO}/releases/latest/download/${TARBALL_NAME}"
  else
    DOWNLOAD_URL="https://github.com/${GITHUB_REPO}/releases/download/${VERSION}/${TARBALL_NAME}"
  fi

  log "downloading $DOWNLOAD_URL"
  if ! curl -fSL "$DOWNLOAD_URL" -o "$TMP_DIR/$TARBALL_NAME"; then
    log_error "failed to download release tarball"
    log_error "URL: $DOWNLOAD_URL"
    rm -rf "$TMP_DIR"
    exit 1
  fi

  log "extracting tarball"
  tar -xzf "$TMP_DIR/$TARBALL_NAME" -C "$TMP_DIR"
  PLUGIN_SRC="$TMP_DIR/plugin"

  if [ ! -f "$PLUGIN_SRC/package.json" ]; then
    log_error "tarball does not contain expected plugin/ directory"
    rm -rf "$TMP_DIR"
    exit 1
  fi
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
    "$OPENCLAW_BIN" plugins install -l "$TARGET_DIR" </dev/null >/dev/null 2>&1 || true
    "$OPENCLAW_BIN" plugins enable "$PLUGIN_NAME" </dev/null >/dev/null 2>&1 || true
    "$OPENCLAW_BIN" gateway restart </dev/null >/dev/null 2>&1 || true
    log "rollback completed"
  fi
  # Clean up backup on success
  if [ "$exit_code" -eq 0 ] && [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
  fi
  # Clean up temp download directory
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
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
# Install npm dependencies (only in local mode — tarball already includes
# node_modules)
# ---------------------------------------------------------------------------
if [ "$USE_LOCAL" = "1" ] && [ ! -d "$TARGET_DIR/node_modules" ]; then
  NPM_BIN="${NPM_BIN:-npm}"
  require_cmd "$NPM_BIN" "Install Node.js + npm then retry: https://nodejs.org"
  log "installing npm dependencies"
  (
    cd "$TARGET_DIR"
    "$NPM_BIN" install --omit=dev
  )
else
  log "node_modules present, skipping npm install"
fi

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
# Register and enable the channel plugin with OpenClaw
# ---------------------------------------------------------------------------
log "registering channel plugin with OpenClaw"

# Disable any previous version first
"$OPENCLAW_BIN" plugins disable "$PLUGIN_NAME" </dev/null >/dev/null 2>&1 || true

# Install from local path and enable
"$OPENCLAW_BIN" plugins install -l "$TARGET_DIR" </dev/null >/dev/null 2>&1 || true
"$OPENCLAW_BIN" plugins enable "$PLUGIN_NAME" </dev/null

# ---------------------------------------------------------------------------
# Installation succeeded -- disable rollback
# ---------------------------------------------------------------------------
ROLLBACK_NEEDED="0"

# ---------------------------------------------------------------------------
# Install server component (if requested)
# ---------------------------------------------------------------------------
if [ "$WITH_SERVER" = "1" ]; then
  log "installing bridge server to $SERVER_DIR"

  # Determine server source directory
  SERVER_SRC="$SCRIPT_DIR/server"
  FRONTEND_SRC="$SCRIPT_DIR/frontend"

  if [ "$USE_LOCAL" != "1" ]; then
    # In remote mode, check if tarball included server files
    SERVER_SRC="$TMP_DIR/server"
    FRONTEND_SRC="$TMP_DIR/frontend"
  fi

  if [ ! -d "$SERVER_SRC" ] || [ ! -f "$SERVER_SRC/requirements.txt" ]; then
    log_error "server/ directory not found"
    log_error "server installation requires running from a git clone or a full release tarball"
    exit 1
  fi

  mkdir -p "$SERVER_DIR"

  log "copying server files"
  cp -r "$SERVER_SRC/"* "$SERVER_DIR/"

  if [ -d "$FRONTEND_SRC" ]; then
    log "copying frontend files"
    mkdir -p "$SERVER_DIR/frontend"
    cp -r "$FRONTEND_SRC/"* "$SERVER_DIR/frontend/"
  fi

  # Create media directory
  mkdir -p "$SERVER_DIR/media"

  log "installing Python dependencies"
  if command -v pip3 >/dev/null 2>&1; then
    pip3 install -r "$SERVER_DIR/requirements.txt" --quiet </dev/null
  else
    python3 -m pip install -r "$SERVER_DIR/requirements.txt" --quiet </dev/null
  fi

  log "server installed to $SERVER_DIR"
  log ""
  log "To start the bridge server:"
  log "  cd $SERVER_DIR && python3 run.py"
  log ""
fi

# ---------------------------------------------------------------------------
# Restart gateway to load and register the channel plugin
# ---------------------------------------------------------------------------
log "restarting OpenClaw gateway to register channel"
"$OPENCLAW_BIN" gateway restart </dev/null >/dev/null 2>&1 || true
sleep 3

# ---------------------------------------------------------------------------
# Write channel configuration
# Config is stored under plugins.entries.<id>.config rather than
# channels.<id> because OpenClaw validates channels.* against known
# channel IDs before loading plugins.  The plugin reads config from
# plugins.entries path at runtime.
# ---------------------------------------------------------------------------
log "configuring channel (name=$ACCOUNT_NAME, server=$SERVER_URL)"

CONFIG_JSON=$(node -e "
  const cfg = {
    enabled: true,
    name: $(printf '%s' "$ACCOUNT_NAME" | node -e "process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))"),
    bridge: {
      url: $(printf '%s' "$SERVER_URL" | node -e "process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))"),
      token: $(printf '%s' "$BOT_TOKEN" | node -e "process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))")
    },
    allowFrom: ['*']
  };
  process.stdout.write(JSON.stringify(cfg));
")

ENTRY_JSON=$(node -e "
  const entry = { enabled: true, config: ${CONFIG_JSON} };
  process.stdout.write(JSON.stringify(entry));
")

if ! "$OPENCLAW_BIN" config set "plugins.entries.$PLUGIN_NAME" --json "$ENTRY_JSON" </dev/null 2>/dev/null; then
  # Fallback: write config directly to the JSON file if CLI fails
  log "config via CLI failed, writing directly to config file"
  node -e "
    const fs = require('fs');
    const cfgPath = '${OPENCLAW_CONFIG_PATH}';
    const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
    if (!cfg.plugins) cfg.plugins = {};
    if (!cfg.plugins.entries) cfg.plugins.entries = {};
    cfg.plugins.entries['${PLUGIN_NAME}'] = ${ENTRY_JSON};
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  " </dev/null
fi
log "channel config updated"

# ---------------------------------------------------------------------------
# Restart gateway again to apply the new configuration
# ---------------------------------------------------------------------------
log "restarting OpenClaw gateway to apply configuration"
"$OPENCLAW_BIN" gateway restart </dev/null >/dev/null 2>&1 || true

log "done! astron-claw channel plugin installed successfully"
log "channel name: $ACCOUNT_NAME"
log "bridge server: $SERVER_URL"
log "plugin directory: $TARGET_DIR"
if [ "$WITH_SERVER" = "1" ]; then
  log "server directory: $SERVER_DIR"
fi

}

main "$@"
