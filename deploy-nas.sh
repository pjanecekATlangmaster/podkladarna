#!/bin/sh
# Nasazení Podkladárny na Synology NAS (GHCR pull → stop → start)
#
# Použití:
#   cd /volume1/docker/podkladarna
#   cp .env.example .env
#   chmod +x deploy-nas.sh
#   ./deploy-nas.sh
#
# Volitelně: ./deploy-nas.sh v1.0.0   (místo tagu latest)

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

TAG="${1:-latest}"

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

echo ""
echo "1/3 Stahuji image..."
if ! docker pull "$IMAGE"; then
  echo ""
  echo "Pull selhal pro: $IMAGE" >&2
  echo "Ověřte GHCR_OWNER (lowercase) a že Actions build proběhl." >&2
  exit 1
fi

echo ""
echo "2/3 Zastavuji starý kontejner..."
$COMPOSE -f docker-compose.nas.yml down --remove-orphans

echo ""
echo "3/3 Spouštím nový kontejner..."
export GHCR_OWNER
export IMAGE_TAG="$TAG"
$COMPOSE -f docker-compose.nas.yml up -d --no-build --pull always

echo ""
echo "Stav kontejneru:"
$COMPOSE -f docker-compose.nas.yml ps

if command -v curl >/dev/null 2>&1; then
  echo ""
  echo "Health check http://127.0.0.1:8672/ ..."
  CODE="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8672/ || true)"
  if [ "$CODE" = "200" ]; then
    echo "OK (HTTP $CODE)"
  else
    echo "Varování: HTTP $CODE – zkontrolujte logy: $COMPOSE -f docker-compose.nas.yml logs --tail=50 podkladarna" >&2
    exit 1
  fi
else
  echo ""
  echo "Hotovo. Ověřte v prohlížeči: https://podkladarna.kibos.link"
fi

echo ""
echo "Deploy dokončen."
