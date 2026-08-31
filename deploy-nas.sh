#!/bin/sh
# Nasazení Podkladárny na Synology NAS (GHCR pull → stop → start)
#
# Použití:
#   cd /volume1/docker/podkladarna
#   cp .env.example .env
#   chmod +x deploy-nas.sh
#   ./deploy-nas.sh
#
# Volitelně:
#   ./deploy-nas.sh v1.0.0      # konkrétní tag
#   ./deploy-nas.sh --force     # vynutit restart i bez nové verze

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

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

# GHCR vždy používá lowercase jméno vlastníka
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

echo "=== Podkladárna – deploy na NAS ==="
echo "Složka:  $SCRIPT_DIR"
echo "Image:   $IMAGE"
echo ""

case "$GHCR_OWNER" in
  owner|""|vase_github_uzivatelske_jmeno|vase_github_*|*placeholder*)
    echo "Chyba: nastavte GHCR_OWNER v .env (lowercase)." >&2
    echo "Příklad: GHCR_OWNER=pjanecekatlangmaster" >&2
    exit 1
    ;;
esac

if [ -n "${GHCR_TOKEN:-}" ]; then
  echo "Přihlašuji se do ghcr.io jako ${GHCR_USER}..."
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
fi

RUNNING_ID="$(docker inspect --format='{{.Image}}' podkladarna 2>/dev/null || true)"
LOCAL_ID="$(docker image inspect "$IMAGE" --format='{{.Id}}' 2>/dev/null || true)"
CONTAINER_RUNNING="$(docker ps -q -f name=^podkladarna$ 2>/dev/null || true)"

if [ "$FORCE" = false ] && [ -n "$CONTAINER_RUNNING" ] && [ -n "$RUNNING_ID" ] && [ "$RUNNING_ID" = "$LOCAL_ID" ]; then
  echo "Lokální image je aktuální a kontejner běží – pull přeskakuji."
  echo "Pro vynucený restart: $0 --force"
  echo ""
  echo "Stav kontejneru:"
  $COMPOSE $COMPOSE_FILE ps
  exit 0
fi

echo ""
echo "1/3 Kontrola / stažení image (jen pokud je novější)..."
export GHCR_OWNER
export IMAGE_TAG="$TAG"
if ! $COMPOSE $COMPOSE_FILE pull; then
  echo ""
  echo "Pull selhal pro: $IMAGE" >&2
  echo "Ověřte GHCR_OWNER (lowercase) a že Actions build proběhl." >&2
  exit 1
fi

NEW_ID="$(docker image inspect "$IMAGE" --format='{{.Id}}' 2>/dev/null || true)"

if [ "$FORCE" = false ] && [ -n "$CONTAINER_RUNNING" ] && [ -n "$RUNNING_ID" ] && [ "$RUNNING_ID" = "$NEW_ID" ]; then
  echo "Remote image beze změny – restart přeskakuji."
  echo ""
  echo "Stav kontejneru:"
  $COMPOSE $COMPOSE_FILE ps
  exit 0
fi

echo ""
echo "2/3 Zastavuji starý kontejner..."
$COMPOSE $COMPOSE_FILE down --remove-orphans

echo ""
echo "3/3 Spouštím nový kontejner..."
$COMPOSE $COMPOSE_FILE up -d --no-build

echo ""
echo "Stav kontejneru:"
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
      echo "Kontejner se restartuje (RestartCount=${RESTARTS}) – pravděpodobně spadl při startu." >&2
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
    echo "Varování: HTTP ${CODE:-000} po 90 s – aplikace ještě neodpovídá." >&2
    echo "Poslední logy:" >&2
    $COMPOSE $COMPOSE_FILE logs --tail=80 podkladarna
    exit 1
  fi
else
  echo ""
  echo "Hotovo. Ověřte v prohlížeči: https://podkladarna.kibos.link"
fi

echo ""
echo "Deploy dokončen."
