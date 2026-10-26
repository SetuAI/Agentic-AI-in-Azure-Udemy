#!/bin/bash
# =============================================================================
# infra/azure/create-app-service.sh
# =============================================================================
#
# WHAT THIS FILE IS FOR
# ---------------------
# Creates the Azure App Service that hosts the API and demo page, and puts
# Microsoft Entra ID sign-in in front of it. Run once; safe to run again.
#
#     RESOURCE_GROUP=rg-fundgraph APP_NAME=fundgraph-demo PG_SERVER=fundgraph-pg \
#         bash infra/azure/create-app-service.sh
#
# Then deploy the code with infra/azure/deploy-app.sh.
#
# WHAT IT DOES
# ------------
#   1. An App Service plan (Linux, B1) and a Python 3.12 web app
#   2. App settings: every value from .env, so the app reads the same
#      settings on Azure as on a laptop
#   3. A Postgres firewall rule for each address the web app can call out
#      from, so the app can reach the database
#   4. An Entra ID app registration, and App Service sign-in ("Easy Auth")
#      using it: anyone not signed in to your tenant is sent to the
#      Microsoft sign-in page. /health stays open for health checks.
#
# BEFORE RUNNING
# --------------
#   az login                      (an account that can create app registrations)
#   .env filled in                (the same values the app uses locally)
# =============================================================================

# HOW TO RUN, STEP BY STEP
#   Mac / Linux (Terminal), from the project folder:
#       RESOURCE_GROUP=rg-fundgraph APP_NAME=fundgraph-demo PG_SERVER=fundgraph-pg bash infra/azure/create-app-service.sh
#   Windows: this is a bash script. Run it from Git Bash or WSL (not plain PowerShell):
#       RESOURCE_GROUP=rg-fundgraph APP_NAME=fundgraph-demo PG_SERVER=fundgraph-pg bash infra/azure/create-app-service.sh
#   Replace the three example values with your own names.
#
# Safety switches for the whole script:
#   -e          stop at the first command that fails
#   -u          stop if a variable is used without being set
#   -o pipefail a failure anywhere in a "a | b" pipe counts as a failure
set -euo pipefail

# ":" does nothing; "${NAME:?message}" stops the script with that message if NAME is not set.
# So these three lines make RESOURCE_GROUP, APP_NAME and PG_SERVER required.
: "${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
: "${APP_NAME:?Set APP_NAME (becomes https://APP_NAME.azurewebsites.net)}"
: "${PG_SERVER:?Set PG_SERVER (the Postgres flexible server name, without .postgres.database.azure.com)}"
# Optional settings: "${NAME:-default}" uses the default when NAME is not set.
# LOCATION = the Azure region.
LOCATION="${LOCATION:-eastus}"
# The App Service plan's name (default: <APP_NAME>-plan).
PLAN_NAME="${PLAN_NAME:-${APP_NAME}-plan}"
# The plan's size/price tier (B1 = Basic, the smallest with Always On).
PLAN_SKU="${PLAN_SKU:-B1}"
# The resource group of the Postgres server (default: the same group).
PG_RESOURCE_GROUP="${PG_RESOURCE_GROUP:-$RESOURCE_GROUP}"

# The project folder: this script's folder ($0 = the script path), two levels up.
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
# Run everything from the project folder (so .env and startup.sh are found).
cd "$PROJECT"

# ---- 1. Plan and web app -----------------------------------------------------
# echo prints a progress line.
echo "1. App Service plan and web app"
# Create the resource group (does nothing if it already exists). --output none hides the JSON reply.
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none
# Create the Linux App Service plan (the machine the app runs on). "\" continues the command on the next line.
az appservice plan create --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --is-linux --sku "$PLAN_SKU" --output none
# If the web app does not exist yet ("!" = not; 2>/dev/null hides the error message) ...
if ! az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
    # ... create it with Python 3.12.
    az webapp create --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --plan "$PLAN_NAME" \
        --runtime "PYTHON:3.12" --output none
# End of the "if".
fi
# Always on: keeps the app loaded, so the first question is not slowed by a
# cold start. healthCheckPath: App Service restarts the app if /health fails.
# Start command = startup.sh; Always On; HTTP/2; health check on /health.
az webapp config set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --startup-file "bash startup.sh" --always-on true --http20-enabled true \
    --generic-configurations '{"healthCheckPath": "/health"}' --output none
# Redirect plain http:// to https://.
az webapp update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --https-only true --output none
# The app's address, e.g. fundgraph-demo.azurewebsites.net ("$( ... )" captures a command's output).
HOST="$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query defaultHostName -o tsv)"

# ---- 2. App settings from .env -------------------------------------------------
echo "2. App settings from .env"
# python-dotenv reads .env exactly as the app does (quotes, comments, ...).
# Empty values are skipped. SCM_DO_BUILD_DURING_DEPLOYMENT makes App Service
# install requirements.txt when code is deployed.
# An empty bash list (array) for the KEY=value settings.
SETTINGS=()
# Read the Python program's output line by line ...
while IFS= read -r line; do
    # ... and add each KEY=value line to the list.
    SETTINGS+=("$line")
# "< <( ... )" feeds the output of the small Python program below into the loop.
# The program prints every non-empty .env value as KEY=value.
done < <(uv run python -c "
from dotenv import dotenv_values
for key, value in dotenv_values('.env').items():
    if value:
        print(f'{key}={value}')
")
# Store every KEY=value as an App Service app setting (the app reads them as environment variables).
az webapp config appsettings set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --settings "${SETTINGS[@]}" SCM_DO_BUILD_DURING_DEPLOYMENT=true --output none
# ${#SETTINGS[@]} = how many items are in the list.
echo "   ${#SETTINGS[@]} settings copied"

# ---- 3. Postgres firewall --------------------------------------------------------
echo "3. Postgres firewall rules for the web app's outbound addresses"
# Every IP address the web app may call out from; tr turns the commas into spaces.
IPS="$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --query possibleOutboundIpAddresses -o tsv | tr ',' ' ')"
# For each address ...
for ip in $IPS; do
    # ... allow it through the Postgres firewall. ${ip//./-} replaces dots with dashes for the rule name.
    az postgres flexible-server firewall-rule create --resource-group "$PG_RESOURCE_GROUP" \
        --name "$PG_SERVER" --rule-name "app-${APP_NAME}-${ip//./-}" \
        --start-ip-address "$ip" --end-ip-address "$ip" --output none
# End of the loop.
done
# wc -w counts the words (addresses); tr -d ' ' removes padding spaces.
echo "   $(echo "$IPS" | wc -w | tr -d ' ') addresses allowed"

# ---- 4. Entra ID sign-in ---------------------------------------------------------
echo "4. Entra ID sign-in"
# Your Entra ID tenant (directory) ID.
TENANT="$(az account show --query tenantId -o tsv)"
# The address Entra ID sends users back to after sign-in.
CALLBACK="https://${HOST}/.auth/login/aad/callback"

# Reuse the app registration if this script ran before.
CLIENT_ID="$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv)"
# -z = "is empty": no app registration found, so create one.
if [ -z "$CLIENT_ID" ]; then
    # AzureADMyOrg: only accounts in your own tenant can sign in.
    CLIENT_ID="$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg \
        --web-redirect-uris "$CALLBACK" --enable-id-token-issuance true --query appId -o tsv)"
    # Create its service principal (the registration's identity in your tenant).
    az ad sp create --id "$CLIENT_ID" --output none
    echo "   app registration created: $CLIENT_ID"
# Found one: update its redirect address in case the host changed.
else
    az ad app update --id "$CLIENT_ID" --web-redirect-uris "$CALLBACK" --enable-id-token-issuance true
    echo "   app registration reused: $CLIENT_ID"
fi

# A client secret lets App Service complete the sign-in. It is stored only
# as an app setting, never printed. --append keeps any older secret working.
SECRET="$(az ad app credential reset --id "$CLIENT_ID" --display-name "app-service-sign-in" \
    --years 1 --append --query password -o tsv)"
# Save the secret as an app setting.
az webapp config appsettings set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --settings MICROSOFT_PROVIDER_AUTHENTICATION_SECRET="$SECRET" --output none

# Point App Service sign-in at the app registration, its secret setting and your tenant.
az webapp auth microsoft update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --client-id "$CLIENT_ID" --client-secret-setting-name MICROSOFT_PROVIDER_AUTHENTICATION_SECRET \
    --issuer "https://login.microsoftonline.com/${TENANT}/v2.0" --yes --output none
# Switch sign-in on: anyone not signed in is redirected to the Microsoft login page,
# except for /health.
az webapp auth update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --enabled true --action RedirectToLoginPage --redirect-provider azureactivedirectory \
    --require-https true --excluded-paths '["/health"]' --output none

echo
# Print the app's address and the next command to run.
echo "Done: https://${HOST}  (sign-in required; /health open)"
echo "Next: APP_NAME=$APP_NAME RESOURCE_GROUP=$RESOURCE_GROUP bash infra/azure/deploy-app.sh"
