#!/bin/sh
# Nasazení / chytrý deploy Podkladárny na Synology NAS
#
# Použití:
#   cd /volume1/docker/podkladarna
#   ./deploy-nas.sh
#
# Volitelně:
#   ./deploy-nas.sh --force     # vynutit pull + restart
#   ./update-nas.sh             # aktualizace (pull jen změněné vrstvy)

set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/nas-lib.sh"

TAG="latest"
FORCE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --force|-f)
      FORCE=true
      shift
      ;;
    -h|--help)
      echo "Použití: $0 [--force] [tag]"
      echo "  Pro vynucenou aktualizaci viz také: ./update-nas.sh --force"
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

echo "=== Podkladárna – deploy na NAS ==="
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
  echo "Restart bez pull..."
  nas_recreate_container "$COMPOSE" "$COMPOSE_FILE"
  $COMPOSE $COMPOSE_FILE ps
  nas_health_check "$COMPOSE" "$COMPOSE_FILE" || exit 1
  echo "Deploy dokončen (bez stažení)."
  exit 0
fi

echo ""
echo "1/3 Stahuji nový image..."
if ! docker pull "$IMAGE"; then
  echo "Pull selhal pro: $IMAGE" >&2
  exit 1
fi

echo ""
echo "2/3 Restartuji kontejner..."
nas_recreate_container "$COMPOSE" "$COMPOSE_FILE"

$COMPOSE $COMPOSE_FILE ps
nas_health_check "$COMPOSE" "$COMPOSE_FILE" || exit 1

echo ""
echo "Deploy dokončen."
