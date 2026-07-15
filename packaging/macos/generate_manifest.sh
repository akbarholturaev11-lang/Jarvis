#!/usr/bin/env bash
# Write a non-secret local build manifest (product identity, sha256, byte size).
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

log "generating build manifest for the DMG"
pipeline manifest
