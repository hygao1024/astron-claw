#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# astron-claw uninstaller
# Removes the astron-claw OpenClaw plugin and its configuration.
# ---------------------------------------------------------------------------

OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
PLUGIN_NAME="astron-claw"
TARGET_DIR="${TARGET_DIR:-$HOME/.openclaw/extensions/astron-claw}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
  cat <<'USAGE'
Usage:
  ./uninstall.sh [options]

Options:
  --target-dir <path>       Plugin install directory
                            (default: ~/.openclaw/extensions/astron-claw)
  --keep-config             Do not remove plugin config from openclaw.json
  -y, --yes                 Skip confirmation prompt
  -h, --help                Show this help message
USAGE
}

log() {
  printf "[astron-uninstall] %s\n" "$*"
}

log_error() {
  printf "[astron-uninstall] ERROR: %s\n" "$*" >&2
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SKIP_CONFIRM="0"
KEEP_CONFIG="0"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target-dir)
      if [ "$#" -lt 2 ]; then
        log_error "missing value for $1"
        exit 1
      fi
      TARGET_DIR="$2"
      shift 2
      ;;
    --keep-config)
      KEEP_CONFIG="1"
      shift
      ;;
    -y|--yes)
      SKIP_CONFIRM="1"
      shift
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
# Confirmation
# ---------------------------------------------------------------------------
if [ "$SKIP_CONFIRM" != "1" ]; then
  printf "[astron-uninstall] This will remove the astron-claw plugin.\n"
  printf "[astron-uninstall]   Plugin directory: %s\n" "$TARGET_DIR"
  printf "[astron-uninstall] Continue? [y/N] "
  read -r answer
  case "$answer" in
    [yY]|[yY][eE][sS]) ;;
    *)
      log "aborted"
      exit 0
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# Check for openclaw CLI
# ---------------------------------------------------------------------------
HAS_OPENCLAW="0"
if command -v "$OPENCLAW_BIN" >/dev/null 2>&1; then
  HAS_OPENCLAW="1"
fi

# ---------------------------------------------------------------------------
# Disable and unregister plugin
# ---------------------------------------------------------------------------
if [ "$HAS_OPENCLAW" = "1" ]; then
  log "disabling plugin"
  "$OPENCLAW_BIN" plugins disable "$PLUGIN_NAME" >/dev/null 2>&1 || true

  log "unregistering plugin"
  "$OPENCLAW_BIN" plugins uninstall "$PLUGIN_NAME" >/dev/null 2>&1 || true

  if [ "$KEEP_CONFIG" != "1" ]; then
    log "removing plugin config"
    "$OPENCLAW_BIN" config set "plugins.entries.$PLUGIN_NAME" --json "null" >/dev/null 2>&1 || true
  else
    log "keeping plugin config (--keep-config)"
  fi
else
  log "openclaw CLI not found, skipping plugin unregister"
fi

# ---------------------------------------------------------------------------
# Remove plugin files (must happen BEFORE gateway restart to prevent
# OpenClaw from auto-discovering the plugin in the extensions directory)
# ---------------------------------------------------------------------------
if [ -d "$TARGET_DIR" ]; then
  log "removing plugin directory: $TARGET_DIR"
  rm -rf "$TARGET_DIR"
else
  log "plugin directory not found: $TARGET_DIR (already removed?)"
fi

# Clean up any leftover backup directories
for bak in "${TARGET_DIR}.bak."*; do
  if [ -d "$bak" ]; then
    log "removing leftover backup: $bak"
    rm -rf "$bak"
  fi
done

# ---------------------------------------------------------------------------
# Restart gateway (after files are removed so plugin won't be re-discovered)
# ---------------------------------------------------------------------------
if [ "$HAS_OPENCLAW" = "1" ]; then
  log "restarting OpenClaw gateway"
  "$OPENCLAW_BIN" gateway restart >/dev/null 2>&1 || true
fi

log "done! astron-claw plugin has been removed"
