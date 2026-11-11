#!/usr/bin/env bash
# Lokální náhrada workflow docker.yml pro Linux / WSL (Docker Engine s buildx):
# z čistého master spustí testy, sestaví image a pushne ho do GHCR jako :latest a :<krátký sha>.
# Test B si :latest vezme cronem.
#
#   ./scripts/release.sh               # testy + build + push
#   ./scripts/release.sh --no-push     # jen testy a build (lokální kontrola)
#   ./scripts/release.sh --skip-tests  # jen když testy na stejném SHA už prošly a je to doložené
#
# Jednorázově: docker login ghcr.io -u <login> s tokenem write:packages
# (z Windows: gh auth refresh -s write:packages; gh auth token | wsl docker login ghcr.io -u <login> --password-stdin).
# Testy: python3 s balíčky z requirements.txt + requirements-dev.txt (venv: PYTHON=/cesta/k/python).

set -euo pipefail

PUSH=true
TESTS=true
BRANCH=master
for arg in "$@"; do
  case "$arg" in
    --no-push) PUSH=false ;;
    --skip-tests) TESTS=false ;;
    --branch=*) BRANCH="${arg#--branch=}" ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
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

if $PUSH; then
  docker buildx imagetools inspect "${image}:latest"
  echo "Hotovo: ${image}:latest = :${sha}. Test B (pt.kibos.link) si ho vezme cronem, až neběží job."
else
  echo "Hotovo bez pushe: ${image}:latest a ${image}:${sha} jsou jen lokálně."
fi
