#!/bin/sh
# Aktualizace nasazené Podkladárny na Synology NAS
#
# Vždy: stop → smazat kontejner → smazat starý image → pull nový → start
# Data v ./data zůstávají (jobs, SQLite).
#
# Použití:
#   cd /volume1/docker/podkladarna
#   chmod +x update-nas.sh
#   sudo ./update-nas.sh
#
# Volitelně:
#   ./update-nas.sh v1.0.0     # konkrétní tag místo latest
#   ./update-nas.sh --keep-image   # nesmazat starý image (jen kontejner)

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

TAG="latest"
KEEP_IMAGE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-image)
      KEEP_IMAGE=true
      shift
      ;;
    -h|--help)
      echo "Použití: $0 [--keep-image] [tag]"
      echo "  Aktualizace běžící instalace – vždy pull a restart."
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

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a
  . ./.env
  set +a
fi

GHCR_OWNER="$(printf '%s' "${GHCR_OWNER:-OWNER}" | tr '[:upper:]' '[:lower:]')"
GHCR_USER="$(printf '%s' "${GHCR_USER:-$GHCR_OWNER}" | tr '[:upper:]' '[:lower:]')"
IMAGE="ghcr.io/${GHCR_OWNER}/podkladarna:${TAG}"
COMPOSE="docker compose"
COMPOSE_FILE="-f docker-compose.nas.yml"

if ! $COMPOSE version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    echo "Chyba: docker compose / docker-compose není k dispozici." >&2
    exit 1
  fi
fi

case "$GHCR_OWNER" in
  owner|""|vase_github_uzivatelske_jmeno|vase_github_*|*placeholder*)
    echo "Chyba: nastavte GHCR_OWNER v .env (lowercase)." >&2
    echo "Příklad: GHCR_OWNER=pjanecekatlangmaster" >&2
    exit 1
    ;;
esac

echo "=== Podkladárna – UPDATE na NAS ==="
echo "Složka:  $SCRIPT_DIR"
echo "Image:   $IMAGE"
echo ""

if [ -n "${GHCR_TOKEN:-}" ]; then
  echo "Přihlašuji se do ghcr.io jako ${GHCR_USER}..."
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
fi

export GHCR_OWNER
export IMAGE_TAG="$TAG"

echo "1/5 Zastavuji a odstraňuji starý kontejner..."
$COMPOSE $COMPOSE_FILE down --remove-orphans 2>/dev/null || true
if docker ps -aq -f name=^podkladarna$ 2>/dev/null | grep -q .; then
  docker rm -f podkladarna 2>/dev/null || true
fi
echo "   hotovo"

if [ "$KEEP_IMAGE" = false ]; then
  echo ""
  echo "2/5 Mažu staré image podkladarna..."
  # Tag latest + staré vrstvy stejného repozitáře
  OLD_IDS="$(docker images "ghcr.io/${GHCR_OWNER}/podkladarna" -q 2>/dev/null | sort -u || true)"
  if [ -n "$OLD_IDS" ]; then
    echo "$OLD_IDS" | while read -r img_id; do
      [ -n "$img_id" ] || continue
      docker rmi -f "$img_id" 2>/dev/null || true
    done
  fi
  # osiřelé <none> vrstvy po rmi
  docker image prune -f >/dev/null 2>&1 || true
  echo "   hotovo"
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
echo "   hotovo"

echo ""
echo "4/5 Spouštím nový kontejner..."
$COMPOSE $COMPOSE_FILE up -d --no-build --pull never
echo "   hotovo"

echo ""
echo "5/5 Kontrola stavu..."
$COMPOSE $COMPOSE_FILE ps

if command -v curl >/dev/null 2>&1; then
  echo ""
  echo "Health check http://127.0.0.1:8672/ (až 90 s)..."
  READY=false
  TRIES=45
  while [ "$TRIES" -gt 0 ]; do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 http://127.0.0.1:8672/ 2>/dev/null || echo 000)"
    if [ "$CODE" = "200" ]; then
      echo "OK (HTTP $CODE)"
      READY=true
      break
    fi

    RESTARTS="$(docker inspect --format='{{.RestartCount}}' podkladarna 2>/dev/null || echo 0)"
    if [ "${RESTARTS:-0}" -gt 0 ] 2>/dev/null; then
      echo "Kontejner spadl – logy:" >&2
      $COMPOSE $COMPOSE_FILE logs --tail=80 podkladarna
      exit 1
    fi

    if ! docker ps -q -f name=^podkladarna$ -f status=running | grep -q .; then
      echo "Kontejner neběží – logy:" >&2
      $COMPOSE $COMPOSE_FILE logs --tail=80 podkladarna
      exit 1
    fi

    TRIES=$((TRIES - 1))
    sleep 2
  done

  if [ "$READY" = false ]; then
    echo "Varování: HTTP ${CODE:-000} po 90 s" >&2
    $COMPOSE $COMPOSE_FILE logs --tail=80 podkladarna
    exit 1
  fi
fi

echo ""
echo "Update dokončen."
echo "Web: https://podkladarna.kibos.link  (nebo http://127.0.0.1:8672)"
