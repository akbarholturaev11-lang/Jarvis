#!/usr/bin/env bash
# Plan (default) or run (--execute) Developer ID signing + notarization.
#
# Reads only public signing labels from the environment; the private key and
# notary credentials stay in the keychain and are referenced by name:
#   JARVIS_MACOS_SIGN_IDENTITY   e.g. "Developer ID Application: Name (TEAMID)"
#   JARVIS_MACOS_TEAM_ID         10-character Apple Team ID
#   JARVIS_MACOS_NOTARY_PROFILE  notarytool keychain profile name
#
# Without those, this prints an honest unsigned-dev-build plan and does not sign.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

if [[ "${1:-}" == "--execute" ]]; then
  log "executing Developer ID signing pipeline"
  pipeline sign --execute
else
  log "planning Developer ID signing (no credentials => unsigned dev build)"
  pipeline sign
fi
