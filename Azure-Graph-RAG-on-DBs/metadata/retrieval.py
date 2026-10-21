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

import json
import re

from metadata import llm


def _vector(question: str) -> str:
    """Embed one piece of text, written the way pgvector reads a vector."""
    return str(llm.embed([question])[0])


def search_tables(conn, question: str, k: int = 5) -> list[tuple[str, float]]:
    """
    The k tables whose notes are closest in meaning to the question.
    <=> is pgvector's cosine distance: 0 = same meaning, 2 = opposite.
    We return 1 - distance as a similarity score, where higher is better.
    """
    v = _vector(question)
    return conn.execute(
        """SELECT table_name, 1 - (embedding <=> %s::vector) AS score FROM meta_embeddings
           WHERE kind = 'table' ORDER BY embedding <=> %s::vector LIMIT %s""",
        (v, v, k),
    ).fetchall()


def match_values(conn, text: str, k: int = 3, table: str | None = None,
                 column: str | None = None) -> list[tuple[str, str, str, float]]:
    """
    The k stored database values closest to a name as a user typed it.
    Example: "sahyadri credit fund" -> ("schemes", "scheme_name", "Sahyadri Credit Risk Fund", 0.8x)
    Optionally limited to one table and column.
    Returns (table, column, value, similarity).
    """
    v = _vector(text)
    return conn.execute(
        """SELECT table_name, column_name, content, 1 - (embedding <=> %s::vector) AS score
           FROM meta_embeddings
           WHERE kind = 'value' AND (%s::text IS NULL OR table_name = %s) AND (%s::text IS NULL OR column_name = %s)
           ORDER BY embedding <=> %s::vector LIMIT %s""",
        (v, table, table, column, column, v, k),
    ).fetchall()


def _parse_edges(agtype_text: str) -> list[dict]:
    """
    AGE returns graph results as "agtype" text: JSON with type tags added,
    e.g. [{"id": 1, "label": "JOINS", "properties": {...}}::edge, ...].
    Removing the ::edge / ::vertex / ::path tags leaves plain JSON.
    We keep only each edge's properties, which hold the join columns.
    """
    plain = re.sub(r"::(edge|vertex|path)", "", agtype_text)
    return [edge["properties"] for edge in json.loads(plain)]


def _routes(conn, graph: str, a: str, b: str, max_hops: int) -> list[list[dict]]:
    """
    Every route between tables a and b, up to max_hops joins long.
    -[*1..N]- (no arrow) means: any edges, in either direction. A join works
    both ways, whichever table the link was recorded on.
    """
    if not re.fullmatch(r"[a-z0-9_]+", a) or not re.fullmatch(r"[a-z0-9_]+", b):
        raise ValueError("table names must be lowercase letters, digits and underscores")
    rows = conn.execute(
        f"""SELECT * FROM cypher('{graph}', $$
                MATCH p = (x:Table {{name: '{a}'}})-[*1..{int(max_hops)}]-(y:Table {{name: '{b}'}})
                RETURN relationships(p)
            $$) AS (edges agtype)"""
    ).fetchall()
    return [_parse_edges(str(r[0])) for r in rows]


def _best(routes: list[list[dict]]) -> list[dict] | None:
    """Shortest route first; among equally short ones, the highest total confidence."""
    if not routes:
        return None
    return min(routes, key=lambda r: (len(r), -sum(e["confidence"] for e in r),
                                      [(e["from_table"], e["from_column"]) for e in r]))


def find_join_path(conn, tables: list[str], graph: str = "schema_graph", max_hops: int = 6) -> dict:
    """
    The joins needed to connect all the given tables.
    Returns {"tables": [...every table involved...], "joins": [edge, ...]},
    where each edge has from_table, from_column, to_table, to_column.
    Raises ValueError if some table cannot be reached within max_hops.
    """
    wanted = list(dict.fromkeys(tables))          # remove duplicates, keep order
    joined = {wanted[0]}
    joins: list[dict] = []

    while any(t not in joined for t in wanted):
        # For every table still missing, find its best route to any table
        # already joined; then take the closest one.
        candidates = []
        for target in (t for t in wanted if t not in joined):
            for start in joined:
                route = _best(_routes(conn, graph, start, target, max_hops))
                if route:
                    candidates.append(route)
        if not candidates:
            missing = [t for t in wanted if t not in joined]
            raise ValueError(f"No join route within {max_hops} hops to: {missing}")
        route = _best(candidates)
        for edge in route:
            if edge not in joins:
                joins.append(edge)
            joined.update((edge["from_table"], edge["to_table"]))

    ordered = list(dict.fromkeys(wanted + [t for e in joins for t in (e["from_table"], e["to_table"])]))
    return {"tables": ordered, "joins": joins}