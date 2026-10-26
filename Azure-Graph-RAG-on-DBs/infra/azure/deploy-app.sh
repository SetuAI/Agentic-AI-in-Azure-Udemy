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

# HOW TO RUN, STEP BY STEP
#   Mac / Linux (Terminal), from the project folder:
#       APP_NAME=fundgraph-demo RESOURCE_GROUP=rg-fundgraph bash infra/azure/deploy-app.sh
#   Windows: this is a bash script. Run it from Git Bash or WSL (not plain PowerShell):
#       APP_NAME=fundgraph-demo RESOURCE_GROUP=rg-fundgraph bash infra/azure/deploy-app.sh
#   It needs: az (logged in with "az login"), uv, zip and curl.
#
# Safety switches: -e stop on the first failure, -u stop on an unset variable,
# -o pipefail treat a failure inside a pipe as a failure.
set -euo pipefail

# Stop with a message if RESOURCE_GROUP or APP_NAME is not set.
: "${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
: "${APP_NAME:?Set APP_NAME}"

# The project folder: this script's folder, two levels up.
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
# Work from the project folder.
cd "$PROJECT"

echo "1. requirements.txt from uv.lock"
# Write the exact package versions from uv.lock into requirements.txt (no dev tools like pytest).
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt --quiet -o requirements.txt

echo "2. Packaging"
# A new, empty temporary folder for the zip.
BUILD="$(mktemp -d)"
# When the script ends (for any reason), delete that temporary folder.
trap 'rm -rf "$BUILD"' EXIT
# Zip the project folder (-q quiet, -r include sub-folders); -x lists what to leave out.
zip -qr "$BUILD/app.zip" . \
    -x ".env" ".env.*" ".git/*" ".venv/*" "*__pycache__*" ".pytest_cache/*" ".ruff_cache/*" \
       "tests/*" "evals/*" "infra/*" "*.md" ".DS_Store"
# Print the zip's size (du -h = human-readable size).
echo "   $(du -h "$BUILD/app.zip" | cut -f1) zip"

echo "3. Uploading (App Service installs packages; this takes a few minutes)"
# Upload the zip to the web app.
az webapp deploy --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --src-path "$BUILD/app.zip" --type zip --output none

echo "4. Waiting for /health"
# The app's address, e.g. fundgraph-demo.azurewebsites.net.
HOST="$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query defaultHostName -o tsv)"
# Try up to 30 times (seq 1 30 = 1, 2, ..., 30).
for _ in $(seq 1 30); do
    # Call /health; -f makes curl fail on an error status. Output is thrown away.
    if curl -fsS "https://${HOST}/health" >/dev/null 2>&1; then
        # It answered: the app is live.
        echo "Live: https://${HOST}"
        # Exit code 0 = success.
        exit 0
    fi
    # Not yet: wait 10 seconds and try again (30 x 10 s = 5 minutes).
    sleep 10
done
# Never answered: print where to look (>&2 = to the error output) ...
echo "No answer from /health after 5 minutes. Logs: az webapp log tail -n $APP_NAME -g $RESOURCE_GROUP" >&2
# ... and exit with code 1 = failure.
exit 1
