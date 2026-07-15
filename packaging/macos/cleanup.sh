#!/usr/bin/env bash
# Remove intermediate build work and DMG staging; keep dist/, the DMG and its
# manifest.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

log "removing intermediate work and staging directories"
pipeline cleanup
