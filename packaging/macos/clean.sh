#!/usr/bin/env bash
# Remove this version's release workspace before a fresh build.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

log "cleaning release workspace"
pipeline clean
