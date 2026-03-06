#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# release.sh — Package the astron-claw plugin into a tarball for GitHub Release
#
# Output: astron-claw-plugin.tar.gz (in repo root)
# Contents:
#   plugin/
#     index.ts
#     src/
#     node_modules/
#     package.json
#     tsconfig.json
#     openclaw.plugin.json
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_DIR="$REPO_ROOT/plugin"
OUTPUT="$REPO_ROOT/astron-claw-plugin.tar.gz"

log() {
  printf "[release] %s\n" "$*"
}

log_error() {
  printf "[release] ERROR: %s\n" "$*" >&2
}

# ---------------------------------------------------------------------------
# Validate plugin directory
# ---------------------------------------------------------------------------
if [ ! -f "$PLUGIN_DIR/package.json" ]; then
  log_error "plugin/package.json not found at $PLUGIN_DIR"
  exit 1
fi

if [ ! -f "$PLUGIN_DIR/index.ts" ]; then
  log_error "plugin/index.ts not found"
  exit 1
fi

if [ ! -d "$PLUGIN_DIR/src" ]; then
  log_error "plugin/src/ not found"
  exit 1
fi

if [ ! -d "$PLUGIN_DIR/node_modules" ]; then
  log_error "plugin/node_modules not found — run 'npm install' in plugin/ first"
  exit 1
fi

if [ ! -f "$PLUGIN_DIR/openclaw.plugin.json" ]; then
  log_error "plugin/openclaw.plugin.json not found"
  exit 1
fi

# ---------------------------------------------------------------------------
# Create tarball
# ---------------------------------------------------------------------------
log "packaging plugin from $PLUGIN_DIR"

tar -czf "$OUTPUT" \
  -C "$REPO_ROOT" \
  plugin/index.ts \
  plugin/src \
  plugin/node_modules \
  plugin/package.json \
  plugin/tsconfig.json \
  plugin/openclaw.plugin.json

log "created $OUTPUT"
log "contents:"
tar -tzf "$OUTPUT" | head -20
log "(use 'tar -tzf $OUTPUT' to see full listing)"
