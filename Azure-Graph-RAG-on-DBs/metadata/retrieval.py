##############################################################################
# metadata/retrieval.py
#
# PURPOSE:
#   The three searches the agent makes before it writes any SQL. They read
#   what Part 4 built in agent_meta.
#
#     search_tables(question)   which tables is this question about?
#                               searches the table notes in meta_embeddings
#     match_values(text)        what is the exact database value for a name
#                               the user typed loosely?
#                               searches the stored values in meta_embeddings
#     find_join_path(tables)    how do these tables join together?
#                               asks the graph, schema_graph
#
# WHICH VIDEO USES WHICH:
#     6A  match_values      used by step 3, match_entities
#     6B  search_tables     used by step 4, select_tables
#     6B  find_join_path    used by step 5, find_join_path
#   The Stage 5 tests in Part 4 also call all three, to prove the metadata works.
#
# HOW THE TWO SEARCHES WORK (search_tables and match_values):
#   In Part 4, publish.py turned every approved table note, and every real
#   name, into a list of 1,536 numbers, and stored them in meta_embeddings.
#   Text with a similar meaning gets similar numbers.
#
#   To search:
#     1. turn the user's text into 1,536 numbers the same way
#     2. ask PostgreSQL for the stored rows whose numbers are closest
#     3. return them, closest first, each with a score from 0 to 1
#        (1 = the same meaning, higher = better)
#
#   The pgvector operator <=> does step 2. It gives the DISTANCE between two
#   lists of numbers: 0 means the same meaning. So the code uses
#   1 - distance as the score, so that higher means better.
#
# HOW find_join_path WORKS:
#   The graph has one point per table and one line per approved link. Each
#   line carries the two column names needed to join.
#
#   For two tables:
#     ask the graph for every route between them, up to 6 joins long,
#     and pick the shortest. If two routes are equally short, pick the one
#     whose links we are more confident in.
#
#   For more than two tables:
#     start with the first table. Then, again and again, add the missing
#     table that is closest to the tables joined so far, with the route to
#     it. Stop when every table is connected.
#
#   The graph can list routes, but it cannot pick the shortest one by
#   itself, so the choosing is done in Python, in _best().
#
# HOW TO USE:
#   Never run directly. The agent's steps import it:
#       from metadata import retrieval
#
# TRY IT YOURSELF (from the project folder):
#       uv run python
#       >>> from agent import context
#       >>> from metadata import retrieval
#       >>> conn = context.agent_conn()
#       >>> retrieval.match_values(conn, "sahyadri credit fund", k=1, table="schemes", column="scheme_name")
#       [('schemes', 'scheme_name', 'Sahyadri Credit Risk Fund', 0.8...)]
#       >>> retrieval.search_tables(conn, "Which bonds were downgraded?", k=3)
#       [('credit_rating_history', 0.5...), ...]
#       >>> retrieval.find_join_path(conn, ["transactions", "distributors"])["joins"]
#       [{'from_table': 'transactions', 'from_column': 'arn_code', 'to_table': 'distributors', 'to_column': 'arn_no', ...}]
##############################################################################


"""
metadata/retrieval.py
=====================

WHAT THIS FILE IS FOR
---------------------
The three lookups the agent makes before writing SQL. Stage 6 calls these;
the Stage 5 tests call them to prove the metadata works.

    search_tables(question)       -> which tables the question is about      (pgvector)
    match_values(text)            -> exact database values for loose names   (pgvector)
    find_join_path(tables)        -> how to join those tables together       (AGE graph)

HOW find_join_path WORKS
------------------------
The graph has one node per table and one JOINS edge per approved link.

    For two tables: ask AGE for every route between them up to max_hops
    joins long, and keep the shortest. If two routes are equally short,
    prefer the one whose links we are most confident in.

    For more than two tables: start with the first table, then repeatedly
    add the remaining table that is closest to the tables already joined,
    together with the route to it. This gives a small set of joins that
    connects every table the question needs.

AGE can list routes of varying length, but has no built-in "shortest
route" function, so choosing among them happens here, in Python.
"""
# The module's own description, kept for help(retrieval).


# ── IMPORTS ──────────────────────────────────────────────────────────────────

import json
# Reads the graph's replies. Once a few tags are removed (see
# _parse_edges below), the graph's reply is ordinary JSON.

import re
# Regular expressions: patterns for matching text. Used for two things:
#   removing the tags from the graph's reply
#   checking a table name contains only safe characters

from metadata import llm
# The Part 4 file that talks to Azure OpenAI. Used here only for
# llm.embed(), which turns text into 1,536 numbers.


# ═════════════════════════════════════════════════════════════════════════════
# HELPER — text to numbers
# ═════════════════════════════════════════════════════════════════════════════

def _vector(question: str) -> str:
    # The _ at the start of the name means "only used inside this file".
    """Embed one piece of text, written the way pgvector reads a vector."""
    # Turns one piece of text into its 1,536 numbers, written as text that
    # PostgreSQL can read.
    return str(llm.embed([question])[0])
    # Read it from the inside out:
    #   [question]            a list with one item, because llm.embed()
    #                         takes a list (Part 4 sends many at once)
    #   llm.embed(...)        one call to the embedding model; returns a list
    #                         of results, one per text
    #   [0]                   our one result: a list of 1,536 numbers
    #   str(...)              written as text: "[0.012, -0.034, ...]"
    #
    # WHY AS TEXT: pgvector accepts a vector written exactly like that, in
    # square brackets. The SQL below then adds ::vector to say "read this
    # text as a vector".
    #
    # Each call is one request to Azure OpenAI, so each search costs a
    # tiny amount and takes a fraction of a second.


# ═════════════════════════════════════════════════════════════════════════════
# SEARCH 1 — which tables is the question about?       (6B, step 4)
# ═════════════════════════════════════════════════════════════════════════════

def search_tables(conn, question: str, k: int = 5) -> list[tuple[str, float]]:
    # Takes:
    #   conn       an open connection to agent_meta
    #   question   the text to search with
    #   k          how many tables to return (5 if not given)
    # Returns: a list of (table name, score), closest first.
    """
    The k tables whose notes are closest in meaning to the question.
    <=> is pgvector's cosine distance: 0 = same meaning, 2 = opposite.
    We return 1 - distance as a similarity score, where higher is better.
    """
    # In plain words: the <=> operator gives a distance. 0 means the same
    # meaning. Subtracting it from 1 turns it into a score where higher is
    # better, which is easier to read.

    v = _vector(question)
    # The question as 1,536 numbers.

    return conn.execute(
        """SELECT table_name, 1 - (embedding <=> %s::vector) AS score FROM meta_embeddings
           WHERE kind = 'table' ORDER BY embedding <=> %s::vector LIMIT %s""",
        # The SQL, line by line:
        #
        #   SELECT table_name,
        #          1 - (embedding <=> %s::vector) AS score
        #       the table name, and how close its note is to the question
        #   FROM meta_embeddings
        #   WHERE kind = 'table'
        #       only table notes — not the stored names, which are kind 'value'
        #   ORDER BY embedding <=> %s::vector
        #       closest first (smallest distance first)
        #   LIMIT %s
        #       only the first k
        #
        # The HNSW index built in Part 4 is what makes ORDER BY ... <=>
        # fast: PostgreSQL does not have to compare against every row.

        (v, v, k),
        # The values for the three %s placeholders, in order:
        # the vector for the score, the vector for the ordering, and k.
    ).fetchall()
    # .fetchall() returns the rows as a list, e.g.
    #   [("credit_rating_history", 0.54), ("securities", 0.47), ...]


# ═════════════════════════════════════════════════════════════════════════════
# SEARCH 2 — the exact value for a loosely typed name   (6A, step 3)
# ═════════════════════════════════════════════════════════════════════════════

def match_values(conn, text: str, k: int = 3, table: str | None = None,
                 column: str | None = None) -> list[tuple[str, str, str, float]]:
    # Takes:
    #   conn            an open connection to agent_meta
    #   text            the name as the user typed it: "sahyadri credit fund"
    #   k               how many matches to return (3 if not given; step 3 asks for 1)
    #   table, column   optional: search only this one table and column.
    #                   None means search every stored name.
    # Returns: a list of (table, column, exact value, score), closest first.
    """
    The k stored database values closest to a name as a user typed it.
    Example: "sahyadri credit fund" -> ("schemes", "scheme_name", "Sahyadri Credit Risk Fund", 0.8x)
    Optionally limited to one table and column.
    Returns (table, column, value, similarity).
    """

    v = _vector(text)
    # The typed name as 1,536 numbers.

    return conn.execute(
        """SELECT table_name, column_name, content, 1 - (embedding <=> %s::vector) AS score
           FROM meta_embeddings
           WHERE kind = 'value' AND (%s::text IS NULL OR table_name = %s) AND (%s::text IS NULL OR column_name = %s)
           ORDER BY embedding <=> %s::vector LIMIT %s""",
        # The SQL, line by line:
        #
        #   SELECT table_name, column_name, content, score
        #       where the value came from, the exact value itself
        #       ("content"), and how close it is
        #   FROM meta_embeddings
        #   WHERE kind = 'value'
        #       only stored names, not table notes
        #     AND (%s::text IS NULL OR table_name = %s)
        #     AND (%s::text IS NULL OR column_name = %s)
        #       THE OPTIONAL FILTER. Read one of these lines like this:
        #         if no table was given  -> "NULL IS NULL" is true -> every row passes
        #         if a table was given   -> only rows from that table pass
        #       This lets one query serve both cases, with or without a filter.
        #       ::text tells PostgreSQL the value is text, because it cannot
        #       work out the type of a NULL on its own.
        #   ORDER BY embedding <=> %s::vector LIMIT %s
        #       closest first, only the first k

        (v, table, table, column, column, v, k),
        # The values for the seven %s placeholders, in the order they appear:
        #   v                score
        #   table, table     the two halves of the table filter
        #   column, column   the two halves of the column filter
        #   v                ordering
        #   k                limit
    ).fetchall()
    # For our demo:
    #   [("schemes", "scheme_name", "Sahyadri Credit Risk Fund", 0.8...)]


# ═════════════════════════════════════════════════════════════════════════════
# SEARCH 3 — how do these tables join?                  (6B, step 5)
# The three functions starting with _ are helpers for find_join_path().
# ═════════════════════════════════════════════════════════════════════════════

def _parse_edges(agtype_text: str) -> list[dict]:
    # Turns the graph's reply into a plain Python list of joins.
    """
    AGE returns graph results as "agtype" text: JSON with type tags added,
    e.g. [{"id": 1, "label": "JOINS", "properties": {...}}::edge, ...].
    Removing the ::edge / ::vertex / ::path tags leaves plain JSON.
    We keep only each edge's properties, which hold the join columns.
    """
    # In plain words: the graph replies with JSON that has extra tags like
    # ::edge stuck on the end of each item. json.loads() cannot read those.
    # Remove the tags and it becomes normal JSON.

    plain = re.sub(r"::(edge|vertex|path)", "", agtype_text)
    # Replace every "::edge", "::vertex" or "::path" with nothing.
    #   r"::(edge|vertex|path)"   the pattern: "::" followed by any of the three words
    #   ""                        replace it with nothing

    return [edge["properties"] for edge in json.loads(plain)]
    # Read the JSON, and keep only the "properties" part of each line in the
    # route. The properties are what publish.py stored on each line in Part 4:
    #   {"from_table": "transactions", "from_column": "arn_code",
    #    "to_table": "distributors", "to_column": "arn_no",
    #    "source": "value_overlap", "confidence": ...}


def _routes(conn, graph: str, a: str, b: str, max_hops: int) -> list[list[dict]]:
    # Asks the graph for every route between two tables.
    # Returns a list of routes; each route is a list of joins.
    """
    Every route between tables a and b, up to max_hops joins long.
    -[*1..N]- (no arrow) means: any edges, in either direction. A join works
    both ways, whichever table the link was recorded on.
    """
    # In plain words: the links are stored with a direction —
    # transactions -> distributors. But a join works both ways, so we ask
    # for routes in either direction.

    if not re.fullmatch(r"[a-z0-9_]+", a) or not re.fullmatch(r"[a-z0-9_]+", b):
        # Check both table names contain only lowercase letters, digits and
        # underscores — every real table name in this project does.
        raise ValueError("table names must be lowercase letters, digits and underscores")
        # Stop with an error.
        #
        # WHY THIS CHECK EXISTS: the graph query below cannot take
        # placeholders, so the table names are pasted straight into its
        # text. A name containing a quote mark could break out of the query
        # and change it. Allowing only safe characters makes that impossible.

    rows = conn.execute(
        f"""SELECT * FROM cypher('{graph}', $$
                MATCH p = (x:Table {{name: '{a}'}})-[*1..{int(max_hops)}]-(y:Table {{name: '{b}'}})
                RETURN relationships(p)
            $$) AS (edges agtype)"""
        # A graph query (written in a language called Cypher) wrapped inside SQL.
        #
        #   SELECT * FROM cypher('schema_graph', $$ ... $$)
        #       run the graph query between the two $$ marks, on schema_graph
        #
        #   MATCH p = (x:Table {name: 'transactions'})
        #             -[*1..6]-
        #             (y:Table {name: 'distributors'})
        #       find every route p from the transactions point to the
        #       distributors point, using 1 to 6 lines, in either direction
        #
        #   RETURN relationships(p)
        #       for each route, give back the lines it uses
        #
        #   AS (edges agtype)
        #       SQL needs to know the shape of what comes back: one column,
        #       called edges, of the graph's own type, agtype
        #
        # {{ and }} are how you write a single { or } inside an f-string.
        # int(max_hops) makes sure that value is a whole number before it
        # goes into the text.
    ).fetchall()
    # One row per route.

    return [_parse_edges(str(r[0])) for r in rows]
    # Turn each route into a plain list of joins, using _parse_edges above.


def _best(routes: list[list[dict]]) -> list[dict] | None:
    # Picks the best route from a list of routes. None if there are none.
    """Shortest route first; among equally short ones, the highest total confidence."""

    if not routes:
        # No route at all between the two tables.
        return None

    return min(routes, key=lambda r: (len(r), -sum(e["confidence"] for e in r),
                                      [(e["from_table"], e["from_column"]) for e in r]))
    # min() picks the route with the smallest "key". The key is three
    # things, compared in order — the second is only looked at when the
    # first is a tie, and so on:
    #
    #   1. len(r)
    #        the number of joins. Fewer joins is better, so the shortest
    #        route wins.
    #   2. -sum(e["confidence"] for e in r)
    #        the links' confidence added up. Higher confidence is better,
    #        but min() picks the SMALLEST, so the minus sign flips it.
    #   3. the list of (table, column) pairs
    #        alphabetical order, as a last tie-breaker. Without it, two
    #        equal routes could be picked in a different order on different
    #        runs. With it, the same question always gets the same route.
    #
    # "lambda r: ..." is a small function written in one line. It takes one
    # route, r, and returns its key.


def find_join_path(conn, tables: list[str], graph: str = "schema_graph", max_hops: int = 6) -> dict:
    # Takes:
    #   conn       an open connection to agent_meta
    #   tables     the tables the question needs, e.g. ["transactions", "distributors"]
    #   graph      the graph's name (schema_graph, as Part 4 created it)
    #   max_hops   the longest route allowed, in joins
    # Returns: {"tables": [...], "joins": [...]}
    """
    The joins needed to connect all the given tables.
    Returns {"tables": [...every table involved...], "joins": [edge, ...]},
    where each edge has from_table, from_column, to_table, to_column.
    Raises ValueError if some table cannot be reached within max_hops.
    """

    wanted = list(dict.fromkeys(tables))          # remove duplicates, keep order
    # The tables we need, with any repeats removed.
    #
    # dict.fromkeys() makes a dictionary whose keys are the tables. A
    # dictionary cannot hold the same key twice, and it keeps the order
    # items were added. list() turns the keys back into a list.
    # So ["a", "b", "a"] becomes ["a", "b"].

    joined = {wanted[0]}
    # The tables connected so far. Start with just the first one.
    # { } with values inside is a SET: a collection with no repeats.

    joins: list[dict] = []
    # The joins chosen so far. Empty to begin with.

    while any(t not in joined for t in wanted):
        # Keep going as long as at least one wanted table is not connected yet.

        # For every table still missing, find its best route to any table
        # already joined; then take the closest one.

        candidates = []
        # The possible next routes.

        for target in (t for t in wanted if t not in joined):
            # Every wanted table that is not connected yet ...

            for start in joined:
                # ... tried from every table that is already connected.

                route = _best(_routes(conn, graph, start, target, max_hops))
                # The best route between these two, or None.

                if route:
                    # A route exists:
                    candidates.append(route)
                    # keep it as a possible next step.

        if not candidates:
            # Not one missing table can be reached from the connected ones.
            missing = [t for t in wanted if t not in joined]
            # Which tables those are.
            raise ValueError(f"No join route within {max_hops} hops to: {missing}")
            # Stop with an error that names them. In 6B, the agent turns
            # this into a "failed" answer instead of a crash.

        route = _best(candidates)
        # Of all the possible next routes, take the best — usually the
        # shortest. That connects the CLOSEST missing table next.

        for edge in route:
            # For every join in that route:

            if edge not in joins:
                # if it is not already in our list ...
                joins.append(edge)
                # ... add it. (Two routes can share a join; we want it once.)

            joined.update((edge["from_table"], edge["to_table"]))
            # Mark both tables of this join as connected. This can include
            # tables we did not ask for, which the route passes THROUGH.
            # For example, transactions -> credit_rating_history passes
            # through schemes, portfolio_holdings and securities.

    ordered = list(dict.fromkeys(wanted + [t for e in joins for t in (e["from_table"], e["to_table"])]))
    # Every table involved, with no repeats: the tables we asked for first,
    # then the in-between tables the routes pass through. The SQL in 6B
    # needs every one of them in its FROM and JOIN lines.

    return {"tables": ordered, "joins": joins}
    # For ["transactions", "distributors"] this is:
    #   {"tables": ["transactions", "distributors"],
    #    "joins":  [{"from_table": "transactions", "from_column": "arn_code",
    #                "to_table": "distributors", "to_column": "arn_no", ...}]}
    # One join, using a hidden link Part 4 discovered.