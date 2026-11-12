##############################################################################
# dataplane/generator/run.py
#
# PURPOSE:
#   The one command that fills mf_data with synthetic data.
#
#       uv run python -m dataplane.generator.run
#
#   It calls the five builder files in the order their data depends on,
#   collects all 20 tables in one dictionary, then hands that dictionary to
#   the loader.
#
#   About 100 seconds at the default scale of 0.2, ending in roughly
#   1.13 million rows.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   The five builders have dependencies on each other that are not obvious
#   from their names, and getting the order wrong does not produce a clean
#   error — it produces a KeyError deep inside a builder, or worse, a
#   half-built table.
#
#   This file is the only place that order is written down, and it is written
#   as straight-line code rather than as configuration, so it can be read
#   top to bottom.
#
# THE ORDER, AND WHAT EACH STEP NEEDS:
#
#   1. fund_house    schemes, plans, managers, NAVs      needs NOTHING
#   2. market        issuers, securities, ratings        needs NOTHING
#   3. investors     investors, folios, SIPs             needs PLANS       (from 1)
#   4. transactions  every purchase and redemption       needs FOLIOS, SIPS (from 3)
#                                                         and NAVS          (from 1)
#   5. summaries     AUM, then holdings                  needs TRANSACTIONS (from 4)
#                                                         and SECURITY FACTS (from 2)
#   6. load          all 20 tables into mf_data
#
#   Steps 1 and 2 are independent of each other and could run in either
#   order. Everything after that is forced.
#
#   The deepest dependency is step 5: AUM is computed from transactions,
#   which need NAVs and folios, which need plans. So a bug in step 1
#   propagates all the way to the last table.
#
# WHAT MUST ALREADY EXIST:
#   The 20 empty tables. Run dataplane/schema/apply_schema.py first, or this
#   fails at the load step with "relation does not exist".
#
# WHY "python -m" AND NOT "python dataplane/generator/run.py":
#   The builders import each other as "dataplane.generator.xxx". Running the
#   file by path puts only its own folder on the import path, so
#   "import dataplane.generator.market" fails with ModuleNotFoundError.
#
#   "python -m dataplane.generator.run" runs it as a MODULE INSIDE THE
#   PROJECT, with the project root on the import path, so those imports
#   resolve. Note the dots and the absence of ".py".
#
#   This is the single most common way to get an unexplained
#   ModuleNotFoundError in a Python project, and the error message says
#   nothing about how the file was launched.
#
# WHAT YOU SEE WHEN IT WORKS:
#   Generating at scale 0.2, seed from config/generator.yaml ...
#   1/5 fund house
#   2/5 market
#   3/5 investors
#   4/5 transactions
#   5/5 AUM and holdings
#   Generated in 38s. Loading into mf_data ...
#     loading scheme_categories              36 rows
#     ... one line per table ...
#   Done: 1,132,667 rows across 20 tables in 103s.
#
# HOW TO RUN:
#       uv run python -m dataplane.generator.run
#
#   Safe to run again — the loader empties every table first.
##############################################################################


import os
# Used once, to read DATA_DB_URL out of the environment and pass it to the
# loader. The loader takes the URL as an argument rather than reading the
# environment itself, which keeps it testable: a test can pass any database
# URL without touching os.environ.

import time
# Used for the two elapsed-time figures printed below. Not timing for
# optimisation — timing so that a person watching knows whether the thing is
# progressing or stuck. At scale 1.0 this run takes several minutes and the
# transactions step alone can sit silent for a long while.

from dotenv import load_dotenv
# Reads .env so DATA_DB_URL is available. Same reasoning as every other
# entry point in the project.

from dataplane.generator import fund_house, investors, market, summaries, transactions
# The five builders. Imported as MODULES, not as the functions inside them,
# so each call site reads as fund_house.build(...) — which names the file
# doing the work at the point it is called.
#
# Note the import list is alphabetical, which is what ruff enforces, and is
# NOT the order they are called in. The call order is in main() below, where
# it is visible as code.

from dataplane.generator.common import load_settings
# Builds the one Settings object — seed, scale, date window — from
# config/generator.yaml. Called exactly once, here.

from dataplane.load.copy_loader import load_all
# The loader. Takes a database URL and the dictionary of tables, and writes
# everything with PostgreSQL's COPY.


def main() -> None:
    """
    Generate all 20 tables and load them into mf_data.

    Takes nothing and returns nothing. Everything it needs comes from
    config/generator.yaml and .env; everything it produces goes into the
    database.

    Raises:
        KeyError:   if DATA_DB_URL is missing from .env.
        ValueError: from the loader, if any of the 20 tables was not
                    generated — raised BEFORE anything is deleted.
        psycopg.errors.UndefinedTable: if the schema was never applied.
    """
    load_dotenv()
    settings = load_settings()
    # THE ONE Settings OBJECT FOR THE WHOLE RUN, created here and passed
    # down into every builder.
    #
    # This is what guarantees every builder uses the same seed, the same
    # scale and the same date window. If each builder called load_settings()
    # itself, each would get its own random generator starting from the same
    # seed — and then two builders would draw the SAME sequence of numbers,
    # which is a subtler kind of wrong than drawing different ones.

    started = time.time()
    print(f"Generating at scale {settings.scale}, seed from config/generator.yaml ...")
    # Printing the SCALE is the useful half of this line. It is the one
    # setting that changes how long the run takes and how big the result is,
    # and the most common surprise is discovering afterwards that it was
    # 1.0 rather than 0.2.

    tables = {}
    # THE ACCUMULATOR. One dictionary, table name -> polars DataFrame, built
    # up across the five steps and handed to the loader at the end.
    #
    # THE KEYS ARE THE POSTGRESQL TABLE NAMES. copy_loader.py uses them to
    # decide which table each frame goes into, so a typo here surfaces as
    # "relation does not exist" at load time — after all the generation work
    # has been done. The loader's LOAD_ORDER check catches it a moment
    # earlier, which is why that check exists.
    #
    # NOTHING IS WRITTEN TO THE DATABASE UNTIL THE END. All 1.13 million rows
    # are held in memory as polars frames first. That is deliberate: it keeps
    # generation and loading as two separate concerns, it lets the loader run
    # in one transaction, and at this scale the memory cost is a few hundred
    # megabytes. At scale 1.0 it is several gigabytes, which is the real
    # limit on how large this generator can go without being rewritten to
    # stream.

    print("1/5 fund house")
    tables.update(fund_house.build(settings))
    # STEP 1. Returns a DICTIONARY of 8 tables, so .update() merges them all
    # in at once.
    #
    # Builds scheme_categories, benchmarks, benchmark_values, schemes,
    # scheme_plans, fund_managers, scheme_manager_assignments and
    # nav_history.
    #
    # NOT scheme_aum_monthly, even though that is in 01_fund_house.sql — AUM
    # is computed from transactions, so it is built in step 5.
    #
    # Needs nothing from any other builder. It is the root of the dependency
    # tree, and the largest single table it produces is nav_history at about
    # 170,000 rows.

    print("2/5 market")
    market_tables, security_facts = market.build(settings)
    tables.update(market_tables)
    # STEP 2. Returns a PAIR, and the second half never reaches the database.
    #
    #   market_tables   5 tables: sectors, issuer_groups, issuers,
    #                   securities, credit_rating_history
    #   security_facts  a helper frame describing every security — its type,
    #                   sector, issuer, group and starting credit rating
    #
    # security_facts is passed to summaries.build_holdings in step 5. It
    # exists because deciding which securities a scheme may hold needs all
    # those attributes together, and re-deriving them by joining the finished
    # tables would be slower and much harder to read.
    #
    # NOTE portfolio_holdings IS NOT BUILT HERE either, despite being in
    # 02_market.sql. A holding's rupee value is a share of the scheme's AUM,
    # and AUM is not known until transactions exist. Step 5 again.
    #
    # So two of the 20 tables are built out of file order, and both for the
    # same reason: they depend on transactions.

    print("3/5 investors")
    investor_tables, folio_facts, sip_schedule = investors.build(settings, tables["scheme_plans"])
    tables.update(investor_tables)
    # STEP 3. Returns a TRIPLE, and takes an argument.
    #
    # THE ARGUMENT IS THE FIRST REAL DEPENDENCY IN THE PIPELINE.
    # tables["scheme_plans"] comes from step 1, and investors needs it
    # because a folio points at a PLAN — "Flexi Cap, Direct, Growth" — not
    # just at a scheme. It builds a lookup from
    # (scheme_id, plan_type, option_type) to plan_id.
    #
    # If step 1 had not run, this line would raise KeyError: 'scheme_plans',
    # which is a clear enough failure.
    #
    # What comes back:
    #   investor_tables  4 tables: distributors, investors, folios,
    #                    sip_registrations
    #   folio_facts      one row per folio with its plan, opening date, ARN
    #                    and the investor's city tier
    #   sip_schedule     one row per SIP with its payment window, day of
    #                    month and amount
    #
    # The last two go to step 4 and never reach PostgreSQL.

    print("4/5 transactions")
    tables["transactions"] = transactions.build(settings, folio_facts, sip_schedule, tables["nav_history"])
    # STEP 4. THE BIGGEST STEP, and the one with the most inputs.
    #
    # Three dependencies, from two earlier steps:
    #   folio_facts         step 3 — which folios exist, in which plan
    #   sip_schedule        step 3 — which SIPs pay, when, how much
    #   tables["nav_history"]  step 1 — the price, to turn rupees into units
    #
    # Returns ONE table rather than a dictionary, so it is assigned directly
    # rather than merged. About 900,000 rows at this scale — roughly 80% of
    # the whole database.
    #
    # This step is also where the run spends most of its time, because of the
    # redemption logic: a folio cannot sell units it does not hold, so the
    # balance has to be computed per folio per date before redemption
    # amounts can be chosen.

    print("5/5 AUM and holdings")
    tables["scheme_aum_monthly"] = summaries.build_aum(
        tables["transactions"], tables["nav_history"], tables["scheme_plans"], tables["folios"])
    tables["portfolio_holdings"] = summaries.build_holdings(settings, tables["scheme_aum_monthly"], security_facts)
    # STEP 5. TWO CALLS, AND THE ORDER BETWEEN THEM IS NOT OPTIONAL.
    #
    # build_aum computes AUM from the units in transactions times the
    # month-end NAV. Four inputs, all from the dictionary, all from earlier
    # steps.
    #
    # build_holdings then SPLITS that AUM across securities. Which is why it
    # takes tables["scheme_aum_monthly"] — the frame the line above just
    # created.
    #
    # THAT DEPENDENCY IS WHAT MAKES THE TWO TABLES AGREE. Each holding's
    # market_value is computed as a percentage of the scheme's AUM, so
    #
    #     SUM(market_value) == aum_amount
    #
    # arithmetically rather than by coincidence. tests/test_stage2_data.py
    # checks exactly that, and it is the strongest test in the suite: it can
    # only pass if both tables came from the same transactions.
    #
    # Swap these two lines and build_holdings gets a KeyError — which is a
    # good failure, because the alternative would be silently inventing
    # holdings that do not add up to anything.
    #
    # Note build_holdings takes `settings` and build_aum does not. build_aum
    # is pure arithmetic over existing data with no random choices in it;
    # build_holdings picks which securities each scheme holds, which needs
    # the seeded generator.

    print(f"Generated in {time.time() - started:.0f}s. Loading into mf_data ...")
    load_all(os.environ["DATA_DB_URL"], tables)
    # STEP 6. Everything is in memory; now it goes to PostgreSQL.
    #
    # The loader empties all 20 tables, writes each frame with COPY in
    # dependency order, and runs ANALYZE at the end. All inside one
    # transaction, so a failure anywhere leaves the database with its
    # previous contents rather than half the new data.
    #
    # os.environ[...] with square brackets, so a missing DATA_DB_URL raises a
    # clear KeyError. Note it is read HERE rather than at the top of main(),
    # which means a missing setting is discovered after 40 seconds of
    # generation rather than immediately — a small wart, and worth knowing if
    # it ever bites.

    total = sum(len(t) for t in tables.values())
    print(f"Done: {total:,} rows across {len(tables)} tables in {time.time() - started:.0f}s.")
    # THE FINAL LINE, AND BOTH NUMBERS ARE WORTH CHECKING ON CAMERA.
    #
    # len(tables) must be 20. Fewer means a builder did not return something
    # the dictionary expected — though the loader's own check would have
    # raised before this point.
    #
    # total is about 1,132,667 at scale 0.2. A number far from that means the
    # scale factor is not what you thought.
    #
    # The {total:,} format inserts thousands separators, so 1132667 prints as
    # 1,132,667. A small thing that makes a seven-digit number readable at a
    # glance — and this line is read aloud in the video.
    #
    # Note `started` is reused, so this elapsed figure covers generation AND
    # loading, while the line above covered generation only. The difference
    # between the two is how long the database write took.


if __name__ == "__main__":
    main()
    # Only when run directly. Lets a test import main() without generating
    #1.13 million rows and wiping the database as a side effect of the
    # import — which is exactly the kind of side effect that costs somebody
    # an afternoon.