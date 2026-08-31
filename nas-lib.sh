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

# Lokální digest (sha256:…) z RepoDigests
nas_local_digest() {
  _image="$1"
  _d="$(docker image inspect "$_image" --format='{{index .RepoDigests 0}}' 2>/dev/null || true)"
  case "$_d" in
    *@sha256:*)
      printf '%s\n' "$_d" | sed 's/.*@//'
      ;;
    *)
      _id="$(docker image inspect "$_image" --format='{{.Id}}' 2>/dev/null || true)"
      case "$_id" in
        sha256:*)
          printf '%s\n' "$_id" | sed 's/^sha256:/sha256:/'
          ;;
      esac
      ;;
  esac
}

# Remote digest bez stažení vrstev (manifest inspect)
nas_remote_digest() {
  _image="$1"
  docker manifest inspect "$_image" 2>/dev/null | sed -n 's/^[[:space:]]*"digest"[[:space:]]*:[[:space:]]*"\(sha256:[^"]*\)".*/\1/p' | head -1
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

# Vypíše stav a vrátí 0 pokud pull není potřeba (stejný digest + běží)
# Vrátí 1 pokud je potřeba pull/restart
nas_check_up_to_date() {
  _image="$1"
  _force="${2:-false}"
  _local="$(nas_local_digest "$_image")"
  _remote="$(nas_remote_digest "$_image")"

  echo "Lokální:  ${_local:-<žádný image>}"
  echo "Remote:   ${_remote:-<manifest nedostupný>}"

  if [ "$_force" = true ]; then
    return 1
  fi

  if [ -z "$_remote" ]; then
    echo "Nepodařilo se načíst remote manifest – pokračuji pull."
    return 1
  fi

  if [ -n "$_local" ] && [ "$_local" = "$_remote" ] && nas_container_running; then
    echo ""
    echo "Image beze změny a kontejner běží – stahování přeskakuji."
    echo "Vynutit: $0 --force"
    return 0
  fi

  if [ -n "$_local" ] && [ "$_local" = "$_remote" ]; then
    echo ""
    echo "Image beze změny – pull přeskakuji, jen restart kontejneru."
    return 2
  fi

  echo ""
  echo "Nová verze image – bude stažen pull."
  return 1
}
