#!/bin/bash
# (The line above is the "shebang": it tells the computer to run this file with bash.)
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

# Start the web server:
#   python -m uvicorn      -> run the uvicorn web server
#   api.main:app           -> the FastAPI object named "app" inside api/main.py
#   --host 0.0.0.0         -> listen on every network card (needed inside Azure)
#   --port "${PORT:-8000}" -> use the PORT variable Azure gives us, or 8000 if it is empty
#   --workers 1            -> exactly one process (see "ONE WORKER" above)
python -m uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
