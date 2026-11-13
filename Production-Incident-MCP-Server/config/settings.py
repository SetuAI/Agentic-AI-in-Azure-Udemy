"""
config/settings.py
==================

WHAT THIS FILE IS FOR
---------------------
Reads every setting the project needs (Azure IDs, keys, names) from the
.env file, and keeps them in one place. Every other file imports its
settings from here, so no file ever reads .env by itself.

WHAT PROBLEM IT SOLVES
----------------------
Secrets and IDs must never be typed inside the code. They live in .env on
your laptop, and in "Environment variables" on Azure App Service. This file
reads them the same way in both places.

HOW TO RUN
----------
You never run this file directly. Other files use it like this:

    from config import settings
    print(settings.LOG_ANALYTICS_WORKSPACE_ID)

EXPECTED OUTPUT
---------------
Nothing is printed. If a needed value is missing, require() stops the
program with a message that names the missing value.
"""

# os: lets Python read "environment variables" (named values the computer holds for a program).
import os

# load_dotenv: reads the .env file and turns each line into an environment variable.
from dotenv import load_dotenv

# Read the .env file now. If there is no .env file (for example on Azure), this does nothing.
load_dotenv()


# ---------------------------------------------------------------------------
# Log Analytics: the place in Azure where all the logs are stored
# ---------------------------------------------------------------------------

# The ID of the Log Analytics workspace. Every search (query) is sent to this workspace.
LOG_ANALYTICS_WORKSPACE_ID = os.getenv("LOG_ANALYTICS_WORKSPACE_ID", "")

# The web address we upload logs to. Used only by loader/send_logs.py.
LOGS_INGESTION_ENDPOINT = os.getenv("LOGS_INGESTION_ENDPOINT", "")

# The ID of the "data collection rule": the Azure rule that accepts our logs and puts them in the tables.
DCR_IMMUTABLE_ID = os.getenv("DCR_IMMUTABLE_ID", "")

# Only if the Deployments_CL table ended up with its OWN rule: that rule's ID.
# If it is left empty, "or" falls back to the shared rule above (the usual case).
DEPLOYMENTS_DCR_IMMUTABLE_ID = os.getenv("DEPLOYMENTS_DCR_IMMUTABLE_ID", "") or DCR_IMMUTABLE_ID

# The name of the "door" in that rule for the app logs table. Azure creates this name for us.
APP_LOGS_STREAM = os.getenv("APP_LOGS_STREAM", "Custom-AppLogs_CL")

# The name of the "door" in that rule for the deployments table.
DEPLOYMENTS_STREAM = os.getenv("DEPLOYMENTS_STREAM", "Custom-Deployments_CL")


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

# The password that every AI app must send to use our server. Without it, the server says "no".
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

# The time zone we show times in. Azure stores logs in UTC; people in India read IST.
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Asia/Kolkata")


# ---------------------------------------------------------------------------
# Azure AI Content Safety: Prompt Shields checks log text before the AI reads it
# ---------------------------------------------------------------------------

# The Content Safety resource's address, like https://<name>.cognitiveservices.azure.com/
# If it is empty, the tools still work, but every log message is hidden from the AI.
CONTENT_SAFETY_ENDPOINT = os.getenv("CONTENT_SAFETY_ENDPOINT", "")


# ---------------------------------------------------------------------------
# Azure AI Search: holds the past postmortems, for the search_postmortems tool
# ---------------------------------------------------------------------------

# The search service's address, like https://<name>.search.windows.net
AI_SEARCH_ENDPOINT = os.getenv("AI_SEARCH_ENDPOINT", "")

# The name of the index that holds the postmortems.
POSTMORTEM_INDEX = os.getenv("POSTMORTEM_INDEX", "postmortems")


# ---------------------------------------------------------------------------
# Evaluation only (the evaluation/ folder). The server never reads these.
# ---------------------------------------------------------------------------

# The MCP server the evaluation asks its questions through. Your laptop by default.
EVAL_MCP_URL = os.getenv("EVAL_MCP_URL", "http://localhost:8000/mcp")

# The Azure OpenAI resource's address, like https://<name>.openai.azure.com/
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")

# The name of the model deployment that plays the AI app, and the judge.
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.1")

# The Azure OpenAI API version to call.
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")


# ---------------------------------------------------------------------------
# A helper that stops early, with a clear message, when a setting is empty
# ---------------------------------------------------------------------------

# Called like require("MCP_API_KEY", MCP_API_KEY) just before a value is used.
def require(name: str, value: str) -> str:
    """Return the value, or stop the program if it is empty."""
    # If the value is an empty text, the person forgot to fill it in.
    if not value:
        # Stop the program and say exactly which line of .env is missing.
        raise RuntimeError(f"{name} is empty. Fill it in your .env file (see .env.example).")
    # The value is there, so give it back.
    return value