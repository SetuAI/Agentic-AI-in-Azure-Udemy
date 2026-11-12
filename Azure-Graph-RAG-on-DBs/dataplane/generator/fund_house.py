##############################################################################
# dataplane/generator/fund_house.py
#
# PURPOSE:
#   Builds 8 of the 9 fund-house tables — the mutual fund company's own
#   products. Which schemes it runs, the plans inside each scheme, who
#   manages them, what they are measured against, and their daily prices.
#
#     scheme_categories              36 rows   the SEBI categories
#     benchmarks                     15 rows   the indices schemes are measured against
#     benchmark_values           ~18,750 rows  the daily level of each index
#     schemes                        40 rows   the funds themselves
#     scheme_plans                  150 rows   Direct/Regular x Growth/IDCW
#     fund_managers                  25 rows   the people who run the money
#     scheme_manager_assignments    ~70 rows   who ran what, and when
#     nav_history               ~170,000 rows  the daily price of every plan
#
#   THE 9TH TABLE, scheme_aum_monthly, IS NOT BUILT HERE. AUM is the total
#   money invested, which is only known once transactions exist. It is
#   calculated later, in summaries.py.
#
#   This is the FIRST builder run by run.py, and it depends on nothing.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Every question this project answers eventually lands on a scheme, and
#   most of them land on its PRICE. "What did this fund return?" "Is it
#   beating its benchmark?" "Did performance change when the manager
#   changed?" All three are questions about nav_history.
#
#   So the hardest thing in this file is not creating 40 rows of scheme
#   metadata. It is generating 170,000 daily prices that behave like real
#   ones:
#
#     - they never go negative, however many bad days accumulate
#     - equity swings far more than debt
#     - a fund tracks its own benchmark rather than wandering freely
#     - an index fund tracks it almost exactly, and falls behind by its fee
#     - the Regular plan of a scheme falls PERMANENTLY and GROWINGLY behind
#       the Direct plan, because its fee compounds
#
#   Get any of those wrong and every performance question in the course has
#   an implausible answer.
#
# HOW THE PRICES ARE MADE — THREE LAYERS:
#
#   LAYER 1   each BENCHMARK gets a daily price series: a random walk that
#             drifts upward at a realistic yearly rate (about 12% for
#             equity, 6-7% for debt) with realistic daily swings.
#
#   LAYER 2   each scheme's DIRECT GROWTH nav follows its benchmark's daily
#             moves, plus a yearly "alpha" (how much it beats or trails the
#             index) plus a little daily noise of its own.
#
#   LAYER 3   the scheme's OTHER PLANS are derived from that Direct Growth
#             series — the Regular plan losing its extra fee every day, the
#             IDCW plan sitting lower because it pays gains out.
#
#             benchmark random walk
#                      │
#                      ├── + scheme noise + alpha ──> Direct Growth NAV
#                      │                                    │
#                      │                                    ├──> Regular Growth
#                      │                                    │     (minus fee gap,
#                      │                                    │      compounding)
#                      │                                    └──> IDCW plans
#                      │                                          (x 0.72)
#                      └──> benchmark_values table
#
# WHICH PLANTED PATTERNS ARE WRITTEN HERE:
#   PATTERN 3 — the Focused Fund's alpha changes on the manager change date,
#               from trailing its benchmark to beating it. Both the NAV
#               series and the assignment rows are written here.
#   PATTERN 1 — a one-day NAV drop on the downgrade date, in the two schemes
#               holding the downgraded bond.
#
# HOW TO USE:
#   Not run directly. run.py calls it first:
#
#       tables.update(fund_house.build(settings))
#
#   Returns a dictionary of 8 table names to polars DataFrames.
##############################################################################


import numpy as np
# Does all the price arithmetic. Specifically three things Python's own
# libraries cannot do:
#
#   rng.normal(...)   draw thousands of random numbers from a bell curve in
#                     one call, from our seeded generator
#   np.cumsum(...)    running totals over a whole array — how log returns
#                     become a price series
#   np.exp(...)       exponential, applied to a whole array at once
#
# The whole price model for 170,000 NAV rows is a handful of array
# operations. Written as Python loops it would be both slower and much
# harder to read.

import polars as pl
# Holds the finished tables. Used here for DataFrame construction, and in
# two places for filtering (picking a scheme's plans out of scheme_plans).

from dataplane.generator import patterns as P
# Patterns 1 and 3. P3's scheme name, change date and the two alpha values;
# P1's downgrade date and the per-scheme NAV drop.

from dataplane.generator.common import (
    FIRST_NAMES,
    SURNAMES,
    # Used to invent 23 fund manager names. The other two are pattern 3's
    # pair, which have fixed names.
    Settings,
    # The seed, scale and date window. Note this file ignores `scale`
    # entirely — a fund house does not launch more schemes because more
    # people buy, so all 8 tables here are the same size at every scale.
    business_days,
    # The ~1,250 trading days in the window. The spine of every price series.
    to_dates,
    # Converts a list containing None into something polars accepts as a
    # Date column. Needed for the manager assignments, where end_date is
    # empty for a current manager.
)


# ── FIXED REFERENCE DATA ─────────────────────────────────────────────────────
#
# The next three lists are where the fund house is actually designed. They
# are hand-written rather than generated, because these are facts about the
# business, not random draws — and because the same seed must always produce
# the same 40 schemes with the same names.

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
# THE 36 SEBI CATEGORIES. Each a 4-tuple: (code, name, asset class, risk
# level).
#
# These are not invented. SEBI — the Indian market regulator — defines the
# categories every scheme must sit in, and a fund house may run at most one
# scheme per category (with a few exceptions, such as sectoral funds, where
# several are allowed).
#
# THE COMMENT AT THE TOP OF THE ORIGINAL FILE SAYS IT: "category_id is the
# position + 1". POSITION IN THE LIST IS THE DATABASE ID.
#
# That convention runs through this entire file, and it is the single most
# common source of off-by-one errors in the generator. You will see "- 1"
# scattered everywhere below, converting a 1-based database id into a
# 0-based array position. Every one of those is this convention.
#
# READ THE RISK LEVELS as a sanity check on the design: every equity
# category is "Very High", every debt category is "Low" to "Moderately
# High", and the hybrids are spread between. An arbitrage fund is "Low"
# despite holding equity, because its equity position is hedged. Those are
# the real riskometer labels, and getting them wrong would make any
# risk-based question nonsense.

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
# THE 15 INDICES, AND THE TWO NUMBERS THAT GENERATE EVERY PRICE IN THE
# DATABASE.
#
# Each entry is (name, type, YEARLY DRIFT, YEARLY VOLATILITY, starting
# level).
#
#   DRIFT       the average yearly rise. 0.12 means 12% a year.
#   VOLATILITY  how much it swings. 0.15 means a yearly standard deviation
#               of 15%.
#
# THESE NUMBERS ENCODE THE ENTIRE RISK-RETURN STORY OF THE DATASET, and they
# are worth reading as a group rather than individually:
#
#   Smallcap 250    drift 18%, volatility 22%   highest return, biggest swings
#   Midcap 150      drift 17%, volatility 19%
#   Deccan 500      drift 13%, volatility 16%
#   Deccan 50       drift 12%, volatility 15%   the broad large-cap index
#   Bank TRI        drift 10%, volatility 18%   lower return, HIGHER risk
#   ...
#   Credit Risk     drift  8.0%, volatility 2.5%
#   Corporate Bond  drift  7.2%, volatility 2.0%
#   Liquid Debt     drift  6.2%, volatility 0.2%  barely moves at all
#
# TWO THINGS TO NOTICE.
#
# Equity volatility is 50 to 100 times the liquid debt index's. That is
# right: a liquid fund's NAV rises almost in a straight line, while an
# equity fund's wanders. Any question comparing the two has to come out that
# way.
#
# And the Bank index has LOWER drift and HIGHER volatility than the broad
# index — more risk for less return, over this particular window. That is a
# deliberate piece of realism: sector indices are not uniformly better, and
# a sectoral fund is not a free upgrade.
#
# The names are invented ("Deccan" rather than any real index family), so
# nothing in the demo can be mistaken for a real published index.

BENCHMARK_ID = {name: i + 1 for i, (name, *_rest) in enumerate(BENCHMARKS)}
# Name to id lookup, built from the list so the two can never disagree.
#
# The `(name, *_rest)` unpacking takes the first field and discards the rest
# — so adding a sixth field to the BENCHMARKS tuples later would not break
# this line.

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
# THE 40 SCHEMES. Each one (name, category code, benchmark name, launch
# date, whether it offers IDCW plans).
#
# Position + 1 is the scheme_id, same convention as the categories.
#
# FOUR THINGS WORTH POINTING AT.
#
# THE LAUNCH DATES ARE SPREAD FROM 2019 TO 2024, and some fall INSIDE our
# five-year window, which opens on 2021-09-01. That matters twice below: a
# scheme launched inside the window starts at Rs 10 (the standard launch
# price), and it has no NAV rows before its launch date. A fund house where
# all 40 schemes launched on the same day at the same price would look
# obviously artificial.
#
# FIVE SCHEMES HAVE False FOR IDCW — the two index funds, the retirement and
# children's funds, and the fund of funds. Which is why the plan count is
# 150 and not 160:
#
#     35 schemes x 4 plans  +  5 schemes x 2 plans  =  150
#
# A liquid or index fund typically offers Growth only, and a solution-
# oriented fund has a lock-in that makes payouts impractical.
#
# FOUR SCHEMES SHARE THE "EQ-ST" SECTORAL CATEGORY — banking, infrastructure,
# technology and manufacturing. That is the one category where SEBI allows a
# fund house several schemes, and it is also where pattern 6's concentration
# breach lives, because a sectoral fund has a small universe to pick from.
#
# AND SEVERAL SCHEMES SHARE A BENCHMARK. Six point at "Deccan 500 TRI",
# which means six schemes' NAV series are generated from the SAME underlying
# random walk, differing only in their noise and alpha. That is realistic —
# many funds are measured against the same broad index — and it means
# "which funds move together?" has a genuine answer in the data.

SCHEME_ID = {name: i + 1 for i, (name, *_rest) in enumerate(SCHEMES)}
# Name to id. Used by transactions.py for pattern 1's named schemes, and
# exported for anything else that needs to turn a scheme name into an id.

CATEGORY_BY_CODE = {c[0]: (i + 1, c) for i, c in enumerate(CATEGORIES)}
# Category code to (id, the whole tuple).
#
# Note it stores BOTH the id and the original tuple. So a lookup can get the
# id for a foreign key, or reach into the tuple for the asset class — which
# is what asset_class_of() below does. Storing both avoids a second
# dictionary.

DAYS_PER_YEAR = 252
# TRADING days per year, not calendar days.
#
# 252 is the standard figure in finance: 365 days, minus weekends, minus
# about 10 public holidays. It is used to convert YEARLY rates into DAILY
# ones, which happens in four places below.
#
# Note our own calendar ignores public holidays (see common.py), so the
# window actually has about 260 business days per year. Using 252 here
# anyway is a small, deliberate inconsistency: 252 is the convention every
# finance textbook and every practitioner uses, and the resulting difference
# in annualised drift is under 3% — far below the noise in any single
# scheme's series.


def asset_class_of(scheme_name: str) -> str:
    """
    The asset class of a scheme, by name: Equity, Debt, Hybrid, Solution
    Oriented or Other.

    Args:
        scheme_name: the exact scheme name, as it appears in SCHEMES.

    Returns:
        The asset class string from the scheme's SEBI category.

    Raises:
        KeyError: if the name is not one of the 40. Deliberate — a typo in a
                  scheme name should fail loudly here rather than silently
                  returning a default.

    WHY THIS IS A SHARED FUNCTION AND NOT A COLUMN LOOKUP: two other
    generator files need it before the schemes table exists in the database.
    investors.py uses it to decide which schemes offer SIPs (not debt ones),
    and transactions.py uses it to size lump sums (debt funds get bigger
    cheques). Both run in memory, with no database to query.
    """
    category_code = SCHEMES[SCHEME_ID[scheme_name] - 1][1]
    # THREE STEPS, AND THE "- 1" IS THE CONVENTION AGAIN:
    #   SCHEME_ID[name]       the 1-based scheme_id
    #   ... - 1               the 0-based position in SCHEMES
    #   SCHEMES[...][1]       field 1 of that tuple, the category code
    return CATEGORY_BY_CODE[category_code][1][2]
    # Then: [1] takes the tuple out of the (id, tuple) pair, and [2] takes
    # the asset class out of (code, name, asset_class, risk_level).
    #
    # Two chained index operations on an untyped tuple is dense, and it is
    # the kind of line worth reading aloud rather than silently. A named
    # tuple or dataclass would be clearer; a plain tuple is what the file
    # uses throughout, and consistency wins.


# ── THE BUILDER ──────────────────────────────────────────────────────────────

def build(settings: Settings) -> dict[str, pl.DataFrame]:
    """
    Build the 8 fund-house tables.

    Args:
        settings: the seeded generator and the date window. `scale` is
                  ignored — none of these tables grows with it.

    Returns:
        A dictionary of table name to polars DataFrame, ready to load. The
        keys are the PostgreSQL table names, so a typo surfaces at load time
        as "relation does not exist".
    """
    rng = settings.rng
    # The one seeded generator. Every random draw below comes from it, in a
    # fixed order — which is why inserting an extra draw anywhere changes
    # every number after it.

    days = business_days(settings.start, settings.end)   # every trading day in the window
    n_days = len(days)
    # About 1,250 dates. THE SPINE OF EVERY PRICE SERIES IN THE DATABASE —
    # benchmark values and NAVs are both indexed against this array, which
    # is what guarantees a NAV exists for every date a benchmark has, and
    # for every date a transaction can fall on.

    # ---------------------------------------------------------------------
    # scheme_categories and benchmarks: fixed lists, written out directly
    # ---------------------------------------------------------------------
    scheme_categories = pl.DataFrame(
        {
            "category_id": list(range(1, len(CATEGORIES) + 1)),
            # 1 to 36. Generated from the list's length rather than typed,
            # so adding a category needs no change here.
            "category_code": [c[0] for c in CATEGORIES],
            "category_name": [c[1] for c in CATEGORIES],
            "asset_class": [c[2] for c in CATEGORIES],
            "risk_level": [c[3] for c in CATEGORIES],
            # Four list comprehensions, one per tuple field. Each one pulls
            # a column out of the list of tuples — the standard way to turn
            # row-shaped data into column-shaped data.
        }
    )

    benchmarks = pl.DataFrame(
        {
            "benchmark_id": list(range(1, len(BENCHMARKS) + 1)),
            "benchmark_name": [b[0] for b in BENCHMARKS],
            "benchmark_type": [b[1] for b in BENCHMARKS],
            "provider": ["Deccan Indices Ltd"] * len(BENCHMARKS),
            # ALL 15 INDICES COME FROM ONE INVENTED PROVIDER. A real market
            # has several (NSE, BSE, CRISIL), and modelling that would add a
            # dimension no question in this course asks about.
            #
            # Worth noting the drift and volatility fields of the BENCHMARKS
            # tuples are NOT stored in the database. They are generator
            # inputs, not facts about the index — a real index publishes its
            # level, not its expected return. So the table has four columns
            # and the tuple has five.
        }
    )

    # ---------------------------------------------------------------------
    # LAYER 1 — benchmark_values: one random-walk price series per benchmark
    # ---------------------------------------------------------------------
    # THE MOST INTERESTING CODE IN THIS FILE. Everything else in the project
    # that has a price traces back to these ~18,750 numbers.
    #
    # WE WORK IN "LOG RETURNS" — the daily change expressed as a logarithm.
    # Add log returns up and take exp(), and you get the price.
    #
    # WHY LOGARITHMS RATHER THAN PERCENTAGES: a price built from summed log
    # returns CAN NEVER GO BELOW ZERO, however many bad days accumulate,
    # because exp() of any real number is positive. A real index never goes
    # negative either.
    #
    # Generating percentage changes and adding them would eventually produce
    # a negative level, and the schema's CHECK (index_value > 0) would reject
    # the row — which is the database catching a modelling error, but only
    # after the generator had run for 30 seconds.
    #
    # FOR A YEARLY DRIFT mu AND VOLATILITY sigma, the standard daily log
    # return is
    #
    #     normal(mean = (mu - sigma^2 / 2) / 252,  sd = sigma / sqrt(252))
    #
    # and both halves of that need explaining.

    bench_log_returns = np.zeros((len(BENCHMARKS), n_days))
    bench_levels = np.zeros((len(BENCHMARKS), n_days))
    # TWO 2-D ARRAYS, 15 rows by ~1,250 columns. Pre-allocated with zeros
    # and filled by the loop below.
    #
    # KEEPING BOTH IS THE POINT. `bench_levels` becomes the
    # benchmark_values table. `bench_log_returns` is kept because LAYER 2
    # needs the benchmark's DAILY MOVES, not its levels — a scheme's NAV is
    # built by starting from its benchmark's returns and adding to them.
    #
    # Throwing the returns away and re-deriving them from the levels would
    # work and would lose precision at every step.

    for i, (_name, _type, mu, sigma, start_level) in enumerate(BENCHMARKS):
        # A Python loop over 15 benchmarks. Small, fixed count — the
        # vectorised work happens inside, over 1,250 days at a time.
        #
        # The unpacking names the three fields used and underscores the two
        # that are not, so a reader does not go looking for where `_name`
        # is used.

        daily = rng.normal(
            (mu - sigma**2 / 2) / DAYS_PER_YEAR, sigma / np.sqrt(DAYS_PER_YEAR), n_days
        )
        # ~1,250 daily log returns, drawn from a bell curve.
        #
        # THE "- sigma SQUARED OVER 2" LOOKS LIKE NOISE AND IS DOING REAL
        # WORK. It is the single least obvious line in the file.
        #
        # When you compound random multiplicative returns, the AVERAGE
        # outcome drifts BELOW the arithmetic mean — volatility drags
        # compound growth down. An index that gains 10% then loses 10% is
        # not back where it started; it is 1% down.
        #
        # Subtracting half the variance corrects for exactly that, so an
        # index with a stated 12% drift actually grows at 12% a year rather
        # than less. Without it, every equity index in the database would
        # quietly underperform its own stated drift — and so would every
        # scheme tracking it, because layer 2 inherits these returns.
        #
        # The effect is largest where volatility is largest: for the
        # smallcap index at 22% volatility it is about 2.4% a year, which
        # over five years is a 12% error in the final level.
        #
        # AND THE TWO DIVISORS ARE DIFFERENT. The drift is divided by 252;
        # the volatility by the SQUARE ROOT of 252. That is because
        # volatility scales with the square root of time — one of the few
        # pieces of real finance mathematics in this project, and getting it
        # wrong by using 252 for both would make daily swings about 16 times
        # too small.

        daily[0] = 0.0   # the first day is the starting level itself
        # Force the first day's return to zero, so day one IS the starting
        # level rather than the starting level already moved by one day.
        #
        # A one-day difference in 1,250 is immaterial to any answer. It is
        # here because the first row of the table should match the
        # start_level a reader sees in the BENCHMARKS list — and a number
        # that nearly matches is more confusing than one that does.

        bench_log_returns[i] = daily
        # Keep the returns for layer 2.

        bench_levels[i] = start_level * np.exp(np.cumsum(daily))
        # TURN RETURNS INTO PRICES. Read it inside out:
        #
        #   np.cumsum(daily)   running total of log returns: the cumulative
        #                      log change from day one to each day
        #   np.exp(...)        turns each cumulative log change into a price
        #                      RATIO. exp(0) = 1, so day one's ratio is 1.
        #   start_level * ...  scales those ratios into actual index levels
        #
        # One line, 1,250 prices, no loop. This is the whole random walk.

    benchmark_values = pl.DataFrame(
        {
            "benchmark_id": np.repeat(np.arange(1, len(BENCHMARKS) + 1), n_days),
            # np.repeat: benchmark 1 for every day, THEN benchmark 2 for
            # every day, and so on. [1,1,1,...,2,2,2,...]
            "value_date": np.tile(days, len(BENCHMARKS)),
            # np.tile: the FULL list of days, once per benchmark.
            # [day1,day2,...,day1,day2,...]
            #
            # REPEAT AND TILE ARE THE PAIR THAT FLATTENS A 2-D GRID, and
            # they are easy to confuse:
            #
            #   np.repeat([1,2], 3)  ->  [1,1,1,2,2,2]
            #   np.tile([1,2], 3)    ->  [1,2,1,2,1,2]
            #
            # Swap them and the ids and dates are misaligned: benchmark 1
            # would get day 1, benchmark 2 would get day 2, and the table
            # would be comprehensively wrong while loading perfectly.
            "index_value": np.round(bench_levels.ravel(), 4),
            # .ravel() flattens the 15 x 1,250 array into one long array,
            # reading ROW BY ROW — which is the same order repeat/tile
            # produce. That agreement is what keeps the three columns
            # aligned.
            #
            # Rounded to 4 decimals, matching numeric(14,4) in the schema.
        }
    )

    # ---------------------------------------------------------------------
    # schemes
    # ---------------------------------------------------------------------
    launch_dates = np.array([s[3] for s in SCHEMES], dtype="datetime64[D]")
    # The launch dates as numpy dates. The strings in SCHEMES are parsed
    # here, once, rather than at each use.
    #
    # dtype="datetime64[D]" pins the unit to days — the same defensive habit
    # as everywhere else in the generator.

    asset_classes = [CATEGORY_BY_CODE[s[1]][1][2] for s in SCHEMES]
    # Each scheme's asset class, looked up through its category code.
    #
    # Computed ONCE here and reused five times below — in the exit load, the
    # minimum SIP, the plan fees, the NAV noise level and the manager pools.
    # Calling asset_class_of() at each site would do the same lookups
    # repeatedly.

    schemes = pl.DataFrame(
        {
            "scheme_id": list(range(1, len(SCHEMES) + 1)),
            "scheme_code": [f"SAH{i:03d}" for i in range(1, len(SCHEMES) + 1)],
            # "SAH001" to "SAH040". The :03d pads with leading zeros, which
            # makes the codes sort correctly as text — "SAH040" after
            # "SAH009", where "SAH40" would sort before "SAH9".
            "scheme_name": [s[0] for s in SCHEMES],
            "category_id": [CATEGORY_BY_CODE[s[1]][0] for s in SCHEMES],
            # [0] of the pair is the id. This is the foreign key to
            # scheme_categories.
            "benchmark_id": [BENCHMARK_ID[s[2]] for s in SCHEMES],
            # Foreign key to benchmarks, by name lookup. If a scheme named a
            # benchmark that does not exist, this raises KeyError here —
            # which is far better than a foreign key violation at load time.
            "launch_date": launch_dates,
            "scheme_status": ["Active"] * len(SCHEMES),
            # ALL 40 ARE ACTIVE. The schema allows 'Merged' and 'Closed',
            # and we never produce either.
            #
            # Worth saying out loud rather than leaving as a trap: the schema
            # permits states the data does not contain. A model reading only
            # the CHECK constraint will believe closed schemes exist; one
            # that samples actual values will not. That is exactly why the
            # metadata pipeline samples real values rather than trusting
            # constraints.
            "exit_load_pct": [0.0 if ac == "Debt" else 1.0 for ac in asset_classes],
            # 1% for equity and hybrid, nothing for debt.
            #
            # REAL BEHAVIOUR, NOT A SIMPLIFICATION: an exit load discourages
            # short-term trading in a fund that holds illiquid assets. A
            # liquid or overnight fund is designed for short-term parking,
            # so charging to leave would defeat its purpose.
            #
            # Note the schema's CHECK is >= 0, not > 0, precisely so these
            # zeros are legal.
            "min_sip_amount": [1000.0 if ac == "Debt" else 500.0 for ac in asset_classes],
            # Rs 500 for equity, Rs 1,000 for debt. Debt funds attract
            # larger, less frequent money, so their minimum is higher — the
            # same logic that makes transactions.py give debt funds bigger
            # lump sums.
            #
            # NOTE created_at IS ABSENT from this frame, even though the
            # table has it. The schema gives it DEFAULT now(), so PostgreSQL
            # fills it in. That works because these 40 rows are reference
            # data created today — unlike transactions, which are backdated
            # across five years and therefore must supply their own
            # timestamp.
        }
    )

    # ---------------------------------------------------------------------
    # scheme_plans: up to 4 plans per scheme
    # ---------------------------------------------------------------------
    plan_rows = []
    for scheme_id, (name, _code, _bench, _launch, has_idcw) in enumerate(SCHEMES, start=1):
        # enumerate(..., start=1) so scheme_id is the database id directly,
        # with no "+ 1" needed. The same trick appears in the NAV loop below.

        is_debt = asset_classes[scheme_id - 1] == "Debt"
        # And there is the "- 1" again: asset_classes is a 0-based list.

        direct_fee = rng.uniform(0.10, 0.40) if is_debt else rng.uniform(0.45, 1.00)
        extra_fee = rng.uniform(0.25, 0.60) if is_debt else rng.uniform(0.75, 1.20)
        # THE TWO NUMBERS THAT MAKE DIRECT AND REGULAR DIFFER.
        #
        #   direct_fee  the Direct plan's yearly expense ratio, in percent
        #   extra_fee   how much MORE the Regular plan costs — the
        #               distributor's commission
        #
        # DEBT FUNDS ARE MUCH CHEAPER, and the ranges say why clearly: a
        # debt Direct plan costs 0.10% to 0.40% a year, an equity one 0.45%
        # to 1.00%. Running a bond portfolio takes far less research than
        # running an equity one, and the regulator caps debt fund expenses
        # lower.
        #
        # These are drawn per scheme, so the two plans of one scheme are
        # related and two different schemes are not. Drawing them per PLAN
        # would let a scheme's Regular plan come out cheaper than its
        # Direct plan, which cannot happen in reality.

        options = ["Growth", "IDCW"] if has_idcw else ["Growth"]
        # The fifth field of the scheme tuple, finally used. Five schemes
        # get Growth only, which is why there are 150 plans and not 160.

        for plan_type in ["Direct", "Regular"]:
            for option in options:
                # A NESTED LOOP: 2 plan types x 1 or 2 options. Four rows for
                # most schemes, two for five of them.
                plan_rows.append(
                    {
                        "scheme_id": scheme_id,
                        "plan_type": plan_type,
                        "option_type": option,
                        "expense_ratio_pct": round(
                            direct_fee + (extra_fee if plan_type == "Regular" else 0.0), 4
                        ),
                        # THE REGULAR PLAN PAYS BOTH FEES; THE DIRECT PLAN
                        # PAYS ONE. That single conditional is the entire
                        # economic difference between the two, and the NAV
                        # calculation below turns it into a price gap that
                        # widens every year.
                        #
                        # Note BOTH options of a plan type get the SAME
                        # expense ratio — Growth and IDCW differ in what
                        # they do with gains, not in what they charge.
                    }
                )

    scheme_plans = pl.DataFrame(plan_rows).with_row_index("plan_id", offset=1)
    # A list of dictionaries becomes a DataFrame with the keys as columns.
    # Then with_row_index adds plan_id as 1, 2, 3... — assigned in the order
    # the loops produced, so plans are grouped by scheme.
    #
    # offset=1 because with_row_index starts at 0 by default and this is a
    # primary key.

    scheme_plans = scheme_plans.with_columns(
        pl.col("plan_id").cast(pl.Int64),
        # with_row_index produces an unsigned 32-bit integer. PostgreSQL has
        # no unsigned types, so cast it explicitly rather than leaving the
        # loader to guess.
        ("INFS01" + pl.col("plan_id").cast(pl.Utf8).str.zfill(5) + "0").alias("isin"),
        # BUILD A 12-CHARACTER ISIN FROM THE plan_id. For plan 7:
        #
        #   "INFS01"              6 characters, a fixed invented prefix
        #   zfill(5) of "7"       "00007"
        #   "0"                   1 check-digit placeholder
        #                         ----
        #                         12 characters total
        #
        # The schema has CHECK (length(isin) = 12), so the arithmetic has to
        # be exactly right — 6 + 5 + 1. A zfill(4) here would fail the load
        # on every row.
        #
        # zfill pads with leading zeros to a fixed width, which is what
        # keeps the length constant as plan_id grows from 1 to 150.
        #
        # Deriving the ISIN from the id rather than drawing it randomly
        # guarantees uniqueness for free, since plan_id is already unique.
    ).select("plan_id", "scheme_id", "plan_type", "option_type", "isin", "expense_ratio_pct")
    # An explicit .select() to fix the column ORDER to match the schema.
    #
    # This matters because the loader uses COPY, which — even with columns
    # named — benefits from the frame matching the table. More importantly
    # it documents the table's shape at the point the frame is finished.

    # ---------------------------------------------------------------------
    # LAYER 2 AND 3 — nav_history: daily NAV for every plan
    # ---------------------------------------------------------------------
    # The largest table in this file: ~150 plans x ~1,250 days, so about
    # 170,000 rows. Only transactions is bigger.

    nav_frames = []
    for scheme_id, (name, _code, bench_name, _launch, _idcw) in enumerate(SCHEMES, start=1):
        asset_class = asset_classes[scheme_id - 1]
        bench_idx = BENCHMARK_ID[bench_name] - 1
        # The benchmark's 0-based row in the bench_log_returns array. The
        # "- 1" converts the 1-based benchmark_id.

        first_idx = int(np.searchsorted(days, max(settings.start, launch_dates[scheme_id - 1])))
        scheme_days = days[first_idx:]
        # WHERE THIS SCHEME'S PRICE HISTORY STARTS.
        #
        # max(window start, launch date) — whichever is LATER. A scheme
        # launched in 2019 starts at the window open; one launched in 2024
        # starts at its launch.
        #
        # np.searchsorted finds the position where that date would be
        # inserted to keep `days` sorted — i.e. the index of the first
        # trading day on or after it. A binary search, so it is fast, and it
        # handles a launch date that falls on a weekend by landing on the
        # next trading day.
        #
        # THE CONSEQUENCE: a scheme launched inside the window has NO NAV
        # ROWS before its launch date. Which is correct, and it is also why
        # the generator must never create a transaction before a scheme's
        # launch — there would be no price to divide by.

        is_index = "Index" in name
        alpha = -0.002 if is_index else rng.normal(0.01, 0.015)
        # ALPHA IS YEARLY OUT- OR UNDER-PERFORMANCE AGAINST THE BENCHMARK.
        #
        # An index fund gets a FIXED minus 0.2% — it is supposed to track
        # its index exactly, and it falls behind by roughly its own fee.
        # That is the correct behaviour for an index fund and it would be
        # wrong to give it a random alpha.
        #
        # Every other scheme draws from a bell curve centred on +1% with a
        # 1.5% spread. So most schemes slightly beat their benchmark, some
        # trail, and a few beat it substantially. Centring on exactly zero
        # would be more "honest" and would make every performance question
        # a coin flip; +1% gives the data a mild positive skew, which is
        # roughly what actively managed funds show before fees.
        #
        # Matching on the NAME to detect an index fund is crude, and it is
        # the one place in this file that does so rather than using the
        # category code. It works because both index schemes have "Index" in
        # their names. Worth flagging as the kind of shortcut that is fine in
        # a generator and would be a bug in production code.

        if asset_class == "Debt":
            noise_sd = 0.004
        elif is_index:
            noise_sd = 0.003
        elif asset_class == "Equity":
            noise_sd = 0.03
        else:
            noise_sd = 0.02
        # HOW MUCH THE SCHEME DEVIATES FROM ITS BENCHMARK DAY TO DAY, as a
        # yearly standard deviation.
        #
        #   index fund   0.003   lowest of all — it tracks almost exactly
        #   debt         0.004   a bond manager has little room to deviate
        #   hybrid/other 0.02
        #   equity       0.03    the most room to be different from the index
        #
        # THE INDEX FUND HAVING THE LOWEST NOISE IS THE DETAIL TO POINT AT.
        # Giving an index fund the same noise as an active equity fund would
        # make it look like a badly run active fund — and "does this index
        # fund track its index?" would get the wrong answer.
        #
        # Note the order of the branches matters: `is_index` is checked
        # before `Equity`, because both index schemes ARE in the "Other"
        # asset class, but an index fund tracking an equity index would
        # otherwise fall through to the final `else`.

        log_returns = bench_log_returns[bench_idx, first_idx:].copy()
        # START FROM THE BENCHMARK'S OWN DAILY MOVES, from this scheme's
        # first day onward.
        #
        # .copy() IS LOAD-BEARING. The next three lines ADD to this array in
        # place. Without the copy, they would modify bench_log_returns
        # itself — corrupting the benchmark for every other scheme that
        # shares it, and six schemes share "Deccan 500 TRI".
        #
        # The symptom would be that schemes processed later inherit earlier
        # schemes' alpha and noise, accumulating. Nothing would error. The
        # prices would just be wrong, in a way that gets worse down the list.

        log_returns += rng.normal(0, noise_sd / np.sqrt(DAYS_PER_YEAR), len(scheme_days))
        # ADD THE SCHEME'S OWN DAILY NOISE. Centred on zero, so it adds
        # variation without adding return.
        #
        # Divided by sqrt(252) for the same square-root-of-time reason as
        # the benchmark volatility above.

        alpha_per_day = np.full(len(scheme_days), alpha / DAYS_PER_YEAR)
        # The scheme's daily share of alpha, as an array rather than a
        # scalar — because pattern 3 replaces it with two different values
        # below, and that needs an array.

        if name == P.P3_SCHEME:
            after_change = scheme_days >= P.P3_CHANGE_DATE
            alpha_per_day = np.where(
                after_change, P.P3_ALPHA_AFTER / DAYS_PER_YEAR, P.P3_ALPHA_BEFORE / DAYS_PER_YEAR
            )
        # PATTERN 3. The Focused Fund trails its benchmark by 3% a year
        # before the manager change, and beats it by 6% a year after.
        #
        # NOTE IT IS A CHANGE OF RATE, NOT A STEP IN THE PRICE. The NAV does
        # not jump on the change date; it starts DRIFTING differently. A step
        # would be obviously artificial, and would read as a data error
        # rather than a performance change.
        #
        # A nine-point swing is large but not absurd for a focused fund
        # changing strategy — and it has to be large enough to be visible
        # above the 3% daily noise an equity fund carries, or the pattern
        # would not be findable in the available sample.
        #
        # np.where picks elementwise: the "after" rate where the date is on
        # or after the change, the "before" rate elsewhere.

        log_returns += alpha_per_day
        # Add alpha. Note this line runs for EVERY scheme — the pattern 3
        # block above only changed what `alpha_per_day` contains.

        if name in P.P1_HOLDERS:
            drop = P.P1_HOLDERS[name][3]
            drop_idx = int(np.searchsorted(scheme_days, P.P1_DOWNGRADE_DATE))
            log_returns[drop_idx] += np.log(1 + drop)
        # PATTERN 1. A ONE-DAY DROP on the downgrade date, because the
        # downgraded bond is marked down in the schemes holding it.
        #
        # [3] is the fourth field of the P1_HOLDERS tuple — minus 0.018 for
        # the Credit Risk Fund, minus 0.006 for the Medium Duration Fund.
        # The other three fields are used by summaries.py and
        # transactions.py, which is what keeps all three files telling the
        # same story.
        #
        # np.log(1 + drop) IS THE LINE THAT MATTERS, because we are working
        # in log space. A minus 1.8% move is log(0.982), which is about
        # minus 0.01816 — not minus 0.018.
        #
        # Using the raw fraction would be close enough to look right and
        # wrong enough to compound through the remaining 600 days of the
        # series. In log space you add logs; mixing in a plain percentage is
        # a units error.
        #
        # Note this is += , not = . The day's benchmark move, noise and
        # alpha all still apply; the drop is added on top. Replacing would
        # discard them.

        log_returns[0] = 0.0
        # Force day one to zero, so the series starts exactly at start_nav.
        # Same reasoning as the benchmark series.
        #
        # Note this comes AFTER the pattern blocks, which is correct: if a
        # scheme's first day somehow coincided with the downgrade date, the
        # drop would be wiped — and that is the right outcome, because a
        # scheme has no price history to drop from on its first day.

        start_nav = 10.0 if first_idx > 0 else rng.uniform(11, 45)
        # THE SINGLE `if` THAT MAKES THE NAV TABLE LOOK REAL.
        #
        #   first_idx > 0   the scheme launched INSIDE the window, so it
        #                   starts at Rs 10 — the standard launch price for
        #                   a new mutual fund scheme in India
        #   first_idx == 0  the scheme already existed when the window
        #                   opened, so it starts somewhere between Rs 11 and
        #                   Rs 45, having already grown
        #
        # Without this, all 40 schemes would start at the same price on the
        # same day, and the NAV table would look obviously manufactured. It
        # would also make "which fund has grown most since inception?"
        # meaningless.

        direct_growth = start_nav * np.exp(np.cumsum(log_returns))
        # THE SCHEME'S DIRECT GROWTH NAV SERIES. Same cumsum-then-exp as the
        # benchmark. This is the series all the scheme's other plans are
        # derived from.

        years = np.arange(len(scheme_days)) / DAYS_PER_YEAR
        # Years elapsed since this scheme's first NAV day, for each day:
        # 0, 0.004, 0.008, ... up to about 5.
        #
        # Used by the Regular plan fee calculation below, which needs to
        # know how long the fee has been compounding.

        for plan in scheme_plans.filter(pl.col("scheme_id") == scheme_id).iter_rows(named=True):
            # Each of this scheme's plans. iter_rows(named=True) yields
            # dictionaries, so fields are reached by name.
            #
            # A filter inside a loop over 40 schemes means 40 filters over a
            # 150-row frame — trivially cheap, and much clearer than
            # pre-grouping.

            nav = direct_growth
            # Start from Direct Growth. The two conditionals below modify it.

            if plan["plan_type"] == "Regular":
                direct_plan_fee = scheme_plans.filter(
                    (pl.col("scheme_id") == scheme_id) & (pl.col("plan_type") == "Direct")
                )["expense_ratio_pct"][0]
                # Look up this scheme's DIRECT plan fee, so the gap can be
                # computed. [0] takes the first match — both Direct plans
                # (Growth and IDCW) have the same fee, so either will do.

                fee_gap = (plan["expense_ratio_pct"] - direct_plan_fee) / 100
                # The extra yearly cost of the Regular plan, as a FRACTION.
                # The column is in percent, so a 0.90 there is 0.009 here.
                #
                # The / 100 is easy to omit and the error would be large: a
                # 0.9% fee treated as 90% would drive the Regular NAV to
                # almost nothing over five years.

                nav = nav * np.exp(-fee_gap * years)
                # THE REGULAR PLAN FALLS PERMANENTLY AND GROWINGLY BEHIND.
                #
                # NOT A FIXED DISCOUNT — A WIDENING GAP, because the fee
                # compounds. exp(-fee_gap * years) is about 0.991 after one
                # year and about 0.956 after five, for a 0.9% gap.
                #
                # Which is exactly the real argument for buying Direct
                # plans, visible in the data: the gap is small in year one
                # and substantial by year five.
                #
                # Subtracting a constant instead would make the gap the same
                # on day one and day 1,250, and "how much does the
                # commission cost me over five years?" would have no answer.

            if plan["option_type"] == "IDCW":
                nav = nav * 0.72
                # AN HONEST SIMPLIFICATION, and worth stating rather than
                # hiding.
                #
                # An IDCW plan pays part of its gains out, so its NAV sits
                # lower than the Growth plan of the same scheme. We apply a
                # flat 72%.
                #
                # IN REALITY THAT GAP DEPENDS ON HOW MUCH HAS BEEN PAID OUT
                # OVER TIME, so it would start near 100% at launch and widen
                # with each distribution. Modelling it properly would mean
                # generating a payout schedule per scheme.
                #
                # We use a constant because no question in this course asks
                # about payout history. The consequence a student should know
                # about: the IDCW series is NOT a realistic IDCW series. It
                # is a plausible-looking lower line, and any return
                # calculated from it alone understates the plan's true total
                # return.

            nav_frames.append(
                pl.DataFrame(
                    {
                        "plan_id": np.full(len(scheme_days), plan["plan_id"]),
                        # np.full repeats one value for every day. The plan
                        # id is the same on all ~1,250 rows.
                        "nav_date": scheme_days,
                        "nav": np.round(nav, 4),
                        # 4 decimals, matching numeric(12,4) and matching how
                        # Indian fund houses publish NAVs.
                    }
                )
            )
            # One small frame per plan, accumulated in a list. Concatenating
            # once at the end is far faster than growing a DataFrame 150
            # times.

    nav_history = pl.concat(nav_frames)
    # ~150 frames become one table of about 170,000 rows.

    # ---------------------------------------------------------------------
    # fund_managers: 25 people, including the two in pattern 3
    # ---------------------------------------------------------------------
    names = [P.P3_OLD_MANAGER, P.P3_NEW_MANAGER]
    # PATTERN 3'S PAIR GOES FIRST, at positions 0 and 1 — so they become
    # manager_id 1 and 2. The assignment loop below refers to those ids
    # directly, which is why their position is fixed rather than searched
    # for.

    while len(names) < 25:
        candidate = f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}"
        if candidate not in names:
            names.append(candidate)
    # Fill to 25 with invented names, REJECTING DUPLICATES.
    #
    # The while-with-a-check loop rather than a single draw, because
    # rng.choice can repeat. 54 first names x 42 surnames gives 2,268
    # combinations for 23 draws, so collisions are rare but not impossible —
    # and manager_name is not UNIQUE in the schema, so a duplicate would
    # load fine and produce two managers with one name, which is realistic
    # for investors but confusing for a 25-person list that a demo question
    # might name.

    qualifications = ["CFA", "MBA (Finance)", "CA", "CFA, MBA", "PGDM", "M.Com, CFA", "FRM"]
    # Real Indian fund management qualifications. CFA is the Chartered
    # Financial Analyst charter, CA a Chartered Accountant, FRM a Financial
    # Risk Manager.

    fund_managers = pl.DataFrame(
        {
            "manager_id": list(range(1, 26)),
            "manager_name": names,
            "experience_years": rng.integers(6, 26, 25),
            # 6 to 25 years. rng.integers excludes the upper bound, so 26
            # gives a maximum of 25.
            #
            # Nobody under 6 years: a fund manager running money has been in
            # the industry a while. The schema's CHECK is >= 0, which is
            # looser than the data.
            "qualification": [None if i == 24 else str(rng.choice(qualifications)) for i in range(25)],
            # EXACTLY ONE MANAGER HAS NO QUALIFICATION RECORDED — the last
            # one, deliberately.
            #
            # One row in 25 is 4%, which is the same "real data has gaps"
            # idea as investors.py's blank_one_percent, done
            # deterministically because 25 rows is too few for a 1% random
            # chance to reliably produce one.
            #
            # It is the only nullable column in this table, and it exists so
            # that a query joining managers has something to be careful
            # about.
            #
            # str(...) around rng.choice because numpy returns its own string
            # type, which polars would store as an object column.
            "joined_amc_date": np.datetime64("2018-06-01")
            + rng.integers(0, 900, 25).astype("timedelta64[D]"),
            # Joined between mid-2018 and late 2020 — so every manager was
            # at the fund house BEFORE the window opens on 2021-09-01.
            #
            # A manager whose joining date fell after an assignment they held
            # would be an obvious data bug, and the assignment loop below
            # starts assignments at a scheme's LAUNCH date, some of which are
            # in 2019.
        }
    )

    # ---------------------------------------------------------------------
    # scheme_manager_assignments: who managed what, and when
    # ---------------------------------------------------------------------
    # Managers 1-2 are the pattern 3 pair (equity). Managers 3-14 run equity,
    # 15-25 run debt. Hybrid and solution schemes get one of each.
    equity_pool = [1, 2] + list(range(3, 15))
    debt_pool = list(range(15, 26))
    # TWO POOLS, SPLIT BY SPECIALISM. A debt manager does not run an equity
    # fund, and vice versa — which is true in reality and is also what makes
    # "who runs our equity book?" a meaningful question.
    #
    # equity_pool includes 1 and 2 at the front, then 3 to 14. But note it
    # is sliced below as equity_pool[2:], which DROPS 1 and 2 — so pattern
    # 3's pair are never randomly assigned to another scheme. They exist
    # solely for the Focused Fund, which keeps the pattern clean.
    #
    # Building the list with 1 and 2 at the front and then slicing them off
    # is slightly roundabout; the effect is that the pattern pair are
    # declared as equity managers (for anyone reading the list) and excluded
    # from the random draw (by the slice).

    rows = []
    for scheme_id, (name, *_rest) in enumerate(SCHEMES, start=1):
        launch = launch_dates[scheme_id - 1]
        asset_class = asset_classes[scheme_id - 1]
        pool = debt_pool if asset_class == "Debt" else equity_pool[2:]
        # Equity, hybrid, solution-oriented and "other" schemes all draw
        # from the equity pool. Only debt schemes get debt managers as
        # primary.

        if name == P.P3_SCHEME:
            rows.append((scheme_id, 1, "Primary", launch, P.P3_CHANGE_DATE - np.timedelta64(1, "D")))
            rows.append((scheme_id, 2, "Primary", P.P3_CHANGE_DATE, None))
            continue
        # PATTERN 3'S TWO ROWS, written explicitly.
        #
        # The old manager's assignment ENDS THE DAY BEFORE the new one
        # starts — so there is NO GAP and NO OVERLAP. A gap would mean a day
        # with no primary manager; an overlap would mean two, which the
        # schema permits and the business does not.
        #
        # The new manager's end_date is None, meaning "still managing
        # today".
        #
        # `continue` skips the random logic below entirely, so this scheme
        # gets exactly two assignment rows and no co-manager. Clean.

        primary = int(rng.choice(pool))
        if rng.random() < 0.25:
            change = settings.start + np.timedelta64(int(rng.integers(120, 1700)), "D")
            successor = int(rng.choice([m for m in pool if m != primary]))
            rows.append((scheme_id, primary, "Primary", launch, change - np.timedelta64(1, "D")))
            rows.append((scheme_id, successor, "Primary", change, None))
        else:
            rows.append((scheme_id, primary, "Primary", launch, None))
        # ABOUT A QUARTER OF SCHEMES CHANGED PRIMARY MANAGER ONCE, at a
        # random date inside the window.
        #
        # AND THIS IS WHAT MAKES PATTERN 3 FINDABLE RATHER THAN OBVIOUS. If
        # the Focused Fund were the ONLY scheme that ever changed manager,
        # you would not need a query — you would just look at the table and
        # see the one scheme with two rows.
        #
        # Ten or so schemes changing manager is the background noise the
        # planted one has to be found against. A planted signal needs a
        # populated baseline, and here the baseline is built by a single
        # `if rng.random() < 0.25`.
        #
        # The successor is drawn from the pool EXCLUDING the current primary
        # — a manager does not hand a fund over to themselves.
        #
        # The same no-gap-no-overlap construction as pattern 3: end the day
        # before the successor starts.
        #
        # Note the change date is drawn 120 to 1,700 days after the WINDOW
        # START, not after the scheme's launch. For a scheme launched in
        # 2024 that could in principle land before its launch date — which
        # would produce an assignment starting before the scheme existed.
        # With a window opening in Sep 2021 and a minimum of 120 days, the
        # earliest change is Jan 2022, and the latest-launching scheme is
        # Feb 2024, so some combinations do overlap oddly. Worth knowing as
        # a rough edge rather than a crash: the schema's only date rule here
        # is end_date >= start_date, which still holds.

        if asset_class in ("Hybrid", "Solution Oriented"):
            rows.append((scheme_id, int(rng.choice(debt_pool)), "Co-manager", launch, None))
        elif rng.random() < 0.35:
            co_pool = [m for m in pool if m != primary]
            rows.append((scheme_id, int(rng.choice(co_pool)), "Co-manager", launch, None))
        # CO-MANAGERS, AND THE FIRST BRANCH IS REAL DOMAIN LOGIC.
        #
        # A hybrid or solution-oriented scheme invests in BOTH shares and
        # bonds, so it ALWAYS gets a debt co-manager alongside its equity
        # primary. That is how such funds are actually staffed, and it is a
        # small piece of realism that costs one line.
        #
        # Other schemes get a co-manager 35% of the time, from the same
        # specialism as the primary.
        #
        # Co-manager assignments always run from launch to None — we do not
        # model co-managers changing. Which keeps the table's ~70 rows
        # readable while still making it a genuine many-to-many table.

    scheme_manager_assignments = pl.DataFrame(
        {
            "assignment_id": list(range(1, len(rows) + 1)),
            "scheme_id": [r[0] for r in rows],
            "manager_id": [r[1] for r in rows],
            "manager_role": [r[2] for r in rows],
            "start_date": pl.Series(to_dates([r[3] for r in rows]), dtype=pl.Date),
            "end_date": pl.Series(to_dates([r[4] for r in rows]), dtype=pl.Date),
            # BOTH DATES GO THROUGH to_dates(), and end_date is why.
            #
            # That list mixes numpy dates with None — a current manager has
            # no end date. polars cannot build a Date column from such a
            # mixture; it would fall back to a generic object column, which
            # the CSV loader cannot write.
            #
            # start_date has no Nones at all, so it does not strictly need
            # the conversion. Passing both through keeps the two lines
            # symmetrical and means adding a nullable start date later would
            # not need a code change.
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
    # EIGHT TABLES. The dictionary keys are the PostgreSQL table names, so
    # copy_loader.py can look up each frame by the table it belongs in.
    #
    # A typo in a key here surfaces at LOAD time as "relation does not
    # exist" — after the full 40 seconds of generation. The loader's
    # LOAD_ORDER check catches a MISSING table a moment earlier, but not a
    # misspelled one.
    #
    # AND NOTE WHAT IS NOT RETURNED: scheme_aum_monthly, the ninth table in
    # 01_fund_house.sql. It is computed in summaries.py from the
    # transactions that do not exist yet.
    #
    # run.py merges this dictionary into its accumulator with
    # tables.update(...), and from this point on nav_history and
    # scheme_plans are inputs to three other builders.