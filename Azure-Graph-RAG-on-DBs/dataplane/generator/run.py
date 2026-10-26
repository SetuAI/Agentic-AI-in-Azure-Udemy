"""
dataplane/generator/run.py
==========================

WHAT THIS FILE IS FOR
---------------------
The one command that fills the mf_data database with synthetic data:

    uv run python -m dataplane.generator.run

It runs the generator files in the order their data depends on, then loads
all 20 tables into Postgres.

THE ORDER, AND WHY
------------------
    1. fund_house    -> schemes, plans, NAVs            (needs nothing)
    2. market        -> issuers, securities, ratings    (needs nothing)
    3. investors     -> investors, folios, SIPs         (needs plans)
    4. transactions  -> every transaction               (needs folios, SIPs, NAVs)
    5. summaries     -> AUM, then holdings              (needs transactions)
    6. load          -> all 20 tables into mf_data

The schema must already exist: run dataplane/schema/apply_schema.py first.

WHY "python -m" AND NOT "python dataplane/generator/run.py"
-----------------------------------------------------------
The generator files import each other as "dataplane.generator.xxx".
"python -m" runs the file as part of the project, so those imports work.
"""

# "os" gives access to environment variables such as DATA_DB_URL.
import os

# "time" is used to measure how long the run takes.
import time

# load_dotenv copies the values in .env into environment variables.
from dotenv import load_dotenv

# The five generator modules, one per group of tables.
from dataplane.generator import fund_house, investors, market, summaries, transactions

# Reads config/generator.yaml (seed, scale, dates).
from dataplane.generator.common import load_settings

# Writes every generated table into Postgres.
from dataplane.load.copy_loader import load_all


# The whole run, start to finish.
def main() -> None:
    # Step 0: read .env so DATA_DB_URL is available.
    load_dotenv()
    # Read the generator settings (one shared random generator for the whole run).
    settings = load_settings()
    # Remember the start time (seconds since 1970) to report the duration later.
    started = time.time()
    # An f-string: anything inside {} is replaced by its value.
    print(f"Generating at scale {settings.scale}, seed from config/generator.yaml ...")

    # tables will map table name -> polars DataFrame, e.g. {"schemes": <DataFrame>, ...}.
    tables = {}

    # Step 1: schemes, plans, managers, NAVs, benchmarks.
    print("1/5 fund house")
    # .update() adds every table that build() returned into our dict.
    tables.update(fund_house.build(settings))

    # Step 2: sectors, issuers, securities, ratings.
    print("2/5 market")
    # build() returns two things: the tables, and extra facts the holdings step needs later.
    market_tables, security_facts = market.build(settings)
    # Add the market tables to the dict.
    tables.update(market_tables)

    # Step 3: distributors, investors, folios, SIPs (needs the plans from step 1).
    print("3/5 investors")
    # Returns the tables plus folio and SIP details the transactions step needs.
    investor_tables, folio_facts, sip_schedule = investors.build(settings, tables["scheme_plans"])
    # Add the investor tables to the dict.
    tables.update(investor_tables)

    # Step 4: every purchase, redemption, SIP instalment and switch.
    print("4/5 transactions")
    # Store the single transactions table under its name.
    tables["transactions"] = transactions.build(settings, folio_facts, sip_schedule, tables["nav_history"])

    # Step 5: month-end summaries built from the transactions.
    print("5/5 AUM and holdings")
    # AUM per scheme per month end, from transactions, NAVs, plans and folios.
    tables["scheme_aum_monthly"] = summaries.build_aum(
        tables["transactions"], tables["nav_history"], tables["scheme_plans"], tables["folios"])
    # What each scheme holds each month end, sized to match the AUM just built.
    tables["portfolio_holdings"] = summaries.build_holdings(settings, tables["scheme_aum_monthly"], security_facts)

    # :.0f = show the number with 0 decimal places.
    print(f"Generated in {time.time() - started:.0f}s. Loading into mf_data ...")
    # Step 6: copy all 20 tables into the mf_data database.
    load_all(os.environ["DATA_DB_URL"], tables)

    # Count the rows across all tables (len(t) = number of rows in a DataFrame).
    total = sum(len(t) for t in tables.values())
    # {total:,} prints the number with thousands separators, e.g. 1,234,567.
    print(f"Done: {total:,} rows across {len(tables)} tables in {time.time() - started:.0f}s.")


# True only when this file is run directly (python -m dataplane.generator.run).
if __name__ == "__main__":
    # Start the run.
    main()