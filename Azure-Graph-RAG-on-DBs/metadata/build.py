"""
metadata/build.py
=================

WHAT THIS FILE IS FOR
---------------------
Step 1 of 3 of the metadata pipeline. Reads the fund database, finds the
links between tables, and drafts notes. Everything it writes is UNAPPROVED.

    uv run python -m metadata.build              # draft only new or changed tables
    uv run python -m metadata.build --redraft    # redraft every table

THE THREE STEPS OF STAGE 5
--------------------------
    1. metadata.build    -> read catalog + stats, find links, draft notes      (this file)
    2. metadata.review   -> a person reads everything and approves it
    3. metadata.publish  -> approved notes -> pgvector, approved links -> AGE graph

WHERE THINGS GO
---------------
Reads from:  DATA_DB_URL  (mf_data)    -- read only, never written
Writes to:   AGENT_DB_URL (agent_meta) -- meta_tables, meta_columns, meta_edges
"""

# argparse: reads command-line options such as --redraft.
import argparse

# os: reads DATA_DB_URL and AGENT_DB_URL.
import os

# Path: builds the path to the config/ folder.
from pathlib import Path

# yaml: reads the config files.
import yaml

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# Reads the fund database's catalog and statistics.
from adapters.postgres import PostgresAdapter

# Finds declared and undeclared links between tables.
from metadata import links as link_finder

# Drafts table/column notes with the LLM.
from metadata import notes as note_writer

# Connects to agent_meta and creates its tables.
from metadata import store

# metadata/build.py -> project folder -> config/.
CONFIG = Path(__file__).resolve().parents[1] / "config"


# Read one YAML file from config/ (also used by publish.py).
def load_yaml(name: str) -> dict:
    # Open config/<name> (closed automatically afterwards).
    with open(CONFIG / name) as f:
        # "or {}" returns an empty dict if the file is empty.
        return yaml.safe_load(f) or {}


# Which (table, column) pairs hold personal data.
def load_pii() -> set[tuple[str, str]]:
    """config/pii.yaml as a set of (table, column) pairs, for quick lookups."""
    # table -> list of PII columns.
    listed = load_yaml("pii.yaml").get("pii_columns", {})
    # Flatten to a set of (table, column) pairs.
    return {(table, column) for table, columns in listed.items() for column in columns}


# The whole build, start to finish.
def main() -> None:
    # Describe the command-line options (shown by --help).
    parser = argparse.ArgumentParser(description="Read the fund database, find links, draft notes.")
    # --redraft is a simple on/off flag (store_true: True when given).
    parser.add_argument("--redraft", action="store_true", help="redraft notes for every table")
    # Read the options actually typed.
    args = parser.parse_args()

    # Read .env.
    load_dotenv()
    # The pipeline's rules (config/metadata.yaml).
    settings = load_yaml("metadata.yaml")
    # The PII columns.
    pii = load_pii()
    # The read-only gateway to mf_data.
    adapter = PostgresAdapter(os.environ["DATA_DB_URL"])

    # ---- 1. Read what the fund database looks like ---------------------
    print("Reading catalog and statistics from the fund database ...")
    # Tables, columns, keys and declared foreign keys.
    catalog = adapter.read_catalog()
    # Null rates, distinct counts and common values per column.
    stats = adapter.read_column_stats(catalog)
    # Report what was found.
    print(f"  {len(catalog.tables)} tables, {len(catalog.foreign_keys)} declared foreign keys")

    # ---- 2. Find links ---------------------------------------------------
    print("Finding links between tables ...")
    # All links: declared ones plus the undeclared ones it could prove from the data.
    found = link_finder.discover(adapter, catalog, stats, settings["link_discovery"])
    # Just the undeclared ones.
    inferred = [lk for lk in found if lk.source != "declared"]
    # Report how many links of each kind.
    print(f"  {len(found) - len(inferred)} declared, {len(inferred)} undeclared links proposed")

    # Connect to agent_meta ...
    conn = store.connect(os.environ["AGENT_DB_URL"])
    # ... and create its tables if they do not exist yet.
    store.ensure_schema(conn)

    # Save every link into meta_edges.
    for lk in found:
        # Declared links are approved on arrival. Proposed links keep any
        # decision a person already made (approved or rejected) on an
        # earlier run; only their confidence and evidence are refreshed.
        # INSERT a new link; ON CONFLICT (it already exists) only refresh confidence and evidence.
        conn.execute(
            # SQL: insert the link; if it already exists, only update confidence and evidence.
            """
            INSERT INTO meta_edges (from_table, from_column, to_table, to_column, source, confidence, evidence, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (from_table, from_column, to_table, to_column)
            DO UPDATE SET confidence = EXCLUDED.confidence, evidence = EXCLUDED.evidence
            """,
            # The 8 values for the 8 %s placeholders ...
            (lk.from_table, lk.from_column, lk.to_table, lk.to_column, lk.source, lk.confidence,
             # ... declared links are approved immediately; others start as proposed.
             lk.evidence, "approved" if lk.source == "declared" else "proposed"),
        )

    # ---- 3. Draft notes, only where needed -------------------------------
    # table -> fingerprint of the tables that already have a note.
    existing = dict(conn.execute("SELECT table_name, fingerprint FROM meta_tables WHERE description IS NOT NULL").fetchall())
    # Draft a table if --redraft was given, or it is new, or its columns changed.
    to_draft = [name for name, table in catalog.tables.items()
                # (the fingerprint changes when columns or types change)
                if args.redraft or existing.get(name) != note_writer.fingerprint(table)]
    # Report how many tables need new notes.
    print(f"Drafting notes for {len(to_draft)} of {len(catalog.tables)} tables "
          # (continued: how many are unchanged)
          f"({len(catalog.tables) - len(to_draft)} unchanged since the last run) ...")
    # Ask the LLM for notes: table name -> its JSON answer.
    drafted = note_writer.draft(adapter, catalog, stats, found, pii, to_draft, settings["notes"])

    # Save each drafted note.
    for name, answer in drafted.items():
        # The table's catalog entry.
        table = catalog.tables[name]
        # A new or changed table always goes back to 'draft': an approval
        # given to an older version of the table does not carry over.
        conn.execute(
            # SQL: insert the note as 'draft', or replace an existing one and reset it to 'draft'.
            """
            INSERT INTO meta_tables (table_name, fingerprint, row_estimate, description, grain,
                                     business_terms, status, drafted_at, approved_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'draft', now(), NULL)
            ON CONFLICT (table_name) DO UPDATE SET
                fingerprint = EXCLUDED.fingerprint, row_estimate = EXCLUDED.row_estimate,
                description = EXCLUDED.description, grain = EXCLUDED.grain,
                business_terms = EXCLUDED.business_terms, status = 'draft',
                drafted_at = now(), approved_at = NULL
            """,
            # The values: name, fingerprint, row estimate, description ...
            (name, note_writer.fingerprint(table), table.row_estimate, answer.get("description"),
             # ... grain and business terms.
             answer.get("grain"), answer.get("business_terms", [])),
        )
        # Remove the table's old column notes ...
        conn.execute("DELETE FROM meta_columns WHERE table_name = %s", (name,))
        # ... and insert a fresh row for every column.
        for column in table.columns:
            # The column's statistics (None if Postgres has none).
            s = stats.get((name, column.name))
            # One row per column: its type, whether it is PII, its distinct count and the drafted note.
            conn.execute(
                # SQL: one column note.
                """INSERT INTO meta_columns (table_name, column_name, data_type, is_pii, distinct_values, description)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                # The values: table, column, type, is it PII ...
                (name, column.name, column.data_type, (name, column.name) in pii,
                 # ... distinct count (if known) and the drafted description.
                 s.distinct_values if s else None, answer["columns"].get(column.name)),
            )
    # Close the agent_meta connection.
    conn.close()
    # Tell the user the next step.
    print("Done. Next: uv run python -m metadata.review")


# True only when run as: uv run python -m metadata.build
if __name__ == "__main__":
    # Start the build.
    main()