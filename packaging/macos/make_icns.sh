#!/usr/bin/env bash
# Generate a provisional JARVIS.icns from the committed PWA icon for local
# unsigned builds, and print its path (use it via JARVIS_APP_ICON).
#
# This is a development-only icon.  A final, rights-cleared macOS app icon is a
# branding gate in docs/PRODUCT_RELEASE_CONTRACT.md and is NOT settled here.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

if [[ "$(uname -s)" != "Darwin" ]]; then
  log "error: make_icns.sh requires macOS (sips/iconutil)"
  exit 2
fi
command -v sips >/dev/null || { log "error: sips not found"; exit 2; }
command -v iconutil >/dev/null || { log "error: iconutil not found"; exit 2; }

SRC="${PROJECT_ROOT}/dashboard/static/icon-512.png"
if [[ ! -f "${SRC}" ]]; then
  log "error: source icon not found at ${SRC}"
  exit 2
fi

require_identity
OUT_DIR="${OUTPUT_ROOT}/${VERSION}+${BUILD_NUMBER}/macos-${ARCH}"
ICONSET="${OUT_DIR}/JARVIS.iconset"
ICNS="${OUT_DIR}/JARVIS.icns"
mkdir -p "${ICONSET}"

for size in 16 32 128 256 512; do
  sips -z "${size}" "${size}" "${SRC}" --out "${ICONSET}/icon_${size}x${size}.png" >/dev/null
  retina=$(( size * 2 ))
  sips -z "${retina}" "${retina}" "${SRC}" \
    --out "${ICONSET}/icon_${size}x${size}@2x.png" >/dev/null
done

iconutil -c icns "${ICONSET}" -o "${ICNS}"
log "generated provisional icon: ${ICNS}"
echo "${ICNS}"
