"""
scripts/pg_setup.py
===================

WHAT THIS FILE IS FOR
---------------------
Prepares an Azure Database for PostgreSQL Flexible Server for the project.
Run it once, after creating the server, before apply_schema.py:

    uv run python scripts/pg_setup.py

It does three things, each safe to repeat:

    1. checks the server allows pgvector and AGE (server parameters)
    2. creates the two databases, mf_data and agent_meta
    3. switches on pgvector and AGE inside agent_meta

BEFORE RUNNING: SERVER PARAMETERS
---------------------------------
Azure only lets an extension be created once it is allow-listed. In the
portal, Server parameters:

    azure.extensions          add VECTOR and AGE
    shared_preload_libraries  add AGE   (then Save, and restart the server)

AGE must be preloaded because it hooks into the query planner as the server
starts. Without the restart, CREATE EXTENSION age succeeds but every cypher()
call fails.
"""

# os: reads DATA_DB_URL and AGENT_DB_URL.
import os

# psycopg: the PostgreSQL driver.
import psycopg

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# sql: builds SQL with safely quoted names (for CREATE DATABASE).
from psycopg import sql

# Read parts of a connection URL (e.g. its database name), or make a copy with a different database.
from psycopg.conninfo import conninfo_to_dict, make_conninfo


# Checks the two server parameters; stops with instructions if they are missing.
def require_settings(conn) -> None:
    """Stop with clear instructions if the server parameters are not set yet."""
    # SHOW azure.extensions only works on Azure; elsewhere it raises an error.
    try:
        # The allow-listed extensions, e.g. "vector,age" (lowercased).
        allowed = conn.execute("SHOW azure.extensions").fetchone()[0].lower()
    # Not an Azure server (e.g. a local Postgres):
    except psycopg.Error:
        allowed = None   # not an Azure server; nothing to allow-list
    # The libraries loaded at server start.
    preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0].lower()

    # Every missing setting, collected so they are all reported at once.
    problems = []
    # On Azure: VECTOR and AGE must both be allow-listed.
    if allowed is not None:
        # Add a problem for each of VECTOR and AGE that is missing from the list.
        problems += [f"azure.extensions must include {name.upper()}"
                     # (only for names missing from the comma-separated list)
                     for name in ("vector", "age") if name not in allowed.split(",")]
    # AGE must be preloaded.
    if "age" not in preload:
        # Add the problem.
        problems.append("shared_preload_libraries must include AGE (then restart the server)")
    # Stop and list everything that still needs fixing.
    if problems:
        # Stop with every problem listed.
        raise SystemExit("Server parameters not ready:\n  - " + "\n  - ".join(problems))


# The whole setup, start to finish.
def main() -> None:
    # Read .env.
    load_dotenv()
    # The two connection URLs.
    data_url, agent_url = os.environ["DATA_DB_URL"], os.environ["AGENT_DB_URL"]
    # The database names inside them (mf_data, agent_meta).
    data_db = conninfo_to_dict(data_url)["dbname"]
    # (and the agent database's name)
    agent_db = conninfo_to_dict(agent_url)["dbname"]

    # ---- 1 and 2: check parameters, create databases ---------------------
    # CREATE DATABASE cannot run inside a transaction, hence autocommit, and
    # it must run from another database: "postgres" always exists.
    with psycopg.connect(make_conninfo(data_url, dbname="postgres"), autocommit=True) as conn:
        # Which PostgreSQL version the server runs.
        version = conn.execute("SHOW server_version").fetchone()[0]
        # Show which server version we reached.
        print(f"Connected: PostgreSQL {version}")
        # Stop here if the server parameters are not ready.
        require_settings(conn)
        # Names of the databases that already exist.
        existing = {r[0] for r in conn.execute("SELECT datname FROM pg_database")}
        # Create each of the two databases if it is missing.
        for name in (data_db, agent_db):
            # Already created on an earlier run:
            if name in existing:
                # just say so.
                print(f"  database {name}: already there")
            # Missing:
            else:
                # sql.Identifier quotes the name safely.
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                # Confirm.
                print(f"  database {name}: created")

    # ---- 3: extensions in agent_meta -------------------------------------
    # Now connect to agent_meta itself.
    with psycopg.connect(agent_url, autocommit=True) as conn:
        # Switch on pgvector (vector storage and similarity search).
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # Switch on Apache AGE (graph queries with Cypher).
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        # Prove AGE really works (it fails here if the server was not restarted).
        conn.execute("SET search_path = public, ag_catalog")
        # A tiny AGE query: fails here if AGE is not preloaded.
        conn.execute("SELECT count(*) FROM ag_catalog.ag_graph").fetchone()
        # Confirm.
        print(f"  {agent_db}: pgvector and AGE ready")

    # Tell the user the next step.
    print("Done. Next: uv run python dataplane/schema/apply_schema.py")


# True only when run as: uv run python scripts/pg_setup.py
if __name__ == "__main__":
    # Start the setup.
    main()
