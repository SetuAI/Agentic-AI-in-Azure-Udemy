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

import os
import re
from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv

from metadata import retrieval, store

load_dotenv()
PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def conn():
    connection = store.connect(os.environ["AGENT_DB_URL"])
    yield connection
    connection.close()


def one(conn, sql, params=None):
    return conn.execute(sql, params).fetchone()[0]


# =============================================================================
# NOTES
# =============================================================================

def test_all_20_tables_have_approved_notes(conn):
    assert one(conn, "SELECT count(*) FROM meta_tables WHERE status = 'approved' AND description IS NOT NULL") == 20


def test_every_column_has_a_description(conn):
    missing = conn.execute(
        "SELECT table_name || '.' || column_name FROM meta_columns WHERE coalesce(description, '') = ''"
    ).fetchall()
    assert missing == []


def test_pii_columns_are_flagged(conn):
    """Every column listed in config/pii.yaml is marked is_pii in meta_columns."""
    listed = yaml.safe_load((PROJECT / "config" / "pii.yaml").read_text())["pii_columns"]
    for table, columns in listed.items():
        for column in columns:
            assert one(conn, "SELECT is_pii FROM meta_columns WHERE table_name = %s AND column_name = %s",
                       (table, column)), f"{table}.{column} should be flagged as PII"


# =============================================================================
# LINKS: scored against the answer key (Gate 2)
# =============================================================================

def _answer_key() -> set[tuple[str, str, str, str]]:
    """The 3 hidden links from evals/answer_key/hidden_links.yaml, as (from_t, from_c, to_t, to_c)."""
    links = yaml.safe_load((PROJECT / "evals" / "answer_key" / "hidden_links.yaml").read_text())["hidden_links"]
    return {tuple(link["from"].split(".") + link["to"].split(".")) for link in links}


def _approved_inferred(conn) -> set[tuple[str, str, str, str]]:
    return set(conn.execute(
        """SELECT from_table, from_column, to_table, to_column FROM meta_edges
           WHERE status = 'approved' AND source <> 'declared'"""
    ).fetchall())


def test_gate2_at_least_2_of_3_hidden_links_found(conn):
    found = _answer_key() & _approved_inferred(conn)
    print(f"\nHidden links found: {len(found)} of 3 -> {sorted(found)}")
    assert len(found) >= 2


def test_no_wrong_links_approved(conn):
    """Every approved undeclared link is a real one. A wrong link means wrong joins."""
    assert _approved_inferred(conn) - _answer_key() == set()


# =============================================================================
# EMBEDDINGS
# =============================================================================

def test_no_personal_data_embedded(conn):
    listed = yaml.safe_load((PROJECT / "config" / "pii.yaml").read_text())["pii_columns"]
    for table, columns in listed.items():
        count = one(conn, "SELECT count(*) FROM meta_embeddings WHERE table_name = %s AND column_name = ANY(%s)",
                    (table, columns))
        assert count == 0, f"PII from {table} was embedded"


def test_no_codes_embedded(conn):
    patterns = yaml.safe_load((PROJECT / "config" / "metadata.yaml").read_text())["embedding"]["exclude_column_patterns"]
    columns = [r[0] for r in conn.execute(
        "SELECT DISTINCT column_name FROM meta_embeddings WHERE kind = 'value'").fetchall()]
    embedded_codes = [c for c in columns if any(re.search(p, c) for p in patterns)]
    assert embedded_codes == []


def test_one_embedding_per_table_note(conn):
    assert one(conn, "SELECT count(*) FROM meta_embeddings WHERE kind = 'table'") == 20


# =============================================================================
# GRAPH
# =============================================================================

def test_graph_has_20_tables_and_every_approved_link(conn):
    graph = yaml.safe_load((PROJECT / "config" / "metadata.yaml").read_text())["graph"]["name"]
    nodes = one(conn, f"SELECT count(*) FROM cypher('{graph}', $$ MATCH (t:Table) RETURN t $$) AS (t agtype)")
    edges = one(conn, f"SELECT count(*) FROM cypher('{graph}', $$ MATCH ()-[j:JOINS]->() RETURN j $$) AS (j agtype)")
    assert nodes == 20
    assert edges == one(conn, "SELECT count(*) FROM meta_edges WHERE status = 'approved'")


def test_join_route_for_the_downgrade_question(conn):
    """
    'Why did net flows in the Credit Risk Fund drop in March 2024?' needs
    transactions joined all the way to credit ratings. The route must use
    hidden link 2 (sec_cd -> security_code), which only discovery can provide.
    """
    route = retrieval.find_join_path(conn, ["transactions", "credit_rating_history"])
    conditions = {(e["from_table"], e["from_column"], e["to_table"], e["to_column"]) for e in route["joins"]}
    assert ("credit_rating_history", "sec_cd", "securities", "security_code") in conditions
    assert {"schemes", "portfolio_holdings", "securities"} <= set(route["tables"])


def test_join_route_for_the_rejection_question(conn):
    """Uses hidden link 3: transactions.arn_code -> distributors.arn_no, one join."""
    route = retrieval.find_join_path(conn, ["transactions", "distributors"])
    assert [(e["from_column"], e["to_column"]) for e in route["joins"]] == [("arn_code", "arn_no")]


# =============================================================================
# SEARCH (calls Azure OpenAI)
# =============================================================================

@pytest.mark.parametrize("question, expected_table", [
    ("Which bonds were downgraded by rating agencies?", "credit_rating_history"),
    ("Which distributor has the most rejected transactions?", "distributors"),
    ("Where are SIP cancellations rising?", "sip_registrations"),
    ("What were net flows into the fund last month?", "transactions"),
    ("How much of the portfolio is in one issuer?", "portfolio_holdings"),
])
def test_questions_find_the_right_table(conn, question, expected_table):
    top3 = [name for name, _score in retrieval.search_tables(conn, question, k=3)]
    assert expected_table in top3, f"{question!r} -> {top3}"


@pytest.mark.parametrize("typed, table, column, expected", [
    ("sahyadri credit fund", "schemes", "scheme_name", "Sahyadri Credit Risk Fund"),
    ("vardhaman housing", "issuers", "issuer_name", "Vardhaman Housing Finance Ltd"),
    ("konkan wealth", "distributors", "dist_nm", "Konkan Wealth Partners"),
])
def test_loose_names_match_exact_values(conn, typed, table, column, expected):
    best = retrieval.match_values(conn, typed, k=1, table=table, column=column)[0]
    assert best[2] == expected