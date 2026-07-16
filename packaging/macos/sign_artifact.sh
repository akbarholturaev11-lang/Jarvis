#!/usr/bin/env bash
# Print the Developer ID signing/notarization readiness plan.
#
# Reads only public signing labels from the environment; the private key and
# notary credentials stay in the keychain and are referenced by name:
#   JARVIS_MACOS_SIGN_IDENTITY   e.g. "Developer ID Application: Name (TEAMID)"
#   JARVIS_MACOS_TEAM_ID         10-character Apple Team ID
#   JARVIS_MACOS_NOTARY_PROFILE  notarytool keychain profile name
#
# Without those, this prints an honest unsigned-dev-build plan and does not sign.
# ``--execute`` is deliberately refused before Python or artifact mutation.  The
# current pipeline builds the DMG before a signed app is embedded and has not had
# its final notarization-result handling independently audited.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

if [[ "${1:-}" == "--execute" ]]; then
  log "not_available: production signing execution is disabled pending an audited final app/DMG sequence"
  exit 2
fi

if [[ "$#" -ne 0 ]]; then
  log "error: unsupported signing argument"
  exit 2
fi

log "planning Developer ID signing readiness (no artifact mutation)"
pipeline sign
