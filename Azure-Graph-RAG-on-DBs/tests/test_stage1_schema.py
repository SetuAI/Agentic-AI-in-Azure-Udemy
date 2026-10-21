"""
tests/test_stage1_schema.py
===========================

WHAT THIS FILE IS FOR
---------------------
Checks that apply_schema.py built the mf_data schema exactly as designed.
These tests are Stage 1's "done when" list, written as code:

    1. All 20 tables exist, with the right names.
    2. transactions is split into exactly 60 monthly pieces.
    3. The 3 hidden links have NO foreign key (they must stay hidden).
    4. The declared links that the graph relies on DO exist.

WHY TEST THE SCHEMA AT ALL
--------------------------
Every later stage depends on it. If a table is missing, the generator fails.
If a hidden link was accidentally declared, Stage 5's link-finding score is
meaningless, because the "hidden" link would be found for free by reading
the declared foreign keys. These tests catch both kinds of mistake the
moment they happen, instead of weeks later.

HOW TO RUN
----------
With the Docker Postgres running, after running apply_schema.py:

    uv run pytest tests/test_stage1_schema.py -v
"""

import os

import psycopg
import pytest
from dotenv import load_dotenv

# Read DATA_DB_URL from .env before any test runs.
load_dotenv()


# ---------------------------------------------------------------------------
# THE EXPECTED SCHEMA
# ---------------------------------------------------------------------------
# Written out in full here, on purpose, rather than read from the SQL files.
# If someone renames or deletes a table in a SQL file by mistake, the test
# compares against THIS list and fails. If the test read the SQL files, it
# would simply agree with the mistake.
EXPECTED_TABLES = {
    # File 01: fund house (9)
    "scheme_categories", "benchmarks", "benchmark_values", "schemes",
    "scheme_plans", "fund_managers", "scheme_manager_assignments",
    "nav_history", "scheme_aum_monthly",
    # File 02: market (6)
    "sectors", "issuer_groups", "issuers", "securities",
    "credit_rating_history", "portfolio_holdings",
    # File 03: investors (4)
    "distributors", "investors", "folios", "sip_registrations",
    # File 04: transactions (1)
    "transactions",
}

# The 3 hidden links, as (table, column). Must match
# evals/answer_key/hidden_links.yaml.
HIDDEN_LINK_COLUMNS = [
    ("scheme_aum_monthly", "scheme_id"),
    ("credit_rating_history", "sec_cd"),
    ("transactions", "arn_code"),
]

# A few declared links the graph depends on for the multi-hop demo question
# (schemes -> portfolio_holdings -> securities -> issuers).
# Format: (from_table, from_column, to_table).
DECLARED_LINKS = [
    ("portfolio_holdings", "scheme_id", "schemes"),
    ("portfolio_holdings", "security_id", "securities"),
    ("securities", "issuer_id", "issuers"),
    ("transactions", "folio_id", "folios"),
    ("folios", "investor_id", "investors"),
]


# ---------------------------------------------------------------------------
# SHARED CONNECTION
# ---------------------------------------------------------------------------
# A "fixture" is something pytest prepares once and hands to each test that
# asks for it by name. scope="module" means: open ONE connection for all
# tests in this file, instead of one per test, which is faster.
@pytest.fixture(scope="module")
def conn():
    # autocommit=True makes every query stand on its own. Without it, psycopg
    # groups all queries into one transaction, and once any query fails,
    # Postgres rejects every query after it with "current transaction is
    # aborted". That turns one real failure into ten reported failures.
    # With autocommit, each test passes or fails on its own merits.
    with psycopg.connect(os.environ["DATA_DB_URL"], autocommit=True) as connection:
        # "yield" hands the connection to the tests. The code after yield
        # (here, the automatic close) runs once all tests are finished.
        yield connection


# ---------------------------------------------------------------------------
# TEST 1: ALL 20 TABLES EXIST
# ---------------------------------------------------------------------------
def test_all_20_tables_exist(conn):
    # pg_tables lists ordinary tables. The 60 monthly transaction pieces are
    # ordinary tables too, but they all start with "transactions_", so we
    # filter them out and keep only the 20 main tables.
    #
    # The parent "transactions" table is a partitioned table, which pg_tables
    # does not list, so we collect it separately from pg_class, where
    # relkind 'p' means "partitioned table".
    rows = conn.execute(
        """
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND tablename NOT LIKE 'transactions\\_%'
        UNION
        SELECT relname FROM pg_class
        WHERE relkind = 'p' AND relnamespace = 'public'::regnamespace
        """
    ).fetchall()

    # fetchall() returns a list of one-item rows, e.g. [("schemes",), ...].
    # This turns it into a plain set of names, e.g. {"schemes", ...}.
    actual_tables = {row[0] for row in rows}

    # Comparing sets: the test fails if any table is missing OR if an
    # unexpected extra table exists. pytest prints the difference.
    assert actual_tables == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# TEST 2: transactions HAS 60 MONTHLY PIECES
# ---------------------------------------------------------------------------
def test_transactions_has_60_monthly_partitions(conn):
    # pg_inherits records which pieces belong to which parent table.
    # 'transactions'::regclass turns the table name into its internal ID.
    count = conn.execute(
        "SELECT count(*) FROM pg_inherits WHERE inhparent = 'transactions'::regclass"
    ).fetchone()[0]

    # Sep 2021 to Aug 2026 inclusive = 60 months.
    assert count == 60


# ---------------------------------------------------------------------------
# TEST 3: THE 3 HIDDEN LINKS HAVE NO FOREIGN KEY
# ---------------------------------------------------------------------------
# @pytest.mark.parametrize runs this ONE test function three times, once per
# (table, column) pair. Each run is reported separately, so if one hidden
# link was declared by mistake, pytest names exactly which one.
@pytest.mark.parametrize("table, column", HIDDEN_LINK_COLUMNS)
def test_hidden_link_has_no_foreign_key(conn, table, column):
    # pg_constraint lists every constraint. contype = 'f' means foreign key.
    # conkey holds the column numbers the constraint covers; joining to
    # pg_attribute turns those numbers into column names.
    #
    # %s marks where psycopg safely inserts the table and column values.
    # Never paste values straight into SQL text: %s prevents SQL injection.
    count = conn.execute(
        """
        SELECT count(*)
        FROM pg_constraint c
        JOIN pg_attribute a
          ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.contype = 'f'
          AND c.conrelid = %s::regclass
          AND a.attname = %s
        """,
        (table, column),
    ).fetchone()[0]

    # Zero foreign keys on this column means the link is still hidden.
    assert count == 0, f"{table}.{column} must NOT have a foreign key"


# ---------------------------------------------------------------------------
# TEST 4: THE DECLARED LINKS THE GRAPH NEEDS DO EXIST
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("from_table, from_column, to_table", DECLARED_LINKS)
def test_declared_link_exists(conn, from_table, from_column, to_table):
    # Same query as test 3, with one extra condition: confrelid is the table
    # the foreign key points TO. So this checks that the link exists AND
    # points to the right table.
    count = conn.execute(
        """
        SELECT count(*)
        FROM pg_constraint c
        JOIN pg_attribute a
          ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.contype = 'f'
          AND c.conrelid = %s::regclass
          AND a.attname = %s
          AND c.confrelid = %s::regclass
        """,
        (from_table, from_column, to_table),
    ).fetchone()[0]

    assert count == 1, f"{from_table}.{from_column} must point to {to_table}"