#!/usr/bin/env bash
# Structurally verify JARVIS.app: bundled interpreter, Info.plist identity, and
# the secret boundary (no keys, memory, tokens or private configs in the bundle).
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

log "verifying JARVIS.app structure and secret boundary"
pipeline verify-app
