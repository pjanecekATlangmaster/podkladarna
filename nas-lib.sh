#!/bin/sh
# Sdílené funkce pro update-nas-ab.sh (A/B nasazení na Synology NAS)

nas_compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    echo "Chyba: docker compose / docker-compose není k dispozici." >&2
    return 1
  fi
}


nas_load_env() {
  SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  cd "$SCRIPT_DIR"
  if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -a
    . ./.env
    set +a
  fi
  GHCR_OWNER="$(printf '%s' "${GHCR_OWNER:-OWNER}" | tr '[:upper:]' '[:lower:]')"
  GHCR_USER="$(printf '%s' "${GHCR_USER:-$GHCR_OWNER}" | tr '[:upper:]' '[:lower:]')"
}


nas_validate_owner() {
  case "$GHCR_OWNER" in
    owner|""|vase_github_uzivatelske_jmeno|vase_github_*|*placeholder*)
      echo "Chyba: nastavte GHCR_OWNER v .env (lowercase)." >&2
      echo "Příklad: GHCR_OWNER=pjanecekatlangmaster" >&2
      return 1
      ;;
  esac
}


nas_ghcr_login() {
  if [ -n "${GHCR_TOKEN:-}" ]; then
    echo "Přihlašuji se do ghcr.io jako ${GHCR_USER}..."
    printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
  fi
}


nas_container_running() {
  _cr_name="${1:-podkladarna}"
  docker ps -q -f "name=^${_cr_name}\$" -f status=running 2>/dev/null | grep -q .
}


# Běžící kontejner jede na jiném image, než je teď lokálně pod jeho tagem
# (po pullu) → `compose up -d` ho znovu vytvoří.
nas_container_image_changed() {
  _ic_name="$1"
  nas_container_running "$_ic_name" || return 1
  _ic_ref="$(docker inspect --format='{{.Config.Image}}' "$_ic_name" 2>/dev/null || true)"
  _ic_cur="$(docker inspect --format='{{.Image}}' "$_ic_name" 2>/dev/null || true)"
  _ic_new="$(docker image inspect --format='{{.Id}}' "$_ic_ref" 2>/dev/null || true)"
  [ -n "$_ic_ref" ] && [ -n "$_ic_cur" ] && [ -n "$_ic_new" ] && [ "$_ic_cur" != "$_ic_new" ]
}


# DSM posílá mail „kontejner ukončen nečekaně“, když stop jde přes docker CLI.
# synowebapi to udělá stejně jako tlačítko Stop v Container Manageru.
nas_synology_stop_container() {
  _name="${1:-podkladarna}"
  if ! nas_container_running "$_name"; then
    return 0
  fi
  if ! command -v synowebapi >/dev/null 2>&1; then
    echo "synowebapi není k dispozici – stop přes docker (DSM může poslat mail)."
    return 1
  fi
  echo "Zastavuji ${_name} přes Synology API (bez mailu o neočekávaném stopu)..."
  synowebapi --exec api=SYNO.Docker.Container version=1 method=stop "name=\"${_name}\"" >/dev/null 2>&1 || \
    synowebapi --exec api=SYNO.Docker.Container version=1 method=stop name="\"${_name}\"" >/dev/null 2>&1 || \
    return 1
  _tries=30
  while [ "$_tries" -gt 0 ]; do
    nas_container_running "$_name" || return 0
    _tries=$((_tries - 1))
    sleep 1
  done
  echo "Varování: kontejner ${_name} po synowebapi stále běží." >&2
  return 1
}


nas_service_port() {
  case "$1" in
    podkladarna) echo 8672 ;;
    podkladarna-test) echo 8673 ;;
    *) return 1 ;;
  esac
}


# Host složka /data instance (docker-compose.nas-ab.yml).
nas_service_data_dir() {
  case "$1" in
    podkladarna) echo data ;;
    podkladarna-test) echo data-test ;;
    *) return 1 ;;
  esac
}


nas_drain_clear() {
  for _dc_f in "$@"; do
    rm -f "$_dc_f"
  done
}


# Čeká, až instance na portu nepočítá (/api/health → "busy": false, nebo neodpovídá).
# Další argumenty: soubory .deploy-drain, kterým se průběžně obnovuje čas (jinak by je
# aplikace po čase považovala za zapomenuté). 0 = volno, 1 = vypršel limit v minutách.
# NAS_POLL_S zkracuje interval (testy).
nas_wait_until_idle() {
  _wi_port="$1"
  _wi_left=$(( $2 * 60 ))
  shift 2
  _wi_poll="${NAS_POLL_S:-15}"
  # Job, který se rozbíhal právě v okamžiku založení .deploy-drain, se stihne ukázat jako busy.
  sleep "${NAS_DRAIN_GRACE_S:-3}"
  while :; do
    _wi_health="$(curl -s --connect-timeout 2 --max-time 5 "http://127.0.0.1:${_wi_port}/api/health" 2>/dev/null || true)"
    if ! printf '%s' "$_wi_health" | grep -q '"busy": *true'; then
      return 0
    fi
    if [ "$_wi_left" -le 0 ]; then
      return 1
    fi
    for _wi_f in "$@"; do
      touch "$_wi_f" 2>/dev/null || true
    done
    sleep "$_wi_poll"
    _wi_left=$(( _wi_left - _wi_poll ))
  done
}


# Úklid po úspěšném nasazení, jen podkladarna:
# - zastavené zbytky po přerušeném recreate (compose je přejmenuje na <id>_podkladarna…),
# - image ghcr.io/<owner>/podkladarna bez tagu (starší :latest / :stable po pullu).
# Bez -f: image používaný kontejnerem (i zastaveným) docker nesmaže.
nas_cleanup_old() {
  docker ps -a --filter status=exited --filter status=created --format '{{.ID}} {{.Names}}' 2>/dev/null | \
    while read -r _cl_id _cl_name; do
      case "$_cl_name" in
        *_podkladarna|*_podkladarna-test)
          # Jen hexa prefix, který dává compose (<id>_jméno); nic cizího.
          case "${_cl_name%_podkladarna*}" in ""|*[!0-9a-f]*) continue ;; esac
          echo "Mažu starý kontejner ${_cl_name}..."
          docker rm "$_cl_id" >/dev/null 2>&1 || true
          ;;
      esac
    done
  docker images "ghcr.io/${GHCR_OWNER}/podkladarna" --format '{{.ID}} {{.Tag}}' 2>/dev/null | \
    while read -r _cl_id _cl_tag; do
      [ "$_cl_tag" = "<none>" ] || continue
      if docker rmi "$_cl_id" >/dev/null 2>&1; then
        echo "Smazán starý image ${_cl_id}."
      fi
    done
  return 0
}
