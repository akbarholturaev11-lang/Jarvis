#!/usr/bin/env bash
# Stage JARVIS.app + an /Applications shortcut, then create the versioned DMG.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

log "staging DMG contents (JARVIS.app + Applications shortcut)"
pipeline stage-dmg
log "creating versioned DMG"
pipeline build-dmg
