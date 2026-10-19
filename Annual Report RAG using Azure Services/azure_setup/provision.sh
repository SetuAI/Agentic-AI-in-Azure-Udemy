#!/usr/bin/env bash
# ==============================================================================
# CREATE EVERY AZURE RESOURCE THE PIPELINE NEEDS
# ==============================================================================
# Run from the azure_based/ folder:
#     az login                          # sign in once
#     bash azure_setup/provision.sh     # create everything (takes ~10 minutes)
#
# At the end it prints the lines to paste into azure_based/.env.
#
# THIS COSTS MONEY while the resources exist (AI Search Basic is billed per hour).
# When you are done:  bash azure_setup/teardown.sh
# ==============================================================================

set -euo pipefail                               # stop at the first error; treat unset variables as errors

# --- names: change these if you like (storage and service names must be globally unique) ---
SUFFIX="${SUFFIX:-$RANDOM}"                     # a random number, so names do not clash with anyone else's
LOCATION="${LOCATION:-eastus2}"                 # a region that offers all four services and these models
RG="rg-annual-report-rag"                       # the resource group: one folder holding everything
OPENAI="aoai-annualreport-$SUFFIX"              # Azure OpenAI resource name
SEARCH="srch-annualreport-$SUFFIX"              # Azure AI Search service name
STORAGE="stannualreport$SUFFIX"                 # storage account: lowercase letters and digits only
CONTAINER="annual-reports"                      # the Blob container for the PDFs

echo "Creating resource group $RG in $LOCATION"
az group create --name "$RG" --location "$LOCATION" --output none   # the folder everything goes in

# --- Azure OpenAI, plus one deployment per model ------------------------------
echo "Creating Azure OpenAI resource $OPENAI"
az cognitiveservices account create --name "$OPENAI" --resource-group "$RG" \
  --location "$LOCATION" --kind OpenAI --sku S0 \
  --custom-domain "$OPENAI" --output none       # custom domain = the resource's own endpoint URL

deploy() {                                      # helper: deploy one model under its own name
  echo "  deploying $1 ($2)"
  az cognitiveservices account deployment create --name "$OPENAI" --resource-group "$RG" \
    --deployment-name "$1" --model-name "$1" --model-version "$2" --model-format OpenAI \
    --sku-name GlobalStandard --sku-capacity "$3" --output none  # capacity = thousands of tokens per minute
}
deploy gpt-4o 2024-11-20 30                     # writes the answers
deploy gpt-4o-mini 2024-07-18 30                # runs the guardrail checks
deploy text-embedding-3-small 1 120             # embeddings -- ingestion sends a lot of text at once

# --- Azure AI Search, with the semantic ranker turned on ----------------------
echo "Creating Azure AI Search service $SEARCH (Basic tier)"
az search service create --name "$SEARCH" --resource-group "$RG" --location "$LOCATION" \
  --sku basic --semantic-search free --output none  # 'free' plan: 1,000 semantic queries a month at no extra cost

# --- Blob Storage ---------------------------------------------------------------
echo "Creating storage account $STORAGE and container $CONTAINER"
az storage account create --name "$STORAGE" --resource-group "$RG" --location "$LOCATION" \
  --sku Standard_LRS --allow-blob-public-access false --output none  # private: nobody can read blobs anonymously
CONN=$(az storage account show-connection-string --name "$STORAGE" --resource-group "$RG" \
  --query connectionString --output tsv)        # the connection string (account name + key)
az storage container create --name "$CONTAINER" --connection-string "$CONN" --output none

# --- print the .env values ------------------------------------------------------
OPENAI_ENDPOINT=$(az cognitiveservices account show --name "$OPENAI" --resource-group "$RG" --query properties.endpoint -o tsv)
OPENAI_KEY=$(az cognitiveservices account keys list --name "$OPENAI" --resource-group "$RG" --query key1 -o tsv)
SEARCH_KEY=$(az search admin-key show --service-name "$SEARCH" --resource-group "$RG" --query primaryKey -o tsv)

cat <<EOF

================================================================================
Done. Paste these lines into azure_based/.env
================================================================================
AZURE_OPENAI_ENDPOINT=$OPENAI_ENDPOINT
AZURE_OPENAI_API_KEY=$OPENAI_KEY
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_GENERATION_DEPLOYMENT=gpt-4o
AZURE_OPENAI_GUARDRAIL_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
AZURE_SEARCH_ENDPOINT=https://$SEARCH.search.windows.net
AZURE_SEARCH_API_KEY=$SEARCH_KEY
AZURE_SEARCH_INDEX=annual-reports
AZURE_STORAGE_CONNECTION_STRING=$CONN
AZURE_STORAGE_CONTAINER=$CONTAINER
LOCAL_PDF_DIR=./pdfs
================================================================================
Remember: bash azure_setup/teardown.sh when you are finished, to stop charges.
EOF
