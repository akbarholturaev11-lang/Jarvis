#!/usr/bin/env bash
# Freeze JARVIS.app with the pinned PyInstaller spec.
#
# Requires a validated non-secret product client config (JARVIS_PRODUCT_CONFIG)
# and an isolated build venv (JARVIS_BUILD_PYTHON) that has installed both
# requirements.txt and requirements-build.txt.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

if [[ -z "${PRODUCT_CONFIG}" ]]; then
  log "error: set JARVIS_PRODUCT_CONFIG to a validated non-secret product.json"
  exit 2
fi

log "freezing JARVIS.app (version ${VERSION} build ${BUILD_NUMBER} arch ${ARCH})"
pipeline build-app --product-config "${PRODUCT_CONFIG}"
