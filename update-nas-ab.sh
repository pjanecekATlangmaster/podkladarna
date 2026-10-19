#!/bin/sh
# Aktualizace A/B nasazení na Synology NAS (docker-compose.nas-ab.yml).
#
#   cd /volume1/docker/podkladarna
#   sudo ./update-nas-ab.sh test      # B (pt.kibos.link, image :latest)  – výchozí
#   sudo ./update-nas-ab.sh stable    # A (podkladarna.kibos.link, image :stable)
#   sudo ./update-nas-ab.sh both
#   sudo ./update-nas-ab.sh test --if-idle   # pro cron: při běžícím jobu restart odloží
#
# Stáhne nový image a znovu vytvoří jen vybranou službu (druhá běží dál). Kontejner se
# restartuje jen při novém image; restart přeruší běžící job (viz --if-idle).
# Cron: */5 * * * * flock -n /tmp/update-ab.lock /volume1/docker/podkladarna/update-nas-ab.sh test --if-idle

set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/nas-lib.sh"

TARGET="test"
IF_IDLE=false
for arg in "$@"; do
  case "$arg" in
    --if-idle) IF_IDLE=true ;;
    *) TARGET="$arg" ;;
  esac
done
COMPOSE_FILE="docker-compose.nas-ab.yml"

case "$TARGET" in
  test)   SERVICES="podkladarna-test"; PORTS="8673" ;;
  stable) SERVICES="podkladarna"; PORTS="8672" ;;
  both)   SERVICES="podkladarna podkladarna-test"; PORTS="8672 8673" ;;
  -h|--help)
    sed -n '2,14p' "$0"
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
if [ "$IF_IDLE" = true ]; then
  # Pro cron: nový image se nasadí, až instance nepočítá (restart by přerušil job).
  # Bez změny image `up -d` kontejner stejně nerestartuje; neběžící instance se spustí.
  for port in $PORTS; do
    health="$(curl -s --connect-timeout 2 --max-time 5 "http://127.0.0.1:${port}/api/health" 2>/dev/null || true)"
    if printf '%s' "$health" | grep -q '"busy": *true'; then
      echo "Instance na portu ${port} právě počítá – restart odkládám (image je stažený, nasadí se při dalším spuštění)."
      exit 0
    fi
  done
fi
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
