#!/usr/bin/env bash
# Confirm only the expected built-bundle structure, then optionally attempt a
# bounded real launch.  Executable presence alone does not prove that the app is
# independent of system Python or that launch never opens a terminal.
#
# The structural check always runs.  The optional bounded launch (JARVIS_SMOKE_LAUNCH=1)
# opens the app with a timeout and confirms it does not crash immediately.  A
# full interactive verification remains a manual step on a real user session.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

log "checking bundle structure (system Python / Terminal requirements remain not_verified)"
pipeline smoke

if [[ "${JARVIS_SMOKE_LAUNCH:-0}" == "1" ]]; then
  APP="${OUTPUT_ROOT}/${VERSION}+${BUILD_NUMBER}/macos-${ARCH}/dist/JARVIS.app"
  if [[ ! -d "${APP}" ]]; then
    log "error: built app not found at ${APP}"
    exit 2
  fi
  log "attempting bounded launch of ${APP}"
  # -g keeps it in the background; -n opens a fresh instance. A clean start for a
  # few seconds without an immediate crash is the smoke signal.  It still does
  # not prove clean-Mac dependency independence.
  open -gn "${APP}"
  sleep 5
  if pgrep -f "JARVIS.app/Contents/MacOS/JARVIS" >/dev/null; then
    log "smoke launch OK: process is alive"
    pkill -f "JARVIS.app/Contents/MacOS/JARVIS" || true
  else
    log "error: smoke launch process did not stay alive"
    exit 1
  fi
fi
