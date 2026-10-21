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

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

from adapters.postgres import PostgresAdapter
from metadata import store

CONFIG = Path(__file__).resolve().parents[1] / "config"
load_dotenv()


@lru_cache(maxsize=1)
def agent_config() -> dict:
    return yaml.safe_load((CONFIG / "agent.yaml").read_text())


@lru_cache(maxsize=1)
def metrics() -> dict:
    return yaml.safe_load((CONFIG / "metrics.yaml").read_text())["metrics"]


@lru_cache(maxsize=1)
def adapter() -> PostgresAdapter:
    return PostgresAdapter(os.environ["DATA_DB_URL"])


def agent_conn():
    """
    A new connection to agent_meta. Each step opens its own and closes it
    when done ("with agent_conn() as conn:"), so two questions arriving at
    the same time never share one connection.
    """
    return store.connect(os.environ["AGENT_DB_URL"])


@lru_cache(maxsize=1)
def table_notes() -> dict[str, dict]:
    """
    table name -> {"description", "grain", "columns": [(name, type, description, is_pii), ...]}
    Only approved notes: an unreviewed draft never reaches the SQL model.
    """
    notes = {}
    with agent_conn() as conn:
        for name, description, grain in conn.execute(
            "SELECT table_name, description, grain FROM meta_tables WHERE status = 'approved'"
        ).fetchall():
            columns = conn.execute(
                """SELECT column_name, data_type, description, is_pii FROM meta_columns
                   WHERE table_name = %s ORDER BY column_name""", (name,)).fetchall()
            notes[name] = {"description": description, "grain": grain, "columns": columns}
    return notes