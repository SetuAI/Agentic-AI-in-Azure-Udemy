#!/bin/bash
# =============================================================================
# startup.sh
# =============================================================================
#
# The command App Service runs to start the app (set as the web app's
# startup file by infra/azure/create-app-service.sh).
#
# App Service installs requirements.txt during deployment and activates that
# environment before running this file, so plain "python" has every package.
#
# ONE WORKER, ONE INSTANCE
# ------------------------
# Conversation history is kept in the server's memory (InMemorySaver,
# agent/graph.py). With two workers or two instances, a follow-up question
# could land on a process that never saw the first one. Keep both at 1
# until Stage 8 moves the history into Postgres.
#
# PORT is set by App Service; 8000 is its default for Python apps.
# =============================================================================

python -m uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
