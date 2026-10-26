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

# json: writes values as safely quoted text for Cypher.
import json

# os: reads DATA_DB_URL and AGENT_DB_URL.
import os

# re: regular expressions, for the exclude_column_patterns.
import re

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# Reads real values from the fund database.
from adapters.postgres import PostgresAdapter

# llm: makes embeddings. store: connects to agent_meta.
from metadata import llm, store

# Reads config files (shared with build.py).
from metadata.build import load_yaml


def cypher_text(value) -> str:
    """
    Write a Python value as a Cypher literal inside a query.

    AGE requires the Cypher query to be one fixed piece of text, so values
    cannot be passed separately as with normal SQL parameters. json.dumps
    produces a correctly quoted and escaped string ("..." with \\" inside).
    Dollar signs are removed, because $$ would end the query text early.
    """
    # Quote the value as JSON text, then drop any "$".
    return json.dumps(value, ensure_ascii=False).replace("$", "")


def table_document(name, description, grain, terms, columns) -> str:
    """The text embedded for one table: everything a search should be able to match."""
    # "column: description" for every described column.
    column_text = "; ".join(f"{c}: {d}" for c, d in columns if d)
    # One long text: name, description, grain, business terms and columns.
    return (f"Table {name}. {description} {grain}. "
            f"Business terms: {', '.join(terms or [])}. Columns: {column_text}")


# The whole publish, start to finish.
def main() -> None:
    # Read .env.
    load_dotenv()
    # The pipeline's rules.
    settings = load_yaml("metadata.yaml")
    # The "embedding" section: which values to embed, batch size.
    embed_rules = settings["embedding"]
    # The AGE graph's name ("schema_graph").
    graph = settings["graph"]["name"]
    # Read-only gateway to mf_data.
    adapter = PostgresAdapter(os.environ["DATA_DB_URL"])
    # Connection to agent_meta.
    conn = store.connect(os.environ["AGENT_DB_URL"])

    # Every approved table note.
    approved = conn.execute(
        "SELECT table_name, description, grain, business_terms FROM meta_tables WHERE status = 'approved' ORDER BY 1"
    ).fetchall()
    # Nothing approved yet: stop with instructions.
    if not approved:
        raise SystemExit("No approved notes yet. Run: uv run python -m metadata.review --approve-all")
    # Just the approved table names, as a set.
    approved_names = {row[0] for row in approved}

    # ---------------------------------------------------------------------
    # 1. Work out everything to embed
    # ---------------------------------------------------------------------
    items = []   # (kind, table, column, text)
    # One "table" item per approved table ...
    for name, description, grain, terms in approved:
        # ... built from its note and its column notes.
        columns = conn.execute(
            "SELECT column_name, description FROM meta_columns WHERE table_name = %s", (name,)).fetchall()
        items.append(("table", name, None, table_document(name, description, grain, terms, columns)))

    # Compile the "looks like a code" patterns once.
    patterns = [re.compile(p) for p in embed_rules["exclude_column_patterns"]]
    # Text columns that are not PII and have 2..max distinct values.
    for table, column, distinct in conn.execute(
        """SELECT table_name, column_name, distinct_values FROM meta_columns
           WHERE data_type = 'text' AND NOT is_pii AND distinct_values BETWEEN 2 AND %s""",
        (embed_rules["max_distinct_values"],),
    ).fetchall():
        # Skip unapproved tables and code-like column names.
        if table not in approved_names or any(p.search(column) for p in patterns):
            continue
        # Read the column's distinct values from mf_data; one "value" item each.
        for value in adapter.sample_values(table, column, embed_rules["max_distinct_values"]):
            items.append(("value", table, column, value))

    # ---------------------------------------------------------------------
    # 2. Embed them in batches and store them
    # ---------------------------------------------------------------------
    # Progress: how many items of each kind.
    print(f"Embedding {len(items):,} items "
          f"({sum(i[0] == 'table' for i in items)} table notes, {sum(i[0] == 'value' for i in items)} values) ...")
    # Empty the table first: publish always rebuilds from scratch.
    conn.execute("TRUNCATE meta_embeddings")
    # Texts per embedding request.
    size = embed_rules["batch_size"]
    # start = 0, size, 2*size, ... (one loop per batch).
    for start in range(0, len(items), size):
        # The next "size" items.
        batch = items[start:start + size]
        # Embed just the text (the last field of each item).
        vectors = llm.embed([text for *_rest, text in batch])
        # executemany runs the INSERT once per row in the list.
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
    # 1 if the graph already exists, else 0.
    exists = conn.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (graph,)).fetchone()[0]
    # drop_graph(name, true) deletes the graph and everything in it.
    if exists:
        conn.execute("SELECT drop_graph(%s, true)", (graph,))
    # Create a new, empty graph.
    conn.execute("SELECT create_graph(%s)", (graph,))

    # One node per approved table, labelled Table.
    # In f-strings, {{ and }} produce literal { and } (Cypher's property brackets).
    for name, description, grain, _terms in approved:
        # Create one Table node holding the table's name and grain.
        conn.execute(
            f"SELECT * FROM cypher('{graph}', $$ CREATE (:Table {{name: {cypher_text(name)}, "
            f"grain: {cypher_text(grain or '')}}}) $$) AS (v agtype)"
        )

    # One JOINS edge per approved link between two approved tables. The edge
    # carries everything needed to write the JOIN condition in SQL.
    # Every approved link ...
    edges = conn.execute(
        """SELECT from_table, from_column, to_table, to_column, source, confidence
           FROM meta_edges WHERE status = 'approved' ORDER BY edge_id"""
    ).fetchall()
    # ... whose two tables are both approved.
    edges = [e for e in edges if e[0] in approved_names and e[2] in approved_names]
    # a.ac -> b.bc, with how it was found and how sure we are.
    for a, ac, b, bc, source, confidence in edges:
        # The edge's properties as Cypher text.
        props = (f"from_table: {cypher_text(a)}, from_column: {cypher_text(ac)}, "
                 f"to_table: {cypher_text(b)}, to_column: {cypher_text(bc)}, "
                 f"source: {cypher_text(source)}, confidence: {float(confidence)}")
        # Find the two table nodes and draw a JOINS edge from x to y.
        conn.execute(
            f"SELECT * FROM cypher('{graph}', $$ MATCH (x:Table {{name: {cypher_text(a)}}}), "
            f"(y:Table {{name: {cypher_text(b)}}}) CREATE (x)-[:JOINS {{{props}}}]->(y) $$) AS (e agtype)"
        )

    # Report what was built.
    print(f"Graph '{graph}': {len(approved)} table nodes, {len(edges)} join edges.")
    # Close the agent_meta connection.
    conn.close()
    # Tell the user the next step.
    print("Done. Next: uv run pytest tests/test_stage5_metadata.py -v")


# True only when run as: uv run python -m metadata.publish
if __name__ == "__main__":
    # Start publishing.
    main()