#!/bin/sh
# Aktualizace A/B nasazení na Synology NAS (docker-compose.nas-ab.yml).
#
#   cd /volume1/docker/podkladarna
#   sudo ./update-nas-ab.sh test      # B (pt.kibos.link, image :latest)  – výchozí
#   sudo ./update-nas-ab.sh stable    # A (podkladarna.kibos.link, image :stable)
#   sudo ./update-nas-ab.sh both
#
# Stáhne nový image a znovu vytvoří jen vybranou službu (druhá běží dál). Běžící job
# se přeruší restartem kontejneru – spouštějte, když instance nepočítá.
# POZOR: skript je nový a na NAS se zatím neběžel; první nasazení sledujte.

set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/nas-lib.sh"

TARGET="${1:-test}"
COMPOSE_FILE="docker-compose.nas-ab.yml"

case "$TARGET" in
  test)   SERVICES="podkladarna-test"; PORTS="8673" ;;
  stable) SERVICES="podkladarna"; PORTS="8672" ;;
  both)   SERVICES="podkladarna podkladarna-test"; PORTS="8672 8673" ;;
  -h|--help)
    sed -n '2,13p' "$0"
    exit 0
    ;;
  *)
    echo "Použití: $0 [test|stable|both]" >&2
    exit 2
    ;;
esac

nas_load_env
nas_validate_owner
COMPOSE="$(nas_compose_cmd)"
nas_ghcr_login || true

mkdir -p shared/slots data data-test

echo "Stahuji image pro: $SERVICES"
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" pull $SERVICES
echo "Spouštím: $SERVICES"
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" up -d --no-deps $SERVICES

for port in $PORTS; do
  i=0
  until [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/")" = "200" ]; do
    i=$((i + 1))
    if [ "$i" -gt 40 ]; then
      echo "Chyba: port ${port} neodpovídá 200 ani po 200 s." >&2
      exit 1
    fi
    sleep 5
  done
  echo "OK: http://127.0.0.1:${port}/ → 200"
done
