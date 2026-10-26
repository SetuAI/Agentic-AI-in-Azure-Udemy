"""
api/main.py
===========

WHAT THIS FILE IS FOR
---------------------
The web API in front of the agent, and the demo page.

    GET  /               -> the demo page (ui/index.html)
    POST /query          -> ask a question; returns table, commentary and every detail
    GET  /schema-graph   -> the 20 tables and their approved links, for drawing the graph
    GET  /graph          -> a full-screen, clickable view of that graph (ui/graph.html)
    GET  /table-counts   -> exact number of rows in each of the 20 tables (counted once, then reused)
    GET  /me             -> the signed-in user (Entra ID on App Service; "local" on a laptop)
    GET  /health         -> "ok" if the server is up (no sign-in needed, for health checks)
    GET  /docs           -> FastAPI's automatic page for trying the API by hand

HOW TO RUN
----------
    uv run uvicorn api.main:app --port 8000
Then open http://localhost:8000

SIGN-IN (ENTRA ID)
------------------
On App Service, sign-in is done by App Service itself ("Easy Auth",
infra/azure/create-app-service.sh), before a request reaches this code.
It adds headers naming the signed-in user, and removes any such headers a
caller tries to send, so they can be trusted there:

    X-MS-CLIENT-PRINCIPAL-ID    the user's unique id in Entra ID
    X-MS-CLIENT-PRINCIPAL-NAME  usually their email address

WEBSITE_SITE_NAME is set only on App Service. There, a request without the
headers means sign-in is switched off or misconfigured, so it is refused
rather than answered anonymously. Locally there is no sign-in, and every
request counts as the user "local".
"""

# os: reads environment variables such as WEBSITE_SITE_NAME.
import os

# uuid: creates random unique IDs for new conversations.
import uuid

# lru_cache: remembers the table counts after the first call.
from functools import lru_cache

# Application Insights must be switched on BEFORE FastAPI is imported and
# the app is created, so it can hook into every request.
# Our Application Insights setup (api/telemetry.py).
from api import telemetry

# Switch monitoring on now (it does nothing if no connection string is set).
telemetry.setup()

# "# noqa: E402" tells the linter that importing after code (telemetry.setup) is on purpose.
from pathlib import Path  # noqa: E402

# FastAPI: the web framework. HTTPException: returns an error status. Request: the incoming HTTP request.
from fastapi import FastAPI, HTTPException, Request  # noqa: E402

# FileResponse: sends a file (the HTML pages) back to the browser.
from fastapi.responses import FileResponse  # noqa: E402

# BaseModel: describes the JSON body a request must send; FastAPI checks it automatically.
from pydantic import BaseModel  # noqa: E402

# Shared config, notes and connections.
from agent import context  # noqa: E402

# ask(): runs one question through the agent.
from agent.graph import ask  # noqa: E402

# The web application. uvicorn runs this object ("api.main:app").
app = FastAPI(title="FundGraph: ask the mutual fund database")
# The ui/ folder holding the HTML pages (api/ -> project folder -> ui/).
STATIC = Path(__file__).parent.parent / "ui"


class QueryRequest(BaseModel):
    """What the caller sends. conversation_id ties follow-up questions together."""
    # The question text (required).
    question: str
    # Optional: send the ID from an earlier answer to ask a follow-up.
    conversation_id: str | None = None


# True when running on Azure App Service, False on a laptop.
ON_APP_SERVICE = bool(os.environ.get("WEBSITE_SITE_NAME"))


def signed_in_user(http: Request) -> dict:
    """The user App Service signed in, or "local" when running on a laptop."""
    # The user ID header added by App Service sign-in (None if missing).
    user_id = http.headers.get("x-ms-client-principal-id")
    # No signed-in user ...
    if not user_id:
        # ... on Azure: refuse with 401 (Unauthorized).
        if ON_APP_SERVICE:
            raise HTTPException(status_code=401, detail="Sign-in required")
        # ... on a laptop: everyone is "local".
        return {"id": "local", "name": "local"}
    # Signed in: the ID and the name (falls back to the ID if no name header).
    return {"id": user_id, "name": http.headers.get("x-ms-client-principal-name", user_id)}


# @app.get("/me") makes the function below answer GET requests to /me.
@app.get("/me")
def me(http: Request) -> dict:
    """Who is signed in, for the demo page's header."""
    # FastAPI turns the returned dict into JSON automatically.
    return signed_in_user(http)


# The page receives at most this many rows; the full count is in details.row_count.
ROWS_SHOWN = 500


# POST /query: the main endpoint. FastAPI reads the JSON body into a QueryRequest.
@app.post("/query")
# request = the JSON body; http = the raw request (for the sign-in headers).
def query(request: QueryRequest, http: Request) -> dict:
    # Who is asking (401 on Azure if nobody is signed in).
    user = signed_in_user(http)
    # No ID, an empty one, or the text "null" (easy to type by mistake in
    # /docs) all mean: start a new conversation.
    # The ID the caller sent, without surrounding spaces ("" if none).
    given = (request.conversation_id or "").strip()
    # Use it, or create a new random ID.
    conversation_id = given if given and given.lower() != "null" else str(uuid.uuid4())
    # The conversation is stored under the user's id as well, so one user
    # can never continue, or read the history of, another user's conversation.
    # Run the agent. Its final state holds everything below.
    state = ask(request.question, f"{user['id']}:{conversation_id}")
    # The query result ({} if no SQL was run).
    result = state.get("result") or {}
    # What step 2 worked out ({} if the question was blocked first).
    understanding = state.get("understanding") or {}
    # Show the tables the SQL really used, and only the joins between them.
    # If no SQL was checked, fall back to the tables the agent considered.
    # The join path ({} if none was found).
    path = state.get("join_path") or {}
    # The tables to show: those in the SQL, else those on the path, else those selected.
    used = state.get("sql_tables") or path.get("tables", state.get("tables", []))
    # Only joins whose two tables are both used.
    joins = [j for j in path.get("joins", []) if j["from_table"] in used and j["to_table"] in used]
    # The JSON response the demo page reads.
    return {
        "status": state.get("status"),
        "conversation_id": conversation_id,
        # Why it was blocked/failed, or the clarifying question, or None.
        "message": state.get("block_reason") or (understanding.get("clarification_question")
                                                 if state.get("status") == "needs_clarification" else None),
        # The result table (at most ROWS_SHOWN rows), or an empty list.
        "tables": [{"columns": result.get("columns", []), "rows": result.get("rows", [])[:ROWS_SHOWN]}] if result else [],
        # The plain-English answer.
        "commentary": state.get("commentary"),
        # Everything the agent did, for the "How this was answered" panel.
        "details": {
            "standalone_question": understanding.get("standalone_question"),
            "intent": understanding.get("intent"),
            "time_range": understanding.get("time_range"),
            "metrics_used": understanding.get("metrics", []),
            "entities": state.get("entities", []),
            "unmatched": state.get("unmatched", []),
            "tables_considered": state.get("tables", []),
            "table_picks": state.get("table_picks", []),
            "tables_used": used,
            "join_path": joins,
            "join_note": path.get("note"),
            "sql": state.get("sql"),
            "sql_explanation": state.get("sql_explanation"),
            "checks": state.get("checks", []),
            "attempts": state.get("attempts", 0),
            "failed_sql": state.get("failed_sql", []),
            "row_count": len(result.get("rows", [])),
            "row_cap": context.agent_config()["run"]["row_cap"],
            "prompt_shield": state.get("prompt_shield"),
            "steps": state.get("steps", []),
        },
    }


# GET /schema-graph: data for drawing the table graph.
@app.get("/schema-graph")
def schema_graph() -> dict:
    """
    Nodes and edges for drawing the graph, read from agent_meta.
      nodes  -> table names (used by the demo page)
      tables -> per table: description, grain and estimated rows (used by /graph)
      edges  -> approved links, with their join columns and how each was found
    """
    # Open a connection to agent_meta.
    with context.agent_conn() as conn:
        # Every approved link between tables.
        edges = conn.execute(
            """SELECT from_table, from_column, to_table, to_column, source FROM meta_edges
               WHERE status = 'approved' ORDER BY edge_id""").fetchall()
        # Every approved table with its description, grain and estimated row count.
        tables = conn.execute(
            """SELECT table_name, description, grain, row_estimate FROM meta_tables
               WHERE status = 'approved' ORDER BY table_name""").fetchall()
    # zip pairs the column names with each row's values; dict(...) makes {name: value}.
    return {
        "nodes": [t[0] for t in tables],
        "tables": {t[0]: {"description": t[1], "grain": t[2], "rows": t[3]} for t in tables},
        "edges": [dict(zip(["from_table", "from_column", "to_table", "to_column", "source"], e)) for e in edges],
    }


# Count once, then reuse the result.
@lru_cache(maxsize=1)
def _table_counts() -> dict[str, int]:
    """
    count(*) for every approved table, in one read-only query. The data does
    not change while the app runs, so it is counted on the first call only
    (about a second) and reused after that. Restart the app after reloading data.
    """
    # Approved table names; isidentifier() keeps only safe names (letters, digits, _) for the SQL below.
    names = [n for n in sorted(context.table_notes()) if n.isidentifier()]
    # One query: SELECT 'schemes', count(*) FROM "schemes" UNION ALL SELECT ... for every table.
    sql = " UNION ALL ".join(f"SELECT '{n}', count(*) FROM \"{n}\"" for n in names)
    # Run it read-only, allowing up to 60 seconds.
    _columns, rows = context.adapter().run_readonly(sql, timeout_ms=60000, row_cap=len(names))
    # Rows are (table, count) pairs -> {table: count}.
    return dict(rows)


# GET /table-counts: row counts for the demo page.
@app.get("/table-counts")
def table_counts() -> dict:
    """Rows per table in the fund database (mf_data), largest first."""
    # The cached counts.
    counts = _table_counts()
    # key=lambda x: -x[1] sorts by count, largest first.
    return {"database": "mf_data", "total": sum(counts.values()),
            "tables": [{"table": t, "rows": n} for t, n in sorted(counts.items(), key=lambda x: -x[1])]}


# GET /graph: the full-screen graph page.
@app.get("/graph")
def graph_page():
    """Full-screen, clickable view of the schema graph."""
    # Send ui/graph.html.
    return FileResponse(STATIC / "graph.html")


# GET /health: App Service pings this to check the app is alive.
@app.get("/health")
def health() -> dict:
    # Always answers "ok" if the server is running.
    return {"status": "ok"}


# GET /: the demo page.
@app.get("/")
def page():
    # Send ui/index.html.
    return FileResponse(STATIC / "index.html")