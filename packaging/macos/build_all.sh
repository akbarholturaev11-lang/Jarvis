#!/usr/bin/env bash
# End-to-end local unsigned pipeline: clean -> build app -> DMG -> manifest ->
# verify -> signing plan -> smoke -> cleanup.
#
# This produces an UNSIGNED local development artifact.  Signing/notarization run
# only when Developer ID credentials are configured (see sign_artifact.sh), and
# even then the result is not distribution ready until every gate in
# docs/PRODUCT_RELEASE_CONTRACT.md is cleared.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/_common.sh"

log "=== 1/8 clean ==="       ; "${HERE}/clean.sh"
log "=== 2/8 build app ==="   ; "${HERE}/build_app.sh"
log "=== 3/8 build dmg ==="   ; "${HERE}/build_dmg.sh"
log "=== 4/8 manifest ==="    ; "${HERE}/generate_manifest.sh"
log "=== 5/8 verify app ===" ; "${HERE}/verify_app.sh"
log "=== 6/8 sign (plan) ===" ; "${HERE}/sign_artifact.sh"
log "=== 7/8 smoke ==="       ; "${HERE}/smoke_launch.sh"
log "=== 8/8 cleanup ==="     ; "${HERE}/cleanup.sh"
log "local unsigned pipeline complete (not distribution ready)"
