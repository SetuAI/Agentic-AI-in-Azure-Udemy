"""
dataplane/generator/fund_house.py
=================================

WHAT THIS FILE IS FOR
---------------------
Builds 8 of the 9 fund-house tables (see dataplane/schema/01_fund_house.sql):

    scheme_categories, benchmarks, benchmark_values, schemes, scheme_plans,
    fund_managers, scheme_manager_assignments, nav_history

The 9th, scheme_aum_monthly, is NOT built here. AUM is total money
invested, which is only known once transactions exist, so it is calculated
later in summaries.py.

HOW THE PRICES ARE MADE
-----------------------
1. Each benchmark index gets a daily price series: a random walk that
   drifts upwards at a realistic yearly rate (about 12% for equity, about
   6-7% for debt) with realistic daily ups and downs.
2. Each scheme's Direct Growth NAV follows its benchmark, plus a small
   yearly "alpha" (how much it beats or trails the index) plus a little
   daily noise of its own.
3. The other plans of the scheme (Regular, IDCW) are derived from the
   Direct Growth NAV, with the Regular plan losing its extra fee every day.

Patterns 1 and 3 change the NAV here (see patterns.py).
"""

# numpy: fast maths on whole arrays of numbers (random walks, exp, cumsum ...).
import numpy as np

# polars: builds the tables (DataFrames) that are later loaded into Postgres.
import polars as pl

# The planted-pattern values, used as P.P1_..., P.P3_... below.
from dataplane.generator import patterns as P

# Shared helpers and name lists from common.py.
from dataplane.generator.common import (
    # Name lists, used for manager names.
    FIRST_NAMES,
    # (surnames)
    SURNAMES,
    # The Settings class (seed, scale, dates).
    Settings,
    # Every trading day in a window.
    business_days,
    # numpy dates -> Python dates.
    to_dates,
)

# ---------------------------------------------------------------------------
# FIXED REFERENCE DATA
# ---------------------------------------------------------------------------
# (code, name, asset_class, risk_level). category_id is the position + 1.
# Each line below is one SEBI category. Equity = shares, Debt = bonds,
# Hybrid = both, Solution Oriented = goal-based, Other = index funds etc.
CATEGORIES = [
    # category_id 1: Multi Cap Fund (Equity, risk Very High)
    ("EQ-MC", "Multi Cap Fund", "Equity", "Very High"),
    # category_id 2: Large Cap Fund (Equity, risk Very High)
    ("EQ-LC", "Large Cap Fund", "Equity", "Very High"),
    # category_id 3: Large & Mid Cap Fund (Equity, risk Very High)
    ("EQ-LM", "Large & Mid Cap Fund", "Equity", "Very High"),
    # category_id 4: Mid Cap Fund (Equity, risk Very High)
    ("EQ-MD", "Mid Cap Fund", "Equity", "Very High"),
    # category_id 5: Small Cap Fund (Equity, risk Very High)
    ("EQ-SC", "Small Cap Fund", "Equity", "Very High"),
    # category_id 6: Dividend Yield Fund (Equity, risk Very High)
    ("EQ-DY", "Dividend Yield Fund", "Equity", "Very High"),
    # category_id 7: Value Fund (Equity, risk Very High)
    ("EQ-VL", "Value Fund", "Equity", "Very High"),
    # category_id 8: Focused Fund (Equity, risk Very High)
    ("EQ-FC", "Focused Fund", "Equity", "Very High"),
    # category_id 9: Sectoral/Thematic Fund (Equity, risk Very High)
    ("EQ-ST", "Sectoral/Thematic Fund", "Equity", "Very High"),
    # category_id 10: ELSS (Equity, risk Very High)
    ("EQ-EL", "ELSS", "Equity", "Very High"),
    # category_id 11: Flexi Cap Fund (Equity, risk Very High)
    ("EQ-FX", "Flexi Cap Fund", "Equity", "Very High"),
    # category_id 12: Overnight Fund (Debt, risk Low)
    ("DT-ON", "Overnight Fund", "Debt", "Low"),
    # category_id 13: Liquid Fund (Debt, risk Low to Moderate)
    ("DT-LQ", "Liquid Fund", "Debt", "Low to Moderate"),
    # category_id 14: Ultra Short Duration Fund (Debt, risk Low to Moderate)
    ("DT-US", "Ultra Short Duration Fund", "Debt", "Low to Moderate"),
    # category_id 15: Low Duration Fund (Debt, risk Moderate)
    ("DT-LD", "Low Duration Fund", "Debt", "Moderate"),
    # category_id 16: Money Market Fund (Debt, risk Low to Moderate)
    ("DT-MM", "Money Market Fund", "Debt", "Low to Moderate"),
    # category_id 17: Short Duration Fund (Debt, risk Moderate)
    ("DT-SD", "Short Duration Fund", "Debt", "Moderate"),
    # category_id 18: Medium Duration Fund (Debt, risk Moderate)
    ("DT-MD", "Medium Duration Fund", "Debt", "Moderate"),
    # category_id 19: Medium to Long Duration Fund (Debt, risk Moderate)
    ("DT-ML", "Medium to Long Duration Fund", "Debt", "Moderate"),
    # category_id 20: Long Duration Fund (Debt, risk Moderate)
    ("DT-LG", "Long Duration Fund", "Debt", "Moderate"),
    # category_id 21: Dynamic Bond Fund (Debt, risk Moderate)
    ("DT-DB", "Dynamic Bond Fund", "Debt", "Moderate"),
    # category_id 22: Corporate Bond Fund (Debt, risk Moderate)
    ("DT-CB", "Corporate Bond Fund", "Debt", "Moderate"),
    # category_id 23: Credit Risk Fund (Debt, risk Moderately High)
    ("DT-CR", "Credit Risk Fund", "Debt", "Moderately High"),
    # category_id 24: Banking and PSU Fund (Debt, risk Low to Moderate)
    ("DT-BP", "Banking and PSU Fund", "Debt", "Low to Moderate"),
    # category_id 25: Gilt Fund (Debt, risk Moderate)
    ("DT-GL", "Gilt Fund", "Debt", "Moderate"),
    # category_id 26: Floater Fund (Debt, risk Low to Moderate)
    ("DT-FL", "Floater Fund", "Debt", "Low to Moderate"),
    # category_id 27: Conservative Hybrid Fund (Hybrid, risk Moderately High)
    ("HY-CH", "Conservative Hybrid Fund", "Hybrid", "Moderately High"),
    # category_id 28: Aggressive Hybrid Fund (Hybrid, risk Very High)
    ("HY-AH", "Aggressive Hybrid Fund", "Hybrid", "Very High"),
    # category_id 29: Dynamic Asset Allocation Fund (Hybrid, risk Moderately High)
    ("HY-DA", "Dynamic Asset Allocation Fund", "Hybrid", "Moderately High"),
    # category_id 30: Multi Asset Allocation Fund (Hybrid, risk High)
    ("HY-MA", "Multi Asset Allocation Fund", "Hybrid", "High"),
    # category_id 31: Arbitrage Fund (Hybrid, risk Low)
    ("HY-AR", "Arbitrage Fund", "Hybrid", "Low"),
    # category_id 32: Equity Savings Fund (Hybrid, risk Moderate)
    ("HY-ES", "Equity Savings Fund", "Hybrid", "Moderate"),
    # category_id 33: Retirement Fund (Solution Oriented, risk High)
    ("SO-RT", "Retirement Fund", "Solution Oriented", "High"),
    # category_id 34: Children's Fund (Solution Oriented, risk High)
    ("SO-CH", "Children's Fund", "Solution Oriented", "High"),
    # category_id 35: Index Funds and ETFs (Other, risk Very High)
    ("OT-IX", "Index Funds and ETFs", "Other", "Very High"),
    # category_id 36: Fund of Funds (Other, risk Very High)
    ("OT-FF", "Fund of Funds", "Other", "Very High"),
]

# (name, type, yearly drift, yearly volatility, starting level)
# Drift = average yearly rise. Volatility = how much it swings.
# benchmark_id is the position + 1.
# Each line: one invented index. TRI = Total Return Index (dividends included).
BENCHMARKS = [
    # benchmark_id 1: Equity index, grows ~12% a year, swings ~15% a year, starts at 18,000
    ("Deccan 50 TRI", "Equity", 0.12, 0.15, 18000),
    # benchmark_id 2: Equity index, grows ~12% a year, swings ~15% a year, starts at 22,000
    ("Deccan 100 TRI", "Equity", 0.12, 0.15, 22000),
    # benchmark_id 3: Equity index, grows ~13% a year, swings ~16% a year, starts at 26,000
    ("Deccan 500 TRI", "Equity", 0.13, 0.16, 26000),
    # benchmark_id 4: Equity index, grows ~17% a year, swings ~19% a year, starts at 15,000
    ("Deccan Midcap 150 TRI", "Equity", 0.17, 0.19, 15000),
    # benchmark_id 5: Equity index, grows ~18% a year, swings ~22% a year, starts at 11,000
    ("Deccan Smallcap 250 TRI", "Equity", 0.18, 0.22, 11000),
    # benchmark_id 6: Equity index, grows ~10% a year, swings ~18% a year, starts at 40,000
    ("Deccan Bank TRI", "Equity", 0.10, 0.18, 40000),
    # benchmark_id 7: Equity index, grows ~16% a year, swings ~18% a year, starts at 7,000
    ("Deccan Infrastructure TRI", "Equity", 0.16, 0.18, 7000),
    # benchmark_id 8: Equity index, grows ~11% a year, swings ~20% a year, starts at 35,000
    ("Deccan IT TRI", "Equity", 0.11, 0.20, 35000),
    # benchmark_id 9: Debt index, grows ~6.2% a year, swings ~0.2% a year, starts at 4,000
    ("Deccan Liquid Debt Index", "Debt", 0.062, 0.002, 4000),
    # benchmark_id 10: Debt index, grows ~6.8% a year, swings ~1.2% a year, starts at 4,500
    ("Deccan Short Duration Debt Index", "Debt", 0.068, 0.012, 4500),
    # benchmark_id 11: Debt index, grows ~7.2% a year, swings ~2% a year, starts at 5,000
    ("Deccan Corporate Bond Index", "Debt", 0.072, 0.020, 5000),
    # benchmark_id 12: Debt index, grows ~8% a year, swings ~2.5% a year, starts at 3,500
    ("Deccan Credit Risk Debt Index", "Debt", 0.080, 0.025, 3500),
    # benchmark_id 13: Debt index, grows ~7% a year, swings ~3.5% a year, starts at 6,000
    ("Deccan Composite Gilt Index", "Debt", 0.070, 0.035, 6000),
    # benchmark_id 14: Hybrid index, grows ~10.5% a year, swings ~10% a year, starts at 12,000
    ("Deccan Hybrid 65:35 Index", "Hybrid", 0.105, 0.10, 12000),
    # benchmark_id 15: Hybrid index, grows ~5.8% a year, swings ~0.4% a year, starts at 2,500
    ("Deccan Arbitrage Index", "Hybrid", 0.058, 0.004, 2500),
]
# A lookup dict: benchmark name -> benchmark_id (its position in the list + 1).
# enumerate() gives (position, item); "*_rest" ignores the other 4 values.
BENCHMARK_ID = {name: i + 1 for i, (name, *_rest) in enumerate(BENCHMARKS)}

# (scheme name, category code, benchmark name, launch date, has IDCW plans)
# scheme_id is the position + 1. 40 schemes.
# Five schemes have no IDCW option, so 35 x 4 + 5 x 2 = 150 plans.
# Each line: one scheme of the invented fund house "Sahyadri".
SCHEMES = [
    # scheme_id 1: category EQ-LC, benchmark Deccan 100 TRI, launched 2019-04-15, 4 plans (has IDCW)
    ("Sahyadri Large Cap Fund", "EQ-LC", "Deccan 100 TRI", "2019-04-15", True),
    # scheme_id 2: category EQ-FX, benchmark Deccan 500 TRI, launched 2019-04-15, 4 plans (has IDCW)
    ("Sahyadri Flexi Cap Fund", "EQ-FX", "Deccan 500 TRI", "2019-04-15", True),
    # scheme_id 3: category EQ-MC, benchmark Deccan 500 TRI, launched 2020-02-10, 4 plans (has IDCW)
    ("Sahyadri Multi Cap Fund", "EQ-MC", "Deccan 500 TRI", "2020-02-10", True),
    # scheme_id 4: category EQ-LM, benchmark Deccan 500 TRI, launched 2019-09-02, 4 plans (has IDCW)
    ("Sahyadri Large & Mid Cap Fund", "EQ-LM", "Deccan 500 TRI", "2019-09-02", True),
    # scheme_id 5: category EQ-MD, benchmark Deccan Midcap 150 TRI, launched 2019-06-03, 4 plans (has IDCW)
    ("Sahyadri Mid Cap Fund", "EQ-MD", "Deccan Midcap 150 TRI", "2019-06-03", True),
    # scheme_id 6: category EQ-SC, benchmark Deccan Smallcap 250 TRI, launched 2020-06-01, 4 plans (has IDCW)
    ("Sahyadri Small Cap Fund", "EQ-SC", "Deccan Smallcap 250 TRI", "2020-06-01", True),
    # scheme_id 7: category EQ-FC, benchmark Deccan 500 TRI, launched 2019-11-04, 4 plans (has IDCW)
    ("Sahyadri Focused Fund", "EQ-FC", "Deccan 500 TRI", "2019-11-04", True),
    # scheme_id 8: category EQ-VL, benchmark Deccan 500 TRI, launched 2020-09-07, 4 plans (has IDCW)
    ("Sahyadri Value Fund", "EQ-VL", "Deccan 500 TRI", "2020-09-07", True),
    # scheme_id 9: category EQ-DY, benchmark Deccan 500 TRI, launched 2021-01-11, 4 plans (has IDCW)
    ("Sahyadri Dividend Yield Fund", "EQ-DY", "Deccan 500 TRI", "2021-01-11", True),
    # scheme_id 10: category EQ-EL, benchmark Deccan 500 TRI, launched 2019-04-15, 4 plans (has IDCW)
    ("Sahyadri ELSS Tax Saver Fund", "EQ-EL", "Deccan 500 TRI", "2019-04-15", True),
    # scheme_id 11: category EQ-ST, benchmark Deccan Bank TRI, launched 2020-11-02, 4 plans (has IDCW)
    ("Sahyadri Banking & Financial Services Fund", "EQ-ST", "Deccan Bank TRI", "2020-11-02", True),
    # scheme_id 12: category EQ-ST, benchmark Deccan Infrastructure TRI, launched 2021-03-01, 4 plans (has IDCW)
    ("Sahyadri Infrastructure Fund", "EQ-ST", "Deccan Infrastructure TRI", "2021-03-01", True),
    # scheme_id 13: category EQ-ST, benchmark Deccan IT TRI, launched 2022-07-04, 4 plans (has IDCW)
    ("Sahyadri Technology Fund", "EQ-ST", "Deccan IT TRI", "2022-07-04", True),
    # scheme_id 14: category EQ-ST, benchmark Deccan 500 TRI, launched 2024-02-05, 4 plans (has IDCW)
    ("Sahyadri Manufacturing Opportunities Fund", "EQ-ST", "Deccan 500 TRI", "2024-02-05", True),
    # scheme_id 15: category OT-IX, benchmark Deccan 50 TRI, launched 2020-03-02, 2 plans (Growth only)
    ("Sahyadri Deccan 50 Index Fund", "OT-IX", "Deccan 50 TRI", "2020-03-02", False),
    # scheme_id 16: category OT-IX, benchmark Deccan Midcap 150 TRI, launched 2023-01-09, 2 plans (Growth only)
    ("Sahyadri Deccan Midcap 150 Index Fund", "OT-IX", "Deccan Midcap 150 TRI", "2023-01-09", False),
    # scheme_id 17: category DT-ON, benchmark Deccan Liquid Debt Index, launched 2019-04-15, 4 plans (has IDCW)
    ("Sahyadri Overnight Fund", "DT-ON", "Deccan Liquid Debt Index", "2019-04-15", True),
    # scheme_id 18: category DT-LQ, benchmark Deccan Liquid Debt Index, launched 2019-04-15, 4 plans (has IDCW)
    ("Sahyadri Liquid Fund", "DT-LQ", "Deccan Liquid Debt Index", "2019-04-15", True),
    # scheme_id 19: category DT-US, benchmark Deccan Short Duration Debt Index, launched 2019-07-01, 4 plans (has IDCW)
    ("Sahyadri Ultra Short Duration Fund", "DT-US", "Deccan Short Duration Debt Index", "2019-07-01", True),
    # scheme_id 20: category DT-LD, benchmark Deccan Short Duration Debt Index, launched 2019-10-01, 4 plans (has IDCW)
    ("Sahyadri Low Duration Fund", "DT-LD", "Deccan Short Duration Debt Index", "2019-10-01", True),
    # scheme_id 21: category DT-MM, benchmark Deccan Liquid Debt Index, launched 2020-01-06, 4 plans (has IDCW)
    ("Sahyadri Money Market Fund", "DT-MM", "Deccan Liquid Debt Index", "2020-01-06", True),
    # scheme_id 22: category DT-SD, benchmark Deccan Short Duration Debt Index, launched 2019-08-05, 4 plans (has IDCW)
    ("Sahyadri Short Duration Fund", "DT-SD", "Deccan Short Duration Debt Index", "2019-08-05", True),
    # scheme_id 23: category DT-MD, benchmark Deccan Corporate Bond Index, launched 2020-04-06, 4 plans (has IDCW)
    ("Sahyadri Medium Duration Fund", "DT-MD", "Deccan Corporate Bond Index", "2020-04-06", True),
    # scheme_id 24: category DT-CB, benchmark Deccan Corporate Bond Index, launched 2019-12-02, 4 plans (has IDCW)
    ("Sahyadri Corporate Bond Fund", "DT-CB", "Deccan Corporate Bond Index", "2019-12-02", True),
    # scheme_id 25: category DT-CR, benchmark Deccan Credit Risk Debt Index, launched 2019-12-02, 4 plans (has IDCW)
    ("Sahyadri Credit Risk Fund", "DT-CR", "Deccan Credit Risk Debt Index", "2019-12-02", True),
    # scheme_id 26: category DT-BP, benchmark Deccan Corporate Bond Index, launched 2020-05-04, 4 plans (has IDCW)
    ("Sahyadri Banking & PSU Debt Fund", "DT-BP", "Deccan Corporate Bond Index", "2020-05-04", True),
    # scheme_id 27: category DT-GL, benchmark Deccan Composite Gilt Index, launched 2020-08-03, 4 plans (has IDCW)
    ("Sahyadri Gilt Fund", "DT-GL", "Deccan Composite Gilt Index", "2020-08-03", True),
    # scheme_id 28: category DT-DB, benchmark Deccan Corporate Bond Index, launched 2020-10-05, 4 plans (has IDCW)
    ("Sahyadri Dynamic Bond Fund", "DT-DB", "Deccan Corporate Bond Index", "2020-10-05", True),
    # scheme_id 29: category DT-FL, benchmark Deccan Short Duration Debt Index, launched 2021-06-07, 4 plans (has IDCW)
    ("Sahyadri Floater Fund", "DT-FL", "Deccan Short Duration Debt Index", "2021-06-07", True),
    # scheme_id 30: category DT-LG, benchmark Deccan Composite Gilt Index, launched 2023-06-05, 4 plans (has IDCW)
    ("Sahyadri Long Duration Fund", "DT-LG", "Deccan Composite Gilt Index", "2023-06-05", True),
    # scheme_id 31: category DT-ML, benchmark Deccan Composite Gilt Index, launched 2021-02-01, 4 plans (has IDCW)
    ("Sahyadri Medium to Long Duration Fund", "DT-ML", "Deccan Composite Gilt Index", "2021-02-01", True),
    # scheme_id 32: category HY-CH, benchmark Deccan Hybrid 65:35 Index, launched 2020-01-06, 4 plans (has IDCW)
    ("Sahyadri Conservative Hybrid Fund", "HY-CH", "Deccan Hybrid 65:35 Index", "2020-01-06", True),
    # scheme_id 33: category HY-AH, benchmark Deccan Hybrid 65:35 Index, launched 2019-05-06, 4 plans (has IDCW)
    ("Sahyadri Aggressive Hybrid Fund", "HY-AH", "Deccan Hybrid 65:35 Index", "2019-05-06", True),
    # scheme_id 34: category HY-DA, benchmark Deccan Hybrid 65:35 Index, launched 2019-09-02, 4 plans (has IDCW)
    ("Sahyadri Balanced Advantage Fund", "HY-DA", "Deccan Hybrid 65:35 Index", "2019-09-02", True),
    # scheme_id 35: category HY-MA, benchmark Deccan Hybrid 65:35 Index, launched 2021-04-05, 4 plans (has IDCW)
    ("Sahyadri Multi Asset Allocation Fund", "HY-MA", "Deccan Hybrid 65:35 Index", "2021-04-05", True),
    # scheme_id 36: category HY-AR, benchmark Deccan Arbitrage Index, launched 2020-02-03, 4 plans (has IDCW)
    ("Sahyadri Arbitrage Fund", "HY-AR", "Deccan Arbitrage Index", "2020-02-03", True),
    # scheme_id 37: category HY-ES, benchmark Deccan Hybrid 65:35 Index, launched 2020-07-06, 4 plans (has IDCW)
    ("Sahyadri Equity Savings Fund", "HY-ES", "Deccan Hybrid 65:35 Index", "2020-07-06", True),
    # scheme_id 38: category SO-RT, benchmark Deccan Hybrid 65:35 Index, launched 2021-05-03, 2 plans (Growth only)
    ("Sahyadri Retirement Fund", "SO-RT", "Deccan Hybrid 65:35 Index", "2021-05-03", False),
    # scheme_id 39: category SO-CH, benchmark Deccan Hybrid 65:35 Index, launched 2021-05-03, 2 plans (Growth only)
    ("Sahyadri Children's Fund", "SO-CH", "Deccan Hybrid 65:35 Index", "2021-05-03", False),
    # scheme_id 40: category OT-FF, benchmark Deccan 500 TRI, launched 2022-01-03, 2 plans (Growth only)
    ("Sahyadri Global Equity Fund of Fund", "OT-FF", "Deccan 500 TRI", "2022-01-03", False),
]
# Lookup dict: scheme name -> scheme_id.
SCHEME_ID = {name: i + 1 for i, (name, *_rest) in enumerate(SCHEMES)}
# Lookup dict: category code -> (category_id, the full category tuple).
CATEGORY_BY_CODE = {c[0]: (i + 1, c) for i, c in enumerate(CATEGORIES)}

# Trading days per year, used to turn yearly rates into daily rates.
DAYS_PER_YEAR = 252


# Small helper: which asset class a scheme belongs to.
def asset_class_of(scheme_name: str) -> str:
    """Equity / Debt / Hybrid / Solution Oriented / Other, via the scheme's category."""
    # Find the scheme's row (id - 1 = list position) and take its category code (item 1).
    category_code = SCHEMES[SCHEME_ID[scheme_name] - 1][1]
    # From the category tuple, item 2 is the asset class.
    return CATEGORY_BY_CODE[category_code][1][2]


# The main function of this file; run.py calls it in step 1.
def build(settings: Settings) -> dict[str, pl.DataFrame]:
    """
    Build the 8 fund-house tables. Returns a dictionary of
    table name -> polars DataFrame, ready to be loaded into Postgres.
    """
    # Short name for the shared random number generator.
    rng = settings.rng
    days = business_days(settings.start, settings.end)   # every trading day in the window
    # How many trading days there are (about 1,250 in 5 years).
    n_days = len(days)

    # ---------------------------------------------------------------------
    # scheme_categories and benchmarks: fixed lists, written out directly
    # ---------------------------------------------------------------------
    # A DataFrame is built from a dict: column name -> list of values.
    scheme_categories = pl.DataFrame(
        # The columns: name -> list of values.
        {
            # IDs 1, 2, 3, ... one per category.
            "category_id": list(range(1, len(CATEGORIES) + 1)),
            # A "list comprehension": take item 0 (the code) from every category tuple.
            "category_code": [c[0] for c in CATEGORIES],
            # Item 1 = name, item 2 = asset class, item 3 = risk level.
            "category_name": [c[1] for c in CATEGORIES],
            # (asset class of each category)
            "asset_class": [c[2] for c in CATEGORIES],
            # (risk level of each category)
            "risk_level": [c[3] for c in CATEGORIES],
        }
    )

    # The benchmarks table, built the same way.
    benchmarks = pl.DataFrame(
        # The columns: name -> list of values.
        {
            # IDs 1, 2, 3, ... one per benchmark.
            "benchmark_id": list(range(1, len(BENCHMARKS) + 1)),
            # Item 0 = the name.
            "benchmark_name": [b[0] for b in BENCHMARKS],
            # Item 1 = Equity / Debt / Hybrid.
            "benchmark_type": [b[1] for b in BENCHMARKS],
            # The same invented provider for every benchmark ([x] * n repeats x n times).
            "provider": ["Deccan Indices Ltd"] * len(BENCHMARKS),
        }
    )

    # ---------------------------------------------------------------------
    # benchmark_values: one random-walk price series per benchmark
    # ---------------------------------------------------------------------
    # We work in "log returns": the daily change as a logarithm. Adding up
    # log returns and taking exp() gives the price, and it can never go
    # below zero, which a real index never does.
    #
    # For a yearly drift mu and volatility sigma, the standard daily log
    # return is  normal(mean = (mu - sigma^2 / 2) / 252, sd = sigma / sqrt(252)).
    # The "- sigma^2 / 2" makes the AVERAGE yearly growth come out as mu.
    # A grid of zeros: one row per benchmark, one column per day (daily log returns).
    bench_log_returns = np.zeros((len(BENCHMARKS), n_days))
    # Same-shaped grid for the index levels (prices).
    bench_levels = np.zeros((len(BENCHMARKS), n_days))
    # For every benchmark: i = its row number; mu = drift; sigma = volatility.
    for i, (_name, _type, mu, sigma, start_level) in enumerate(BENCHMARKS):
        # n_days random daily log returns from a normal (bell-curve) distribution.
        daily = rng.normal(
            # mean per day, spread per day, and how many days.
            (mu - sigma**2 / 2) / DAYS_PER_YEAR, sigma / np.sqrt(DAYS_PER_YEAR), n_days
        )
        daily[0] = 0.0   # the first day is the starting level itself
        # Save the returns: the schemes' NAVs are built from them later.
        bench_log_returns[i] = daily
        # cumsum adds the returns up day by day; exp turns them into price ratios.
        bench_levels[i] = start_level * np.exp(np.cumsum(daily))

    # Turn the grid into a long table: one row per benchmark per day.
    benchmark_values = pl.DataFrame(
        # The columns: name -> values.
        {
            # np.repeat: benchmark 1 for every day, then benchmark 2 for every day, ...
            "benchmark_id": np.repeat(np.arange(1, len(BENCHMARKS) + 1), n_days),
            # np.tile: the full list of days, once per benchmark.
            "value_date": np.tile(days, len(BENCHMARKS)),
            # ravel() flattens the grid row by row (same order as repeat/tile); round to 4 decimals.
            "index_value": np.round(bench_levels.ravel(), 4),
        }
    )

    # ---------------------------------------------------------------------
    # schemes
    # ---------------------------------------------------------------------
    # Every scheme's launch date (item 3) as a numpy date array.
    launch_dates = np.array([s[3] for s in SCHEMES], dtype="datetime64[D]")
    # Every scheme's asset class, in scheme order.
    asset_classes = [CATEGORY_BY_CODE[s[1]][1][2] for s in SCHEMES]

    # The schemes table.
    schemes = pl.DataFrame(
        # The columns: name -> values.
        {
            # IDs 1 .. 40.
            "scheme_id": list(range(1, len(SCHEMES) + 1)),
            # Codes SAH001, SAH002, ... ({i:03d} pads the number to 3 digits with zeros).
            "scheme_code": [f"SAH{i:03d}" for i in range(1, len(SCHEMES) + 1)],
            # Item 0 = the name.
            "scheme_name": [s[0] for s in SCHEMES],
            # Look up each scheme's category_id from its category code.
            "category_id": [CATEGORY_BY_CODE[s[1]][0] for s in SCHEMES],
            # Look up each scheme's benchmark_id from its benchmark name.
            "benchmark_id": [BENCHMARK_ID[s[2]] for s in SCHEMES],
            # Launch dates from the list above.
            "launch_date": launch_dates,
            # Every scheme is still running.
            "scheme_status": ["Active"] * len(SCHEMES),
            # Equity and hybrid schemes charge 1% for redeeming within a year;
            # debt schemes charge nothing.
            "exit_load_pct": [0.0 if ac == "Debt" else 1.0 for ac in asset_classes],
            # Smallest SIP: Rs 1,000 for debt schemes, Rs 500 for the rest.
            "min_sip_amount": [1000.0 if ac == "Debt" else 500.0 for ac in asset_classes],
        }
    )

    # ---------------------------------------------------------------------
    # scheme_plans: up to 4 plans per scheme
    # ---------------------------------------------------------------------
    # Collect one dict per plan, then turn the list into a DataFrame.
    plan_rows = []
    # enumerate(..., start=1) numbers the schemes from 1, matching scheme_id.
    for scheme_id, (name, _code, _bench, _launch, has_idcw) in enumerate(SCHEMES, start=1):
        # True for debt schemes.
        is_debt = asset_classes[scheme_id - 1] == "Debt"
        # Yearly fee of the Direct plan, and how much more the Regular plan
        # costs (the distributor's commission). Debt funds are cheaper.
        direct_fee = rng.uniform(0.10, 0.40) if is_debt else rng.uniform(0.45, 1.00)
        # Extra yearly fee of the Regular plan: 0.25-0.60% (debt) or 0.75-1.20% (others).
        extra_fee = rng.uniform(0.25, 0.60) if is_debt else rng.uniform(0.75, 1.20)
        # Schemes without IDCW only get Growth plans.
        options = ["Growth", "IDCW"] if has_idcw else ["Growth"]
        # Two plan types x one or two options = 2 or 4 plans per scheme.
        for plan_type in ["Direct", "Regular"]:
            # ... and for each option (Growth / IDCW) ...
            for option in options:
                # Add this plan's values to the list.
                plan_rows.append(
                    # One plan's values:
                    {
                        # which scheme it belongs to,
                        "scheme_id": scheme_id,
                        # Direct or Regular,
                        "plan_type": plan_type,
                        # Growth or IDCW,
                        "option_type": option,
                        # Regular plans pay the Direct fee plus the extra commission.
                        "expense_ratio_pct": round(
                            # The Direct fee, plus the extra fee for Regular plans, rounded to 4 decimals.
                            direct_fee + (extra_fee if plan_type == "Regular" else 0.0), 4
                        ),
                    }
                )
    # with_row_index adds a plan_id column numbered 1, 2, 3, ...
    scheme_plans = pl.DataFrame(plan_rows).with_row_index("plan_id", offset=1)
    # Build a 12-character ISIN from the plan_id, e.g. INFS01000070 for plan 7.
    # with_columns changes/adds columns; .select puts them in the table's column order.
    scheme_plans = scheme_plans.with_columns(
        # Make plan_id a normal 64-bit whole number.
        pl.col("plan_id").cast(pl.Int64),
        # "INFS01" + plan_id padded to 5 digits + "0" = 12 characters, named "isin".
        ("INFS01" + pl.col("plan_id").cast(pl.Utf8).str.zfill(5) + "0").alias("isin"),
    ).select("plan_id", "scheme_id", "plan_type", "option_type", "isin", "expense_ratio_pct")

    # ---------------------------------------------------------------------
    # nav_history: daily NAV for every plan
    # ---------------------------------------------------------------------
    # One small DataFrame per plan, joined together at the end.
    nav_frames = []
    # Build NAVs scheme by scheme.
    for scheme_id, (name, _code, bench_name, _launch, _idcw) in enumerate(SCHEMES, start=1):
        # This scheme's asset class.
        asset_class = asset_classes[scheme_id - 1]
        # Row number of its benchmark in bench_log_returns.
        bench_idx = BENCHMARK_ID[bench_name] - 1

        # The first trading day this scheme has a NAV: the window start, or
        # its launch date if it launched inside the window.
        # searchsorted finds the position of that date in the sorted list of trading days.
        first_idx = int(np.searchsorted(days, max(settings.start, launch_dates[scheme_id - 1])))
        # The trading days from that position to the end.
        scheme_days = days[first_idx:]

        # Alpha (yearly out- or under-performance) and daily tracking noise.
        # Index funds track their index almost exactly.
        # True if "Index" appears in the scheme name.
        is_index = "Index" in name
        # Index funds trail slightly (fees); others get a random alpha around +1% a year.
        alpha = -0.002 if is_index else rng.normal(0.01, 0.015)
        # How noisy the scheme is compared with its benchmark (yearly size).
        if asset_class == "Debt":
            # Debt schemes: very little noise.
            noise_sd = 0.004
        # Index funds:
        elif is_index:
            # almost no noise (they copy their index).
            noise_sd = 0.003
        # Active equity funds:
        elif asset_class == "Equity":
            # the most noise.
            noise_sd = 0.03
        # Hybrid and others:
        else:
            # medium noise.
            noise_sd = 0.02

        # Start from the benchmark's own daily moves...
        log_returns = bench_log_returns[bench_idx, first_idx:].copy()
        # ...add the scheme's daily noise...
        log_returns += rng.normal(0, noise_sd / np.sqrt(DAYS_PER_YEAR), len(scheme_days))
        # ...and its daily share of alpha.
        alpha_per_day = np.full(len(scheme_days), alpha / DAYS_PER_YEAR)

        # PATTERN 3: the Focused Fund trails before the manager change and
        # beats its benchmark after it.
        if name == P.P3_SCHEME:
            # True for every day on or after the manager change.
            after_change = scheme_days >= P.P3_CHANGE_DATE
            # np.where picks the "after" alpha on True days, the "before" alpha otherwise.
            alpha_per_day = np.where(
                # (yearly alpha divided by 252 trading days = daily alpha)
                after_change, P.P3_ALPHA_AFTER / DAYS_PER_YEAR, P.P3_ALPHA_BEFORE / DAYS_PER_YEAR
            )
        # Add the alpha to every day's return.
        log_returns += alpha_per_day

        # PATTERN 1: a one-day drop on the downgrade date, because the
        # downgraded bond is marked down in the schemes holding it.
        if name in P.P1_HOLDERS:
            # The size of the one-day drop for this scheme (item 3 of its tuple).
            drop = P.P1_HOLDERS[name][3]
            # Position of the downgrade date in this scheme's days.
            drop_idx = int(np.searchsorted(scheme_days, P.P1_DOWNGRADE_DATE))
            # Turn e.g. -1.8% into a log return and add it to that day.
            log_returns[drop_idx] += np.log(1 + drop)

        # The first day is the starting NAV itself (no change).
        log_returns[0] = 0.0
        # Schemes launched inside the window start at Rs 10, the usual
        # launch price. Older schemes have already grown by the window start.
        start_nav = 10.0 if first_idx > 0 else rng.uniform(11, 45)
        # The Direct Growth NAV for every day: start value times the accumulated growth.
        direct_growth = start_nav * np.exp(np.cumsum(log_returns))

        # Years since this scheme's first NAV day, for each day.
        # Day number 0, 1, 2, ... divided by 252 = years since the first day.
        years = np.arange(len(scheme_days)) / DAYS_PER_YEAR

        # Loop over this scheme's plans; iter_rows(named=True) gives each row as a dict.
        for plan in scheme_plans.filter(pl.col("scheme_id") == scheme_id).iter_rows(named=True):
            # Every plan starts from the Direct Growth NAV.
            nav = direct_growth
            # Regular plans only:
            if plan["plan_type"] == "Regular":
                # The Regular plan pays a higher fee every day, so it falls
                # slowly behind the Direct plan. Fee gap in % -> fraction.
                direct_plan_fee = scheme_plans.filter(
                    # The Direct plan of the same scheme.
                    (pl.col("scheme_id") == scheme_id) & (pl.col("plan_type") == "Direct")
                )["expense_ratio_pct"][0]
                # The extra yearly fee as a fraction, e.g. 0.9% -> 0.009.
                fee_gap = (plan["expense_ratio_pct"] - direct_plan_fee) / 100
                # Shrink the NAV a little more every year by that extra fee.
                nav = nav * np.exp(-fee_gap * years)
            # IDCW plans only:
            if plan["option_type"] == "IDCW":
                # IDCW plans pay out part of their gains, so their NAV sits
                # lower. We keep it as a fixed fraction for simplicity.
                nav = nav * 0.72
            # Add this plan's daily NAVs as a small table.
            nav_frames.append(
                # A small table for this plan:
                pl.DataFrame(
                    # The columns: name -> values.
                    {
                        # the plan's ID repeated for every day,
                        "plan_id": np.full(len(scheme_days), plan["plan_id"]),
                        # each trading day,
                        "nav_date": scheme_days,
                        # and the NAV, rounded to 4 decimals.
                        "nav": np.round(nav, 4),
                    }
                )
            )
    # Stack all the small tables into one big nav_history table.
    nav_history = pl.concat(nav_frames)

    # ---------------------------------------------------------------------
    # fund_managers: 25 people, including the two in pattern 3
    # ---------------------------------------------------------------------
    # Start the list with the two pattern-3 managers, so they get IDs 1 and 2.
    names = [P.P3_OLD_MANAGER, P.P3_NEW_MANAGER]
    # Keep adding random names until there are 25.
    while len(names) < 25:
        # A random first name + a random surname.
        candidate = f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}"
        # Skip duplicates, so every manager name is different.
        if candidate not in names:
            # Keep it.
            names.append(candidate)
    # Possible qualifications to pick from.
    qualifications = ["CFA", "MBA (Finance)", "CA", "CFA, MBA", "PGDM", "M.Com, CFA", "FRM"]
    # The fund_managers table.
    fund_managers = pl.DataFrame(
        # The columns: name -> values.
        {
            # IDs 1 .. 25.
            "manager_id": list(range(1, 26)),
            # The 25 names (the pattern-3 pair are IDs 1 and 2).
            "manager_name": names,
            # 25 random whole numbers from 6 to 25 (the upper bound 26 is not included).
            "experience_years": rng.integers(6, 26, 25),
            # About 1% missing values, as real data has: one manager has no
            # qualification recorded.
            "qualification": [None if i == 24 else str(rng.choice(qualifications)) for i in range(25)],
            # Join dates: 1 June 2018 plus 0-899 random days.
            "joined_amc_date": np.datetime64("2018-06-01")
            # (plus a random 0-899 days each)
            + rng.integers(0, 900, 25).astype("timedelta64[D]"),
        }
    )

    # ---------------------------------------------------------------------
    # scheme_manager_assignments: who managed what, and when
    # ---------------------------------------------------------------------
    # Managers 1-2 are the pattern 3 pair (equity). Managers 3-14 run equity,
    # 15-25 run debt. Hybrid and solution schemes get one of each.
    # Manager IDs who can run equity schemes.
    equity_pool = [1, 2] + list(range(3, 15))
    # Manager IDs who can run debt schemes.
    debt_pool = list(range(15, 26))
    # Each assignment is a tuple: (scheme_id, manager_id, role, start, end).
    rows = []
    # Give every scheme its managers.
    for scheme_id, (name, *_rest) in enumerate(SCHEMES, start=1):
        # Assignments start on the scheme's launch date.
        launch = launch_dates[scheme_id - 1]
        # The scheme's asset class decides which pool of managers to use.
        asset_class = asset_classes[scheme_id - 1]
        # Pick from the debt pool, or the equity pool without the pattern-3 pair ([2:] skips IDs 1, 2).
        pool = debt_pool if asset_class == "Debt" else equity_pool[2:]

        # The Focused Fund gets the fixed pattern-3 assignments instead of random ones.
        if name == P.P3_SCHEME:
            # PATTERN 3: old manager until the day before, new manager from the change date.
            rows.append((scheme_id, 1, "Primary", launch, P.P3_CHANGE_DATE - np.timedelta64(1, "D")))
            # Manager 2 (new) from the change date, still managing (end None).
            rows.append((scheme_id, 2, "Primary", P.P3_CHANGE_DATE, None))
            # "continue" skips the rest of the loop body and moves to the next scheme.
            continue

        # A random primary manager from the pool.
        primary = int(rng.choice(pool))
        # About a quarter of schemes changed primary manager once, at a
        # random date inside the window.
        if rng.random() < 0.25:
            # The change date: 120-1699 days after the window start.
            change = settings.start + np.timedelta64(int(rng.integers(120, 1700)), "D")
            # A different manager takes over.
            successor = int(rng.choice([m for m in pool if m != primary]))
            # The first manager until the day before the change ...
            rows.append((scheme_id, primary, "Primary", launch, change - np.timedelta64(1, "D")))
            # ... the successor from the change date onwards.
            rows.append((scheme_id, successor, "Primary", change, None))
        # No change: one primary manager for the whole period (end_date None = still managing).
        else:
            # One primary manager from launch, still managing.
            rows.append((scheme_id, primary, "Primary", launch, None))

        # Hybrid and solution schemes invest in both shares and bonds, so
        # they always have a debt co-manager. Some others have a co-manager too.
        if asset_class in ("Hybrid", "Solution Oriented"):
            # A random debt manager as co-manager.
            rows.append((scheme_id, int(rng.choice(debt_pool)), "Co-manager", launch, None))
        # Other schemes: 35% chance of a co-manager ...
        elif rng.random() < 0.35:
            # The co-manager must not be the primary manager.
            co_pool = [m for m in pool if m != primary]
            # ... picked from the same pool.
            rows.append((scheme_id, int(rng.choice(co_pool)), "Co-manager", launch, None))

    # Turn the tuples into the scheme_manager_assignments table.
    scheme_manager_assignments = pl.DataFrame(
        # The columns: name -> values.
        {
            # IDs 1, 2, 3, ...
            "assignment_id": list(range(1, len(rows) + 1)),
            # Item 0 of each tuple = scheme_id ...
            "scheme_id": [r[0] for r in rows],
            # item 1 = manager_id ...
            "manager_id": [r[1] for r in rows],
            # item 2 = role ...
            "manager_role": [r[2] for r in rows],
            # to_dates turns numpy dates (and None) into proper Date values for polars.
            "start_date": pl.Series(to_dates([r[3] for r in rows]), dtype=pl.Date),
            # item 4 = end date (None = still managing).
            "end_date": pl.Series(to_dates([r[4] for r in rows]), dtype=pl.Date),
        }
    )

    # Hand all 8 tables back to run.py, keyed by table name.
    return {
        # The 36 SEBI categories.
        "scheme_categories": scheme_categories,
        # The 15 benchmark indices.
        "benchmarks": benchmarks,
        # Their daily values.
        "benchmark_values": benchmark_values,
        # The 40 schemes.
        "schemes": schemes,
        # Their ~150 plans.
        "scheme_plans": scheme_plans,
        # The 25 managers.
        "fund_managers": fund_managers,
        # Who managed what, when.
        "scheme_manager_assignments": scheme_manager_assignments,
        # Daily NAV of every plan.
        "nav_history": nav_history,
    }