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

import os
import uuid
from functools import lru_cache

# Application Insights must be switched on BEFORE FastAPI is imported and
# the app is created, so it can hook into every request.
from api import telemetry

telemetry.setup()

from pathlib import Path  # noqa: E402

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from agent import context  # noqa: E402
from agent.graph import ask  # noqa: E402

app = FastAPI(title="FundGraph: ask the mutual fund database")
STATIC = Path(__file__).parent.parent / "ui"


class QueryRequest(BaseModel):
    """What the caller sends. conversation_id ties follow-up questions together."""
    question: str
    conversation_id: str | None = None


ON_APP_SERVICE = bool(os.environ.get("WEBSITE_SITE_NAME"))


def signed_in_user(http: Request) -> dict:
    """The user App Service signed in, or "local" when running on a laptop."""
    user_id = http.headers.get("x-ms-client-principal-id")
    if not user_id:
        if ON_APP_SERVICE:
            raise HTTPException(status_code=401, detail="Sign-in required")
        return {"id": "local", "name": "local"}
    return {"id": user_id, "name": http.headers.get("x-ms-client-principal-name", user_id)}


@app.get("/me")
def me(http: Request) -> dict:
    """Who is signed in, for the demo page's header."""
    return signed_in_user(http)


# The page receives at most this many rows; the full count is in details.row_count.
ROWS_SHOWN = 500


@app.post("/query")
def query(request: QueryRequest, http: Request) -> dict:
    user = signed_in_user(http)
    # No ID, an empty one, or the text "null" (easy to type by mistake in
    # /docs) all mean: start a new conversation.
    given = (request.conversation_id or "").strip()
    conversation_id = given if given and given.lower() != "null" else str(uuid.uuid4())
    # The conversation is stored under the user's id as well, so one user
    # can never continue, or read the history of, another user's conversation.
    state = ask(request.question, f"{user['id']}:{conversation_id}")
    result = state.get("result") or {}
    understanding = state.get("understanding") or {}
    # Show the tables the SQL really used, and only the joins between them.
    # If no SQL was checked, fall back to the tables the agent considered.
    path = state.get("join_path") or {}
    used = state.get("sql_tables") or path.get("tables", state.get("tables", []))
    joins = [j for j in path.get("joins", []) if j["from_table"] in used and j["to_table"] in used]
    return {
        "status": state.get("status"),
        "conversation_id": conversation_id,
        "message": state.get("block_reason") or (understanding.get("clarification_question")
                                                 if state.get("status") == "needs_clarification" else None),
        "tables": [{"columns": result.get("columns", []), "rows": result.get("rows", [])[:ROWS_SHOWN]}] if result else [],
        "commentary": state.get("commentary"),
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


@app.get("/schema-graph")
def schema_graph() -> dict:
    """
    Nodes and edges for drawing the graph, read from agent_meta.
      nodes  -> table names (used by the demo page)
      tables -> per table: description, grain and estimated rows (used by /graph)
      edges  -> approved links, with their join columns and how each was found
    """
    with context.agent_conn() as conn:
        edges = conn.execute(
            """SELECT from_table, from_column, to_table, to_column, source FROM meta_edges
               WHERE status = 'approved' ORDER BY edge_id""").fetchall()
        tables = conn.execute(
            """SELECT table_name, description, grain, row_estimate FROM meta_tables
               WHERE status = 'approved' ORDER BY table_name""").fetchall()
    return {
        "nodes": [t[0] for t in tables],
        "tables": {t[0]: {"description": t[1], "grain": t[2], "rows": t[3]} for t in tables},
        "edges": [dict(zip(["from_table", "from_column", "to_table", "to_column", "source"], e)) for e in edges],
    }


@lru_cache(maxsize=1)
def _table_counts() -> dict[str, int]:
    """
    count(*) for every approved table, in one read-only query. The data does
    not change while the app runs, so it is counted on the first call only
    (about a second) and reused after that. Restart the app after reloading data.
    """
    names = [n for n in sorted(context.table_notes()) if n.isidentifier()]
    sql = " UNION ALL ".join(f"SELECT '{n}', count(*) FROM \"{n}\"" for n in names)
    _columns, rows = context.adapter().run_readonly(sql, timeout_ms=60000, row_cap=len(names))
    return dict(rows)


@app.get("/table-counts")
def table_counts() -> dict:
    """Rows per table in the fund database (mf_data), largest first."""
    counts = _table_counts()
    return {"database": "mf_data", "total": sum(counts.values()),
            "tables": [{"table": t, "rows": n} for t, n in sorted(counts.items(), key=lambda x: -x[1])]}


@app.get("/graph")
def graph_page():
    """Full-screen, clickable view of the schema graph."""
    return FileResponse(STATIC / "graph.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def page():
    return FileResponse(STATIC / "index.html")