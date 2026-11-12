##############################################################################
# guardrails/sql_checks.py
#
# PURPOSE:
#   Checks the SQL the model wrote, BEFORE it runs.
#
#   The agent's step 7, check_sql (in agent/steps/build_query.py), calls this
#   file. If any check fails, the SQL never runs. The error goes back to
#   step 6, and the model writes the SQL again.
#
#   There are four checks:
#     1. exactly one statement     stops tricks like "SELECT ...; DROP ..."
#     2. it only reads             SELECT only; nothing that changes data
#     3. only known tables         only the 20 tables that have approved notes
#     4. date filter               a query on transactions must compare
#                                  txn_date with a date
#
#   A fifth check, the cost check (EXPLAIN), is not in this file. It needs a
#   database connection, so it lives in step 7 itself.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   A model writes the SQL, and a model can make mistakes. It might write a
#   query that changes data, reads a table it shouldn't, or reads all 60
#   months of transactions when it only needed one. This file catches those
#   before anything reaches the database.
#
# HOW IT CHECKS — STRUCTURE, NOT WORDS:
#   The easy way would be to search the SQL text for words like "DELETE".
#   That is easy to fool, and it also gets things wrong: a column called
#   deleted_flag contains the word "delete" but changes nothing.
#
#   Instead, this file uses a library called sqlglot. sqlglot reads the SQL
#   and breaks it into its parts: the SELECT, each table, each WHERE
#   condition, each comparison. This is called a TREE. The checks then look
#   at the parts, so they see exactly what the query does.
#
#   For example, "SELECT name FROM schemes WHERE scheme_id = 5" becomes:
#
#       Select
#        ├── Column: name
#        ├── From
#        │    └── Table: schemes
#        └── Where
#             └── EQ  (=)
#                  ├── Column: scheme_id
#                  └── Literal: 5
#
# THE LAST LINE OF DEFENCE IS NOT HERE:
#   Even if these checks ever missed something, step 8 runs every query in
#   a READ ONLY transaction (adapters/postgres.py). PostgreSQL itself would
#   refuse any change. These checks are the first line, not the only one.
#
# HOW TO TEST IT (no Azure, no database, under a second):
#       uv run pytest tests/test_stage6_agent.py -v -k checks
#   11 passed: 7 unsafe queries refused, 4 safe queries allowed.
#
# TRY IT YOURSELF (from the project folder):
#       uv run python
#       >>> from guardrails import sql_checks
#       >>> known = {"transactions", "schemes"}
#       >>> dates = {"transactions": "txn_date"}
#       >>> for c in sql_checks.check("DELETE FROM schemes", known, dates): print(c)
#       {'check': 'single_statement', 'passed': True, 'detail': '1 statement(s)'}
#       {'check': 'read_only', 'passed': False, 'detail': "Not allowed: ['Delete']"}
#       {'check': 'known_tables', 'passed': True, 'detail': 'Tables: schemes'}
##############################################################################


"""
guardrails/sql_checks.py
========================

WHAT THIS FILE IS FOR
---------------------
Checks SQL written by the model BEFORE it runs. Uses sqlglot, which reads
SQL text into a tree, so the checks look at the real structure of the query
rather than searching the text for words (which is easy to fool).

    1. exactly one statement           -> no "SELECT ...; DROP ..." tricks
    2. it only reads                   -> SELECT / WITH ... SELECT only, nothing that writes
    3. only known tables               -> every table must be one of the 20 in the catalog
    4. date filter where required      -> transactions queries must filter on txn_date

The cost check (EXPLAIN) runs separately, in the agent, because it needs
the database. The database also refuses writes on its own: queries run in a
READ ONLY transaction (adapters/postgres.py). That is the backstop if these
checks ever missed something.
"""
# The module's own description, kept for help(sql_checks).


# ── IMPORTS ──────────────────────────────────────────────────────────────────

import sqlglot
# The library that reads SQL text and turns it into a tree of parts.
# It understands many SQL dialects; we tell it to read PostgreSQL's.

from sqlglot import exp
# "exp" holds the names of every kind of part in the tree, for example:
#   exp.Select   a SELECT
#   exp.Table    a table name
#   exp.Column   a column name
#   exp.Where    a WHERE clause
#   exp.Delete   a DELETE
# The checks below ask "does the tree contain a part of this kind?"


# ── THE PARTS THAT CHANGE DATA ───────────────────────────────────────────────

WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
               exp.TruncateTable, exp.Merge, exp.Command, exp.Grant)
# Every kind of part that would change something. If ANY of these appears
# ANYWHERE in the query, the query is refused.
#
#   Insert, Update, Delete    add, change or remove rows
#   Drop, Create, Alter       remove, create or change tables
#   TruncateTable             empty a table
#   Merge                     insert-or-update in one statement
#   Command                   anything sqlglot reads as a raw command it
#                             doesn't fully understand — refused to be safe
#   Grant                     change who is allowed to do what
#
# WHY "ANYWHERE": a write can hide inside a query that starts with SELECT.
# For example, a WITH block can contain a DELETE in PostgreSQL. Checking only
# the first word would miss it. Checking every part catches it.


# ═════════════════════════════════════════════════════════════════════════════
# THE CHECKS
# ═════════════════════════════════════════════════════════════════════════════

def check(sql: str, known_tables: set[str], date_filter_required: dict[str, str]) -> list[dict]:
    # Takes:
    #   sql                    the query the model wrote
    #   known_tables           the table names allowed — step 7 passes the 20
    #                          tables that have approved notes
    #   date_filter_required   from agent.yaml: {"transactions": "txn_date"}
    # Returns: a list with one result per check.
    """
    Returns one entry per check: {"check": name, "passed": bool, "detail": text}.
    The query may run only if every entry passed.
    """
    # Each result looks like:
    #   {"check": "read_only", "passed": True, "detail": "SELECT only"}
    #
    #   check    the check's name
    #   passed   True or False
    #   detail   one readable sentence. If it failed, this sentence is sent
    #            back to the model on the retry, so it says exactly what to fix.

    results = []
    # The list of results. Each check adds one entry.

    def record(name, passed, detail):
        # A small helper, defined inside check(), used by every check below.
        # It adds one result to the list, and hands back whether it passed.
        results.append({"check": name, "passed": passed, "detail": detail})
        return passed
        # Returning "passed" lets a check be written as
        #     if not record(...):
        #         return results
        # which means: record the result, and if it failed, stop here.

    # ── CHECK 1a — can the SQL be read at all? ──────────────────────────────

    try:
        # If the SQL has a syntax error, sqlglot raises an error, and Python
        # jumps to "except" below.

        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
        # Read the SQL into trees — one tree per statement.
        #
        #   sqlglot.parse(sql, read="postgres")
        #       read it as PostgreSQL, so PostgreSQL-only syntax such as
        #       '2024-03-01'::date is understood
        #   if s is not None
        #       a stray ";" at the end can produce an empty statement; drop it
        #
        # "SELECT 1"              -> 1 tree
        # "SELECT 1; DELETE ..."  -> 2 trees

    except sqlglot.errors.ParseError as error:
        # The SQL could not be read — for example, a missing bracket.
        record("parses", False, f"Not valid SQL: {error}")
        # Record the failure, with sqlglot's own message about what's wrong.
        return results
        # Stop. If the SQL can't be read, nothing else can be checked.
        # Note: "parses" is only recorded when it FAILS. A query that reads
        # fine simply goes on to the next check.

    # ── CHECK 1b — exactly one statement ────────────────────────────────────

    if not record("single_statement", len(statements) == 1, f"{len(statements)} statement(s)"):
        # Passes only if there is exactly one statement.
        #
        # WHY: the classic trick is "SELECT * FROM schemes; DELETE FROM schemes".
        # The first part looks harmless. The second part does the damage.
        # Allowing only one statement makes this trick impossible.
        return results
        # Stop if it failed. The checks below look at ONE tree; with two
        # trees they wouldn't know which to look at.

    tree = statements[0]
    # The one statement's tree. Every check below looks at this.

    # ── CHECK 2 — it only reads ─────────────────────────────────────────────

    # 2. Read only: the top must be a SELECT (or UNION etc.), and nothing
    # inside it may write.

    is_query = isinstance(tree, exp.Query)
    # Is the top of the tree a query — something that returns rows?
    # SELECT, WITH ... SELECT and UNION all count as a query.
    # DELETE, UPDATE and DROP do not.

    writes = [type(n).__name__ for n in tree.walk() if isinstance(n, WRITE_NODES)]
    # Look at EVERY part of the tree, and collect the name of any part that
    # writes.
    #
    #   tree.walk()                    visits every part, one at a time
    #   isinstance(n, WRITE_NODES)     is this part one of the writing kinds?
    #   type(n).__name__               its name, e.g. "Delete"
    #
    # For a safe query, writes is an empty list.

    record("read_only", is_query and not writes,
           "SELECT only" if is_query and not writes else f"Not allowed: {writes or type(tree).__name__}")
    # Passes only if BOTH are true: the top is a query, AND nothing inside writes.
    #
    # The detail:
    #   passed   "SELECT only"
    #   failed   "Not allowed: ['Delete']"  — the writing parts found, or,
    #            if none were found, the kind of statement it is instead.
    #
    # Note: no "return" here. Even if this fails, the other checks still
    # run, so the model is told about EVERY problem at once.

    # ── CHECK 3 — only known tables ─────────────────────────────────────────

    # 3. Known tables only. Names defined in WITH (CTEs) are the query's own
    # temporary results, not real tables, so they are left out.

    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    # The names the query makes up for itself with WITH.
    #
    # In
    #     WITH march AS (SELECT ...) SELECT * FROM march
    # "march" looks like a table to sqlglot, but it isn't one. It's a
    # temporary result the query creates. These are called CTEs. We collect
    # their names so we don't wrongly call them unknown tables.
    #
    #   tree.find_all(exp.CTE)   every WITH block in the query
    #   cte.alias_or_name        its name
    #   { ... }                  a set, so each name appears once

    used = {t.name for t in tree.find_all(exp.Table)} - cte_names
    # Every real table the query reads:
    #   every table name in the tree  MINUS  the WITH names.

    unknown = sorted(used - known_tables)
    # The tables the query reads that are NOT in our list of 20.
    # sorted() so the message is always in the same order.

    record("known_tables", not unknown, f"Tables: {', '.join(sorted(used))}" if not unknown
           else f"Unknown tables: {', '.join(unknown)}")
    # Passes only if there are no unknown tables.
    #
    # WHY THIS MATTERS: PostgreSQL has its own system tables. One of them,
    # pg_shadow, stores password information. A query reading it would pass
    # check 2 — it's only a SELECT. This check stops it, because pg_shadow is
    # not one of our 20 tables.
    #
    # It also catches a model that invents a table name, such as
    # "fund_transactions". The message names the unknown table, so on the
    # retry the model knows exactly what to fix.

    # ── CHECK 4 — the date filter ───────────────────────────────────────────

    # 4. Date filter on big partitioned tables. Some WHERE clause must COMPARE
    # the date column with a value (>=, <, BETWEEN, =, ...). Merely
    # mentioning it is not enough: "txn_date IS NOT NULL" is true for every
    # row, so it filters nothing and Postgres would still read all 60 months.

    comparisons = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Between, exp.In)
    # The kinds of part that count as a real comparison:
    #   GT  >      GTE  >=      LT  <      LTE  <=
    #   EQ  =      Between  BETWEEN ... AND ...      In  IN (...)
    #
    # "IS NOT NULL" is not in this list on purpose — see the comment above.

    for table, date_column in date_filter_required.items():
        # For each rule in agent.yaml. Today there is one:
        #   table = "transactions", date_column = "txn_date"

        if table in used:
            # Only checked if the query actually reads that table.
            # A query on schemes alone needs no date filter.

            filtered = any(
                any(col.name == date_column for col in cmp.find_all(exp.Column))
                for where in tree.find_all(exp.Where) for cmp in where.find_all(*comparisons)
            )
            # True if ANY comparison, inside ANY WHERE, mentions txn_date.
            #
            # Read the bottom line first, then the top:
            #   for where in tree.find_all(exp.Where)
            #       every WHERE clause in the query — including ones inside
            #       a WITH block or a subquery
            #   for cmp in where.find_all(*comparisons)
            #       every comparison inside that WHERE
            #   any(col.name == date_column for col in cmp.find_all(exp.Column))
            #       does that comparison mention the txn_date column?
            #
            # So "WHERE t.txn_date >= '2024-03-01'" passes,
            # and "WHERE txn_date IS NOT NULL" fails.

            record("date_filter", filtered, f"{table} filtered by a range on {date_column}" if filtered
                   else f"{table} must be filtered by a date range on {date_column} (e.g. >= and <)")
            # The failure message gives an example of the fix — ">= and <" —
            # because this exact sentence is what the model reads on the retry.

    return results
    # All the results. Step 7 lets the query run only if every one passed.


# ═════════════════════════════════════════════════════════════════════════════
# HELPER — which tables does a query really read?
# ═════════════════════════════════════════════════════════════════════════════

def tables_in(sql: str) -> list[str]:
    # Takes a query, returns the real tables it reads, sorted.
    """The real tables a query reads (not its WITH names). Empty if it does not parse."""
    # WHERE IT IS USED: step 7 stores this as "sql_tables". The demo page
    # uses it to tick the tables the final SQL really used, and the Stage 6
    # tests check it — for example, that the net flows question really read
    # "transactions".
    #
    # Step 4 picked tables, but the model might not use all of them. This
    # shows the ones it actually used.

    try:
        # A query that cannot be read reads no known tables.
        tree = sqlglot.parse_one(sql, read="postgres")
        # parse_one: read exactly one statement into a tree.
    except sqlglot.errors.ParseError:
        # Not valid SQL.
        return []
        # An empty list, rather than an error. This is only used for display.

    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    # The WITH names, left out — same as check 3 above.

    return sorted({t.name for t in tree.find_all(exp.Table)} - cte_names)
    # Every table name, minus the WITH names, sorted.