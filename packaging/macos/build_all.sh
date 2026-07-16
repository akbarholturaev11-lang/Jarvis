#!/usr/bin/env bash
# End-to-end local unsigned pipeline: clean -> build app -> DMG -> manifest ->
# verify -> signing plan -> smoke -> cleanup.
#
# This produces an UNSIGNED local development artifact.  The signing step is a
# read-only plan; production execution is mechanically unavailable until its
# final ordering and verification are independently audited.  No output from
# this script is distribution ready.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/_common.sh"

log "=== 1/8 clean ==="       ; "${HERE}/clean.sh"
log "=== 2/8 build app ==="   ; "${HERE}/build_app.sh"
log "=== 3/8 build dmg ==="   ; "${HERE}/build_dmg.sh"
log "=== 4/8 manifest ==="    ; "${HERE}/generate_manifest.sh"
log "=== 5/8 verify app ===" ; "${HERE}/verify_app.sh"
log "=== 6/8 signing readiness (plan only) ===" ; "${HERE}/sign_artifact.sh"
log "=== 7/8 smoke ==="       ; "${HERE}/smoke_launch.sh"
log "=== 8/8 cleanup ==="     ; "${HERE}/cleanup.sh"
log "local unsigned pipeline complete (not distribution ready)"
