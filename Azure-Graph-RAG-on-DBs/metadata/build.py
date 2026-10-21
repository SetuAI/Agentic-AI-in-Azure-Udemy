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

import argparse
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from adapters.postgres import PostgresAdapter
from metadata import links as link_finder
from metadata import notes as note_writer
from metadata import store

CONFIG = Path(__file__).resolve().parents[1] / "config"


def load_yaml(name: str) -> dict:
    with open(CONFIG / name) as f:
        return yaml.safe_load(f) or {}


def load_pii() -> set[tuple[str, str]]:
    """config/pii.yaml as a set of (table, column) pairs, for quick lookups."""
    listed = load_yaml("pii.yaml").get("pii_columns", {})
    return {(table, column) for table, columns in listed.items() for column in columns}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read the fund database, find links, draft notes.")
    parser.add_argument("--redraft", action="store_true", help="redraft notes for every table")
    args = parser.parse_args()

    load_dotenv()
    settings = load_yaml("metadata.yaml")
    pii = load_pii()
    adapter = PostgresAdapter(os.environ["DATA_DB_URL"])

    # ---- 1. Read what the fund database looks like ---------------------
    print("Reading catalog and statistics from the fund database ...")
    catalog = adapter.read_catalog()
    stats = adapter.read_column_stats(catalog)
    print(f"  {len(catalog.tables)} tables, {len(catalog.foreign_keys)} declared foreign keys")

    # ---- 2. Find links ---------------------------------------------------
    print("Finding links between tables ...")
    found = link_finder.discover(adapter, catalog, stats, settings["link_discovery"])
    inferred = [lk for lk in found if lk.source != "declared"]
    print(f"  {len(found) - len(inferred)} declared, {len(inferred)} undeclared links proposed")

    conn = store.connect(os.environ["AGENT_DB_URL"])
    store.ensure_schema(conn)

    for lk in found:
        # Declared links are approved on arrival. Proposed links keep any
        # decision a person already made (approved or rejected) on an
        # earlier run; only their confidence and evidence are refreshed.
        conn.execute(
            """
            INSERT INTO meta_edges (from_table, from_column, to_table, to_column, source, confidence, evidence, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (from_table, from_column, to_table, to_column)
            DO UPDATE SET confidence = EXCLUDED.confidence, evidence = EXCLUDED.evidence
            """,
            (lk.from_table, lk.from_column, lk.to_table, lk.to_column, lk.source, lk.confidence,
             lk.evidence, "approved" if lk.source == "declared" else "proposed"),
        )

    # ---- 3. Draft notes, only where needed -------------------------------
    existing = dict(conn.execute("SELECT table_name, fingerprint FROM meta_tables WHERE description IS NOT NULL").fetchall())
    to_draft = [name for name, table in catalog.tables.items()
                if args.redraft or existing.get(name) != note_writer.fingerprint(table)]
    print(f"Drafting notes for {len(to_draft)} of {len(catalog.tables)} tables "
          f"({len(catalog.tables) - len(to_draft)} unchanged since the last run) ...")
    drafted = note_writer.draft(adapter, catalog, stats, found, pii, to_draft, settings["notes"])

    for name, answer in drafted.items():
        table = catalog.tables[name]
        # A new or changed table always goes back to 'draft': an approval
        # given to an older version of the table does not carry over.
        conn.execute(
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
            (name, note_writer.fingerprint(table), table.row_estimate, answer.get("description"),
             answer.get("grain"), answer.get("business_terms", [])),
        )
        conn.execute("DELETE FROM meta_columns WHERE table_name = %s", (name,))
        for column in table.columns:
            s = stats.get((name, column.name))
            conn.execute(
                """INSERT INTO meta_columns (table_name, column_name, data_type, is_pii, distinct_values, description)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (name, column.name, column.data_type, (name, column.name) in pii,
                 s.distinct_values if s else None, answer["columns"].get(column.name)),
            )
    conn.close()
    print("Done. Next: uv run python -m metadata.review")


if __name__ == "__main__":
    main()