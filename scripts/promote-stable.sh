#!/usr/bin/env bash
# Lokální náhrada workflow promote-stable.yml (Linux / WSL): přetaguje ověřený image na :stable
# (stejný digest, nic se nebuilduje). Ostrá A si :stable vezme cronem do 5 minut.
#
#   ./scripts/promote-stable.sh            # :latest -> :stable
#   ./scripts/promote-stable.sh 1a2b3c4    # konkrétní sha tag -> :stable
#
# Spouští Petr po ověření změny na testu B (pt.kibos.link).

set -euo pipefail

src="${1:-latest}"
owner="$(printf '%s' "${GHCR_OWNER:-pjanecekatlangmaster}" | tr '[:upper:]' '[:lower:]')"
image="ghcr.io/${owner}/podkladarna"

echo "Promote ${image}:${src} -> :stable"
docker buildx imagetools create -t "${image}:stable" "${image}:${src}"
docker buildx imagetools inspect "${image}:stable"
echo "Hotovo. Ostrá A na pve1 si :stable vezme cronem do 5 minut (nebo ručně ./update-nas-ab.sh stable)."
