#!/bin/sh
# Aktualizace A/B nasazení na Synology NAS (docker-compose.nas-ab.yml).
#
#   cd /volume1/docker/podkladarna
#   sudo ./update-nas-ab.sh test      # B (pt.kibos.link, image :latest)  – výchozí
#   sudo ./update-nas-ab.sh stable    # A (podkladarna.kibos.link, image :stable)
#   sudo ./update-nas-ab.sh both
#   sudo ./update-nas-ab.sh test --force     # restart hned, i když počítá job (přeruší ho)
#   sudo ./update-nas-ab.sh stable --wait-minutes=150
#
# Stáhne nový image a znovu vytvoří jen vybranou službu (druhá běží dál). Kontejner se
# restartuje jen při novém image a nikdy nepřeruší běžící job: založí v datech instance
# soubor .deploy-drain (nové joby se jen řadí do fronty, po restartu se spustí), počká na
# doběhnutí jobu (/api/health → busy) a teprve pak restartuje. Když job nedoběhne do
# --wait-minutes (výchozí JOB_TIMEOUT_MINUTES + 30), nasazení se vzdá a nic nerestartuje.
# Cron: */5 * * * * flock -n /tmp/update-ab.lock /volume1/docker/podkladarna/update-nas-ab.sh test --if-idle
# (--if-idle je dnes totéž co výchozí chování; flock zabrání souběhu při dlouhém čekání.)

set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/nas-lib.sh"

TARGET="test"
FORCE=false
WAIT_MINUTES=""
for arg in "$@"; do
  case "$arg" in
    --if-idle) ;;  # zpětná kompatibilita (cron): čekání na job je výchozí
    --force) FORCE=true ;;
    --wait-minutes=*) WAIT_MINUTES="${arg#--wait-minutes=}" ;;
    *) TARGET="$arg" ;;
  esac
done
COMPOSE_FILE="docker-compose.nas-ab.yml"

case "$TARGET" in
  test)   SERVICES="podkladarna-test"; PORTS="8673" ;;
  stable) SERVICES="podkladarna"; PORTS="8672" ;;
  both)   SERVICES="podkladarna podkladarna-test"; PORTS="8672 8673" ;;
  -h|--help)
    sed -n '2,19p' "$0"
    exit 0
    ;;
  *)
    echo "Použití: $0 [test|stable|both] [--force] [--wait-minutes=N]" >&2
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
case "$WAIT_MINUTES" in
  "") WAIT_MINUTES=$(( ${JOB_TIMEOUT_MINUTES:-90} + 30 )) ;;
  *[!0-9]*) echo "Chyba: --wait-minutes musí být celé číslo." >&2; exit 2 ;;
esac

# Služby s novým image: jen ty se restartují, jen u nich se čeká na doběhnutí jobu.
CHANGED=""
for svc in $SERVICES; do
  if nas_container_image_changed "$svc"; then
    CHANGED="$CHANGED $svc"
  fi
done

DRAIN_FILES=""
trap 'nas_drain_clear $DRAIN_FILES' EXIT
trap 'exit 130' INT TERM
if [ -n "$CHANGED" ] && [ "$FORCE" != true ]; then
  for svc in $CHANGED; do
    f="$(nas_service_data_dir "$svc")/.deploy-drain"
    : > "$f"
    DRAIN_FILES="$DRAIN_FILES $f"
    echo "${svc}: nové joby jen do fronty, čekám na doběhnutí běžícího jobu (max ${WAIT_MINUTES} min)..."
  done
  for svc in $CHANGED; do
    if ! nas_wait_until_idle "$(nas_service_port "$svc")" "$WAIT_MINUTES" $DRAIN_FILES; then
      echo "Chyba: ${svc} pořád počítá job ani po ${WAIT_MINUTES} min – nasazení vzdávám, nic nerestartuji (image je stažený)." >&2
      exit 1
    fi
  done
fi

# Kontejner s novým image nejdřív zastavíme přes Synology API (jako Stop v Container
# Manageru), jinak ho `up -d` zastaví přes docker CLI a DSM pošle mail „stopped
# unexpectedly“. Když synowebapi selže, pokračuje se jako dřív.
for svc in $CHANGED; do
  nas_synology_stop_container "$svc" || true
done
echo "Spouštím: $SERVICES"
# shellcheck disable=SC2086
$COMPOSE -f "$COMPOSE_FILE" up -d --no-deps $SERVICES
# .deploy-drain až po `up -d`: kdyby synowebapi stop selhal, starý kontejner do restartu
# nic z fronty nespustí. Nová instance frontu po smazání spustí sama (drain_watch, ~15 s).
nas_drain_clear $DRAIN_FILES
DRAIN_FILES=""

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

nas_cleanup_old
