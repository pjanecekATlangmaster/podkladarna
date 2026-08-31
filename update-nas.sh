#!/bin/sh
# Aktualizace nasazené Podkladárny na Synology NAS
#
# Nejdřív pull (Docker znovu stáhne jen změněné vrstvy), teprve pak restart.
# Stejný digest → nic nestahuje. Vynucení: ./update-nas.sh --force
#
# Použití:
#   cd /volume1/docker/podkladarna
#   sudo ./update-nas.sh

set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/nas-lib.sh"

TAG="latest"
FORCE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-image)
      # Zpětná kompatibilita – image se před pullem už nemaže.
      shift
      ;;
    --force|-f)
      FORCE=true
      shift
      ;;
    -h|--help)
      echo "Použití: $0 [--force] [tag]"
      echo "  Bez --force: přeskočí pull, pokud je digest stejný jako na GHCR."
      echo "  Pull nemaže staré vrstvy – Docker stáhne jen rozdíl."
      exit 0
      ;;
    -*)
      echo "Neznámý parametr: $1" >&2
      exit 1
      ;;
    *)
      TAG="$1"
      shift
      ;;
  esac
done

nas_load_env
COMPOSE="$(nas_compose_cmd)" || exit 1
COMPOSE_FILE="-f docker-compose.nas.yml"
IMAGE="ghcr.io/${GHCR_OWNER}/podkladarna:${TAG}"

nas_validate_owner || exit 1

echo "=== Podkladárna – UPDATE na NAS ==="
echo "Složka:  $(pwd)"
echo "Image:   $IMAGE"
echo ""

nas_ghcr_login

export GHCR_OWNER
export IMAGE_TAG="$TAG"

nas_check_up_to_date "$IMAGE" "$FORCE"

if [ "$NAS_UPDATE_ACTION" = skip ]; then
  $COMPOSE $COMPOSE_FILE ps
  exit 0
fi

if [ "$NAS_UPDATE_ACTION" = restart ]; then
  echo "1/2 Restart kontejneru (bez pull)..."
  nas_recreate_container "$COMPOSE" "$COMPOSE_FILE"
  $COMPOSE $COMPOSE_FILE ps
  nas_health_check "$COMPOSE" "$COMPOSE_FILE" || exit 1
  echo "Update dokončen (bez stažení image)."
  exit 0
fi

echo "1/3 Stahuji image (nezměněné vrstvy zůstanou)..."
if ! docker pull "$IMAGE"; then
  echo "Pull selhal: $IMAGE" >&2
  exit 1
fi

echo ""
echo "2/3 Restartuji kontejner..."
nas_recreate_container "$COMPOSE" "$COMPOSE_FILE"

echo ""
echo "3/3 Kontrola..."
$COMPOSE $COMPOSE_FILE ps
nas_health_check "$COMPOSE" "$COMPOSE_FILE" || exit 1
docker image prune -f >/dev/null 2>&1 || true

echo ""
echo "Update dokončen."
