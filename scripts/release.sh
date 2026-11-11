#!/usr/bin/env bash
# Lokální náhrada workflow docker.yml pro Linux / WSL (Docker Engine s buildx):
# z čistého master spustí testy, sestaví image a pushne ho do GHCR jako :latest a :<krátký sha>.
# Test B si :latest vezme cronem.
#
#   ./scripts/release.sh               # testy + build + push
#   ./scripts/release.sh --no-push     # jen testy a build (lokální kontrola)
#   ./scripts/release.sh --skip-tests  # jen když testy na stejném SHA už prošly a je to doložené
#   ./scripts/release.sh --test-b      # po pushi počká, až test B běží s novou verzí (cron na NAS do 5 min + drain)
#   ./scripts/release.sh --test-b --nas=petr@192.168.10.11   # nasazení na test B hned přes ssh (pod zámkem cronu)
#
# Jednorázově: docker login ghcr.io -u <login> s tokenem write:packages
# (z Windows: gh auth refresh -s write:packages; gh auth token | wsl docker login ghcr.io -u <login> --password-stdin).
# Testy: python3 s balíčky z requirements.txt + requirements-dev.txt (venv: PYTHON=/cesta/k/python).

set -euo pipefail

PUSH=true
TESTS=true
BRANCH=master
TEST_B=false
NAS=""
TEST_B_URL="${TEST_B_URL:-https://pt.kibos.link}"
TEST_B_WAIT_MINUTES="${TEST_B_WAIT_MINUTES:-150}"
NAS_DIR="${NAS_DIR:-/volume1/docker/podkladarna}"
for arg in "$@"; do
  case "$arg" in
    --no-push) PUSH=false ;;
    --skip-tests) TESTS=false ;;
    --branch=*) BRANCH="${arg#--branch=}" ;;
    --test-b) TEST_B=true ;;
    --nas=*) NAS="${arg#--nas=}"; TEST_B=true ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Neznámý argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"

current="$(git rev-parse --abbrev-ref HEAD)"
[ "$current" = "$BRANCH" ] || { echo "Chyba: jsi na větvi '$current', release jen z '$BRANCH'." >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Chyba: pracovní strom není čistý (git status)." >&2; exit 1; }
git pull --ff-only origin "$BRANCH"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ] || {
  echo "Chyba: lokální $BRANCH se liší od origin/$BRANCH (nepushnuté commity), build jen z kódu na GitHubu." >&2; exit 1; }
sha="$(git rev-parse --short=7 HEAD)"

owner="$(printf '%s' "${GHCR_OWNER:-pjanecekatlangmaster}" | tr '[:upper:]' '[:lower:]')"
image="ghcr.io/${owner}/podkladarna"
echo "Release ${image} @ ${sha}"

if $TESTS; then
  "$PY" -m pytest tests/ -q --tb=short
else
  echo "Testy přeskočeny (--skip-tests)."
fi

built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
trap 'git checkout -- configs/whats_new.yaml' EXIT
"$PY" scripts/generate_whats_new.py --released-at "$built_at"

# Výchozí driver „docker“ neumí export cache do registry → vlastní builder docker-container.
builder=podkladarna
docker buildx inspect "$builder" >/dev/null 2>&1 || docker buildx create --name "$builder" --driver docker-container

args=(buildx build --builder "$builder" --platform linux/amd64
  --build-arg "BUILD_DATE=${built_at}"
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)"
  --label "org.opencontainers.image.created=${built_at}"
  --cache-from "type=registry,ref=${image}:buildcache"
  -t "${image}:latest" -t "${image}:${sha}" -f Dockerfile)
if $PUSH; then
  args+=(--push --cache-to "type=registry,ref=${image}:buildcache,mode=max")
else
  args+=(--load)
fi
docker "${args[@]}" .

if ! $PUSH; then
  echo "Hotovo bez pushe: ${image}:latest a ${image}:${sha} jsou jen lokálně."
  exit 0
fi
docker buildx imagetools inspect "${image}:latest"
echo "Pushnuto: ${image}:latest = :${sha}."
$TEST_B || { echo "Test B (pt.kibos.link) si ho vezme cronem, až neběží job."; exit 0; }

# Nasazení na test B: cron na NAS (každých 5 min, čeká na doběhnutí jobu), nebo hned přes ssh
# pod stejným zámkem jako cron. Když zámek drží cron, nasazuje už on – jen čekáme.
version="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' app/settings.py)"
if [ -n "$NAS" ]; then
  echo "Spouštím nasazení test B na ${NAS} (čeká na doběhnutí běžícího jobu)..."
  ssh -o BatchMode=yes "$NAS" "cd '${NAS_DIR}' && sudo -n /usr/bin/flock -n /tmp/update-ab.lock '${NAS_DIR}/update-nas-ab.sh' test" \
    || echo "ssh nasazení neproběhlo (zámek drží cron, nebo chybí sudo) – čekám na cron."
fi
echo "Čekám, až ${TEST_B_URL} hlásí verzi ${version} (max ${TEST_B_WAIT_MINUTES} min)..."
deadline=$(( $(date +%s) + TEST_B_WAIT_MINUTES * 60 ))
while :; do
  health="$(curl -fsS --max-time 15 "${TEST_B_URL}/api/health" 2>/dev/null || true)"
  if printf '%s' "$health" | "$PY" -c 'import json,sys; h=json.load(sys.stdin); sys.exit(0 if h.get("version")==sys.argv[1] else 1)' "$version" 2>/dev/null; then
    code="$(curl -s -o /dev/null --max-time 15 -w '%{http_code}' "${TEST_B_URL}/")"
    echo "OK: test B běží ${version}, ${TEST_B_URL}/ → ${code}."
    [ "$code" = "200" ] || exit 1
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Chyba: test B ani po ${TEST_B_WAIT_MINUTES} min nehlásí ${version} (běží dlouhý job? log cronu na NAS)." >&2
    exit 1
  fi
  sleep 30
done
