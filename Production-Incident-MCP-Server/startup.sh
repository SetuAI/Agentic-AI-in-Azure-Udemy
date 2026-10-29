#!/bin/bash
# (The line above tells the computer to run this file with bash.)
# =============================================================================
# startup.sh
# =============================================================================
#
# The command Azure App Service runs to start our MCP server.
# You set it in the portal: your web app -> Configuration -> Startup Command:
#     bash startup.sh
#
# App Service installs requirements.txt when the code is uploaded, and
# switches that environment on before running this file, so plain "python"
# already has every library.
#
# PORT is given by App Service; 8000 is its usual value for Python apps.
#
# WINDOWS USERS: this file must keep "LF" line endings. If you edit it on
# Windows, check the bottom-right corner of VS Code says "LF", not "CRLF".
# With CRLF, App Service fails to start with a "\r: command not found" error.
# =============================================================================

# Start the web server:
#   python -m uvicorn       -> run the uvicorn web server
#   mcp_server.server:app   -> the web app named "app" inside mcp_server/server.py
#   --host 0.0.0.0          -> listen on every network card (needed inside Azure)
#   --port "${PORT:-8000}"  -> use the PORT Azure gives us, or 8000 if it is empty
python -m uvicorn mcp_server.server:app --host 0.0.0.0 --port "${PORT:-8000}"