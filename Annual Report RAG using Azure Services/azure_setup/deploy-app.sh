#!/bin/bash
# =============================================================================
# azure_setup/deploy-app.sh  -  send the current code to the web app
# =============================================================================
#
# PURPOSE
#   Uploads this project's code to the Azure web app created in hosting.md.
#   Run it after the web app is set up, and again after every code change.
#
# WHAT PROBLEM DOES IT SOLVE
#   The web app must get the code, but NOT the things that should never leave
#   your laptop (.env with its keys) or that it doesn't need (the PDFs, the
#   markdown cache, the tests, the virtual environment). Zipping by hand makes
#   it easy to include the wrong files. This script always leaves them out.
#
# WHAT IT DOES
#   1. Zips the project, leaving out:
#        .env          the keys are already app settings on Azure
#        data/, pdfs/  the PDFs and markdown cache: the web app only searches
#                      the index, it never reads a PDF
#        .venv/        Azure builds its own environment from requirements.txt
#        tests/        not needed to run the app
#        azure_setup/  set-up scripts, run from your laptop only
#        *.md          documentation
#   2. Uploads the zip. Because SCM_DO_BUILD_DURING_DEPLOYMENT is true,
#      Azure installs requirements.txt, then runs startup.sh.
#      3 to 5 minutes the first time; faster afterwards.
#   3. Calls /health every 10 seconds, for up to 5 minutes, until it answers.
#
# HOW TO RUN (from the project folder, after az login)
#   RESOURCE_GROUP=rg-annual-report-rag APP_NAME=annualreport-rag-tarka bash azure_setup/deploy-app.sh
#
# EXPECTED OUTPUT
#   1. Packaging
#      52K zip
#   2. Uploading (Azure installs the packages; this takes a few minutes)
#   3. Waiting for /health
#   Live: https://annualreport-rag-tarka.azurewebsites.net
# =============================================================================

set -euo pipefail                               # stop at the first error; an unset variable is an error too

: "${RESOURCE_GROUP:?Set RESOURCE_GROUP}"       # stop with this message if RESOURCE_GROUP is not set
: "${APP_NAME:?Set APP_NAME}"                   # stop with this message if APP_NAME is not set

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"     # the project folder: this script's folder, one level up
cd "$PROJECT"                                   # work from there, wherever the script was started

echo "1. Packaging"
BUILD="$(mktemp -d)"                            # a new, empty temporary folder for the zip
trap 'rm -rf "$BUILD"' EXIT                     # delete it when the script ends, whatever happens
zip -qr "$BUILD/app.zip" . \
    -x ".env" ".env.*" ".git/*" ".venv/*" "data/*" "pdfs/*" "tests/*" "azure_setup/*" \
       "*__pycache__*" ".pytest_cache/*" "*.md" ".DS_Store"   # -q quiet, -r include sub-folders, -x leave these out
echo "   $(du -h "$BUILD/app.zip" | cut -f1) zip"  # the zip's size: small, because the PDFs are left out

echo "2. Uploading (Azure installs the packages; this takes a few minutes)"
az webapp deploy --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --src-path "$BUILD/app.zip" --type zip --output none   # upload; Azure unzips it, installs, and restarts the app

echo "3. Waiting for /health"
HOST="$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query defaultHostName -o tsv)"  # the app's address
for _ in $(seq 1 30); do                        # up to 30 tries
    if curl -fsS "https://${HOST}/health" >/dev/null 2>&1; then  # -f: an error status counts as a failure
        echo "Live: https://${HOST}"            # it answered: the app is running
        exit 0
    fi
    sleep 10                                    # not yet: wait 10 seconds (30 x 10 s = 5 minutes)
done
echo "No answer from /health after 5 minutes. Logs: az webapp log tail -n $APP_NAME -g $RESOURCE_GROUP" >&2
exit 1                                          # never answered: exit with a failure code