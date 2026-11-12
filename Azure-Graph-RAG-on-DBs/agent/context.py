##############################################################################
# agent/context.py
#
# PURPOSE:
#   Loads the things that EVERY step of the agent needs, and shares them.
#   There are five of them, one function each:
#
#     agent_config()   the agent's settings, from config/agent.yaml
#     metrics()        the approved business definitions, from config/metrics.yaml
#     adapter()        the connection to the fund database, mf_data (read only)
#     agent_conn()     a new connection to agent_meta (the notes, the
#                      searchable data, and the graph)
#     table_notes()    the APPROVED notes for every table and column
#
#   This file is not a step. It does not do anything to the question. It
#   only hands the steps what they need.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Without this file, every step would open its own copy of the settings
#   files and its own database connections, for every question. That is
#   slow, and it is easy for two steps to end up reading different settings.
#
#   With this file, each thing is loaded ONCE, the first time a step asks
#   for it, and every later request gets the same copy back.
#
# HOW "LOADED ONCE" WORKS — @lru_cache:
#   @lru_cache is written on the line above a function. It means:
#     the first time the function is called  -> run it, and keep the result
#     every time after that                  -> skip running it, hand back
#                                               the kept result
#
#   So config/agent.yaml is read from disk only once, however many
#   questions the agent answers.
#
#   ONE CONSEQUENCE TO REMEMBER: if you re-run metadata.publish, or change
#   a config file, the running server will not see the change, because it
#   is still holding the old copy. Restart the server to pick it up.
#
# HOW TO USE:
#   Never run directly. Every step imports it:
#
#       from agent import context
#       settings = context.agent_config()
#
# TRY IT YOURSELF (from the project folder):
#       uv run python
#       >>> from agent import context
#       >>> context.agent_config()["entity_min_score"]
#       0.5
#       >>> list(context.metrics())
#       ['net_flows', 'redemptions', 'aum', 'rejection_rate', ...]
#       >>> len(context.table_notes())
#       20
##############################################################################


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
# The module's own description. Python keeps this text and shows it when
# someone types help(context). The banner above says the same thing at
# more length.


# ── IMPORTS ──────────────────────────────────────────────────────────────────

import os
# Python's built-in module for talking to the operating system.
#
# We use one thing from it: os.environ, which holds the environment
# variables. That is where the two database addresses come from:
#     DATA_DB_URL   -> the address of mf_data
#     AGENT_DB_URL  -> the address of agent_meta

from functools import lru_cache
# The "run once, keep the result" tool explained in the banner.
# It comes with Python; nothing to install.

from pathlib import Path
# Python's built-in way of working with file and folder paths.
# Used once, to find the config/ folder.

import yaml
# Reads YAML files (the .yaml settings files) and turns them into Python
# dictionaries. Installed as the package "pyyaml".

from dotenv import load_dotenv
# Reads the .env file and copies every KEY=value line in it into
# os.environ. That is how the database addresses and the Azure keys
# reach the code without being written in the code.

from adapters.postgres import PostgresAdapter
# The class that talks to the fund database, mf_data.
# It is the same class the metadata pipeline used in Part 4. Every query it
# runs is read only: the database itself refuses any write.

from metadata import store
# The Part 4 file that knows how to connect to agent_meta.
# We use its connect() function, which also sets things up so the graph
# functions can be called.


# ── WHERE THE SETTINGS FILES ARE ─────────────────────────────────────────────

CONFIG = Path(__file__).resolve().parents[1] / "config"
# The full path to the config/ folder. Read it left to right:
#
#   __file__      this file's path:   .../agent/context.py
#   .resolve()    turned into the full path from the top of the disk
#   .parents[1]   two folders up:
#                   parents[0] -> .../agent
#                   parents[1] -> .../Azure-Graph-RAG-on-DBs  (the project folder)
#   / "config"    then into the config folder
#
# WHY NOT JUST WRITE "config": a plain "config" is looked for in whichever
# folder the terminal is in. Building the path from this file's own
# location means it works from any folder.

load_dotenv()
# Read .env now, as soon as this file is imported.
#
# It has to happen here, before any function below runs, because adapter()
# and agent_conn() read the database addresses from os.environ. If .env had
# not been read yet, they would fail with KeyError: 'DATA_DB_URL'.


# ── 1. THE AGENT'S SETTINGS ──────────────────────────────────────────────────

@lru_cache(maxsize=1)
# Run once, keep the result. maxsize=1 means keep exactly one result —
# this function takes no inputs, so there is only ever one result to keep.
def agent_config() -> dict:
    # Returns config/agent.yaml as a Python dictionary.
    #
    # Steps in 6A read four things from it:
    #   ["prompt_shields"]["fail_open"]   step 1: what to do if Content Safety cannot be reached
    #   ["entity_columns"]                step 3: which table and column each kind of name lives in
    #   ["entity_min_score"]              step 3: how close a match must be to be accepted
    #   and 6B reads the rest (retries, row cap, timeout, date filter rule).
    return yaml.safe_load((CONFIG / "agent.yaml").read_text())
    # Read it right to left:
    #   CONFIG / "agent.yaml"   the full path to the file
    #   .read_text()            the file's contents, as one long piece of text
    #   yaml.safe_load(...)     that text turned into a Python dictionary
    #
    # WHY safe_load AND NOT load: safe_load only builds plain values —
    # text, numbers, lists, dictionaries. yaml.load can be made to run code
    # hidden inside a YAML file. A settings file should never run code, so
    # safe_load is always the one to use.


# ── 2. THE APPROVED BUSINESS DEFINITIONS ─────────────────────────────────────

@lru_cache(maxsize=1)
# Run once, keep the result.
def metrics() -> dict:
    # Returns the approved business definitions from config/metrics.yaml,
    # as   name -> {description, definition, tables}
    #
    # For example, metrics()["net_flows"] is:
    #   description: "Money into a scheme minus money out, from completed transactions."
    #   definition:  the exact rule for calculating it in SQL
    #   tables:      [transactions, scheme_plans, schemes]
    #
    # WHERE 6A USES IT: step 2 sends the model the NAME and DESCRIPTION of
    # every metric, so the model can say "this question needs net_flows".
    # 6B then uses the DEFINITION, so net flows is calculated the same way
    # every time.
    return yaml.safe_load((CONFIG / "metrics.yaml").read_text())["metrics"]
    # Same as agent_config(), with one addition at the end: ["metrics"].
    # The file has one top-level key called "metrics", with all the
    # definitions under it. ["metrics"] takes just that part, so callers
    # can write metrics()["net_flows"] instead of metrics()["metrics"]["net_flows"].


# ── 3. THE CONNECTION TO THE FUND DATABASE ───────────────────────────────────

@lru_cache(maxsize=1)
# Create once, then reuse. One adapter is shared by every question.
def adapter() -> PostgresAdapter:
    # Returns the object that runs queries on mf_data.
    #
    # Nothing in 6A uses it. 6B does: step 7 asks it how expensive a query
    # would be, and step 8 uses it to run the SQL.
    return PostgresAdapter(os.environ["DATA_DB_URL"])
    # os.environ["DATA_DB_URL"] reads the mf_data address that load_dotenv()
    # copied in from .env.
    #
    # Square brackets, not .get(), on purpose: if DATA_DB_URL is missing,
    # this fails straight away with KeyError: 'DATA_DB_URL', which tells
    # you exactly what to fix.


# ── 4. A CONNECTION TO agent_meta ────────────────────────────────────────────

def agent_conn():
    # NOTE: there is no @lru_cache on this one. That is deliberate.
    """
    A new connection to agent_meta. Each step opens its own and closes it
    when done ("with agent_conn() as conn:"), so two questions arriving at
    the same time never share one connection.
    """
    # The docstring above says why it is not cached. In plain terms:
    #
    #   if it WERE cached  -> every question would use the same one connection.
    #                         When two users ask at the same moment, both
    #                         questions would send queries down that one
    #                         connection at once, and they would get in
    #                         each other's way.
    #   it is NOT cached   -> each step opens a new connection, uses it,
    #                         and closes it. Two questions never share one.
    #
    # HOW A STEP USES IT (you will see this in understand.py, step 3):
    #       with context.agent_conn() as conn:
    #           ... queries ...
    #   When the "with" block ends, the connection is closed automatically,
    #   even if something inside it failed.
    return store.connect(os.environ["AGENT_DB_URL"])
    # Opens the connection using the Part 4 function store.connect().
    # It also sets the search_path, so the graph functions (cypher and so
    # on) can be called by their short names in 6B.


# ── 5. THE APPROVED NOTES FOR EVERY TABLE ────────────────────────────────────

@lru_cache(maxsize=1)
# Load once, then reuse. Restart the server to pick up new notes.
def table_notes() -> dict[str, dict]:
    # Returns the approved description of every table and its columns,
    # read from agent_meta.
    """
    table name -> {"description", "grain", "columns": [(name, type, description, is_pii), ...]}
    Only approved notes: an unreviewed draft never reaches the SQL model.
    """
    # WHAT THE RESULT LOOKS LIKE, for one table:
    #
    #   notes["credit_rating_history"] = {
    #       "description": "Credit rating events for securities ...",
    #       "grain": "One row per credit rating event for a specific security ...",
    #       "columns": [
    #           ("agency", "text", "The rating agency that gave the rating", False),
    #           ("rtg", "text", "The rating given, e.g. AAA", False),
    #           ("rtg_dt", "date", "The date the agency gave the rating", False),
    #           ...
    #       ],
    #   }
    #
    # WHERE IT IS USED: in 6B, step 6. The model that writes the SQL is
    # shown the notes for the chosen tables only — this is how it knows
    # that rtg_dt means "the date the rating was given".
    #
    # "Only approved notes" is the Part 4 review step paying off: a draft
    # that nobody checked can never reach the SQL-writing model.

    notes = {}
    # An empty dictionary. Filled in one table at a time by the loop below.

    with agent_conn() as conn:
        # Open a connection to agent_meta. It closes by itself when this
        # block ends.

        for name, description, grain in conn.execute(
            "SELECT table_name, description, grain FROM meta_tables WHERE status = 'approved'"
        ).fetchall():
            # Ask agent_meta for every APPROVED table note, and go through
            # them one at a time.
            #
            #   conn.execute(...)    run the SQL
            #   .fetchall()          bring back all the rows, as a list
            #   for name, description, grain in ...
            #                        each row has three values; give them
            #                        those three names
            #
            # WHERE status = 'approved' is the important part of the SQL.
            # A table whose note is still 'draft' is skipped.

            columns = conn.execute(
                """SELECT column_name, data_type, description, is_pii FROM meta_columns
                   WHERE table_name = %s ORDER BY column_name""", (name,)).fetchall()
            # For this one table, fetch the note for each of its columns.
            #
            # %s is a placeholder. psycopg fills it with the value in the
            # tuple after the SQL — (name,) — so it becomes, for example,
            # WHERE table_name = 'credit_rating_history'.
            #
            # WHY A PLACEHOLDER AND NOT f"...{name}...": with a placeholder
            # the database treats the value as data, never as part of the
            # SQL. Pasting values straight into SQL text is how SQL
            # injection happens, so placeholders are the habit to keep.
            #
            # (name,) has a comma because psycopg needs a tuple, and a tuple
            # with one item needs a trailing comma. (name) without the comma
            # is just name in brackets.
            #
            # Each row that comes back is (column name, type, description, is_pii).

            notes[name] = {"description": description, "grain": grain, "columns": columns}
            # Store this table's notes under its name.

    return notes
    # All approved notes, as one dictionary. Because of @lru_cache, this
    # whole function — the connection and both queries — runs only on the
    # first call. Every later call returns this same dictionary straight away.