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

import numpy as np
import polars as pl

from dataplane.generator import patterns as P
from dataplane.generator.common import (
    FIRST_NAMES,
    SURNAMES,
    Settings,
    business_days,
    to_dates,
)

# ---------------------------------------------------------------------------
# FIXED REFERENCE DATA
# ---------------------------------------------------------------------------
# (code, name, asset_class, risk_level). category_id is the position + 1.
CATEGORIES = [
    ("EQ-MC", "Multi Cap Fund", "Equity", "Very High"),
    ("EQ-LC", "Large Cap Fund", "Equity", "Very High"),
    ("EQ-LM", "Large & Mid Cap Fund", "Equity", "Very High"),
    ("EQ-MD", "Mid Cap Fund", "Equity", "Very High"),
    ("EQ-SC", "Small Cap Fund", "Equity", "Very High"),
    ("EQ-DY", "Dividend Yield Fund", "Equity", "Very High"),
    ("EQ-VL", "Value Fund", "Equity", "Very High"),
    ("EQ-FC", "Focused Fund", "Equity", "Very High"),
    ("EQ-ST", "Sectoral/Thematic Fund", "Equity", "Very High"),
    ("EQ-EL", "ELSS", "Equity", "Very High"),
    ("EQ-FX", "Flexi Cap Fund", "Equity", "Very High"),
    ("DT-ON", "Overnight Fund", "Debt", "Low"),
    ("DT-LQ", "Liquid Fund", "Debt", "Low to Moderate"),
    ("DT-US", "Ultra Short Duration Fund", "Debt", "Low to Moderate"),
    ("DT-LD", "Low Duration Fund", "Debt", "Moderate"),
    ("DT-MM", "Money Market Fund", "Debt", "Low to Moderate"),
    ("DT-SD", "Short Duration Fund", "Debt", "Moderate"),
    ("DT-MD", "Medium Duration Fund", "Debt", "Moderate"),
    ("DT-ML", "Medium to Long Duration Fund", "Debt", "Moderate"),
    ("DT-LG", "Long Duration Fund", "Debt", "Moderate"),
    ("DT-DB", "Dynamic Bond Fund", "Debt", "Moderate"),
    ("DT-CB", "Corporate Bond Fund", "Debt", "Moderate"),
    ("DT-CR", "Credit Risk Fund", "Debt", "Moderately High"),
    ("DT-BP", "Banking and PSU Fund", "Debt", "Low to Moderate"),
    ("DT-GL", "Gilt Fund", "Debt", "Moderate"),
    ("DT-FL", "Floater Fund", "Debt", "Low to Moderate"),
    ("HY-CH", "Conservative Hybrid Fund", "Hybrid", "Moderately High"),
    ("HY-AH", "Aggressive Hybrid Fund", "Hybrid", "Very High"),
    ("HY-DA", "Dynamic Asset Allocation Fund", "Hybrid", "Moderately High"),
    ("HY-MA", "Multi Asset Allocation Fund", "Hybrid", "High"),
    ("HY-AR", "Arbitrage Fund", "Hybrid", "Low"),
    ("HY-ES", "Equity Savings Fund", "Hybrid", "Moderate"),
    ("SO-RT", "Retirement Fund", "Solution Oriented", "High"),
    ("SO-CH", "Children's Fund", "Solution Oriented", "High"),
    ("OT-IX", "Index Funds and ETFs", "Other", "Very High"),
    ("OT-FF", "Fund of Funds", "Other", "Very High"),
]

# (name, type, yearly drift, yearly volatility, starting level)
# Drift = average yearly rise. Volatility = how much it swings.
# benchmark_id is the position + 1.
BENCHMARKS = [
    ("Deccan 50 TRI", "Equity", 0.12, 0.15, 18000),
    ("Deccan 100 TRI", "Equity", 0.12, 0.15, 22000),
    ("Deccan 500 TRI", "Equity", 0.13, 0.16, 26000),
    ("Deccan Midcap 150 TRI", "Equity", 0.17, 0.19, 15000),
    ("Deccan Smallcap 250 TRI", "Equity", 0.18, 0.22, 11000),
    ("Deccan Bank TRI", "Equity", 0.10, 0.18, 40000),
    ("Deccan Infrastructure TRI", "Equity", 0.16, 0.18, 7000),
    ("Deccan IT TRI", "Equity", 0.11, 0.20, 35000),
    ("Deccan Liquid Debt Index", "Debt", 0.062, 0.002, 4000),
    ("Deccan Short Duration Debt Index", "Debt", 0.068, 0.012, 4500),
    ("Deccan Corporate Bond Index", "Debt", 0.072, 0.020, 5000),
    ("Deccan Credit Risk Debt Index", "Debt", 0.080, 0.025, 3500),
    ("Deccan Composite Gilt Index", "Debt", 0.070, 0.035, 6000),
    ("Deccan Hybrid 65:35 Index", "Hybrid", 0.105, 0.10, 12000),
    ("Deccan Arbitrage Index", "Hybrid", 0.058, 0.004, 2500),
]
BENCHMARK_ID = {name: i + 1 for i, (name, *_rest) in enumerate(BENCHMARKS)}

# (scheme name, category code, benchmark name, launch date, has IDCW plans)
# scheme_id is the position + 1. 40 schemes.
# Five schemes have no IDCW option, so 35 x 4 + 5 x 2 = 150 plans.
SCHEMES = [
    ("Sahyadri Large Cap Fund", "EQ-LC", "Deccan 100 TRI", "2019-04-15", True),
    ("Sahyadri Flexi Cap Fund", "EQ-FX", "Deccan 500 TRI", "2019-04-15", True),
    ("Sahyadri Multi Cap Fund", "EQ-MC", "Deccan 500 TRI", "2020-02-10", True),
    ("Sahyadri Large & Mid Cap Fund", "EQ-LM", "Deccan 500 TRI", "2019-09-02", True),
    ("Sahyadri Mid Cap Fund", "EQ-MD", "Deccan Midcap 150 TRI", "2019-06-03", True),
    ("Sahyadri Small Cap Fund", "EQ-SC", "Deccan Smallcap 250 TRI", "2020-06-01", True),
    ("Sahyadri Focused Fund", "EQ-FC", "Deccan 500 TRI", "2019-11-04", True),
    ("Sahyadri Value Fund", "EQ-VL", "Deccan 500 TRI", "2020-09-07", True),
    ("Sahyadri Dividend Yield Fund", "EQ-DY", "Deccan 500 TRI", "2021-01-11", True),
    ("Sahyadri ELSS Tax Saver Fund", "EQ-EL", "Deccan 500 TRI", "2019-04-15", True),
    ("Sahyadri Banking & Financial Services Fund", "EQ-ST", "Deccan Bank TRI", "2020-11-02", True),
    ("Sahyadri Infrastructure Fund", "EQ-ST", "Deccan Infrastructure TRI", "2021-03-01", True),
    ("Sahyadri Technology Fund", "EQ-ST", "Deccan IT TRI", "2022-07-04", True),
    ("Sahyadri Manufacturing Opportunities Fund", "EQ-ST", "Deccan 500 TRI", "2024-02-05", True),
    ("Sahyadri Deccan 50 Index Fund", "OT-IX", "Deccan 50 TRI", "2020-03-02", False),
    ("Sahyadri Deccan Midcap 150 Index Fund", "OT-IX", "Deccan Midcap 150 TRI", "2023-01-09", False),
    ("Sahyadri Overnight Fund", "DT-ON", "Deccan Liquid Debt Index", "2019-04-15", True),
    ("Sahyadri Liquid Fund", "DT-LQ", "Deccan Liquid Debt Index", "2019-04-15", True),
    ("Sahyadri Ultra Short Duration Fund", "DT-US", "Deccan Short Duration Debt Index", "2019-07-01", True),
    ("Sahyadri Low Duration Fund", "DT-LD", "Deccan Short Duration Debt Index", "2019-10-01", True),
    ("Sahyadri Money Market Fund", "DT-MM", "Deccan Liquid Debt Index", "2020-01-06", True),
    ("Sahyadri Short Duration Fund", "DT-SD", "Deccan Short Duration Debt Index", "2019-08-05", True),
    ("Sahyadri Medium Duration Fund", "DT-MD", "Deccan Corporate Bond Index", "2020-04-06", True),
    ("Sahyadri Corporate Bond Fund", "DT-CB", "Deccan Corporate Bond Index", "2019-12-02", True),
    ("Sahyadri Credit Risk Fund", "DT-CR", "Deccan Credit Risk Debt Index", "2019-12-02", True),
    ("Sahyadri Banking & PSU Debt Fund", "DT-BP", "Deccan Corporate Bond Index", "2020-05-04", True),
    ("Sahyadri Gilt Fund", "DT-GL", "Deccan Composite Gilt Index", "2020-08-03", True),
    ("Sahyadri Dynamic Bond Fund", "DT-DB", "Deccan Corporate Bond Index", "2020-10-05", True),
    ("Sahyadri Floater Fund", "DT-FL", "Deccan Short Duration Debt Index", "2021-06-07", True),
    ("Sahyadri Long Duration Fund", "DT-LG", "Deccan Composite Gilt Index", "2023-06-05", True),
    ("Sahyadri Medium to Long Duration Fund", "DT-ML", "Deccan Composite Gilt Index", "2021-02-01", True),
    ("Sahyadri Conservative Hybrid Fund", "HY-CH", "Deccan Hybrid 65:35 Index", "2020-01-06", True),
    ("Sahyadri Aggressive Hybrid Fund", "HY-AH", "Deccan Hybrid 65:35 Index", "2019-05-06", True),
    ("Sahyadri Balanced Advantage Fund", "HY-DA", "Deccan Hybrid 65:35 Index", "2019-09-02", True),
    ("Sahyadri Multi Asset Allocation Fund", "HY-MA", "Deccan Hybrid 65:35 Index", "2021-04-05", True),
    ("Sahyadri Arbitrage Fund", "HY-AR", "Deccan Arbitrage Index", "2020-02-03", True),
    ("Sahyadri Equity Savings Fund", "HY-ES", "Deccan Hybrid 65:35 Index", "2020-07-06", True),
    ("Sahyadri Retirement Fund", "SO-RT", "Deccan Hybrid 65:35 Index", "2021-05-03", False),
    ("Sahyadri Children's Fund", "SO-CH", "Deccan Hybrid 65:35 Index", "2021-05-03", False),
    ("Sahyadri Global Equity Fund of Fund", "OT-FF", "Deccan 500 TRI", "2022-01-03", False),
]
SCHEME_ID = {name: i + 1 for i, (name, *_rest) in enumerate(SCHEMES)}
CATEGORY_BY_CODE = {c[0]: (i + 1, c) for i, c in enumerate(CATEGORIES)}

# Trading days per year, used to turn yearly rates into daily rates.
DAYS_PER_YEAR = 252


def asset_class_of(scheme_name: str) -> str:
    """Equity / Debt / Hybrid / Solution Oriented / Other, via the scheme's category."""
    category_code = SCHEMES[SCHEME_ID[scheme_name] - 1][1]
    return CATEGORY_BY_CODE[category_code][1][2]


def build(settings: Settings) -> dict[str, pl.DataFrame]:
    """
    Build the 8 fund-house tables. Returns a dictionary of
    table name -> polars DataFrame, ready to be loaded into Postgres.
    """
    rng = settings.rng
    days = business_days(settings.start, settings.end)   # every trading day in the window
    n_days = len(days)

    # ---------------------------------------------------------------------
    # scheme_categories and benchmarks: fixed lists, written out directly
    # ---------------------------------------------------------------------
    scheme_categories = pl.DataFrame(
        {
            "category_id": list(range(1, len(CATEGORIES) + 1)),
            "category_code": [c[0] for c in CATEGORIES],
            "category_name": [c[1] for c in CATEGORIES],
            "asset_class": [c[2] for c in CATEGORIES],
            "risk_level": [c[3] for c in CATEGORIES],
        }
    )

    benchmarks = pl.DataFrame(
        {
            "benchmark_id": list(range(1, len(BENCHMARKS) + 1)),
            "benchmark_name": [b[0] for b in BENCHMARKS],
            "benchmark_type": [b[1] for b in BENCHMARKS],
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
    bench_log_returns = np.zeros((len(BENCHMARKS), n_days))
    bench_levels = np.zeros((len(BENCHMARKS), n_days))
    for i, (_name, _type, mu, sigma, start_level) in enumerate(BENCHMARKS):
        daily = rng.normal(
            (mu - sigma**2 / 2) / DAYS_PER_YEAR, sigma / np.sqrt(DAYS_PER_YEAR), n_days
        )
        daily[0] = 0.0   # the first day is the starting level itself
        bench_log_returns[i] = daily
        # cumsum adds the returns up day by day; exp turns them into price ratios.
        bench_levels[i] = start_level * np.exp(np.cumsum(daily))

    benchmark_values = pl.DataFrame(
        {
            # np.repeat: benchmark 1 for every day, then benchmark 2 for every day, ...
            "benchmark_id": np.repeat(np.arange(1, len(BENCHMARKS) + 1), n_days),
            # np.tile: the full list of days, once per benchmark.
            "value_date": np.tile(days, len(BENCHMARKS)),
            "index_value": np.round(bench_levels.ravel(), 4),
        }
    )

    # ---------------------------------------------------------------------
    # schemes
    # ---------------------------------------------------------------------
    launch_dates = np.array([s[3] for s in SCHEMES], dtype="datetime64[D]")
    asset_classes = [CATEGORY_BY_CODE[s[1]][1][2] for s in SCHEMES]

    schemes = pl.DataFrame(
        {
            "scheme_id": list(range(1, len(SCHEMES) + 1)),
            "scheme_code": [f"SAH{i:03d}" for i in range(1, len(SCHEMES) + 1)],
            "scheme_name": [s[0] for s in SCHEMES],
            "category_id": [CATEGORY_BY_CODE[s[1]][0] for s in SCHEMES],
            "benchmark_id": [BENCHMARK_ID[s[2]] for s in SCHEMES],
            "launch_date": launch_dates,
            "scheme_status": ["Active"] * len(SCHEMES),
            # Equity and hybrid schemes charge 1% for redeeming within a year;
            # debt schemes charge nothing.
            "exit_load_pct": [0.0 if ac == "Debt" else 1.0 for ac in asset_classes],
            "min_sip_amount": [1000.0 if ac == "Debt" else 500.0 for ac in asset_classes],
        }
    )

    # ---------------------------------------------------------------------
    # scheme_plans: up to 4 plans per scheme
    # ---------------------------------------------------------------------
    plan_rows = []
    for scheme_id, (name, _code, _bench, _launch, has_idcw) in enumerate(SCHEMES, start=1):
        is_debt = asset_classes[scheme_id - 1] == "Debt"
        # Yearly fee of the Direct plan, and how much more the Regular plan
        # costs (the distributor's commission). Debt funds are cheaper.
        direct_fee = rng.uniform(0.10, 0.40) if is_debt else rng.uniform(0.45, 1.00)
        extra_fee = rng.uniform(0.25, 0.60) if is_debt else rng.uniform(0.75, 1.20)
        options = ["Growth", "IDCW"] if has_idcw else ["Growth"]
        for plan_type in ["Direct", "Regular"]:
            for option in options:
                plan_rows.append(
                    {
                        "scheme_id": scheme_id,
                        "plan_type": plan_type,
                        "option_type": option,
                        "expense_ratio_pct": round(
                            direct_fee + (extra_fee if plan_type == "Regular" else 0.0), 4
                        ),
                    }
                )
    scheme_plans = pl.DataFrame(plan_rows).with_row_index("plan_id", offset=1)
    # Build a 12-character ISIN from the plan_id, e.g. INFS01000070 for plan 7.
    scheme_plans = scheme_plans.with_columns(
        pl.col("plan_id").cast(pl.Int64),
        ("INFS01" + pl.col("plan_id").cast(pl.Utf8).str.zfill(5) + "0").alias("isin"),
    ).select("plan_id", "scheme_id", "plan_type", "option_type", "isin", "expense_ratio_pct")

    # ---------------------------------------------------------------------
    # nav_history: daily NAV for every plan
    # ---------------------------------------------------------------------
    nav_frames = []
    for scheme_id, (name, _code, bench_name, _launch, _idcw) in enumerate(SCHEMES, start=1):
        asset_class = asset_classes[scheme_id - 1]
        bench_idx = BENCHMARK_ID[bench_name] - 1

        # The first trading day this scheme has a NAV: the window start, or
        # its launch date if it launched inside the window.
        first_idx = int(np.searchsorted(days, max(settings.start, launch_dates[scheme_id - 1])))
        scheme_days = days[first_idx:]

        # Alpha (yearly out- or under-performance) and daily tracking noise.
        # Index funds track their index almost exactly.
        is_index = "Index" in name
        alpha = -0.002 if is_index else rng.normal(0.01, 0.015)
        if asset_class == "Debt":
            noise_sd = 0.004
        elif is_index:
            noise_sd = 0.003
        elif asset_class == "Equity":
            noise_sd = 0.03
        else:
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
            after_change = scheme_days >= P.P3_CHANGE_DATE
            alpha_per_day = np.where(
                after_change, P.P3_ALPHA_AFTER / DAYS_PER_YEAR, P.P3_ALPHA_BEFORE / DAYS_PER_YEAR
            )
        log_returns += alpha_per_day

        # PATTERN 1: a one-day drop on the downgrade date, because the
        # downgraded bond is marked down in the schemes holding it.
        if name in P.P1_HOLDERS:
            drop = P.P1_HOLDERS[name][3]
            drop_idx = int(np.searchsorted(scheme_days, P.P1_DOWNGRADE_DATE))
            log_returns[drop_idx] += np.log(1 + drop)

        log_returns[0] = 0.0
        # Schemes launched inside the window start at Rs 10, the usual
        # launch price. Older schemes have already grown by the window start.
        start_nav = 10.0 if first_idx > 0 else rng.uniform(11, 45)
        direct_growth = start_nav * np.exp(np.cumsum(log_returns))

        # Years since this scheme's first NAV day, for each day.
        years = np.arange(len(scheme_days)) / DAYS_PER_YEAR

        for plan in scheme_plans.filter(pl.col("scheme_id") == scheme_id).iter_rows(named=True):
            nav = direct_growth
            if plan["plan_type"] == "Regular":
                # The Regular plan pays a higher fee every day, so it falls
                # slowly behind the Direct plan. Fee gap in % -> fraction.
                direct_plan_fee = scheme_plans.filter(
                    (pl.col("scheme_id") == scheme_id) & (pl.col("plan_type") == "Direct")
                )["expense_ratio_pct"][0]
                fee_gap = (plan["expense_ratio_pct"] - direct_plan_fee) / 100
                nav = nav * np.exp(-fee_gap * years)
            if plan["option_type"] == "IDCW":
                # IDCW plans pay out part of their gains, so their NAV sits
                # lower. We keep it as a fixed fraction for simplicity.
                nav = nav * 0.72
            nav_frames.append(
                pl.DataFrame(
                    {
                        "plan_id": np.full(len(scheme_days), plan["plan_id"]),
                        "nav_date": scheme_days,
                        "nav": np.round(nav, 4),
                    }
                )
            )
    nav_history = pl.concat(nav_frames)

    # ---------------------------------------------------------------------
    # fund_managers: 25 people, including the two in pattern 3
    # ---------------------------------------------------------------------
    names = [P.P3_OLD_MANAGER, P.P3_NEW_MANAGER]
    while len(names) < 25:
        candidate = f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}"
        if candidate not in names:
            names.append(candidate)
    qualifications = ["CFA", "MBA (Finance)", "CA", "CFA, MBA", "PGDM", "M.Com, CFA", "FRM"]
    fund_managers = pl.DataFrame(
        {
            "manager_id": list(range(1, 26)),
            "manager_name": names,
            "experience_years": rng.integers(6, 26, 25),
            # About 1% missing values, as real data has: one manager has no
            # qualification recorded.
            "qualification": [None if i == 24 else str(rng.choice(qualifications)) for i in range(25)],
            "joined_amc_date": np.datetime64("2018-06-01")
            + rng.integers(0, 900, 25).astype("timedelta64[D]"),
        }
    )

    # ---------------------------------------------------------------------
    # scheme_manager_assignments: who managed what, and when
    # ---------------------------------------------------------------------
    # Managers 1-2 are the pattern 3 pair (equity). Managers 3-14 run equity,
    # 15-25 run debt. Hybrid and solution schemes get one of each.
    equity_pool = [1, 2] + list(range(3, 15))
    debt_pool = list(range(15, 26))
    rows = []
    for scheme_id, (name, *_rest) in enumerate(SCHEMES, start=1):
        launch = launch_dates[scheme_id - 1]
        asset_class = asset_classes[scheme_id - 1]
        pool = debt_pool if asset_class == "Debt" else equity_pool[2:]

        if name == P.P3_SCHEME:
            # PATTERN 3: old manager until the day before, new manager from the change date.
            rows.append((scheme_id, 1, "Primary", launch, P.P3_CHANGE_DATE - np.timedelta64(1, "D")))
            rows.append((scheme_id, 2, "Primary", P.P3_CHANGE_DATE, None))
            continue

        primary = int(rng.choice(pool))
        # About a quarter of schemes changed primary manager once, at a
        # random date inside the window.
        if rng.random() < 0.25:
            change = settings.start + np.timedelta64(int(rng.integers(120, 1700)), "D")
            successor = int(rng.choice([m for m in pool if m != primary]))
            rows.append((scheme_id, primary, "Primary", launch, change - np.timedelta64(1, "D")))
            rows.append((scheme_id, successor, "Primary", change, None))
        else:
            rows.append((scheme_id, primary, "Primary", launch, None))

        # Hybrid and solution schemes invest in both shares and bonds, so
        # they always have a debt co-manager. Some others have a co-manager too.
        if asset_class in ("Hybrid", "Solution Oriented"):
            rows.append((scheme_id, int(rng.choice(debt_pool)), "Co-manager", launch, None))
        elif rng.random() < 0.35:
            co_pool = [m for m in pool if m != primary]
            rows.append((scheme_id, int(rng.choice(co_pool)), "Co-manager", launch, None))

    scheme_manager_assignments = pl.DataFrame(
        {
            "assignment_id": list(range(1, len(rows) + 1)),
            "scheme_id": [r[0] for r in rows],
            "manager_id": [r[1] for r in rows],
            "manager_role": [r[2] for r in rows],
            "start_date": pl.Series(to_dates([r[3] for r in rows]), dtype=pl.Date),
            "end_date": pl.Series(to_dates([r[4] for r in rows]), dtype=pl.Date),
        }
    )

    return {
        "scheme_categories": scheme_categories,
        "benchmarks": benchmarks,
        "benchmark_values": benchmark_values,
        "schemes": schemes,
        "scheme_plans": scheme_plans,
        "fund_managers": fund_managers,
        "scheme_manager_assignments": scheme_manager_assignments,
        "nav_history": nav_history,
    }