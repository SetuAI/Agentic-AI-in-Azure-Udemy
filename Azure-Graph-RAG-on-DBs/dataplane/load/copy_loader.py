##############################################################################
# dataplane/load/copy_loader.py
#
# PURPOSE:
#   Takes the 20 generated tables — polars DataFrames sitting in memory — and
#   writes them into the mf_data database.
#
#   This is the last step of the data plane. Everything before it produced
#   data in memory; nothing was in PostgreSQL until this file runs.
#
#   The whole file is one function, load_all(), and it does four things:
#     1. check every expected table was generated
#     2. empty all 20 tables
#     3. write each one with PostgreSQL's COPY, in dependency order
#     4. run ANALYZE so PostgreSQL's statistics are fresh
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Writing 1.13 million rows into PostgreSQL naively takes a very long time
#   and can leave the database in a half-loaded state. Three separate
#   problems, and this file solves all three:
#
#     SPEED        INSERT sends one statement per row. COPY streams a whole
#                  table as one block. 50 to 100 times faster.
#     ORDER        foreign keys mean a row can only be inserted after the
#                  rows it points at. LOAD_ORDER encodes that.
#     ALL-OR-NOTHING  one transaction, so a failure on table 19 leaves the
#                  database exactly as it was.
#
# WHY COPY, AND NOT INSERT — THE NUMBERS:
#   An INSERT is a round trip: send the statement, PostgreSQL parses it,
#   plans it, executes it, replies. At 900,000 transaction rows that is
#   900,000 round trips, and on a connection to Azure with 20 milliseconds of
#   latency it would take five hours.
#
#   COPY sends the entire table as one stream of CSV text and PostgreSQL
#   loads it in a single pass, with no per-row parsing or planning. The same
#   900,000 rows take a few seconds.
#
#   The trade-off: COPY takes no parameters and does no per-row error
#   reporting. One bad value anywhere fails the whole table, and the error
#   names a line number rather than a row id. For a bulk load of
#   machine-generated data that is the right trade; for user input it would
#   not be.
#
# WHAT YOU SEE WHEN IT WORKS:
#     loading scheme_categories                   36 rows
#     loading benchmarks                          15 rows
#     ...
#     loading transactions                   903,112 rows
#     loading scheme_aum_monthly               2,400 rows
#     loading portfolio_holdings             144,000 rows
#
# HOW TO USE:
#   Never run directly — it has no main(). run.py calls it:
#
#       from dataplane.load.copy_loader import load_all
#       load_all(os.environ["DATA_DB_URL"], tables)
##############################################################################


import io
# Python's in-memory byte and text streams.
#
# We use io.BytesIO, which behaves exactly like a file but lives in memory.
# polars writes CSV into it, and psycopg reads the bytes back out.
#
# WHY NOT A REAL TEMPORARY FILE: writing to disk and reading back would be
# two unnecessary trips through the filesystem, and would leave files behind
# if the process died mid-load. The largest table is a few hundred megabytes
# of CSV at this scale, which fits comfortably in memory.

import polars as pl
# Imported only for the TYPE ANNOTATION on the tables argument —
# dict[str, pl.DataFrame]. The code calls .write_csv(), len() and .columns on
# the frames, which needs no import.
#
# A type annotation that requires an import is still worth it: it tells a
# reader exactly what the dictionary holds without them having to go and
# look at run.py.

import psycopg
# The PostgreSQL driver. This file uses the one feature that makes it worth
# choosing over alternatives: cursor.copy(), which gives a Python-side stream
# straight into PostgreSQL's COPY protocol.


LOAD_ORDER = [
    "scheme_categories", "benchmarks", "benchmark_values", "schemes", "scheme_plans",
    "fund_managers", "scheme_manager_assignments", "nav_history",
    "sectors", "issuer_groups", "issuers", "securities", "credit_rating_history",
    "distributors", "investors", "folios", "sip_registrations",
    "transactions", "scheme_aum_monthly", "portfolio_holdings",
]
# ALL 20 TABLES, IN AN ORDER WHERE EVERY TABLE COMES AFTER THE TABLES IT
# POINTS AT. This list is the most important thing in the file.
#
# WHY THE ORDER MATTERS AT ALL: a foreign key makes PostgreSQL check, for
# every single row, that the value it points at already exists. Load folios
# before investors and every row fails:
#
#     ERROR: insert or update on table "folios" violates foreign key
#            constraint "folios_investor_id_fkey"
#
# READING THE ORDER, GROUPED BY SOURCE FILE:
#
#   lines 1-2  the 8 fund-house tables, each after its own dependencies:
#              categories and benchmarks have none, so they go first;
#              schemes needs both; scheme_plans needs schemes;
#              nav_history needs scheme_plans.
#   line 3     the 5 market tables: sectors and groups first, then issuers,
#              then securities, then ratings.
#   line 4     the 4 investor tables: distributors and investors first
#              (independent), then folios, then SIPs.
#   line 5     the 3 computed tables last.
#
# THREE THINGS WORTH NOTICING:
#
#   transactions IS NEARLY LAST, because it points at both folios (file 03)
#   and scheme_plans (file 01). It cannot go until both groups are done.
#
#   scheme_aum_monthly COULD GO ANYWHERE, because its scheme_id is a HIDDEN
#   link with no foreign key — PostgreSQL would not check it. It is placed
#   with the computed tables because that is where it belongs logically, not
#   because it has to be. The same is true of credit_rating_history's sec_cd.
#
#   An undeclared link is therefore one less constraint on load order. Which
#   is, uncomfortably, one of the reasons a real team leaves one undeclared:
#   it makes their load script simpler. The cost arrives later, when nobody
#   can tell what joins to what.


def load_all(database_url: str, tables: dict[str, pl.DataFrame]) -> None:
    """
    Empty all 20 tables, then load each DataFrame into its table.

    Args:
        database_url: the mf_data connection string, passed in rather than
                      read from the environment. Keeps this function
                      testable against any database.
        tables:       table name -> polars DataFrame. Must contain every name
                      in LOAD_ORDER; extra entries are ignored.

    Returns:
        Nothing. Progress is printed as it goes.

    Raises:
        ValueError: if any expected table is missing — raised BEFORE
                    anything is deleted.
        psycopg.errors.UndefinedTable: if the schema was never applied.
        psycopg.errors.ForeignKeyViolation: if LOAD_ORDER is wrong, or the
                    generator produced a row pointing at something absent.
    """

    missing = set(LOAD_ORDER) - set(tables)
    if missing:
        raise ValueError(f"Not generated: {sorted(missing)}")
    # THE GUARD, AND ITS POSITION IS THE WHOLE POINT.
    #
    # Set subtraction: every name we expect, minus every name we were given.
    # Whatever is left was not generated.
    #
    # THIS RUNS BEFORE THE CONNECTION IS EVEN OPENED, and therefore before
    # the TRUNCATE below. If a builder failed to return something, we find
    # out while the database still holds its previous contents.
    #
    # Move this check to after the TRUNCATE and the failure mode becomes:
    # every table emptied, then an error, and a database containing nothing
    # at all. The transaction would roll it back — but only because the
    # TRUNCATE is inside the same transaction, which is a second line of
    # defence rather than a reason to skip the first.
    #
    # sorted(missing) so the message is deterministic. A set prints in
    # arbitrary order, which makes an error message that differs between runs
    # for no reason.
    #
    # Note the direction of the subtraction. It catches MISSING tables, not
    # EXTRA ones — a dictionary with 21 entries passes, and the 21st is
    # silently ignored. That is deliberate: the generator passes helper
    # frames around, and a stray extra key should not stop a load.

    with psycopg.connect(database_url) as conn:
        # ONE CONNECTION, ONE TRANSACTION, for all 20 tables.
        #
        # Same context-manager behaviour as apply_schema.py: the block
        # COMMITS on success and ROLLS BACK on any exception. So if table 19
        # fails, the TRUNCATE and the 18 successful loads are all undone, and
        # mf_data still holds whatever it held before.
        #
        # WHY THAT MATTERS MORE HERE THAN ANYWHERE ELSE: this function starts
        # by deleting everything. Without the transaction, a failure halfway
        # would leave a database that is neither the old data nor the new —
        # and the only way back would be to fix the bug and run the whole
        # 100-second generation again.

        conn.execute("TRUNCATE " + ", ".join(LOAD_ORDER) + " CASCADE")
        # EMPTY ALL 20 TABLES IN ONE STATEMENT.
        #
        # TRUNCATE versus DELETE, and the difference is large:
        #   DELETE FROM t    removes rows one at a time, writing an undo
        #                    record for each. On 900,000 rows, slow.
        #   TRUNCATE t       discards the table's storage wholesale.
        #                    Effectively instant whatever the row count.
        #
        # CASCADE IS REQUIRED HERE, and not for the reason it might seem.
        # PostgreSQL refuses to truncate a table that others reference,
        # because it cannot check afterwards that nothing is orphaned. Naming
        # all 20 tables in one statement means they are all emptied together,
        # and CASCADE tells PostgreSQL that is intentional.
        #
        # Listing them all in ONE statement also matters for the foreign
        # keys: truncating them one at a time would be refused, because
        # emptying investors while folios still has rows would orphan them.
        #
        # ", ".join(LOAD_ORDER) builds "scheme_categories, benchmarks, ..."
        # — the order is irrelevant for TRUNCATE, but reusing the same list
        # means a new table cannot be added to the load and forgotten here.
        #
        # AND THIS IS THE LINE THAT MAKES THE WHOLE PIPELINE RE-RUNNABLE.
        # Without it, a second run would try to insert rows whose primary
        # keys already exist and fail on every table.

        for name in LOAD_ORDER:
            frame = tables[name]
            # Note it iterates LOAD_ORDER, not tables. So the order is
            # guaranteed, and an extra key in the dictionary is skipped
            # rather than loaded into a table that may not exist.

            print(f"  loading {name:<28} {len(frame):>10,} rows")
            # One line per table, printed BEFORE the load starts — so if a
            # table fails, the last line on screen names it.
            #
            # {name:<28} left-aligns in 28 characters and {len(frame):>10,}
            # right-aligns the count in 10 with thousands separators. The
            # result is a column-aligned list where the row counts line up
            # and are readable at a glance:
            #
            #   loading nav_history                    168,750 rows
            #   loading transactions                   903,112 rows
            #
            # The same alignment idea as the log formatter in the reference
            # file, and worth the two format specifiers: an unaligned list of
            # 20 numbers is much harder to scan for the one that looks wrong.

            buffer = io.BytesIO()
            frame.write_csv(buffer, include_header=True)
            # Write the whole DataFrame as CSV text into memory.
            #
            # polars handles the three things that make hand-rolled CSV
            # dangerous: quoting values that contain commas, escaping
            # embedded quotes, and writing an empty field for a null.
            #
            # THAT LAST ONE IS LOAD-BEARING. An empty CSV field is what COPY
            # reads as NULL. So a null in `arn_code` — a direct transaction
            # with no distributor — travels as ,, and arrives as NULL.
            # Writing the string "None" or "null" instead would either fail
            # the load on a type error or, worse, store the text "None" in a
            # column where NULL was meant.
            #
            # And recall common.py's SURNAMES contains "D'Souza". That
            # apostrophe passes through here untouched, because CSV quoting
            # handles it. A loader built by concatenating SQL strings would
            # break on that row — which is the practical reason this approach
            # is also the safe one.

            columns = ", ".join(frame.columns)
            copy_sql = f"COPY {name} ({columns}) FROM STDIN WITH (FORMAT csv, HEADER true)"
            # Build the COPY statement, NAMING EVERY COLUMN EXPLICITLY.
            #
            # THIS IS THE MOST IMPORTANT DETAIL IN THE FILE. Without the
            # column list, COPY matches the CSV's columns to the table's
            # columns BY POSITION. So the moment a DataFrame's column order
            # differs from the table's — which is easy to cause with one
            # reordered .select() in a generator file — data is loaded into
            # the wrong columns.
            #
            # And it may not even error. Two text columns swapped load
            # perfectly, and the database is silently wrong. Two numeric
            # columns of the same type, likewise.
            #
            # Naming the columns makes the match BY NAME, so the DataFrame's
            # order stops mattering and a genuinely missing column fails
            # loudly instead.
            #
            # FROM STDIN means "the data is coming over the connection",
            # rather than FROM '/path/file.csv', which would mean a file on
            # the PostgreSQL SERVER — a path that does not exist on Azure and
            # that a non-superuser could not read anyway.
            #
            # HEADER true tells COPY to skip the first line, which polars
            # wrote because of include_header=True above. Those two settings
            # have to agree; set one without the other and either the header
            # row is loaded as data, or the first real row is discarded.

            with conn.cursor() as cur, cur.copy(copy_sql) as copy:
                copy.write(buffer.getvalue())
            # Stream the bytes into PostgreSQL.
            #
            # TWO CONTEXT MANAGERS ON ONE LINE, separated by a comma — a
            # cursor, and a copy stream opened on it. Both are closed when
            # the block ends, in reverse order, and the copy stream being
            # closed is what tells PostgreSQL the data is complete.
            #
            # cur.copy() is psycopg 3's interface to the COPY protocol. It is
            # not SQL being executed; it is a side channel that bypasses
            # statement parsing entirely, which is where the speed comes
            # from.
            #
            # buffer.getvalue() hands over the whole CSV as one bytes object.
            # For very large tables a streaming write in chunks would use
            # less memory; at this scale one block is simpler and the
            # difference does not matter.

        conn.execute("ANALYZE")
        # THE LAST STATEMENT, AND IT IS NOT AN AFTERTHOUGHT.
        #
        # ANALYZE makes PostgreSQL re-read every table and update its
        # statistics: how many rows, how many distinct values per column,
        # which values are most common, what share is null.
        #
        # IT MATTERS FOR TWO COMPLETELY SEPARATE REASONS, and the second is
        # specific to this project.
        #
        # ONE — QUERY SPEED. The query planner uses those statistics to
        # choose how to run a query. After a bulk load, PostgreSQL still
        # believes the tables are empty, so it plans as if every table had a
        # handful of rows and picks strategies that are catastrophic on
        # 900,000. Queries that should take 200 milliseconds take 30 seconds,
        # and nothing indicates why.
        #
        # TWO — STAGE 5 READS THESE STATISTICS DIRECTLY.
        # adapters/postgres.py's read_column_stats() queries pg_stats to
        # learn each column's null share, distinct count and most common
        # values, and metadata/notes.py puts those in front of the model when
        # drafting a description.
        #
        # So without ANALYZE, the metadata pipeline would describe every
        # column as having no data in it. The notes would be vague, the
        # review would approve vague notes, and the agent would be working
        # from a description of an empty database.
        #
        # ONE LINE, AND THE SECOND HALF OF THE PROJECT DEPENDS ON IT.
        #
        # Note it is inside the transaction and takes no table list, so it
        # analyses everything the current user can see. On a database with
        # only our 20 tables that is exactly what we want.