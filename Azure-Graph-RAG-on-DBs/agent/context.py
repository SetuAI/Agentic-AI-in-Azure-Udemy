"""
agent/context.py
================

WHAT THIS FILE IS FOR
---------------------
Things every agent step needs, loaded once and shared:

    agent_config()   -> config/agent.yaml (limits, retries, entity lookups)
    metrics()        -> config/metrics.yaml (approved business definitions)
    adapter()        -> the connection to the fund database (read only)
    agent_conn()     -> a fresh connection to agent_meta (pgvector + AGE)
    table_notes()    -> the APPROVED notes for every table and column

@lru_cache means "run this once, then hand back the same result". Config
and notes do not change while the server runs, so there is no need to read
them again for every question. (Restart the server after re-running
metadata.publish, so it picks up new notes.)
"""

# os: reads environment variables (DATA_DB_URL, AGENT_DB_URL).
import os

# lru_cache: remembers a function's result (see the explanation above).
from functools import lru_cache

# Path: builds the path to the config/ folder.
from pathlib import Path

# yaml: reads the YAML config files.
import yaml

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# The read-only gateway to the fund database.
from adapters.postgres import PostgresAdapter

# Opens connections to agent_meta.
from metadata import store

# This file is agent/context.py; parents[1] is the project folder; add "config".
CONFIG = Path(__file__).resolve().parents[1] / "config"
# Read .env as soon as this module is imported.
load_dotenv()


# maxsize=1: keep exactly one remembered result.
@lru_cache(maxsize=1)
def agent_config() -> dict:
    # Read config/agent.yaml and return it as a dict.
    return yaml.safe_load((CONFIG / "agent.yaml").read_text())


# Read once, then reuse.
@lru_cache(maxsize=1)
# The approved business metric definitions: name -> {description, definition, tables}.
def metrics() -> dict:
    # Read config/metrics.yaml and return just the "metrics" section.
    return yaml.safe_load((CONFIG / "metrics.yaml").read_text())["metrics"]


# Create once, then reuse.
@lru_cache(maxsize=1)
# The read-only gateway to the fund database (mf_data).
def adapter() -> PostgresAdapter:
    # One adapter for the fund database, shared by every question.
    return PostgresAdapter(os.environ["DATA_DB_URL"])


# Opens a NEW connection every time it is called (not cached on purpose).
def agent_conn():
    """
    A new connection to agent_meta. Each step opens its own and closes it
    when done ("with agent_conn() as conn:"), so two questions arriving at
    the same time never share one connection.
    """
    # Not cached: every call returns a brand-new connection.
    return store.connect(os.environ["AGENT_DB_URL"])


# Load once, then reuse (restart the server to pick up new notes).
@lru_cache(maxsize=1)
# The approved notes for every table, used to write SQL.
def table_notes() -> dict[str, dict]:
    """
    table name -> {"description", "grain", "columns": [(name, type, description, is_pii), ...]}
    Only approved notes: an unreviewed draft never reaches the SQL model.
    """
    # Filled in below: table name -> its notes.
    notes = {}
    # Open a connection to agent_meta (closed automatically afterwards).
    with agent_conn() as conn:
        # Loop over every APPROVED table note (grain = what one row represents).
        for name, description, grain in conn.execute(
            # SQL: name, description and grain of every APPROVED table.
            "SELECT table_name, description, grain FROM meta_tables WHERE status = 'approved'"
        ).fetchall():
            # That table's column notes, sorted by column name.
            columns = conn.execute(
                # SQL: that table's column notes; %s is filled with the table name.
                """SELECT column_name, data_type, description, is_pii FROM meta_columns
                   WHERE table_name = %s ORDER BY column_name""", (name,)).fetchall()
            # Store this table's notes.
            notes[name] = {"description": description, "grain": grain, "columns": columns}
    # Hand back all notes (cached after the first call).
    return notes