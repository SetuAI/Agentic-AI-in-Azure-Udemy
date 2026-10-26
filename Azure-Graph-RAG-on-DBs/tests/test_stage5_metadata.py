"""
tests/test_stage5_metadata.py
=============================

WHAT THIS FILE IS FOR
---------------------
Checks the metadata layer in agent_meta. This is Gate 2 from the build
plan: the agent is not built until the notes and links are good, because
everything the agent does rests on them.

    NOTES      every table and column is described and approved; PII is flagged
    LINKS      the hidden links were found (scored against the answer key)
    EMBEDDINGS nothing personal and no codes were embedded
    GRAPH      the AGE graph matches the approved links, and finds real join routes
    SEARCH     questions find the right tables; loose names find the right values

The SEARCH tests call Azure OpenAI to embed each question, so they need the
.env settings and cost a fraction of a rupee.

HOW TO RUN
----------
After build -> review --approve-all -> publish:

    uv run pytest tests/test_stage5_metadata.py -v
"""

# HOW TO RUN (from the project folder):
#     Windows (PowerShell):  uv run pytest tests/test_stage5_metadata.py -v
#     Mac / Linux:           uv run pytest tests/test_stage5_metadata.py -v
# os: reads AGENT_DB_URL.
import os

# re: regular expressions, to test column names against the code patterns.
import re

# Path: builds paths to config/ and evals/.
from pathlib import Path

# pytest: the test runner.
import pytest

# yaml: reads the config and answer-key files.
import yaml

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# The code under test (retrieval) and the agent_meta connection (store).
from metadata import retrieval, store

# Read .env as soon as the test file is loaded.
load_dotenv()
# The project folder (tests/ -> project).
PROJECT = Path(__file__).resolve().parents[1]


# One agent_meta connection shared by every test in this file.
@pytest.fixture(scope="module")
def conn():
    # Open the connection.
    connection = store.connect(os.environ["AGENT_DB_URL"])
    # Hand it to the tests ...
    yield connection
    # ... and close it after the last test.
    connection.close()


# Helper: run a query and return its single value.
def one(conn, sql, params=None):
    # First row, first column.
    return conn.execute(sql, params).fetchone()[0]


# =============================================================================
# NOTES
# =============================================================================

# NOTES: all 20 tables approved and described.
def test_all_20_tables_have_approved_notes(conn):
    # Exactly 20 tables must be approved and described.
    assert one(conn, "SELECT count(*) FROM meta_tables WHERE status = 'approved' AND description IS NOT NULL") == 20


# NOTES: no column has an empty description.
def test_every_column_has_a_description(conn):
    # Columns whose description is empty.
    missing = conn.execute(
        "SELECT table_name || '.' || column_name FROM meta_columns WHERE coalesce(description, '') = ''"
    ).fetchall()
    # There must be none.
    assert missing == []


# NOTES: personal-data columns are flagged.
def test_pii_columns_are_flagged(conn):
    """Every column listed in config/pii.yaml is marked is_pii in meta_columns."""
    # table -> PII columns, from config/pii.yaml.
    listed = yaml.safe_load((PROJECT / "config" / "pii.yaml").read_text())["pii_columns"]
    # For every listed PII column, is_pii must be true.
    for table, columns in listed.items():
        # For each listed column ...
        for column in columns:
            # ... is_pii must be true in meta_columns.
            assert one(conn, "SELECT is_pii FROM meta_columns WHERE table_name = %s AND column_name = %s",
                       (table, column)), f"{table}.{column} should be flagged as PII"


# =============================================================================
# LINKS: scored against the answer key (Gate 2)
# =============================================================================

# Helper: the expected hidden links.
def _answer_key() -> set[tuple[str, str, str, str]]:
    """The 3 hidden links from evals/answer_key/hidden_links.yaml, as (from_t, from_c, to_t, to_c)."""
    # Read the answer key file.
    links = yaml.safe_load((PROJECT / "evals" / "answer_key" / "hidden_links.yaml").read_text())["hidden_links"]
    # "table.column" strings split into 4-part tuples.
    return {tuple(link["from"].split(".") + link["to"].split(".")) for link in links}


# The approved links that were FOUND (not declared), as 4-part tuples.
def _approved_inferred(conn) -> set[tuple[str, str, str, str]]:
    # Read them from meta_edges as a set.
    return set(conn.execute(
        """SELECT from_table, from_column, to_table, to_column FROM meta_edges
           WHERE status = 'approved' AND source <> 'declared'"""
    ).fetchall())


# LINKS: "&" = the links in both sets (found AND in the answer key). At least 2 of 3.
def test_gate2_at_least_2_of_3_hidden_links_found(conn):
    # The hidden links that were found and approved.
    found = _answer_key() & _approved_inferred(conn)
    # Shown with pytest -s (or in the failure output).
    print(f"\nHidden links found: {len(found)} of 3 -> {sorted(found)}")
    # Gate 2: at least 2 of the 3.
    assert len(found) >= 2


# LINKS: nothing wrong was approved.
def test_no_wrong_links_approved(conn):
    """Every approved undeclared link is a real one. A wrong link means wrong joins."""
    # "-" = found links NOT in the answer key: must be none.
    assert _approved_inferred(conn) - _answer_key() == set()


# =============================================================================
# EMBEDDINGS
# =============================================================================

# EMBEDDINGS: no value from a PII column was embedded.
def test_no_personal_data_embedded(conn):
    # table -> PII columns.
    listed = yaml.safe_load((PROJECT / "config" / "pii.yaml").read_text())["pii_columns"]
    # For each table with PII columns:
    for table, columns in listed.items():
        # How many embeddings came from those columns (ANY(%s) = "is in this list").
        count = one(conn, "SELECT count(*) FROM meta_embeddings WHERE table_name = %s AND column_name = ANY(%s)",
                    (table, columns))
        # Must be zero.
        assert count == 0, f"PII from {table} was embedded"


# EMBEDDINGS: no code-like column (e.g. *_code) had its values embedded.
def test_no_codes_embedded(conn):
    # The "looks like a code" patterns from config/metadata.yaml.
    patterns = yaml.safe_load((PROJECT / "config" / "metadata.yaml").read_text())["embedding"]["exclude_column_patterns"]
    # Every column that had values embedded.
    columns = [r[0] for r in conn.execute(
        "SELECT DISTINCT column_name FROM meta_embeddings WHERE kind = 'value'").fetchall()]
    # Those whose names look like codes.
    embedded_codes = [c for c in columns if any(re.search(p, c) for p in patterns)]
    # Must be none.
    assert embedded_codes == []


# EMBEDDINGS: exactly one note embedding per table.
def test_one_embedding_per_table_note(conn):
    # 20 table-note embeddings.
    assert one(conn, "SELECT count(*) FROM meta_embeddings WHERE kind = 'table'") == 20


# =============================================================================
# GRAPH
# =============================================================================

# GRAPH: 20 Table nodes, and one JOINS edge per approved link.
def test_graph_has_20_tables_and_every_approved_link(conn):
    # The graph's name from config/metadata.yaml.
    graph = yaml.safe_load((PROJECT / "config" / "metadata.yaml").read_text())["graph"]["name"]
    # Count the Table nodes with a Cypher query.
    nodes = one(conn, f"SELECT count(*) FROM cypher('{graph}', $$ MATCH (t:Table) RETURN t $$) AS (t agtype)")
    # Count the JOINS edges.
    edges = one(conn, f"SELECT count(*) FROM cypher('{graph}', $$ MATCH ()-[j:JOINS]->() RETURN j $$) AS (j agtype)")
    # One node per table.
    assert nodes == 20
    # One edge per approved link.
    assert edges == one(conn, "SELECT count(*) FROM meta_edges WHERE status = 'approved'")


# GRAPH: the multi-hop route for the headline question.
def test_join_route_for_the_downgrade_question(conn):
    """
    'Why did net flows in the Credit Risk Fund drop in March 2024?' needs
    transactions joined all the way to credit ratings. The route must use
    hidden link 2 (sec_cd -> security_code), which only discovery can provide.
    """
    # Find the join route between the two tables.
    route = retrieval.find_join_path(conn, ["transactions", "credit_rating_history"])
    # Every join on the route, as a 4-part tuple.
    conditions = {(e["from_table"], e["from_column"], e["to_table"], e["to_column"]) for e in route["joins"]}
    # The route must use hidden link 2 ...
    assert ("credit_rating_history", "sec_cd", "securities", "security_code") in conditions
    # ... and pass through these three tables.
    assert {"schemes", "portfolio_holdings", "securities"} <= set(route["tables"])


# GRAPH: the one-hop route through hidden link 3.
def test_join_route_for_the_rejection_question(conn):
    """Uses hidden link 3: transactions.arn_code -> distributors.arn_no, one join."""
    # Must be exactly one join, on arn_code -> arn_no.
    route = retrieval.find_join_path(conn, ["transactions", "distributors"])
    # Exactly one join, arn_code -> arn_no.
    assert [(e["from_column"], e["to_column"]) for e in route["joins"]] == [("arn_code", "arn_no")]


# =============================================================================
# SEARCH (calls Azure OpenAI)
# =============================================================================

# SEARCH: each question must find its table among the top 3 search results.
@pytest.mark.parametrize("question, expected_table", [
    ("Which bonds were downgraded by rating agencies?", "credit_rating_history"),
    ("Which distributor has the most rejected transactions?", "distributors"),
    ("Where are SIP cancellations rising?", "sip_registrations"),
    ("What were net flows into the fund last month?", "transactions"),
    ("How much of the portfolio is in one issuer?", "portfolio_holdings"),
])
# Runs once per (question, expected_table) pair above.
def test_questions_find_the_right_table(conn, question, expected_table):
    # The names of the 3 best-matching tables.
    top3 = [name for name, _score in retrieval.search_tables(conn, question, k=3)]
    # The expected table must be among them.
    assert expected_table in top3, f"{question!r} -> {top3}"


# SEARCH: a loosely typed name must match the exact database value.
@pytest.mark.parametrize("typed, table, column, expected", [
    ("sahyadri credit fund", "schemes", "scheme_name", "Sahyadri Credit Risk Fund"),
    ("vardhaman housing", "issuers", "issuer_name", "Vardhaman Housing Finance Ltd"),
    ("konkan wealth", "distributors", "dist_nm", "Konkan Wealth Partners"),
])
# Runs once per (typed, table, column, expected) row above.
def test_loose_names_match_exact_values(conn, typed, table, column, expected):
    # The single best match: (table, column, value, score).
    best = retrieval.match_values(conn, typed, k=1, table=table, column=column)[0]
    # Item 2 is the matched value.
    assert best[2] == expected