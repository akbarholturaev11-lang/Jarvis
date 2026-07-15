#!/usr/bin/env bash
# Shared setup for the local unsigned JARVIS macOS packaging scripts.
#
# These scripts drive scripts/release_pipeline.py.  They never sign, notarize,
# publish, or claim distribution readiness on their own, and they never echo
# secret values.  Build tooling must come from an isolated build virtualenv
# (JARVIS_BUILD_PYTHON), never the customer runtime environment.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${HERE}/../.." && pwd)"

# Build interpreter: an isolated build venv with requirements-build.txt pinned.
PYTHON="${JARVIS_BUILD_PYTHON:-python3.12}"

VERSION="${JARVIS_BUILD_VERSION:-}"
BUILD_NUMBER="${JARVIS_BUILD_NUMBER:-}"
ARCH="${JARVIS_TARGET_ARCH:-$(uname -m)}"
OUTPUT_ROOT="${JARVIS_OUTPUT_ROOT:-${PROJECT_ROOT}/build/local-release}"
PRODUCT_CONFIG="${JARVIS_PRODUCT_CONFIG:-}"

log() { printf '[jarvis-pkg] %s\n' "$*" >&2; }

require_identity() {
  if [[ -z "${VERSION}" || -z "${BUILD_NUMBER}" ]]; then
    log "error: set JARVIS_BUILD_VERSION (MAJOR.MINOR.PATCH) and JARVIS_BUILD_NUMBER"
    exit 2
  fi
}

# Run one release-pipeline subcommand with the shared identity.
pipeline() {
  local subcommand="$1"; shift
  require_identity
  ( cd "${PROJECT_ROOT}" && "${PYTHON}" scripts/release_pipeline.py "${subcommand}" \
      --version "${VERSION}" \
      --build "${BUILD_NUMBER}" \
      --architecture "${ARCH}" \
      --output-root "${OUTPUT_ROOT}" \
      "$@" )
}
