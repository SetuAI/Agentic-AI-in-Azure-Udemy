#!/bin/bash
# =============================================================================
# azure_setup/deploy-app.sh  -  send the current code to the web app
# =============================================================================
#
# PURPOSE
#   Uploads this project's code to the Azure web app created in docs/architecture/webapps.md.
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
#      The script checks every 20 seconds until Azure says this deployment
#      finished (3 to 10 minutes; up to 15 before it gives up).
#   3. Calls /health every 10 seconds, for up to 5 minutes, until it answers.
#
# HOW TO RUN (from the project folder, after az login)
#   RESOURCE_GROUP=rg-annual-report-rag APP_NAME=annualreport-rag-tarka bash azure_setup/deploy-app.sh
#
# EXPECTED OUTPUT
#   1. Packaging
#      52K zip
#   2. Uploading
#      WARNING: Status: Building the app... Time: 20(s)      (Azure's progress, every ~16 s)
#      WARNING: Status: Build successful. Time: 192(s)
#      WARNING: Status: Starting the site... Time: 208(s)
#      WARNING: Deployment has completed successfully
#      Installing packages on Azure (...)
#      Installed
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

latest_deployment() {                           # prints "<id> <complete> <status>" of the newest deployment
    az webapp log deployment list --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
        --query "max_by(@, &start_time).[id, complete, status]" -o tsv 2>/dev/null | paste -sd' ' - || true   # paste: join the three lines into one
}
PREVIOUS="$(latest_deployment | cut -d' ' -f1)"  # the deployment before this one, so we can tell ours apart

echo "2. Uploading"
az webapp deploy --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --src-path "$BUILD/app.zip" --type zip --async true --output none   # upload; Azure unzips it, installs, and restarts the app
                                                # --async: return once uploaded, and wait below instead. Without it the
                                                # CLI gives up after a few minutes (504 Gateway Timeout) while Azure
                                                # is still installing, and the script stops even though nothing failed.

echo "   Installing packages on Azure (3 to 10 minutes; a status line every 20 seconds)"   # usually done already: the upload above waits too
INSTALLED=no
for _ in $(seq 1 45); do                        # up to 45 checks x 20 s = 15 minutes
    read -r ID COMPLETE STATUS <<< "$(latest_deployment)"   # split the line into three variables
    if [ -n "${ID:-}" ] && [ "$ID" != "$PREVIOUS" ]; then   # our deployment has appeared
        if [ "$COMPLETE" = "True" ] || [ "$COMPLETE" = "true" ]; then
            [ "$STATUS" = "4" ] && { echo "   Installed"; INSTALLED=yes; break; }   # 4 = success
            echo "Deployment failed. Logs: az webapp log deployment show -n $APP_NAME -g $RESOURCE_GROUP" >&2
            exit 1                              # 3 = failed
        fi
        echo "   still installing..."
    else
        echo "   waiting for Azure to start the deployment..."
    fi
    sleep 20
done
if [ "$INSTALLED" != yes ]; then                # 15 minutes passed without an answer
    echo "Still not finished after 15 minutes. Logs: az webapp log deployment show -n $APP_NAME -g $RESOURCE_GROUP" >&2
    exit 1
fi

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