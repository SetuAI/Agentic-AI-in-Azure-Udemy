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

import os

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def require_settings(conn) -> None:
    """Stop with clear instructions if the server parameters are not set yet."""
    try:
        allowed = conn.execute("SHOW azure.extensions").fetchone()[0].lower()
    except psycopg.Error:
        allowed = None   # not an Azure server; nothing to allow-list
    preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0].lower()

    problems = []
    if allowed is not None:
        problems += [f"azure.extensions must include {name.upper()}"
                     for name in ("vector", "age") if name not in allowed.split(",")]
    if "age" not in preload:
        problems.append("shared_preload_libraries must include AGE (then restart the server)")
    if problems:
        raise SystemExit("Server parameters not ready:\n  - " + "\n  - ".join(problems))


def main() -> None:
    load_dotenv()
    data_url, agent_url = os.environ["DATA_DB_URL"], os.environ["AGENT_DB_URL"]
    data_db = conninfo_to_dict(data_url)["dbname"]
    agent_db = conninfo_to_dict(agent_url)["dbname"]

    # ---- 1 and 2: check parameters, create databases ---------------------
    # CREATE DATABASE cannot run inside a transaction, hence autocommit, and
    # it must run from another database: "postgres" always exists.
    with psycopg.connect(make_conninfo(data_url, dbname="postgres"), autocommit=True) as conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        print(f"Connected: PostgreSQL {version}")
        require_settings(conn)
        existing = {r[0] for r in conn.execute("SELECT datname FROM pg_database")}
        for name in (data_db, agent_db):
            if name in existing:
                print(f"  database {name}: already there")
            else:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                print(f"  database {name}: created")

    # ---- 3: extensions in agent_meta -------------------------------------
    with psycopg.connect(agent_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        # Prove AGE really works (it fails here if the server was not restarted).
        conn.execute("SET search_path = public, ag_catalog")
        conn.execute("SELECT count(*) FROM ag_catalog.ag_graph").fetchone()
        print(f"  {agent_db}: pgvector and AGE ready")

    print("Done. Next: uv run python dataplane/schema/apply_schema.py")


if __name__ == "__main__":
    main()
