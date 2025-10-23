#!/usr/bin/env bash
# Pack WotR Czech releases locally (both variants) and compute SHA256 checksums.
# Usage: ./pack_release.sh 1.0

set -euo pipefail
VER="${1:?Usage: $0 <version>}"
BASE="releases/v${VER}"

for PROFILE in final final-en-terms; do
  DIR="${BASE}/${PROFILE}"
  OUT="wotr-cs-v${VER}-${PROFILE}.zip"
  [ -d "${DIR}" ] || { echo "Missing ${DIR}"; exit 1; }
  (cd "${BASE}" && zip -r "../../${OUT}" "${PROFILE}")
  sha256sum "${OUT}" > "${OUT}.sha256"
  echo "[ok] ${OUT}"
done
