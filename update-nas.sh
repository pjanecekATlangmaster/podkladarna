#!/bin/sh
# Aktualizace nasazené Podkladárny na Synology NAS
#
# Chytrý režim: pokud je digest stejný jako na GHCR, nestahuje znovu ~350 MB.
# Vynucení: ./update-nas.sh --force
#
# Použití:
#   cd /volume1/docker/podkladarna
#   sudo ./update-nas.sh

set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/nas-lib.sh"

TAG="latest"
KEEP_IMAGE=false
FORCE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-image)
      KEEP_IMAGE=true
      shift
      ;;
    --force|-f)
      FORCE=true
      shift
      ;;
    -h|--help)
      echo "Použití: $0 [--force] [--keep-image] [tag]"
      echo "  Bez --force: přeskočí pull pokud je image stejný (digest sha256:…)"
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
  $COMPOSE $COMPOSE_FILE down --remove-orphans
  nas_remove_compose_network
  $COMPOSE $COMPOSE_FILE up -d --no-build --pull never
  $COMPOSE $COMPOSE_FILE ps
  nas_health_check "$COMPOSE" "$COMPOSE_FILE" || exit 1
  echo "Update dokončen (bez stažení image)."
  exit 0
fi

echo "1/5 Zastavuji starý kontejner..."
$COMPOSE $COMPOSE_FILE down --remove-orphans 2>/dev/null || true
docker rm -f podkladarna 2>/dev/null || true
nas_remove_compose_network

if [ "$KEEP_IMAGE" = false ]; then
  echo ""
  echo "2/5 Mažu staré image podkladarna..."
  OLD_IDS="$(docker images "ghcr.io/${GHCR_OWNER}/podkladarna" -q 2>/dev/null | sort -u || true)"
  if [ -n "$OLD_IDS" ]; then
    echo "$OLD_IDS" | while read -r img_id; do
      [ -n "$img_id" ] || continue
      docker rmi -f "$img_id" 2>/dev/null || true
    done
  fi
  docker image prune -f >/dev/null 2>&1 || true
else
  echo ""
  echo "2/5 Starý image ponechán (--keep-image)"
fi

echo ""
echo "3/5 Stahuji nový image z GHCR..."
if ! docker pull "$IMAGE"; then
  echo "Pull selhal: $IMAGE" >&2
  exit 1
fi

echo ""
echo "4/5 Spouštím nový kontejner..."
$COMPOSE $COMPOSE_FILE up -d --no-build --pull never

echo ""
echo "5/5 Kontrola..."
$COMPOSE $COMPOSE_FILE ps
nas_health_check "$COMPOSE" "$COMPOSE_FILE" || exit 1

echo ""
echo "Update dokončen."
