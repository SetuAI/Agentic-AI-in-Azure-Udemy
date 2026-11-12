##############################################################################
# dataplane/schema/apply_schema.py
#
# PURPOSE:
#   Creates the 20 empty tables of the fund database.
#   It runs the four .sql files sitting next to this script, in order,
#   against the mf_data database on Azure. Nothing else. No data is
#   inserted — that is the generator's job, in the next stage.
#
#   This is the SECOND thing you run in the whole project, straight after
#   scripts/pg_setup.py has created the two databases.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   The schema is 20 tables spread across four files, and those files MUST
#   run in a fixed order, because later files point at tables created by
#   earlier ones. File 02 creates portfolio_holdings, which references
#   schemes — and schemes is created in file 01.
#
#   Run them by hand and three things go wrong. You run them in the wrong
#   order and get "relation schemes does not exist". You forget one and the
#   generator fails later with a confusing error about a missing table. Or
#   one file fails halfway, leaving the database holding half a schema with
#   no obvious sign that anything is wrong.
#
#   This script removes all three. The order is decided by the file names,
#   every file is accounted for before anything runs, and all four run
#   inside ONE transaction — so the outcome is either all 20 tables or none.
#
# WHAT RUNS, IN WHAT ORDER:
#
#   01_fund_house.sql   ──>   9 tables    the fund company
#   02_market.sql       ──>   6 tables    the securities it invests in
#   03_investors.sql    ──>   4 tables    the people who buy the funds
#   04_transactions.sql ──>   1 table     every rupee that moved
#   │                         │           (split into 60 monthly pieces)
#   │                         │
#   the order comes from      9 + 6 + 4 + 1 = 20 tables
#   the 01_ 02_ 03_ 04_
#   prefixes, sorted
#
# WHAT YOU SEE WHEN IT WORKS:
#   Running 01_fund_house.sql ...
#   Running 02_market.sql ...
#   Running 03_investors.sql ...
#   Running 04_transactions.sql ...
#   Schema applied: all 20 tables created in mf_data.
#
# IS IT SAFE TO RUN MORE THAN ONCE:
#   Yes. Each .sql file begins by DROPPING its own tables, so every run
#   rebuilds the schema from empty.
#
#   But read that again, because it has a consequence: every run also
#   DELETES any data already loaded into mf_data. That is acceptable here
#   only because mf_data holds nothing but synthetic data, and the
#   generator can rebuild all 1.1 million rows in about 100 seconds.
#
#   On a client's real database, a script that silently drops tables would
#   be unacceptable. The reason it is fine here is worth saying out loud,
#   rather than letting anyone assume this is a safe pattern in general.
#
# HOW TO RUN:
#   From the project folder, with .env filled in and
#   tests/test_stage0_setup.py passing:
#
#       uv run python dataplane/schema/apply_schema.py
#
#   Then prove it worked:
#
#       uv run pytest tests/test_stage1_schema.py -v        -> 10 passed
##############################################################################


import os
# os is Python's built-in way of talking to the operating system.
# We use exactly one thing from it: os.environ, which is the dictionary of
# environment variables — the settings that live outside the code.
# Here that is DATA_DB_URL, the address of the mf_data database.
#
# Why the address is not written in this file: it contains a password.
# Anything written in a .py file ends up in git, and a password in git is
# a password that has leaked. Reading it from the environment keeps it out.

from pathlib import Path
# Path is Python's modern way of handling file and folder locations.
#
# The older approach is to join folder names with strings and slashes:
#   "dataplane" + "/" + "schema" + "/" + "01_fund_house.sql"
# That breaks on Windows, where the separator is a backslash. Path handles
# the difference itself, so the same code runs on macOS, Linux and Windows.
#
# We use it for one job here: finding the four .sql files that sit in the
# same folder as this script.

import psycopg
# psycopg is the PostgreSQL driver — the library that lets Python open a
# connection to a Postgres database and send it SQL.
#
# It is version 3 of the library, despite the name having no number.
# Version 2 was called psycopg2, and you will see that name in older code
# and in older answers online. They are not interchangeable: psycopg2 has
# no autocommit keyword on connect(), and its connection objects do not
# have an .execute() method. Code copied from a psycopg2 example into this
# project will fail in ways that look unrelated to the library version.
#
# pyproject.toml installs it as psycopg[binary], which bundles the Postgres
# client libraries. That is why this project needs no local PostgreSQL
# installation at all.

from dotenv import load_dotenv
# python-dotenv reads a .env file and copies every line in it into
# os.environ, so that os.environ["DATA_DB_URL"] finds the value.
#
# Why a file rather than typing the settings into the terminal each time:
# a .env file is listed in .gitignore, so it is never committed, and it is
# read automatically every run. Nobody has to remember to export anything.
#
# Note that load_dotenv() never OVERWRITES a variable that is already set.
# So if DATA_DB_URL exists in the real environment — which is how it works
# on Azure App Service — that value wins and .env is ignored. The same code
# works on a laptop and in the cloud with no change.


# ── WHERE THE SQL FILES ARE ──────────────────────────────────────────────

SCHEMA_FOLDER = Path(__file__).parent
# The folder this script is sitting in, which is also where the four .sql
# files live.
#
# Reading it right to left:
#   __file__   a Python built-in: the full path of THIS file
#   Path(...)  turns that string into a Path object
#   .parent    the folder containing it, i.e. dataplane/schema/
#
# WHY NOT JUST WRITE "dataplane/schema": because a relative path like that
# is resolved against the folder your TERMINAL is in, not the folder this
# script is in. It would work when run from the project root:
#
#     uv run python dataplane/schema/apply_schema.py      works
#
# and fail the moment anyone runs it from somewhere else:
#
#     cd dataplane/schema && uv run python apply_schema.py    breaks
#
# Anchoring to __file__ means the script finds its own files no matter
# where it is launched from.


def main() -> None:
    """
    Run every numbered .sql file in this folder, in name order, inside one
    transaction.

    Takes no arguments and returns nothing. Everything it needs comes from
    .env, and its only output is the tables it creates plus the lines it
    prints.

    Raises:
        SystemExit: if there are not exactly four .sql files. Stopping here
                    with a clear message is better than building a partial
                    schema and failing three stages later.
        KeyError:   if DATA_DB_URL is missing from .env.
        psycopg.OperationalError: if the database cannot be reached — wrong
                    password, server stopped, or your IP not on the Azure
                    firewall.
    """

    load_dotenv()
    # Read .env into os.environ. This must happen BEFORE the next line,
    # because the next line reads os.environ.
    #
    # Called with no arguments, it searches for a file named .env starting
    # in the current folder and walking upwards. So it finds the project
    # root's .env whether you run from the root or from a subfolder.

    database_url = os.environ["DATA_DB_URL"]
    # The connection address of the mf_data database, for example:
    #
    #   postgresql://fundgraph_admin:password@fundgraphdbexample1.postgres.database.azure.com:5432/mf_data?sslmode=require
    #   │            │               │        │                                                   │    │       │
    #   driver       user            password server                                              port database settings
    #
    # SQUARE BRACKETS, NOT .get(). This matters.
    #
    #   os.environ["DATA_DB_URL"]        raises KeyError if it is missing
    #   os.environ.get("DATA_DB_URL")    returns None if it is missing
    #
    # With .get(), a missing setting would pass None onwards, and psycopg
    # would fail several lines later with a vague message about an invalid
    # connection string. With square brackets, Python stops right here and
    # names the exact setting that is missing:
    #
    #   KeyError: 'DATA_DB_URL'
    #
    # Fail at the point where the cause is obvious, not where the symptom
    # appears.

    sql_files = sorted(SCHEMA_FOLDER.glob("0*.sql"))
    # The four .sql files, in the order they must run.
    #
    # Reading inside out:
    #   .glob("0*.sql")  every file in that folder whose name starts with
    #                    "0" and ends with ".sql". The * matches anything
    #                    in between. So: 01_fund_house.sql through
    #                    04_transactions.sql.
    #   sorted(...)      puts them in alphabetical order, which for these
    #                    names is 01, 02, 03, 04.
    #
    # THE FILE NAMES ARE THE DEPENDENCY ORDER, and that is the whole design.
    # Nothing in this script knows that fund_house must run before market.
    # It only knows how to sort. The 01_, 02_, 03_, 04_ prefixes carry that
    # knowledge, and they carry it somewhere a human can see it — in the
    # file listing — rather than buried in a list inside the code.
    #
    # The consequence to be aware of: adding a fifth schema file means
    # naming it 05_ and nothing else. Name it market_extra.sql and glob
    # will not match it at all, and the script will stop at the count check
    # below rather than silently skipping it.
    #
    # Note glob() does not promise any particular order on its own, which
    # is exactly why sorted() is wrapped around it. Without sorted(), the
    # order would depend on the filesystem, and the script would work on
    # one machine and fail on another.

    if len(sql_files) != 4:
        # Exactly four, no more and no fewer.
        #
        # This catches a broken checkout, a file renamed by accident, or
        # someone running the script from the wrong folder entirely.
        #
        # WITHOUT THIS CHECK, zero files found would mean the loop below
        # runs zero times, the final line prints "all 20 tables created",
        # and the script exits successfully having done nothing at all.
        # The failure would then surface two stages later, as the generator
        # complaining about a missing table — and nobody would connect that
        # back to this script, which reported success.
        #
        # A check that turns a silent success into a loud failure is worth
        # four lines.
        raise SystemExit(
            f"Expected 4 schema files in {SCHEMA_FOLDER}, found {len(sql_files)}."
        )
        # SystemExit, not an ordinary exception.
        #
        # raise SystemExit("message") prints the message and exits with a
        # non-zero status code, with no Python traceback. For a problem the
        # user caused and the user can fix, a traceback is noise — it shows
        # them 15 lines of internals above the one line that matters.
        #
        # The non-zero exit code also matters: a shell script or CI job
        # running this can tell that it failed.
        #
        # The f at the front of the string makes it an f-string, so
        # {SCHEMA_FOLDER} and {len(sql_files)} are replaced by their values.
        # Naming both in the message is deliberate — it tells the user
        # which folder was searched and what was found there, which is
        # usually enough to see the problem without asking anything else.

    with psycopg.connect(database_url) as conn:
        # Open ONE connection and run all four files through it.
        #
        # THIS LINE IS THE MOST IMPORTANT ONE IN THE FILE, and the reason
        # is in the word "with".
        #
        # psycopg's connection works as a context manager, which means the
        # "with" block has a defined outcome at both ends:
        #
        #   block finishes with no error   ->  COMMIT   (everything saved)
        #   anything raises inside it      ->  ROLLBACK (everything undone)
        #
        # So all four files are ONE transaction. If 04_transactions.sql
        # fails on its last line, the tables created by files 01, 02 and 03
        # are rolled back too, and the database is left exactly as it was
        # before the script started.
        #
        # WHY THAT IS WORTH ARRANGING: the alternative is a database
        # holding 19 of 20 tables, with nothing to indicate which one is
        # missing or that anything is wrong. The generator would then fail
        # with "relation transactions does not exist", and the obvious next
        # move — re-run the generator — would fail the same way forever.
        #
        # All-or-nothing means there is only ever one thing to do after a
        # failure: fix the SQL and run this script again.
        #
        # And note that PostgreSQL supports this for schema changes. CREATE
        # TABLE and DROP TABLE can be rolled back, which is not true of
        # every database engine — in MySQL, each CREATE TABLE commits
        # immediately and this guarantee would not exist.
        #
        # One thing this is NOT: autocommit. scripts/pg_setup.py passes
        # autocommit=True because CREATE DATABASE cannot run inside a
        # transaction. CREATE TABLE has no such restriction, so here we
        # want the transaction, and we leave autocommit off.

        for sql_file in sql_files:
            # One pass per file, in sorted order: 01, then 02, then 03,
            # then 04. Four iterations.

            print(f"Running {sql_file.name} ...")
            # Print the name BEFORE running it, not after.
            #
            # That ordering is the point. If a file hangs or fails, its
            # name is already on screen, so the last line printed names the
            # file that caused the problem. Printing after each file
            # finished would mean a failure shows the name of the file that
            # SUCCEEDED, and the user would go and debug the wrong one.
            #
            # .name gives just the file name — "01_fund_house.sql" — rather
            # than the full path, which would be long and add nothing.

            sql_text = sql_file.read_text()
            # Read the whole file into a single string.
            #
            # These files are a few hundred lines each, so reading them
            # whole costs nothing. For a very large file this would be
            # wasteful, but "very large" here would mean hundreds of
            # megabytes of SQL, which a schema file never is.

            conn.execute(sql_text)
            # Send the entire file to PostgreSQL in one call.
            #
            # Each of these files contains MANY statements — DROP TABLE,
            # CREATE TABLE, CREATE INDEX, and in file 04 a PL/pgSQL loop
            # that creates 60 partitions. All of it goes across in one go.
            #
            # THE CONDITION THAT MAKES THIS LEGAL: psycopg allows multiple
            # statements in one execute() only when NO PARAMETERS are
            # passed. The moment you write
            #
            #     conn.execute(sql_text, (some_value,))
            #
            # psycopg switches to a prepared statement, and a prepared
            # statement must be exactly one statement. It would then fail
            # with "cannot insert multiple commands into a prepared
            # statement" — an error whose wording gives no hint that the
            # cause was adding a parameter.
            #
            # We pass no parameters here, and we never will: this is a
            # fixed schema file, not a query with values in it. Worth
            # knowing all the same, because it is a real trap and the error
            # message does not help.

    # The "with" block has ended here. Because nothing raised inside it,
    # psycopg has COMMITTED, and the connection is closed. All 20 tables
    # now exist in mf_data.

    print("Schema applied: all 20 tables created in mf_data.")
    # Reaching this line is itself the proof that the commit succeeded —
    # any failure inside the block would have raised and skipped it.
    #
    # The message says what is now true, not what the script did. "Schema
    # applied: all 20 tables created" tells the user the state of their
    # database. "Finished running 4 files" would tell them about the
    # script's activity, which is less useful.


# ── ENTRY POINT ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
    # __name__ is a variable Python sets in every module. Its value depends
    # on HOW the file was loaded:
    #
    #   run directly   ->  __name__ == "__main__"      so main() is called
    #   imported       ->  __name__ == "dataplane.schema.apply_schema"
    #                                                  so main() is NOT called
    #
    # WHY THAT MATTERS HERE: it means another file — a test, or a future
    # orchestration script — can do
    #
    #     from dataplane.schema.apply_schema import main
    #
    # to get hold of the function WITHOUT the schema being dropped and
    # rebuilt as a side effect of the import.
    #
    # Without this guard, merely importing this module would wipe the
    # database. That is the kind of side effect that costs somebody an
    # afternoon and is impossible to guess from the import line.