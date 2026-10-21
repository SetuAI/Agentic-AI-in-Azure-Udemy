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

set -euo pipefail

: "${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
: "${APP_NAME:?Set APP_NAME (becomes https://APP_NAME.azurewebsites.net)}"
: "${PG_SERVER:?Set PG_SERVER (the Postgres flexible server name, without .postgres.database.azure.com)}"
LOCATION="${LOCATION:-eastus}"
PLAN_NAME="${PLAN_NAME:-${APP_NAME}-plan}"
PLAN_SKU="${PLAN_SKU:-B1}"
PG_RESOURCE_GROUP="${PG_RESOURCE_GROUP:-$RESOURCE_GROUP}"

PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT"

# ---- 1. Plan and web app -----------------------------------------------------
echo "1. App Service plan and web app"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none
az appservice plan create --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --is-linux --sku "$PLAN_SKU" --output none
if ! az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
    az webapp create --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --plan "$PLAN_NAME" \
        --runtime "PYTHON:3.12" --output none
fi
# Always on: keeps the app loaded, so the first question is not slowed by a
# cold start. healthCheckPath: App Service restarts the app if /health fails.
az webapp config set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --startup-file "bash startup.sh" --always-on true --http20-enabled true \
    --generic-configurations '{"healthCheckPath": "/health"}' --output none
az webapp update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --https-only true --output none
HOST="$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query defaultHostName -o tsv)"

# ---- 2. App settings from .env -------------------------------------------------
echo "2. App settings from .env"
# python-dotenv reads .env exactly as the app does (quotes, comments, ...).
# Empty values are skipped. SCM_DO_BUILD_DURING_DEPLOYMENT makes App Service
# install requirements.txt when code is deployed.
SETTINGS=()
while IFS= read -r line; do
    SETTINGS+=("$line")
done < <(uv run python -c "
from dotenv import dotenv_values
for key, value in dotenv_values('.env').items():
    if value:
        print(f'{key}={value}')
")
az webapp config appsettings set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --settings "${SETTINGS[@]}" SCM_DO_BUILD_DURING_DEPLOYMENT=true --output none
echo "   ${#SETTINGS[@]} settings copied"

# ---- 3. Postgres firewall --------------------------------------------------------
echo "3. Postgres firewall rules for the web app's outbound addresses"
IPS="$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --query possibleOutboundIpAddresses -o tsv | tr ',' ' ')"
for ip in $IPS; do
    az postgres flexible-server firewall-rule create --resource-group "$PG_RESOURCE_GROUP" \
        --name "$PG_SERVER" --rule-name "app-${APP_NAME}-${ip//./-}" \
        --start-ip-address "$ip" --end-ip-address "$ip" --output none
done
echo "   $(echo "$IPS" | wc -w | tr -d ' ') addresses allowed"

# ---- 4. Entra ID sign-in ---------------------------------------------------------
echo "4. Entra ID sign-in"
TENANT="$(az account show --query tenantId -o tsv)"
CALLBACK="https://${HOST}/.auth/login/aad/callback"

# Reuse the app registration if this script ran before.
CLIENT_ID="$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv)"
if [ -z "$CLIENT_ID" ]; then
    # AzureADMyOrg: only accounts in your own tenant can sign in.
    CLIENT_ID="$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg \
        --web-redirect-uris "$CALLBACK" --enable-id-token-issuance true --query appId -o tsv)"
    az ad sp create --id "$CLIENT_ID" --output none
    echo "   app registration created: $CLIENT_ID"
else
    az ad app update --id "$CLIENT_ID" --web-redirect-uris "$CALLBACK" --enable-id-token-issuance true
    echo "   app registration reused: $CLIENT_ID"
fi

# A client secret lets App Service complete the sign-in. It is stored only
# as an app setting, never printed. --append keeps any older secret working.
SECRET="$(az ad app credential reset --id "$CLIENT_ID" --display-name "app-service-sign-in" \
    --years 1 --append --query password -o tsv)"
az webapp config appsettings set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --settings MICROSOFT_PROVIDER_AUTHENTICATION_SECRET="$SECRET" --output none

az webapp auth microsoft update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --client-id "$CLIENT_ID" --client-secret-setting-name MICROSOFT_PROVIDER_AUTHENTICATION_SECRET \
    --issuer "https://login.microsoftonline.com/${TENANT}/v2.0" --yes --output none
az webapp auth update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --enabled true --action RedirectToLoginPage --redirect-provider azureactivedirectory \
    --require-https true --excluded-paths '["/health"]' --output none

echo
echo "Done: https://${HOST}  (sign-in required; /health open)"
echo "Next: APP_NAME=$APP_NAME RESOURCE_GROUP=$RESOURCE_GROUP bash infra/azure/deploy-app.sh"
