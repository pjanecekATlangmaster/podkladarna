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

nas_github_token() {
  printf '%s' "${GH_TOKEN:-${GITHUB_TOKEN:-${GHCR_TOKEN:-}}}"
}

nas_github_repo() {
  printf '%s' "${GITHUB_REPO:-${GHCR_OWNER}/podkladarna}"
}

nas_github_branch() {
  printf '%s' "${GITHUB_BRANCH:-master}"
}

# První "key": "value" po rozsekaní JSON (stačí pro GitHub REST).
nas_json_str() {
  _json="$1"
  _key="$2"
  printf '%s' "$_json" | tr '{},' '\n' | sed -n "s/^[[:space:]]*\"${_key}\":[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -n 1
}

nas_github_api() {
  # $1 = cesta /repos/...  → NAS_GH_HTTP + NAS_GH_BODY
  _path="$1"
  NAS_GH_HTTP=000
  NAS_GH_BODY=""
  if ! command -v curl >/dev/null 2>&1; then
    return 1
  fi
  _url="https://api.github.com${_path}"
  _tok="$(nas_github_token)"
  _tmp="$(mktemp /tmp/podkladarna-gh.XXXXXX 2>/dev/null || mktemp)"
  if [ -n "$_tok" ]; then
    NAS_GH_HTTP="$(curl -sS -o "$_tmp" -w '%{http_code}' \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -H "Authorization: Bearer ${_tok}" \
      "$_url" 2>/dev/null || echo 000)"
  else
    NAS_GH_HTTP="$(curl -sS -o "$_tmp" -w '%{http_code}' \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "$_url" 2>/dev/null || echo 000)"
  fi
  NAS_GH_BODY="$(cat "$_tmp" 2>/dev/null || true)"
  rm -f "$_tmp"
  [ "$NAS_GH_HTTP" = "200" ]
}

nas_github_head_sha() {
  _repo="$(nas_github_repo)"
  _branch="$(nas_github_branch)"
  nas_github_api "/repos/${_repo}/commits/${_branch}" || return 1
  nas_json_str "$NAS_GH_BODY" sha
}

nas_github_latest_docker_run() {
  _repo="$(nas_github_repo)"
  _branch="$(nas_github_branch)"
  nas_github_api "/repos/${_repo}/actions/workflows/docker.yml/runs?branch=${_branch}&per_page=1" || return 1
  NAS_GH_RUN_STATUS="$(nas_json_str "$NAS_GH_BODY" status)"
  NAS_GH_RUN_CONCLUSION="$(nas_json_str "$NAS_GH_BODY" conclusion)"
  NAS_GH_RUN_SHA="$(nas_json_str "$NAS_GH_BODY" head_sha)"
  NAS_GH_RUN_URL="$(nas_json_str "$NAS_GH_BODY" html_url)"
}

nas_run_is_active() {
  case "${1:-}" in
    queued|in_progress|waiting|pending|requested) return 0 ;;
    *) return 1 ;;
  esac
}

# Počká, až workflow docker.yml dokončí build pro aktuální HEAD větve.
# Přeskočí se pro jiný tag než latest, --no-wait, nebo když API nejde.
# 0 = můžeme tahat GHCR; 1 = build selhal / timeout.
nas_wait_for_gh_build() {
  _do_wait="${1:-true}"
  _tag="${2:-latest}"

  if [ "$_do_wait" = false ] || [ "${NAS_SKIP_BUILD_WAIT:-}" = "1" ]; then
    echo "Čekání na GitHub Actions přeskočeno."
    return 0
  fi
  if [ "$_tag" != "latest" ]; then
    echo "Tag ${_tag} – na Actions pro latest nečekám."
    return 0
  fi

  _max="${NAS_BUILD_WAIT_MAX_SEC:-2400}"
  _tok="$(nas_github_token)"
  if [ -n "${NAS_BUILD_WAIT_POLL_SEC:-}" ]; then
    _poll="$NAS_BUILD_WAIT_POLL_SEC"
  elif [ -n "$_tok" ]; then
    _poll=15
  else
    # Anonymní GitHub API: 60 req/hod. 45 s × 40 min se vejde.
    _poll=45
  fi
  _repo="$(nas_github_repo)"
  _branch="$(nas_github_branch)"

  echo "GitHub Actions: ${_repo} (${_branch}, docker.yml)"
  if [ -z "$_tok" ]; then
    echo "Bez tokenu – veřejné API, interval ${_poll}s."
  fi

  if ! _head="$(nas_github_head_sha)" || [ -z "$_head" ]; then
    echo "Varování: GitHub API ${NAS_GH_HTTP:-?} – čekání na build přeskakuji."
    echo "  Zkontrolujte GITHUB_REPO (${_repo}) a síť z NAS."
    return 0
  fi
  echo "HEAD ${_branch}: ${_head}"

  _digest_before="$(nas_remote_digests "ghcr.io/${GHCR_OWNER}/podkladarna:${_tag}" | tr '\n' ' ')"
  _started="$(date +%s)"
  _announced=""

  while :; do
    _now="$(date +%s)"
    _elapsed=$((_now - _started))
    if [ "$_elapsed" -ge "$_max" ]; then
      echo "Timeout (${_max} s): GitHub Actions pořád nedokončilo image." >&2
      [ -n "${NAS_GH_RUN_URL:-}" ] && echo "  ${NAS_GH_RUN_URL}" >&2
      echo "  Po dokončení znovu $0, nebo $0 --no-wait pro aktuální GHCR." >&2
      return 1
    fi

    NAS_GH_RUN_STATUS=""
    NAS_GH_RUN_CONCLUSION=""
    NAS_GH_RUN_SHA=""
    NAS_GH_RUN_URL=""
    if ! nas_github_latest_docker_run; then
      if [ "${NAS_GH_HTTP:-}" = "403" ] || [ "${NAS_GH_HTTP:-}" = "429" ]; then
        echo "  GitHub rate limit (${NAS_GH_HTTP}) – čekám 60 s..."
        sleep 60
        continue
      fi
      echo "Varování: Actions API ${NAS_GH_HTTP:-?} – čekání přeskakuji."
      return 0
    fi

    _short="$(printf '%s' "${NAS_GH_RUN_SHA:-????????}" | cut -c1-7)"
    if [ -z "$NAS_GH_RUN_SHA" ]; then
      echo "  [${_elapsed}s] čekám, až GitHub založí běh pro HEAD..."
    elif [ "$NAS_GH_RUN_SHA" != "$_head" ]; then
      if nas_run_is_active "$NAS_GH_RUN_STATUS"; then
        echo "  [${_elapsed}s] ještě běží starší build ${_short} (${NAS_GH_RUN_STATUS}) – čekám na HEAD..."
      else
        echo "  [${_elapsed}s] poslední běh je ${_short}, HEAD je novější – čekám na nový build..."
      fi
    elif nas_run_is_active "$NAS_GH_RUN_STATUS"; then
      if [ "$_announced" != "$NAS_GH_RUN_URL" ]; then
        echo "Build běží (${NAS_GH_RUN_STATUS}): ${NAS_GH_RUN_URL}"
        _announced="$NAS_GH_RUN_URL"
      else
        echo "  [${_elapsed}s] ${NAS_GH_RUN_STATUS}..."
      fi
    elif [ "$NAS_GH_RUN_STATUS" = "completed" ]; then
      if [ "$NAS_GH_RUN_CONCLUSION" = "success" ]; then
        echo "Build dokončen (${_short})."
        _i=0
        while [ "$_i" -lt 12 ]; do
          _digest_now="$(nas_remote_digests "ghcr.io/${GHCR_OWNER}/podkladarna:${_tag}" | tr '\n' ' ')"
          if [ -n "$_digest_now" ] && [ "$_digest_now" != "$_digest_before" ]; then
            echo "GHCR má nový digest."
            return 0
          fi
          # Stejný digest může být cache hit – po chvíli jdeme dál.
          _i=$((_i + 1))
          [ "$_i" -ge 3 ] && [ -n "$_digest_now" ] && break
          sleep 5
        done
        echo "GHCR je připravené (digest se nezměnil, nebo se ještě projeví při pull)."
        return 0
      fi
      echo "Build selhal (${NAS_GH_RUN_CONCLUSION:-unknown}): ${NAS_GH_RUN_URL}" >&2
      return 1
    else
      echo "  [${_elapsed}s] neočekávaný status ${NAS_GH_RUN_STATUS:-?} – čekám..."
    fi

    sleep "$_poll"
  done
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
