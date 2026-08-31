#!/bin/sh
# Sdílené funkce pro deploy-nas.sh a update-nas.sh

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

# Lokální digest(y) z RepoDigests (index i platforma).
nas_local_digests() {
  _image="$1"
  docker image inspect "$_image" --format '{{json .RepoDigests}}' 2>/dev/null \
    | tr ',' '\n' | sed -n 's/.*@\(sha256:[a-fA-F0-9]*\).*/\1/p' || true
}

# Všechny digest z remote manifestu (tag/index i architektura).
# --verbose dá Descriptor.digest = to, co Docker ukládá do RepoDigests.
nas_remote_digests() {
  _image="$1"
  _raw="$(docker manifest inspect --verbose "$_image" 2>/dev/null || docker manifest inspect "$_image" 2>/dev/null || true)"
  printf '%s\n' "$_raw" | sed -n 's/.*"digest"[[:space:]]*:[[:space:]]*"\(sha256:[a-fA-F0-9]*\)".*/\1/p' | sort -u
}

nas_digest_match() {
  _image="$1"
  _ld="$(nas_local_digests "$_image")"
  _rd="$(nas_remote_digests "$_image")"
  [ -n "$_ld" ] && [ -n "$_rd" ] || return 1
  for _l in $_ld; do
    printf '%s\n' "$_rd" | grep -F -x "$_l" >/dev/null 2>&1 && return 0
  done
  return 1
}

nas_container_running() {
  docker ps -q -f name=^podkladarna$ -f status=running 2>/dev/null | grep -q .
}

nas_health_check() {
  _compose="$1"
  _compose_file="$2"
  if ! command -v curl >/dev/null 2>&1; then
    return 0
  fi
  echo ""
  echo "Health check http://127.0.0.1:8672/ (až 90 s)..."
  _tries=45
  while [ "$_tries" -gt 0 ]; do
    _code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 http://127.0.0.1:8672/ 2>/dev/null || echo 000)"
    if [ "$_code" = "200" ]; then
      echo "OK (HTTP $_code)"
      return 0
    fi
    _restarts="$(docker inspect --format='{{.RestartCount}}' podkladarna 2>/dev/null || echo 0)"
    if [ "${_restarts:-0}" -gt 0 ] 2>/dev/null; then
      echo "Kontejner spadl – logy:" >&2
      $_compose $_compose_file logs --tail=80 podkladarna
      return 1
    fi
    if ! nas_container_running; then
      echo "Kontejner neběží – logy:" >&2
      $_compose $_compose_file logs --tail=80 podkladarna
      return 1
    fi
    _tries=$((_tries - 1))
    sleep 2
  done
  echo "Varování: HTTP ${_code:-000} po 90 s" >&2
  $_compose $_compose_file logs --tail=80 podkladarna
  return 1
}

# Nastaví NAS_UPDATE_ACTION=skip|restart|pull. Vždy return 0
# (nesmí vracet 1/2 – volající skripty mají set -e a hned by skončily).
nas_check_up_to_date() {
  _image="$1"
  _force="${2:-false}"
  _local="$(nas_local_digests "$_image" | tr '\n' ' ')"
  _remote="$(nas_remote_digests "$_image" | tr '\n' ' ')"
  NAS_UPDATE_ACTION=pull

  echo "Lokální:  ${_local:-<žádný image>}"
  echo "Remote:   ${_remote:-<manifest nedostupný>}"

  if [ "$_force" = true ]; then
    echo ""
    echo "Vynucený update (--force) – stahuji pull."
    NAS_UPDATE_ACTION=pull
    return 0
  fi

  if [ -z "$_remote" ]; then
    echo "Nepodařilo se načíst remote manifest – pokračuji pull."
    NAS_UPDATE_ACTION=pull
    return 0
  fi

  if nas_digest_match "$_image" && nas_container_running; then
    echo ""
    echo "Image beze změny a kontejner běží – stahování přeskakuji."
    echo "Vynutit: $0 --force"
    NAS_UPDATE_ACTION=skip
    return 0
  fi

  if nas_digest_match "$_image"; then
    echo ""
    echo "Image beze změny – pull přeskakuji, jen restart kontejneru."
    NAS_UPDATE_ACTION=restart
    return 0
  fi

  echo ""
  echo "Nová verze image – stahuji pull (nezměněné vrstvy zůstanou v cache)."
  NAS_UPDATE_ACTION=pull
  return 0
}

# DSM posílá mail „kontejner ukončen nečekaně“, když stop jde přes docker CLI.
# synowebapi to udělá stejně jako tlačítko Stop v Container Manageru.
nas_synology_stop_container() {
  _name="${1:-podkladarna}"
  if ! nas_container_running; then
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
    nas_container_running || return 0
    _tries=$((_tries - 1))
    sleep 1
  done
  echo "Varování: kontejner ${_name} po synowebapi stále běží." >&2
  return 1
}

nas_recreate_container() {
  _compose="$1"
  _compose_file="$2"
  nas_synology_stop_container podkladarna || true
  $_compose $_compose_file down --remove-orphans 2>/dev/null || true
  docker rm -f podkladarna 2>/dev/null || true
  nas_remove_compose_network
  $_compose $_compose_file up -d --no-build --pull never
}

# Po přechodu na network_mode: bridge zbývá starý compose bridge bez NAT.
nas_remove_compose_network() {
  docker network ls --format '{{.Name}}' 2>/dev/null | grep -E '^podkladarna(_default)?$' | while read -r n; do
    [ -n "$n" ] || continue
    echo "Mažu starou síť $n..."
    docker network rm "$n" 2>/dev/null || true
  done
  return 0
}
