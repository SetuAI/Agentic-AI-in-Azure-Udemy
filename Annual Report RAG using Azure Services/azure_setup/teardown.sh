#!/usr/bin/env bash
# ==============================================================================
# DELETE EVERYTHING provision.sh CREATED -- stops all charges
# ==============================================================================
#     bash azure_setup/teardown.sh
#
# Deletes the whole resource group, and with it every resource inside: Azure
# OpenAI, AI Search (and the index), and the storage
# account (and the uploaded PDFs). This cannot be undone.
# ==============================================================================

set -euo pipefail                               # stop at the first error

RG="rg-annual-report-rag"                       # must match the name in provision.sh

read -r -p "Delete resource group '$RG' and everything in it? Type yes to confirm: " ANSWER  # ask first
if [ "$ANSWER" != "yes" ]; then                 # anything other than "yes"...
  echo "Cancelled. Nothing was deleted."        # ...does nothing
  exit 0
fi

az group delete --name "$RG" --yes --no-wait    # start the deletion; --no-wait returns straight away
echo "Deletion started. It finishes in a few minutes -- check the Azure portal."

# Azure OpenAI is "soft-deleted" for 48 hours, which
# blocks re-using the same names. To free the names immediately, purge them:
#   az cognitiveservices account list-deleted
#   az cognitiveservices account purge --name <name> --resource-group rg-annual-report-rag --location <region>
