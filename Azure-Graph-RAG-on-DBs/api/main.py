##############################################################################
# api/main.py
#
# PURPOSE:
#   The web server in front of the agent. It does two jobs:
#     1. it gives the browser the demo page (ui/index.html and ui/graph.html)
#     2. it answers the page's requests — most importantly "here is a
#        question, please answer it" — by calling the agent's ask()
#
#   It's built with FastAPI, a Python library for building web APIs.
#
# THE ADDRESSES IT ANSWERS (each one is called an ENDPOINT):
#
#     GET  /               the demo page            (ui/index.html)
#     GET  /graph          the full-screen graph    (ui/graph.html)
#     POST /query          ask a question -> the answer, the table, and every detail
#     GET  /schema-graph   the 20 tables and their approved links, to draw the graph
#     GET  /table-counts   how many rows each table has
#     GET  /me             who is signed in ("local" on a laptop)
#     GET  /health         "ok" if the server is up
#     GET  /docs           made by FastAPI by itself: try every endpoint by hand
#
#   GET means "give me something". POST means "here is some data, work on it".
#
# HOW THE PAGE AND THE SERVER WORK TOGETHER:
#
#     browser opens http://localhost:8000
#        │
#        ▼  GET /                 -> this file sends back ui/index.html
#     the page loads, then asks:
#        ├── GET /schema-graph    -> draws the graph of 20 tables
#        ├── GET /table-counts    -> fills in "Records per table"
#        └── GET /me              -> shows who is signed in
#     the user types a question and clicks Ask
#        │
#        ▼  POST /query           -> this file calls ask() from agent/graph.py
#                                    -> all 11 steps run
#                                    -> the result goes back to the page as JSON
#     the page shows the answer, the table, the SQL, the trace and the graph
#
#   ONE SERVER DOES BOTH. The page and the API come from the same server, on
#   the same address, so there is only one command to start them.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   The agent so far could only be used from Python. This file lets anyone
#   use it from a browser — and lets other programs use it too, by sending
#   a request to /query.
#
# HOW TO RUN:
#       uv run uvicorn api.main:app --port 8000
#   Then open http://localhost:8000 in a browser.
#
#   uvicorn is the program that actually runs the server. "api.main:app"
#   means: in the file api/main.py, the object called app.
#
# EXPECTED OUTPUT IN THE TERMINAL:
#       INFO:     Started server process [12345]
#       INFO:     Waiting for application startup.
#       INFO:     Application startup complete.
#       INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
#
# SIGN-IN:
#   On a laptop there is no sign-in: everyone is "local".
#   On Azure App Service (Part 8), Azure itself makes users sign in with
#   their Microsoft account BEFORE a request reaches this code, and tells
#   this code who they are. That's covered in the deployment video.
##############################################################################


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
# The module's own description, kept for help(main).


# ── IMPORTS — PART 1: MONITORING, WHICH MUST COME FIRST ─────────────────────

import os
# Reads environment variables. Used for WEBSITE_SITE_NAME, which Azure sets
# only when the app runs on App Service.

import uuid
# Makes random unique IDs, used as conversation IDs for new conversations.

from functools import lru_cache
# "Run once, keep the result" — the same tool context.py uses.
# Used for the table counts, which are slow to work out and never change.

# Application Insights must be switched on BEFORE FastAPI is imported and
# the app is created, so it can hook into every request.

from api import telemetry
# Our Application Insights setup, in api/telemetry.py. Covered in Part 9.

telemetry.setup()
# Switch monitoring on, NOW, before anything else is imported.
#
# ON YOUR LAPTOP THIS DOES NOTHING: it only switches on if
# APPLICATIONINSIGHTS_CONNECTION_STRING is set in .env. Without it, setup()
# returns straight away, and the app runs exactly the same.
#
# WHY IT MUST BE HERE: monitoring works by hooking into FastAPI as FastAPI
# loads. If FastAPI were imported first, the hook would be too late, and no
# requests would be recorded.


# ── IMPORTS — PART 2: EVERYTHING ELSE ────────────────────────────────────────
# Each line below ends with "# noqa: E402". That tells the code checker
# (ruff) "yes, this import comes after other code, on purpose" — because of
# telemetry.setup() above. Without it, ruff would report every line.

from pathlib import Path  # noqa: E402
# Builds the path to the ui/ folder.

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
# Three things from FastAPI:
#   FastAPI         the web application itself
#   HTTPException   stop and send back an error, e.g. 401 "Sign-in required"
#   Request         the incoming request from the browser, with its headers

from fastapi.responses import FileResponse  # noqa: E402
# Sends a file back to the browser. Used to send the two HTML pages.

from pydantic import BaseModel  # noqa: E402
# Describes the shape of the data a request must send. FastAPI checks every
# request against it, and rejects a bad one before our code runs.

from agent import context  # noqa: E402
# The shared settings and connections (agent/context.py, from Part 6A).

from agent.graph import ask  # noqa: E402
# THE AGENT. ask(question, conversation_id) runs all 11 steps. This is the
# same function we called by hand in Part 6C.
#
# Importing it also builds the agent, once, when the server starts.


# ── THE APP ──────────────────────────────────────────────────────────────────

app = FastAPI(title="FundGraph: ask the mutual fund database")
# The web application. Every @app.get(...) and @app.post(...) below adds an
# endpoint to it. uvicorn runs this object — that's the "app" in "api.main:app".
#
# The title is shown at the top of the /docs page.

STATIC = Path(__file__).parent.parent / "ui"
# The full path to the ui/ folder, where the HTML pages are.
#   __file__       this file: .../api/main.py
#   .parent        .../api
#   .parent        .../  (the project folder)
#   / "ui"         .../ui


# ── WHAT A QUESTION REQUEST LOOKS LIKE ──────────────────────────────────────

class QueryRequest(BaseModel):
    # The data POST /query must receive, as JSON. For example:
    #   {"question": "Which distributor has the highest rejection rate?",
    #    "conversation_id": null}
    """What the caller sends. conversation_id ties follow-up questions together."""

    question: str
    # The question. REQUIRED: a request without it is rejected by FastAPI
    # with error 422, before our code even runs.

    conversation_id: str | None = None
    # OPTIONAL. Leave it out (or null) to start a NEW conversation.
    # Send back the conversation_id from an earlier answer to ask a FOLLOW-UP.


# ── SIGN-IN ──────────────────────────────────────────────────────────────────

ON_APP_SERVICE = bool(os.environ.get("WEBSITE_SITE_NAME"))
# True when running on Azure App Service, False on a laptop.
# Azure sets WEBSITE_SITE_NAME on every App Service app, and nowhere else.
# So this one line tells the code where it's running.


def signed_in_user(http: Request) -> dict:
    # Works out who is asking. Used by every endpoint that needs to know.
    """The user App Service signed in, or "local" when running on a laptop."""

    user_id = http.headers.get("x-ms-client-principal-id")
    # On Azure, App Service adds this header to every request, holding the
    # signed-in user's ID. On a laptop, it's never there, so this is None.
    #
    # WHY THE HEADER CAN BE TRUSTED ON AZURE: App Service removes this header
    # from any request a user sends, and adds its own. So nobody can pretend
    # to be someone else by sending it themselves.

    if not user_id:
        # No signed-in user ...

        if ON_APP_SERVICE:
            # ... and we're on Azure. That means sign-in is switched off or
            # broken. Refuse, rather than answer someone we can't identify.
            raise HTTPException(status_code=401, detail="Sign-in required")
            # 401 means "not allowed — sign in first".

        return {"id": "local", "name": "local"}
        # ... and we're on a laptop. No sign-in exists here, so everyone is "local".

    return {"id": user_id, "name": http.headers.get("x-ms-client-principal-name", user_id)}
    # Signed in: their ID, and their name (usually their email). If the name
    # header is missing, use the ID as the name.


@app.get("/me")
# @app.get("/me") means: when the browser asks for GET /me, run the function below.
def me(http: Request) -> dict:
    """Who is signed in, for the demo page's header."""
    return signed_in_user(http)
    # FastAPI turns the returned dictionary into JSON by itself:
    #   {"id": "local", "name": "local"}
    # The page shows the name in its top corner.


# ── POST /query — THE MAIN ENDPOINT ──────────────────────────────────────────

ROWS_SHOWN = 500
# The page receives at most 500 rows. The agent may have fetched up to 5,000,
# but 500 is plenty for a browser table. The full count is still sent, in
# details.row_count, so the page can say "showing 500 of 3,200".


@app.post("/query")
# When the page sends POST /query, run the function below.
def query(request: QueryRequest, http: Request) -> dict:
    # Two inputs, both filled in by FastAPI:
    #   request   the JSON body, already checked against QueryRequest
    #   http      the raw request, for the sign-in headers

    user = signed_in_user(http)
    # Who is asking. On Azure with no sign-in, this stops here with 401.

    # No ID, an empty one, or the text "null" (easy to type by mistake in
    # /docs) all mean: start a new conversation.

    given = (request.conversation_id or "").strip()
    # The conversation ID the page sent, with spaces removed. "" if none.

    conversation_id = given if given and given.lower() != "null" else str(uuid.uuid4())
    # Use the given ID to CONTINUE a conversation, or make a brand-new
    # random ID to START one.

    # The conversation is stored under the user's id as well, so one user
    # can never continue, or read the history of, another user's conversation.

    state = ask(request.question, f"{user['id']}:{conversation_id}")
    # RUN THE AGENT — all 11 steps. This is where the 10 to 20 seconds go.
    #
    # Note the conversation ID passed to the agent: "user-id:conversation-id".
    # Even if someone guessed another user's conversation ID, their own user
    # ID is in front of it, so they'd get a different, empty conversation.
    # One user can never see another user's history.

    # ── everything below only reshapes the agent's state for the page ──

    result = state.get("result") or {}
    # The rows from step 8. An empty dictionary if no SQL ran (a blocked question).

    understanding = state.get("understanding") or {}
    # What step 2 worked out. Empty if the question was blocked at step 1.

    # Show the tables the SQL really used, and only the joins between them.
    # If no SQL was checked, fall back to the tables the agent considered.

    path = state.get("join_path") or {}
    # The join path from step 5.

    used = state.get("sql_tables") or path.get("tables", state.get("tables", []))
    # The tables to light up on the graph. First choice: the tables the SQL
    # REALLY read (step 7). If there's no checked SQL, the tables on the join
    # path. If not even that, the tables step 4 picked.

    joins = [j for j in path.get("joins", []) if j["from_table"] in used and j["to_table"] in used]
    # Only the joins between tables that were used — those are the orange
    # lines on the page's graph.

    return {
        # The answer, as JSON. The page reads every key below.
        # This is the "contract" between the server and the page: change a
        # key name here, and the page must change too.

        "status": state.get("status"),
        # completed / blocked / needs_clarification / failed.

        "conversation_id": conversation_id,
        # The page keeps this, and sends it back with the next question —
        # that's how follow-ups work from the browser.

        "message": state.get("block_reason") or (understanding.get("clarification_question")
                                                 if state.get("status") == "needs_clarification" else None),
        # A message for the user, if there is one:
        #   blocked or failed          -> why
        #   needs_clarification        -> the question asked back
        #   completed                  -> None

        "tables": [{"columns": result.get("columns", []), "rows": result.get("rows", [])[:ROWS_SHOWN]}] if result else [],
        # The result table: column names and at most 500 rows.
        # It's a LIST of tables, so a future version could return more than one.

        "commentary": state.get("commentary"),
        # The plain-English answer from step 10.

        "details": {
            # EVERYTHING the agent did. The page uses this for the trace,
            # the graph and the SQL panel. Each key comes from one step.

            "standalone_question": understanding.get("standalone_question"),
            # Step 2: the complete question (matters for follow-ups).

            "intent": understanding.get("intent"),
            # Step 2: one sentence on what the user wants.

            "time_range": understanding.get("time_range"),
            # Step 2: the period.

            "metrics_used": understanding.get("metrics", []),
            # Step 2: the approved metrics applied.

            "entities": state.get("entities", []),
            # Step 3: names matched to exact values.

            "unmatched": state.get("unmatched", []),
            # Step 3: names that couldn't be matched.

            "tables_considered": state.get("tables", []),
            # Step 4: every table picked.

            "table_picks": state.get("table_picks", []),
            # Step 4: why each was picked, with search scores.

            "tables_used": used,
            # The tables the SQL really read.

            "join_path": joins,
            # Step 5: the joins used.

            "join_note": path.get("note"),
            # Step 5: a note if some table couldn't be joined.

            "sql": state.get("sql"),
            # Step 6: the final SQL.

            "sql_explanation": state.get("sql_explanation"),
            # Step 6: one sentence on what it calculates.

            "checks": state.get("checks", []),
            # Step 7: every safety check and its result.

            "attempts": state.get("attempts", 0),
            # How many times SQL was written.

            "failed_sql": state.get("failed_sql", []),
            # Every attempt that failed, and why.

            "row_count": len(result.get("rows", [])),
            # How many rows the query returned in total (before the 500 limit).

            "row_cap": context.agent_config()["run"]["row_cap"],
            # The 5,000-row limit, so the page can warn when it was reached.

            "prompt_shield": state.get("prompt_shield"),
            # Step 1: the Content Safety result.

            "steps": state.get("steps", []),
            # Every step, with its time — the "GraphRAG trace" panel.
        },
    }


# ── GET /schema-graph — THE DATA FOR THE GRAPH ──────────────────────────────

@app.get("/schema-graph")
def schema_graph() -> dict:
    """
    Nodes and edges for drawing the graph, read from agent_meta.
      nodes  -> table names (used by the demo page)
      tables -> per table: description, grain and estimated rows (used by /graph)
      edges  -> approved links, with their join columns and how each was found
    """
    # The page calls this once, when it opens, and draws the 20 tables and
    # their links. It reads straight from agent_meta — the Part 4 metadata.

    with context.agent_conn() as conn:
        # Open a connection to agent_meta.

        edges = conn.execute(
            """SELECT from_table, from_column, to_table, to_column, source FROM meta_edges
               WHERE status = 'approved' ORDER BY edge_id""").fetchall()
        # Every APPROVED link. "source" says how it was found:
        # declared, same_name or value_overlap. The page draws the last two
        # as DASHED lines — the links Part 4 discovered by itself.

        tables = conn.execute(
            """SELECT table_name, description, grain, row_estimate FROM meta_tables
               WHERE status = 'approved' ORDER BY table_name""").fetchall()
        # Every APPROVED table, with its description, grain and rough row count.
        # Shown when you click a table on the graph page.

    return {
        "nodes": [t[0] for t in tables],
        # Just the 20 table names.

        "tables": {t[0]: {"description": t[1], "grain": t[2], "rows": t[3]} for t in tables},
        # table name -> its description, grain and rows.

        "edges": [dict(zip(["from_table", "from_column", "to_table", "to_column", "source"], e)) for e in edges],
        # Each link as a dictionary with named keys.
        # zip pairs each name with the matching value in the row; dict() makes
        # them a dictionary, e.g.
        #   {"from_table": "transactions", "from_column": "arn_code",
        #    "to_table": "distributors", "to_column": "arn_no", "source": "value_overlap"}
    }


# ── GET /table-counts — ROWS PER TABLE ───────────────────────────────────────

@lru_cache(maxsize=1)
# Count once, then keep the result for as long as the server runs.
def _table_counts() -> dict[str, int]:
    """
    count(*) for every approved table, in one read-only query. The data does
    not change while the app runs, so it is counted on the first call only
    (about a second) and reused after that. Restart the app after reloading data.
    """

    names = [n for n in sorted(context.table_notes()) if n.isidentifier()]
    # The 20 approved table names, in alphabetical order.
    # isidentifier() keeps only names made of letters, digits and _. Every
    # real table name passes. It's a safety check, because the names go
    # straight into the SQL text below.

    sql = " UNION ALL ".join(f"SELECT '{n}', count(*) FROM \"{n}\"" for n in names)
    # ONE query that counts every table, built from 20 small ones:
    #   SELECT 'benchmarks', count(*) FROM "benchmarks"
    #   UNION ALL
    #   SELECT 'credit_rating_history', count(*) FROM "credit_rating_history"
    #   UNION ALL ...
    # UNION ALL stacks their results into one list of 20 rows.

    _columns, rows = context.adapter().run_readonly(sql, timeout_ms=60000, row_cap=len(names))
    # Run it the same safe way the agent runs SQL: read only.
    # 60 seconds allowed (counting 1.1 million rows takes about one), and at
    # most 20 rows back. The _ in _columns means "we don't need this".

    return dict(rows)
    # Each row is (table, count). dict() turns them into
    #   {"benchmarks": 15, "transactions": 683159, ...}


@app.get("/table-counts")
def table_counts() -> dict:
    """Rows per table in the fund database (mf_data), largest first."""
    # The page calls this once, to fill in the "Records per table" box.

    counts = _table_counts()
    # The counts — worked out on the first call, then reused.

    return {"database": "mf_data", "total": sum(counts.values()),
            "tables": [{"table": t, "rows": n} for t, n in sorted(counts.items(), key=lambda x: -x[1])]}
    # database   which database the counts are from
    # total      all rows added up — about 1.1 million
    # tables     one entry per table, BIGGEST FIRST.
    #            key=lambda x: -x[1] sorts by the count (x[1]); the minus
    #            sign makes it largest first.


# ── THE TWO PAGES ────────────────────────────────────────────────────────────

@app.get("/graph")
def graph_page():
    """Full-screen, clickable view of the schema graph."""
    return FileResponse(STATIC / "graph.html")
    # Send ui/graph.html to the browser: the 20 tables, full screen.


@app.get("/health")
def health() -> dict:
    # A tiny endpoint that only says "I'm running".
    return {"status": "ok"}
    # WHERE IT'S USED: on Azure (Part 8), App Service calls /health every
    # minute or so. If it stops answering, Azure restarts the app.
    # It does no work and touches no database, so it answers instantly.


@app.get("/")
def page():
    # The address you type in the browser: http://localhost:8000
    return FileResponse(STATIC / "index.html")
    # Send ui/index.html — the demo page. Everything the page does after
    # this, it does by calling the endpoints above.