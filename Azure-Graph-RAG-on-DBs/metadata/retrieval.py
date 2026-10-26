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

# json: reads AGE's results once the type tags are removed.
import json

# re: regular expressions (removing tags, checking table names).
import re

# Makes embeddings.
from metadata import llm


# Helper: text -> vector text for pgvector.
def _vector(question: str) -> str:
    """Embed one piece of text, written the way pgvector reads a vector."""
    # Embed a one-item list, take the first vector, write it as "[0.1, 0.2, ...]".
    return str(llm.embed([question])[0])


# Used by the agent's select_tables step.
def search_tables(conn, question: str, k: int = 5) -> list[tuple[str, float]]:
    """
    The k tables whose notes are closest in meaning to the question.
    <=> is pgvector's cosine distance: 0 = same meaning, 2 = opposite.
    We return 1 - distance as a similarity score, where higher is better.
    """
    # The question as a vector.
    v = _vector(question)
    # Run the search; %s placeholders are filled from the tuple (v, v, k) in order.
    return conn.execute(
        # SQL: the k nearest table notes and their similarity.
        """SELECT table_name, 1 - (embedding <=> %s::vector) AS score FROM meta_embeddings
           WHERE kind = 'table' ORDER BY embedding <=> %s::vector LIMIT %s""",
        # The values for the three %s: the vector (twice) and k.
        (v, v, k),
    ).fetchall()


# Used by the agent's match_entities step.
def match_values(conn, text: str, k: int = 3, table: str | None = None,
                 # table/column are optional (None = search every column).
                 column: str | None = None) -> list[tuple[str, str, str, float]]:
    """
    The k stored database values closest to a name as a user typed it.
    Example: "sahyadri credit fund" -> ("schemes", "scheme_name", "Sahyadri Credit Risk Fund", 0.8x)
    Optionally limited to one table and column.
    Returns (table, column, value, similarity).
    """
    # The typed name as a vector.
    v = _vector(text)
    # "%s::text IS NULL OR table_name = %s": when no table is given, every table matches.
    return conn.execute(
        # SQL: the k nearest values, optionally in one table and column.
        """SELECT table_name, column_name, content, 1 - (embedding <=> %s::vector) AS score
           FROM meta_embeddings
           WHERE kind = 'value' AND (%s::text IS NULL OR table_name = %s) AND (%s::text IS NULL OR column_name = %s)
           ORDER BY embedding <=> %s::vector LIMIT %s""",
        # The values for the seven %s, in order.
        (v, table, table, column, column, v, k),
    ).fetchall()


# Helper: AGE output -> a list of edge property dicts.
def _parse_edges(agtype_text: str) -> list[dict]:
    """
    AGE returns graph results as "agtype" text: JSON with type tags added,
    e.g. [{"id": 1, "label": "JOINS", "properties": {...}}::edge, ...].
    Removing the ::edge / ::vertex / ::path tags leaves plain JSON.
    We keep only each edge's properties, which hold the join columns.
    """
    # Remove the type tags.
    plain = re.sub(r"::(edge|vertex|path)", "", agtype_text)
    # Parse the JSON and keep each edge's properties.
    return [edge["properties"] for edge in json.loads(plain)]


# Helper: ask AGE for every route between two tables.
def _routes(conn, graph: str, a: str, b: str, max_hops: int) -> list[list[dict]]:
    """
    Every route between tables a and b, up to max_hops joins long.
    -[*1..N]- (no arrow) means: any edges, in either direction. A join works
    both ways, whichever table the link was recorded on.
    """
    # Table names are put straight into the query text, so only allow safe characters.
    if not re.fullmatch(r"[a-z0-9_]+", a) or not re.fullmatch(r"[a-z0-9_]+", b):
        # Refuse unsafe names.
        raise ValueError("table names must be lowercase letters, digits and underscores")
    # MATCH every path p between the two Table nodes of 1..max_hops edges; return its edges.
    rows = conn.execute(
        # Cypher inside SQL (the graph name and table names are written into the text).
        f"""SELECT * FROM cypher('{graph}', $$
                MATCH p = (x:Table {{name: '{a}'}})-[*1..{int(max_hops)}]-(y:Table {{name: '{b}'}})
                RETURN relationships(p)
            $$) AS (edges agtype)"""
    ).fetchall()
    # One list of edge properties per route.
    return [_parse_edges(str(r[0])) for r in rows]


# Helper: pick the best of several routes.
def _best(routes: list[list[dict]]) -> list[dict] | None:
    """Shortest route first; among equally short ones, the highest total confidence."""
    # No route at all.
    if not routes:
        # Nothing to choose from.
        return None
    # min() with a key: fewest joins, then highest confidence (minus = higher is better),
    # then alphabetical, so the choice is always the same.
    return min(routes, key=lambda r: (len(r), -sum(e["confidence"] for e in r),
                                      # (last tie-breaker: alphabetical order, so the result never changes between runs)
                                      [(e["from_table"], e["from_column"]) for e in r]))


# Used by the agent's find_join_path step.
def find_join_path(conn, tables: list[str], graph: str = "schema_graph", max_hops: int = 6) -> dict:
    """
    The joins needed to connect all the given tables.
    Returns {"tables": [...every table involved...], "joins": [edge, ...]},
    where each edge has from_table, from_column, to_table, to_column.
    Raises ValueError if some table cannot be reached within max_hops.
    """
    wanted = list(dict.fromkeys(tables))          # remove duplicates, keep order
    # Start with the first table.
    joined = {wanted[0]}
    # The joins chosen so far.
    joins: list[dict] = []

    # Repeat while some wanted table is not connected yet.
    while any(t not in joined for t in wanted):
        # For every table still missing, find its best route to any table
        # already joined; then take the closest one.
        # The best route for each missing table.
        candidates = []
        # Every table not yet joined ...
        for target in (t for t in wanted if t not in joined):
            # ... from every table already joined.
            for start in joined:
                # The best route between them (None if none).
                route = _best(_routes(conn, graph, start, target, max_hops))
                # Keep it as a candidate.
                if route:
                    # Keep it.
                    candidates.append(route)
        # No missing table can be reached: give up with a clear error.
        if not candidates:
            # The tables that could not be connected.
            missing = [t for t in wanted if t not in joined]
            # Stop with an error naming them.
            raise ValueError(f"No join route within {max_hops} hops to: {missing}")
        # The closest missing table's route.
        route = _best(candidates)
        # Add its joins (each only once) ...
        for edge in route:
            # Avoid adding the same join twice.
            if edge not in joins:
                # Add the join.
                joins.append(edge)
            # ... and mark both of each join's tables as joined.
            joined.update((edge["from_table"], edge["to_table"]))

    # Every table involved: the wanted ones first, then any in-between tables the routes pass through.
    ordered = list(dict.fromkeys(wanted + [t for e in joins for t in (e["from_table"], e["to_table"])]))
    # The tables and the joins for the SQL-writing step.
    return {"tables": ordered, "joins": joins}