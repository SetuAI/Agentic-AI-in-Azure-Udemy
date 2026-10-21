"""
dataplane/schema/apply_schema.py
================================

WHAT THIS FILE IS FOR
---------------------
Runs the four schema files, in order, against the mf_data database:

    01_fund_house.sql    -> 9 tables
    02_market.sql        -> 6 tables
    03_investors.sql     -> 4 tables
    04_transactions.sql  -> 1 table, split into 60 monthly pieces

After it finishes, mf_data contains all 20 tables, empty and ready for the
data generator.

WHY A PYTHON SCRIPT, AND NOT A TERMINAL COMMAND
-----------------------------------------------
We could run each file with psql, but that needs psql installed on the Mac
and the username and password typed into the command. This script reads the
connection address from .env (DATA_DB_URL), which is already set up and
already tested in Stage 0. The same script works unchanged on any machine
that has a correct .env.

WHY THE ORDER MATTERS
---------------------
Later files point to tables created by earlier ones. For example, file 02's
portfolio_holdings points to schemes, created in file 01. If file 02 ran
first, Postgres would stop with "relation schemes does not exist".
The files are named 01_, 02_, ... so that sorting them by name gives the
correct order.

IS IT SAFE TO RUN MORE THAN ONCE?
---------------------------------
Yes. Each SQL file starts by dropping its own tables, so every run rebuilds
the schema from empty. That also means every run DELETES any data already
loaded into mf_data. That is fine here, because mf_data holds only synthetic
data, and the generator can recreate it in minutes.

HOW TO RUN
----------
From the project folder, with the Docker Postgres running:

    uv run python dataplane/schema/apply_schema.py
"""

# ---------------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------------

# "os" reads settings (environment variables), here DATA_DB_URL.
import os

# "Path" handles file and folder paths in a way that works on any operating
# system. We use it to find the .sql files sitting next to this script.
from pathlib import Path

# "psycopg" is the Postgres driver installed in Stage 0. It opens the
# connection to the database and sends it SQL to run.
import psycopg

# Reads the .env file so that os.environ contains DATA_DB_URL.
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# __file__ is the path of THIS script. .parent is the folder it sits in,
# dataplane/schema/. The SQL files are in that same folder.
#
# Finding them relative to this script (rather than relative to wherever
# the terminal happens to be) means the script works no matter which folder
# you run it from.
SCHEMA_FOLDER = Path(__file__).parent


def main() -> None:
    """Run every numbered .sql file in this folder, in name order."""

    # Load DATA_DB_URL (and everything else) from .env.
    load_dotenv()

    # The address of the mf_data database, e.g.
    #   postgresql://user:password@localhost:5433/mf_data
    # Square brackets: if the setting is missing, Python stops here with a
    # clear KeyError naming DATA_DB_URL, instead of failing later with a
    # vaguer connection error.
    database_url = os.environ["DATA_DB_URL"]

    # Find the SQL files.
    #   glob("0*.sql") -> every file whose name starts with "0" and ends with
    #                     ".sql", i.e. 01_... to 04_...
    #   sorted(...)    -> put them in name order, which is the correct run order.
    sql_files = sorted(SCHEMA_FOLDER.glob("0*.sql"))

    # Guard against running from a broken checkout: if the files are
    # missing, stop with a clear message instead of "succeeding" at nothing.
    if len(sql_files) != 4:
        raise SystemExit(
            f"Expected 4 schema files in {SCHEMA_FOLDER}, found {len(sql_files)}."
        )

    # Open ONE connection and run all four files through it.
    #
    # "with ... as conn" means: when the block below finishes without an
    # error, psycopg COMMITS (saves) everything. If any file fails, it
    # ROLLS BACK (undoes) everything, and the database is left exactly as
    # it was before the run. We never end up with half a schema.
    with psycopg.connect(database_url) as conn:
        for sql_file in sql_files:
            # Show progress, so a slow or failing file is easy to spot.
            print(f"Running {sql_file.name} ...")

            # read_text() loads the whole file as one string of SQL.
            sql_text = sql_file.read_text()

            # Send the whole file to Postgres in one go.
            # psycopg allows several SQL statements in one execute() call
            # as long as no parameters are passed, which is the case here.
            conn.execute(sql_text)

    # Reaching this line means the "with" block committed successfully.
    print("Schema applied: all 20 tables created in mf_data.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
# This is true only when the file is run directly
# ("uv run python dataplane/schema/apply_schema.py"), not when another file
# imports it. That lets tests import main() later without running it.
if __name__ == "__main__":
    main()