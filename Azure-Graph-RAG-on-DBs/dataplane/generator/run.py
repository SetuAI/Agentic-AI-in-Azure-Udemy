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

import os
import time

from dotenv import load_dotenv

from dataplane.generator import fund_house, investors, market, summaries, transactions
from dataplane.generator.common import load_settings
from dataplane.load.copy_loader import load_all


def main() -> None:
    load_dotenv()
    settings = load_settings()
    started = time.time()
    print(f"Generating at scale {settings.scale}, seed from config/generator.yaml ...")

    tables = {}

    print("1/5 fund house")
    tables.update(fund_house.build(settings))

    print("2/5 market")
    market_tables, security_facts = market.build(settings)
    tables.update(market_tables)

    print("3/5 investors")
    investor_tables, folio_facts, sip_schedule = investors.build(settings, tables["scheme_plans"])
    tables.update(investor_tables)

    print("4/5 transactions")
    tables["transactions"] = transactions.build(settings, folio_facts, sip_schedule, tables["nav_history"])

    print("5/5 AUM and holdings")
    tables["scheme_aum_monthly"] = summaries.build_aum(
        tables["transactions"], tables["nav_history"], tables["scheme_plans"], tables["folios"])
    tables["portfolio_holdings"] = summaries.build_holdings(settings, tables["scheme_aum_monthly"], security_facts)

    print(f"Generated in {time.time() - started:.0f}s. Loading into mf_data ...")
    load_all(os.environ["DATA_DB_URL"], tables)

    total = sum(len(t) for t in tables.values())
    print(f"Done: {total:,} rows across {len(tables)} tables in {time.time() - started:.0f}s.")


if __name__ == "__main__":
    main()