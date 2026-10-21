"""
metadata/publish.py
===================

WHAT THIS FILE IS FOR
---------------------
Step 3 of 3. Takes only APPROVED items and makes them usable by the agent:

    approved table notes          -> embeddings in meta_embeddings (kind 'table')
    category values from tables   -> embeddings in meta_embeddings (kind 'value')
    approved links                -> the AGE graph "schema_graph"

    uv run python -m metadata.publish

Safe to run again at any time: it clears and rebuilds everything it owns,
from meta_tables and meta_edges, which are the source of truth.

WHICH VALUES GET EMBEDDED (tradeoff 8)
--------------------------------------
A column's values are embedded only if ALL of these hold:
    - it is a text column in an approved table
    - it is not listed in config/pii.yaml
    - it has between 2 and max_distinct_values distinct values
    - its name does not look like a code or identifier (exclude_column_patterns)
This gives the scheme, issuer, group, sector, manager and distributor
names, and short lists like transaction types, and nothing else.
"""

import json
import os
import re

from dotenv import load_dotenv

from adapters.postgres import PostgresAdapter
from metadata import llm, store
from metadata.build import load_yaml


def cypher_text(value) -> str:
    """
    Write a Python value as a Cypher literal inside a query.

    AGE requires the Cypher query to be one fixed piece of text, so values
    cannot be passed separately as with normal SQL parameters. json.dumps
    produces a correctly quoted and escaped string ("..." with \\" inside).
    Dollar signs are removed, because $$ would end the query text early.
    """
    return json.dumps(value, ensure_ascii=False).replace("$", "")


def table_document(name, description, grain, terms, columns) -> str:
    """The text embedded for one table: everything a search should be able to match."""
    column_text = "; ".join(f"{c}: {d}" for c, d in columns if d)
    return (f"Table {name}. {description} {grain}. "
            f"Business terms: {', '.join(terms or [])}. Columns: {column_text}")


def main() -> None:
    load_dotenv()
    settings = load_yaml("metadata.yaml")
    embed_rules = settings["embedding"]
    graph = settings["graph"]["name"]
    adapter = PostgresAdapter(os.environ["DATA_DB_URL"])
    conn = store.connect(os.environ["AGENT_DB_URL"])

    approved = conn.execute(
        "SELECT table_name, description, grain, business_terms FROM meta_tables WHERE status = 'approved' ORDER BY 1"
    ).fetchall()
    if not approved:
        raise SystemExit("No approved notes yet. Run: uv run python -m metadata.review --approve-all")
    approved_names = {row[0] for row in approved}

    # ---------------------------------------------------------------------
    # 1. Work out everything to embed
    # ---------------------------------------------------------------------
    items = []   # (kind, table, column, text)
    for name, description, grain, terms in approved:
        columns = conn.execute(
            "SELECT column_name, description FROM meta_columns WHERE table_name = %s", (name,)).fetchall()
        items.append(("table", name, None, table_document(name, description, grain, terms, columns)))

    patterns = [re.compile(p) for p in embed_rules["exclude_column_patterns"]]
    for table, column, distinct in conn.execute(
        """SELECT table_name, column_name, distinct_values FROM meta_columns
           WHERE data_type = 'text' AND NOT is_pii AND distinct_values BETWEEN 2 AND %s""",
        (embed_rules["max_distinct_values"],),
    ).fetchall():
        if table not in approved_names or any(p.search(column) for p in patterns):
            continue
        for value in adapter.sample_values(table, column, embed_rules["max_distinct_values"]):
            items.append(("value", table, column, value))

    # ---------------------------------------------------------------------
    # 2. Embed them in batches and store them
    # ---------------------------------------------------------------------
    print(f"Embedding {len(items):,} items "
          f"({sum(i[0] == 'table' for i in items)} table notes, {sum(i[0] == 'value' for i in items)} values) ...")
    conn.execute("TRUNCATE meta_embeddings")
    size = embed_rules["batch_size"]
    for start in range(0, len(items), size):
        batch = items[start:start + size]
        vectors = llm.embed([text for *_rest, text in batch])
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO meta_embeddings (kind, table_name, column_name, content, embedding)
                   VALUES (%s, %s, %s, %s, %s::vector)""",
                # pgvector accepts a vector written as text: "[0.1, 0.2, ...]".
                [(k, t, c, text, str(v)) for (k, t, c, text), v in zip(batch, vectors)],
            )

    # ---------------------------------------------------------------------
    # 3. Rebuild the AGE graph from approved links
    # ---------------------------------------------------------------------
    # Drop the old graph if it exists, then create an empty one.
    exists = conn.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (graph,)).fetchone()[0]
    if exists:
        conn.execute("SELECT drop_graph(%s, true)", (graph,))
    conn.execute("SELECT create_graph(%s)", (graph,))

    # One node per approved table, labelled Table.
    for name, description, grain, _terms in approved:
        conn.execute(
            f"SELECT * FROM cypher('{graph}', $$ CREATE (:Table {{name: {cypher_text(name)}, "
            f"grain: {cypher_text(grain or '')}}}) $$) AS (v agtype)"
        )

    # One JOINS edge per approved link between two approved tables. The edge
    # carries everything needed to write the JOIN condition in SQL.
    edges = conn.execute(
        """SELECT from_table, from_column, to_table, to_column, source, confidence
           FROM meta_edges WHERE status = 'approved' ORDER BY edge_id"""
    ).fetchall()
    edges = [e for e in edges if e[0] in approved_names and e[2] in approved_names]
    for a, ac, b, bc, source, confidence in edges:
        props = (f"from_table: {cypher_text(a)}, from_column: {cypher_text(ac)}, "
                 f"to_table: {cypher_text(b)}, to_column: {cypher_text(bc)}, "
                 f"source: {cypher_text(source)}, confidence: {float(confidence)}")
        conn.execute(
            f"SELECT * FROM cypher('{graph}', $$ MATCH (x:Table {{name: {cypher_text(a)}}}), "
            f"(y:Table {{name: {cypher_text(b)}}}) CREATE (x)-[:JOINS {{{props}}}]->(y) $$) AS (e agtype)"
        )

    print(f"Graph '{graph}': {len(approved)} table nodes, {len(edges)} join edges.")
    conn.close()
    print("Done. Next: uv run pytest tests/test_stage5_metadata.py -v")


if __name__ == "__main__":
    main()