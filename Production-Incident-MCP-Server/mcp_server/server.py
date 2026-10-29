"""
mcp_server/server.py
====================

WHAT THIS FILE IS FOR
---------------------
Starts our MCP server: the program that AI apps (Claude, Claude Code,
VS Code Copilot) connect to. It does three jobs:

    1. Creates the MCP server and adds the 5 tools from tools.py.
    2. Checks the API key on every request. No key, or a wrong key -> refused.
    3. Turns the server into a web app at the address /mcp, so AI apps can
       reach it over the internet (on your laptop first, then on Azure).

HOW A QUESTION FLOWS THROUGH THIS FILE
--------------------------------------
    AI app sends a request to  http://<server>/mcp
      -> ApiKeyCheck: is the "Authorization: Bearer <key>" header right?
           no  -> reply 401 "Missing or wrong API key" and stop
           yes -> pass the request on
      -> the MCP server reads which tool the AI wants, and its inputs
      -> it runs that tool (tools.py), which searches Log Analytics
      -> the result goes back to the AI app as JSON

HOW TO RUN
----------
On your laptop, from the project folder:

    uv run python -m mcp_server.server

On Azure App Service, startup.sh starts it instead.

EXPECTED OUTPUT
---------------
    INFO:     Uvicorn running on http://127.0.0.1:8000
The MCP address is then  http://127.0.0.1:8000/mcp
"""

# secrets: compares the API key safely (in a way that does not leak hints about the right key).
import secrets

# uvicorn: the web server program that runs our app and listens for requests.
import uvicorn

# MCPServer: the official MCP library's ready-made server.
from mcp.server.mcpserver import MCPServer

# ToolAnnotations: extra labels for a tool. We use it to say "this tool only reads".
from mcp.types import ToolAnnotations

# BaseHTTPMiddleware: lets us run our own check on every request before the server sees it.
from starlette.middleware.base import BaseHTTPMiddleware

# JSONResponse: a ready-made reply in JSON, used to say "refused".
from starlette.responses import JSONResponse

# Our settings (the API key).
from config import settings

# Our logger, to print messages with the time.
from config.logging_config import get_logger

# The 5 tools.
from mcp_server import tools

# A logger named after this file.
logger = get_logger(__name__)

# The API key every request must carry. If it is empty, stop right away: we never run an open server.
API_KEY = settings.require("MCP_API_KEY", settings.MCP_API_KEY)

# Instructions the AI app reads when it first connects. They describe what this server is for.
INSTRUCTIONS = """\
Production logs of Sahyadri Bank's 4 services: api-gateway, loan-service, payments, notifications.
Use these tools to investigate incidents. A good order is:
get_error_summary (where and when) -> get_errors (what) -> what_changed (why: a release?)
-> trace_request (one request's path). Use run_log_query only when the others don't fit.
All tool inputs and outputs use local time (IST) unless a tool says otherwise.
"""

# ---------------------------------------------------------------------------
# Step 1: create the MCP server and add the tools
# ---------------------------------------------------------------------------

# Create the server. The name and instructions are what AI apps see when they connect.
mcp = MCPServer(name="sahyadri-incident-desk", instructions=INSTRUCTIONS)

# Add each of the 5 tools to the server.
for tool in tools.ALL_TOOLS:
    # read_only_hint=True tells AI apps this tool never changes anything, so it is safe to call.
    mcp.add_tool(tool, annotations=ToolAnnotations(read_only_hint=True))


# ---------------------------------------------------------------------------
# Step 2: the API key check
# ---------------------------------------------------------------------------

# Runs before every request reaches the MCP server.
class ApiKeyCheck(BaseHTTPMiddleware):
    """Refuse every request that does not carry 'Authorization: Bearer <MCP_API_KEY>'."""

    # dispatch() is called once for every request.
    async def dispatch(self, request, call_next):
        # What the header must say: the word Bearer, a space, then our key.
        expected = f"Bearer {API_KEY}"
        # What the request actually sent ("" if it sent nothing).
        given = request.headers.get("authorization", "")
        # Compare them safely. If they are different, refuse.
        if not secrets.compare_digest(given.encode(), expected.encode()):
            # Write a warning in our own log (never the key itself).
            logger.warning("Refused a request with a missing or wrong API key")
            # Reply 401 ("not allowed") and stop here. The tools never run.
            return JSONResponse({"error": "Missing or wrong API key"}, status_code=401)
        # The key is right: pass the request on to the MCP server.
        return await call_next(request)


# ---------------------------------------------------------------------------
# Step 3: turn the server into a web app
# ---------------------------------------------------------------------------

# Create the web app. Its MCP address is /mcp. The settings:
#   stateless_http=True -> every request stands on its own; the server keeps no memory between them.
#                          This suits App Service, which may restart the app at any time.
#   json_response=True  -> reply with plain JSON (simple, and easy to read when testing).
#   host="0.0.0.0"      -> do not limit requests to "localhost". On Azure, requests arrive with
#                          the App Service web address, and must not be refused for that.
#                          Our API key check protects the server instead.
app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")

# Put the API key check in front of the whole web app.
app.add_middleware(ApiKeyCheck)


# Run the web server only when this file is started directly (on your laptop).
if __name__ == "__main__":
    # Listen on this computer only (127.0.0.1), on port 8000.
    uvicorn.run(app, host="127.0.0.1", port=8000)