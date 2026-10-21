#!/bin/bash
# =============================================================================
# infra/azure/deploy-app.sh
# =============================================================================
#
# WHAT THIS FILE IS FOR
# ---------------------
# Sends the current code to the App Service made by create-app-service.sh.
# Run after every change you want live:
#
#     APP_NAME=fundgraph-demo RESOURCE_GROUP=rg-fundgraph bash infra/azure/deploy-app.sh
#
# WHAT IT DOES
# ------------
#   1. Writes requirements.txt from uv.lock, so App Service installs exactly
#      the versions used locally
#   2. Zips the code the app needs. Never included: .env (the secrets are
#      already app settings), .venv, caches, tests, evals
#   3. Uploads the zip. App Service installs requirements.txt, then runs
#      startup.sh
#   4. Waits for /health to answer
# =============================================================================

set -euo pipefail

: "${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
: "${APP_NAME:?Set APP_NAME}"

PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT"

echo "1. requirements.txt from uv.lock"
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt --quiet -o requirements.txt

echo "2. Packaging"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
zip -qr "$BUILD/app.zip" . \
    -x ".env" ".env.*" ".git/*" ".venv/*" "*__pycache__*" ".pytest_cache/*" ".ruff_cache/*" \
       "tests/*" "evals/*" "infra/*" "*.md" ".DS_Store"
echo "   $(du -h "$BUILD/app.zip" | cut -f1) zip"

echo "3. Uploading (App Service installs packages; this takes a few minutes)"
az webapp deploy --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --src-path "$BUILD/app.zip" --type zip --output none

echo "4. Waiting for /health"
HOST="$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query defaultHostName -o tsv)"
for _ in $(seq 1 30); do
    if curl -fsS "https://${HOST}/health" >/dev/null 2>&1; then
        echo "Live: https://${HOST}"
        exit 0
    fi
    sleep 10
done
echo "No answer from /health after 5 minutes. Logs: az webapp log tail -n $APP_NAME -g $RESOURCE_GROUP" >&2
exit 1
