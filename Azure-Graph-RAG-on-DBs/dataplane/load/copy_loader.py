"""
dataplane/load/copy_loader.py
=============================

WHAT THIS FILE IS FOR
---------------------
Takes the generated tables (polars DataFrames held in memory) and loads
them into the mf_data database.

WHY COPY, AND NOT INSERT
------------------------
INSERT sends rows one statement at a time. At 800,000 transactions that is
very slow. COPY streams a whole table as one block of CSV text, and
Postgres loads it in a single pass, typically 50 to 100 times faster.

WHY THE ORDER MATTERS
---------------------
Foreign keys make Postgres check, for every row, that what it points to
already exists. So a table can only be loaded after the tables it points
to. LOAD_ORDER below lists all 20 tables in a safe order.
"""

# "io" gives an in-memory file (BytesIO), so the CSV never touches the disk.
import io

# polars: the generated tables are polars DataFrames.
import polars as pl

# psycopg: the PostgreSQL driver that runs COPY.
import psycopg

# Every table appears after all the tables it points to.
LOAD_ORDER = [
    # Fund-house tables (file 01) ...
    "scheme_categories", "benchmarks", "benchmark_values", "schemes", "scheme_plans",
    # ... continued ...
    "fund_managers", "scheme_manager_assignments", "nav_history",
    # Market tables (file 02, except holdings) ...
    "sectors", "issuer_groups", "issuers", "securities", "credit_rating_history",
    # Investor tables (file 03) ...
    "distributors", "investors", "folios", "sip_registrations",
    # ... then transactions and the two summary tables, which point to the tables above.
    "transactions", "scheme_aum_monthly", "portfolio_holdings",
]


# Called by run.py in step 6 with DATA_DB_URL and the dict of 20 tables.
def load_all(database_url: str, tables: dict[str, pl.DataFrame]) -> None:
    """Empty all 20 tables, then load each DataFrame into its table."""

    # Guard: every table must have been generated before we delete anything.
    # Table names we expect but did not receive (set difference).
    missing = set(LOAD_ORDER) - set(tables)
    # An empty set counts as False, so this runs only if something is missing.
    if missing:
        # Stop with an error that lists the missing tables.
        raise ValueError(f"Not generated: {sorted(missing)}")

    # One connection, one transaction: if any table fails to load, the
    # whole load is undone and the database keeps its previous contents.
    with psycopg.connect(database_url) as conn:
        # TRUNCATE empties tables instantly. CASCADE is needed because the
        # tables point to each other; listing all 20 empties them together.
        conn.execute("TRUNCATE " + ", ".join(LOAD_ORDER) + " CASCADE")

        # Load the tables one by one, in the safe order.
        for name in LOAD_ORDER:
            # The DataFrame for this table.
            frame = tables[name]
            # Progress line. {name:<28} pads the name to 28 characters; {...:>10,} right-aligns the count.
            print(f"  loading {name:<28} {len(frame):>10,} rows")

            # Write the DataFrame as CSV text into memory. Empty values
            # (None) become empty fields, which COPY reads as NULL.
            # An empty in-memory file.
            buffer = io.BytesIO()
            # Write the table into it as CSV, with a header line of column names.
            frame.write_csv(buffer, include_header=True)

            # Name the columns explicitly, so the CSV columns always match
            # the table columns, whatever order the DataFrame is in.
            # e.g. "scheme_id, scheme_code, scheme_name, ..."
            columns = ", ".join(frame.columns)
            # FROM STDIN = the data comes from this program; HEADER true = skip the first line.
            copy_sql = f"COPY {name} ({columns}) FROM STDIN WITH (FORMAT csv, HEADER true)"
            # Open a cursor and start the COPY; both close automatically at the end of the block.
            with conn.cursor() as cur, cur.copy(copy_sql) as copy:
                # Send all the CSV bytes to Postgres.
                copy.write(buffer.getvalue())

        # Refresh Postgres's statistics about each table (row counts, value
        # spreads). The query planner uses them to choose fast query plans,
        # and Stage 5 reads them to learn about each column.
        conn.execute("ANALYZE")