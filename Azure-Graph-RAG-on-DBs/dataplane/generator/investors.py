##############################################################################
# dataplane/generator/investors.py
#
# PURPOSE:
#   Builds the 4 investor tables — the people who put money into the schemes,
#   and the agents who sold to them.
#
#     distributors          600 rows    the agents, identified by an ARN
#     investors          40,000 rows    the people
#     folios            ~60,000 rows    one investor's account in one scheme
#     sip_registrations  24,000 rows    standing instructions to invest monthly
#
#   Those row counts are at the default scale factor of 0.2. At scale 1.0 it
#   is 3,000 distributors, 200,000 investors, ~300,000 folios and 120,000
#   SIPs. These four tables are the ones that actually grow with scale —
#   the fund house and market tables stay almost the same size, because a
#   fund house does not launch more schemes just because more people buy.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Up to now the database has schemes, plans, NAVs, securities and ratings.
#   Nobody has invested a rupee. Without investors there are no folios,
#   without folios there are no transactions, and without transactions there
#   is no AUM. This file creates the demand side.
#
#   It also has to create it in a way that holds together, because the
#   Stage 2 validation tests check the joins:
#
#     - every folio's investor_id must exist in investors
#     - every folio's scheme_id must exist in schemes
#     - every non-empty arn_code must exist in distributors.arn_no
#     - a folio may not open before its scheme launched
#     - a folio may not open before its investor was onboarded
#     - a SIP may not start before its folio opened
#
#   Each of those is a line of code in here, and each is pointed out below.
#
# HOW THE PIECES CONNECT:
#
#     investor ─ opens 1 to 3 ─> folio ─ belongs to ─> ONE scheme + ONE plan
#                                  │
#                                  ├─ Regular plan: an agent sold it
#                                  │                → arn_code is filled in
#                                  │
#                                  ├─ Direct plan:  bought straight from the AMC
#                                  │                → arn_code is EMPTY
#                                  │
#                                  └─ may have ONE SIP
#                                         → a fixed amount, every month
#
#   An investor with three folios is one person holding three different
#   funds. That is the normal case in India, not an edge case.
#
# PERSONAL DATA — READ THIS BEFORE RUNNING ANYTHING:
#   This file generates PAN numbers, email addresses, mobile numbers, dates
#   of birth and bank accounts. Every one of them is generated ALREADY
#   MASKED:
#
#       PAN      ABCPX****F     real shape, middle digits replaced
#       mobile   9XXXXXX417     only the last 3 digits exist
#       email    r*****42@mail.example
#       bank     XXXXXXXX8831
#
#   There is no step later that masks these. They are never real in the
#   first place, so even a developer's laptop with a full copy of the
#   database holds nothing that looks like a person's identity.
#
#   These same columns are listed in config/pii.yaml, which is what stops
#   the metadata pipeline embedding their values or sending them to a
#   model. Masking here and listing them there are two separate protections
#   and we use both.
#
# WHICH PATTERN IS PLANTED HERE:
#   PATTERN 4 — SIP cancellations in one city tier.
#   From a certain month onwards, investors in tier-3 cities cancel their
#   SIPs at about 3.5 times the normal rate. Answering "where are we losing
#   SIPs?" needs sip_registrations joined to folios joined to investors —
#   three tables — which is why this pattern is worth planting.
#
# WHAT THIS FILE RETURNS, AND WHY IT IS THREE THINGS:
#   build() hands back a TRIPLE:
#
#     1. the 4 tables                 → go to PostgreSQL
#     2. folio_facts                  → used by transactions.py, never stored
#     3. sip_schedule                 → used by transactions.py, never stored
#
#   transactions.py has to know, for every folio, which plan it is in, when
#   it opened, and which agent sold it — on every single transaction row.
#   And for every SIP it has to know the payment window and the day of the
#   month. Handing those over as two small helper tables is far cheaper
#   than re-deriving them by joining the finished tables back together.
##############################################################################


import numpy as np
# numpy is what makes 40,000 investors generatable in under a second. Every
# column here is built as one array operation over all investors at once,
# never a Python loop over rows. Where a Python loop does appear below, it
# is over the 40 schemes or the 3 city tiers — small, fixed counts.

import polars as pl
# polars holds the finished tables. We build columns in numpy, then hand
# the arrays to polars at the end to become a DataFrame.

from dataplane.generator import patterns as P
# The planted-story constants. Pattern 4's tier, start month and multiplier
# live in patterns.py, and pattern 5's distributor name and ARN do too —
# this file creates that distributor, transactions.py gives it the high
# rejection rate.

from dataplane.generator.common import (
    CITIES,
    # 40-odd Indian cities, each as (city, state, state_code, tier).
    FIRST_NAMES,
    # Indian first names, drawn from for investor names.
    NAME_WORDS,
    # Place and nature words ("Sahyadri", "Konkan", "Deccan") used to build
    # believable company names for banks and national distributors.
    SURNAMES,
    # Indian surnames, used both for investor names and for IFA firm names
    # ("Deshpande Financial Services").
    TIER_SHARE,
    # How investors split across city tiers: roughly 45% tier 1, 35% tier 2,
    # 20% tier 3. Pattern 4 depends on tier 3 being a real share of the
    # book, not a rounding error.
    Settings,
    # The one object carrying the scale factor, the date window and the
    # seeded random number generator.
    roll_forward,
    # Moves a date forward to the next business day if it lands on a
    # weekend or a market holiday.
    to_dates,
    # Converts a list that may contain None into something polars accepts
    # as a Date column.
)

from dataplane.generator.fund_house import SCHEMES, asset_class_of
# SCHEMES is the list of 40 schemes, in order; its position in the list IS
# the scheme_id. asset_class_of tells us whether a scheme is Equity, Debt
# or Hybrid, which decides whether SIPs are offered in it.


# ─────────────────────────────────────────────────────────────────────────────
# HOW POPULAR EACH SCHEME IS
# ─────────────────────────────────────────────────────────────────────────────

POPULAR = {
    "Sahyadri Flexi Cap Fund": 9, "Sahyadri Large Cap Fund": 8, "Sahyadri Mid Cap Fund": 7,
    "Sahyadri Small Cap Fund": 7, "Sahyadri ELSS Tax Saver Fund": 7,
    "Sahyadri Balanced Advantage Fund": 6, "Sahyadri Liquid Fund": 5,
    "Sahyadri Deccan 50 Index Fund": 5, "Sahyadri Aggressive Hybrid Fund": 5,
    "Sahyadri Credit Risk Fund": 4, "Sahyadri Medium Duration Fund": 3,
}
# When an investor opens a folio, which scheme do they pick?
#
# NOT UNIFORMLY. If every scheme were equally likely, each of the 40 schemes
# would end up with about 2.5% of the book, and that is not what a real fund
# house looks like. In reality a flexi cap fund has lakhs of retail
# investors and a medium duration debt fund has a few thousand.
#
# These are relative weights, not percentages. A 9 is three times as likely
# to be picked as a 3. They get divided by their total further down to
# become real probabilities.
#
# NOTE ONLY 11 OF THE 40 SCHEMES ARE NAMED HERE. The other 29 fall through to
# DEFAULT_POPULARITY below and stay niche — which is the intended outcome, not
# an omission. The 11 named ones are the ones a retail investor would
# actually recognise.
#
# WHY IT MATTERS FOR THE COURSE: this is what makes "which scheme has the
# most investors?" have an interesting answer, and what makes the AUM chart
# look like a real fund house rather than 40 identical bars.

DEFAULT_POPULARITY = 2
# Any scheme not named above. Keeping this LOW matters: the schemes left out
# are the niche ones, and they should stay niche. If this were 9, every
# unnamed scheme would silently become the most popular fund in the book.

FREQUENCY_MONTHS = {"Monthly": 1, "Quarterly": 3}
# Months between one SIP payment and the next.
#
# This is used by transactions.py, not here — it goes into the sip_schedule
# helper table as "step_months". transactions.py then lists payment dates by
# adding step_months repeatedly. Putting the number here rather than there
# keeps the meaning of the word "Quarterly" in one place.


# ─────────────────────────────────────────────────────────────────────────────
# DATE ARITHMETIC: "the same day, N months later"
# ─────────────────────────────────────────────────────────────────────────────

def _add_months(dates: np.ndarray, months: np.ndarray, day_of_month: np.ndarray) -> np.ndarray:
    """
    The date that is `months` whole months after each date, landing on the
    given day of the month.

        (2024-01-20, 2 months, day 5)  ->  2024-03-05

    Works on whole arrays at once: pass 24,000 dates, 24,000 month counts
    and 24,000 days, get 24,000 answers back.

    WHY THIS FUNCTION HAS TO EXIST:
      "One month later" is not a fixed number of days. 31 January plus one
      month is 28 February in a normal year and 29 February in a leap year.
      Adding 30 days instead would drift: a SIP on the 5th would slowly
      become a SIP on the 3rd, then the 1st, then the previous month.

      numpy's datetime64 has a month unit that handles this properly, so we
      use it rather than counting days.
    """
    month = dates.astype("datetime64[M]") + months.astype("timedelta64[M]")
    # Step 1. Drop the day part entirely. Casting a datetime64[D] (a day) to
    # datetime64[M] (a month) throws the day away: 2024-01-20 becomes
    # 2024-01. Then adding 2 months gives 2024-03. No day involved, so no
    # end-of-month problem can arise.

    return month.astype("datetime64[D]") + (day_of_month - 1).astype("timedelta64[D]")
    # Step 2. Put a day back on. Casting 2024-03 back to a day gives the
    # FIRST of the month, 2024-03-01. So to land on the 5th we add 4 days,
    # which is why it is (day_of_month - 1) and not day_of_month.
    #
    # An off-by-one here would shift every SIP payment in the database by
    # one day, and nothing would fail — the dates would simply all be wrong.


# ─────────────────────────────────────────────────────────────────────────────
# THE BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build(settings: Settings, plans: pl.DataFrame) -> tuple[dict[str, pl.DataFrame], pl.DataFrame, pl.DataFrame]:
    """
    Build the 4 investor tables, plus the 2 helper tables transactions.py needs.

    INPUT:
      settings  the scale factor, the date window and the seeded generator
      plans     the plans table from fund_house.py. Needed because a folio
                points at a PLAN, not just a scheme — "Flexi Cap, Direct,
                Growth" is a different plan from "Flexi Cap, Regular, IDCW"
                and they have different NAVs.

    RETURNS a 3-tuple:
      tables         dict of 4 polars DataFrames, ready to load
      folio_facts    one row per folio, for transactions.py
      sip_schedule   one row per SIP, for transactions.py
    """
    rng = settings.rng
    # The seeded random number generator. Every random draw below comes from
    # this one object, which is what makes two runs with the same seed
    # produce byte-identical data. Using np.random.* directly anywhere in
    # this file would break that, because those calls use a separate global
    # generator we do not control.

    start, end = settings.start, settings.end
    # The 5-year window. Nothing in this file may fall outside it.

    # =========================================================================
    # TABLE 1 OF 4 — distributors
    # =========================================================================
    # Who they are: the agents who sell mutual funds. In India every one of
    # them has an ARN — an AMFI Registration Number — and that ARN, not a
    # name, is what appears on a transaction.
    #
    # This is the table that hidden link 3 points at:
    #
    #     transactions.arn_code  →  distributors.arn_no
    #
    # Same values, DIFFERENT COLUMN NAMES, and no foreign key declared. The
    # metadata pipeline has to find that link by comparing values. Which is
    # exactly why the column here is called arn_no and the one in
    # transactions is called arn_code — if both were called arn_code, the
    # name-matching source would find it and the value-overlap source would
    # never be exercised.

    n_dist = settings.scaled(3000)
    # 3,000 at scale 1.0, 600 at the default 0.2.

    arn_numbers = rng.choice(np.setdiff1d(np.arange(100000, 1000000), [int(P.P5_ARN[4:])]), n_dist - 1, replace=False)
    # Unique 6-digit ARN numbers, drawn from 100000–999999.
    #
    # Read this inside out:
    #   np.arange(100000, 1000000)  every possible 6-digit number
    #   P.P5_ARN[4:]                "ARN-123456" with the "ARN-" sliced off
    #   np.setdiff1d(..., [that])   that one number REMOVED from the pool
    #   rng.choice(..., replace=False)  draw without repeats
    #
    # WHY REMOVE ONE NUMBER FIRST: pattern 5 needs a distributor with a
    # known, fixed ARN so that transactions.py can find it and the test can
    # assert on it. If that exact number also got drawn at random for
    # another distributor, arn_no would have a duplicate — and arn_no is the
    # primary key, so the load would fail with a unique-violation.
    #
    # replace=False is what guarantees no two of the other distributors
    # collide either.

    arns = [P.P5_ARN] + [f"ARN-{n}" for n in arn_numbers]
    # The pattern 5 distributor is deliberately put FIRST, at index 0. Several
    # lines below refer to "index 0" to adjust it, and keeping it at a known
    # position is simpler than searching for it each time.

    dist_types = rng.choice(["IFA", "Bank", "National Distributor", "Online Platform"],
                            n_dist, p=[0.85, 0.05, 0.03, 0.07])
    # What kind of agent each one is. The 85% share for IFA is real: the
    # Indian distribution business is overwhelmingly individual advisors,
    # with a handful of banks and platforms doing very large volumes.
    #
    # p= must sum to 1.0. If it does not, numpy raises — which is the
    # behaviour we want, rather than silently normalising a typo.

    dist_names = []
    for i, kind in enumerate(dist_types):
        # A Python loop over 600 rows. Slower than a vectorised build, but
        # this runs once and takes milliseconds, and the readability is worth
        # more than the microseconds here. The 40,000-row investor names
        # below are NOT built this way.
        if i == 0:
            dist_names.append(P.P5_DISTRIBUTOR_NAME)
            # Index 0 is pattern 5's distributor, with the name the test
            # looks for.
        elif kind == "Bank":
            dist_names.append(f"{rng.choice(NAME_WORDS)} Bank Ltd")
        elif kind == "Online Platform":
            dist_names.append(f"{rng.choice(NAME_WORDS)}Invest Online Pvt Ltd")
        elif kind == "National Distributor":
            dist_names.append(f"{rng.choice(NAME_WORDS)} Wealth Management Ltd")
        else:
            dist_names.append(f"{rng.choice(SURNAMES)} {rng.choice(['Financial Services', 'Wealth Advisors', 'Investments', 'Capital'])}")
            # An IFA firm, named after a person — which is how they really
            # are named.

    dist_types[0] = "IFA"
    # Force pattern 5's distributor to be an IFA.
    #
    # WHY: rng.choice already picked a type for index 0, and it could have
    # picked "Bank". A bank with a 17% rejection rate is not a believable
    # story; an individual advisor with sloppy mandate paperwork is. The
    # story has to survive a student reading the row.

    dist_city = rng.integers(0, len(CITIES), n_dist)
    # A position in the CITIES list for each distributor. Note this is a
    # UNIFORM draw — unlike investors below, distributors are not weighted
    # by city tier. There is no pattern that depends on where a distributor
    # sits, so there is no reason to add the complexity.

    distributors = pl.DataFrame(
        {
            "arn_no": arns,
            # PRIMARY KEY. The target of hidden link 3.
            "dist_nm": dist_names,
            # Deliberately abbreviated column names in this table — dist_nm,
            # dist_typ, empnl_dt, actv_flg. Real registry tables are full of
            # these. It also means a language model cannot read the schema
            # and guess what the column holds, which is precisely the problem
            # the metadata pipeline exists to solve: a human writes
            # "distributor name" once, it gets approved, and from then on the
            # agent knows.
            "dist_typ": dist_types,
            "city_nm": [CITIES[c][0] for c in dist_city],
            # CITIES[c] is (city, state, state_code, tier), so [0] is the
            # city name.
            "st_cd": [CITIES[c][2] for c in dist_city],
            # [2] is the 2-letter state code — "MH", "KA", "TN".
            "empnl_dt": np.datetime64("2008-01-01") + rng.integers(0, 4900, n_dist).astype("timedelta64[D]"),
            # When AMFI empanelled them. 2008 plus up to 4,900 days (about
            # 13 years) — so distributors were empanelled BEFORE our 5-year
            # transaction window opened. An agent whose empanelment date is
            # after the transaction they sold would be an obvious data bug.
            "actv_flg": np.where(rng.random(n_dist) < 0.95, "Y", "N"),
            # 5% have left the business. A "Y"/"N" character flag rather than
            # a boolean — again, what registry systems actually use, and
            # another thing the metadata layer has to explain to the agent.
        }
    )

    distributors = distributors.with_columns(
        pl.when(pl.col("arn_no") == P.P5_ARN).then(pl.lit("Y")).otherwise(pl.col("actv_flg")).alias("actv_flg")
    )
    # Force pattern 5's distributor to be ACTIVE.
    #
    # WHY THIS LINE EXISTS: the np.where above gave every distributor a 5%
    # chance of being inactive, including this one. If it came out "N", the
    # obvious analyst question — "which active distributor has the worst
    # rejection rate?" — would filter it straight out and the planted story
    # would be invisible. One line to make the pattern robust to a dice roll.

    dist_weight = rng.pareto(1.3, n_dist) + 0.05
    # How much business each distributor brings in.
    #
    # A Pareto distribution: a few agents with very large books, many with
    # small ones. This is the right shape for distribution businesses, and
    # it matters here because it decides which agent is attached to each
    # folio a few sections down.
    #
    # The 1.3 is the shape parameter — lower means more extreme inequality.
    # The + 0.05 stops any weight being effectively zero, which would mean
    # a distributor in the table with no folios at all.

    dist_weight[0] = np.sort(dist_weight)[-20]
    # Make pattern 5's distributor the 20th largest.
    #
    # WHY NOT THE LARGEST: a 17% rejection rate on the single biggest
    # distributor is suspicious in a teaching dataset — it looks placed.
    # The 20th largest is big enough that the rate is computed over
    # thousands of transactions (so it is statistically solid and the test
    # is stable), but not so big that it stands out before you run the
    # query.
    #
    # If this were left random, the distributor might get 12 transactions,
    # and 17% of 12 is 2 — a rejection rate indistinguishable from noise,
    # and a test that fails intermittently.

    dist_weight = dist_weight / dist_weight.sum()
    # Normalise to sum to 1.0, because rng.choice(p=...) requires
    # probabilities, not weights.

    # =========================================================================
    # TABLE 2 OF 4 — investors
    # =========================================================================
    # The people. This is the table that holds personal data, and every
    # personal column is generated already masked.

    n_inv = settings.scaled(200_000)
    investor_ids = np.arange(1, n_inv + 1)
    # 1, 2, 3, ... n. Starting at 1, not 0 — these are database ids, and an
    # id of 0 reads like a missing value to anyone looking at the data.

    span_days = int((end - np.timedelta64(60, "D") - start).astype(int))
    # How many days wide the onboarding window is.
    #
    # Why end MINUS 60 DAYS: an investor onboarded in the last two months of
    # the window has no time to open a folio, make a purchase and build any
    # history. They would just be rows with nothing attached. Cutting the
    # window short by 60 days means every investor in the table has had a
    # chance to do something.

    onboarded = roll_forward(start + np.floor(rng.random(n_inv) ** 1.3 * span_days).astype("timedelta64[D]"))
    # When each investor joined the AMC.
    #
    # Read the middle outwards:
    #   rng.random(n_inv)        40,000 numbers evenly spread over 0 to 1
    #   ** 1.3                   bends that spread toward 0
    #   * span_days              scales it to the window
    #   start + ...              turns it into a date
    #   roll_forward(...)        moves weekends and holidays to the next
    #                            working day, because a KYC cannot be
    #                            completed on a Sunday
    #
    # WHAT ** 1.3 ACTUALLY DOES: raising a number between 0 and 1 to a power
    # above 1 makes it smaller — 0.5 ** 1.3 is 0.41. So more investors land
    # in the earlier part of the window.
    #
    # WHY WE WANT THAT: it gives the AMC an existing book on day one that
    # keeps growing, rather than an AMC that starts from zero investors. The
    # AUM chart then looks like a real fund house's.

    tiers = rng.choice([1, 2, 3], n_inv, p=[TIER_SHARE[1], TIER_SHARE[2], TIER_SHARE[3]])
    # City tier first, city second. This order matters.
    #
    # If we picked a city uniformly from the CITIES list instead, the tier
    # split would be decided by how many cities of each tier happen to be in
    # that list — and there are far more tier-3 cities than tier-1 ones, so
    # we would end up with a mostly tier-3 investor book. That is the
    # opposite of reality and it would wreck pattern 4, which needs tier 3
    # to be a meaningful but minority share.
    #
    # Picking the tier from TIER_SHARE first makes the split exactly what we
    # intend, whatever the city list contains.

    city_idx = np.empty(n_inv, dtype=int)
    # Pre-allocate the array, then fill it tier by tier. np.empty does not
    # initialise the memory, which is fine because every slot gets written
    # by the loop below.

    for tier in (1, 2, 3):
        in_tier = [i for i, c in enumerate(CITIES) if c[3] == tier]
        # Positions in CITIES whose 4th element (the tier) matches.
        mask = tiers == tier
        # A True/False array: which investors are in this tier.
        city_idx[mask] = rng.choice(in_tier, mask.sum())
        # Fill only those slots, with cities drawn only from that tier.
        # mask.sum() counts the Trues — how many values we need.

    first = rng.choice(FIRST_NAMES, n_inv)
    last = rng.choice(SURNAMES, n_inv)
    # Names, drawn independently. Two investors will share a name, and that
    # is correct — real investor tables are full of repeated names, which is
    # exactly why PAN exists as the identifier and the name does not.

    letters = np.array(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

    def pan_for(i):
        """
        A masked PAN that is nonetheless unique.

        A real PAN is 5 letters, 4 digits, 1 letter — ABCDE1234F — and the
        4th letter says what kind of holder it is, with "P" for an
        individual person.

        We need two things that pull against each other:
          1. it must look like a PAN, so students see realistic data
          2. it must be UNIQUE, because the column has a UNIQUE constraint

        So: the LETTERS are derived from the investor_id, which makes them
        unique by construction, and the DIGITS are replaced with ****, which
        makes the whole thing unusable as an identity.

        The arithmetic is base-26. Think of the investor_id written in base
        26, with each digit becoming a letter:

            i // 26**4 % 26   the 26^4 place
            i // 26**3 % 26   the 26^3 place
            ... and so on down to i % 26

        Five letter positions gives 26^5 = 11.8 million distinct PANs, far
        more than the 200,000 investors at full scale, so no two can collide.
        """
        a, b, c, d, e = (i // 26**4) % 26, (i // 26**3) % 26, (i // 26**2) % 26, (i // 26) % 26, i % 26
        return f"{letters[a]}{letters[b]}{letters[c]}P{letters[d]}****{letters[e]}"
        # Note the hard-coded "P" in the 4th position — every investor here
        # is an individual, never a company or a trust.

    def blank_one_percent(values):
        """
        Replace about 1% of the values with None — a missing value.

        WHY DELIBERATELY BREAK THE DATA: because real data is like this. An
        email address is missing, a mobile was never captured, a date of
        birth was skipped. If every optional column were 100% filled, the
        students' SQL would never need a NULL check and they would write
        queries that fall over on the first real database they touch.

        It also makes COUNT(email) differ from COUNT(*), which is a lesson
        in itself.

        Note this only ever touches columns that are NULL-able in the
        schema. PAN, name, city and onboarded_date are NOT NULL and are not
        passed through here — doing so would make the load fail.
        """
        return [None if rng.random() < 0.01 else v for v in values]

    investors = pl.DataFrame(
        {
            "investor_id": investor_ids,
            # PRIMARY KEY.
            "pan": [pan_for(int(i)) for i in investor_ids],
            # UNIQUE. Masked. int(i) because numpy integers do not behave
            # quite like Python ints under // and %.
            "full_name": [f"{a} {b}" for a, b in zip(first, last)],
            "email": blank_one_percent([f"{a[0].lower()}*****{i % 100:02d}@mail.example" for a, i in zip(first, investor_ids)]),
            # First initial, stars, two digits. The domain is
            # "mail.example" — .example is a reserved top-level domain that
            # can never be registered, so none of these addresses can ever
            # reach a real inbox even by accident.
            "mobile": blank_one_percent([f"9XXXXXX{n:03d}" for n in rng.integers(0, 1000, n_inv)]),
            # Indian mobiles start with 6-9, so the leading 9 is realistic.
            # Only the last 3 digits are real, so these are not dialable.
            "date_of_birth": pl.Series(
                to_dates(blank_one_percent(list(np.datetime64("1955-01-01") + rng.integers(0, 18000, n_inv).astype("timedelta64[D]")))),
                dtype=pl.Date,
            ),
            # 1955 plus up to 18,000 days (49 years), so investors are born
            # between 1955 and 2004 — aged roughly 21 to 70 during our
            # window. Nobody under 18, because a minor cannot hold a folio
            # in their own name.
            #
            # to_dates() is needed because blank_one_percent has turned a
            # clean numpy datetime array into a Python list containing both
            # datetime64 values and None, which polars will not cast on its
            # own.
            "city": [CITIES[c][0] for c in city_idx],
            "state": [CITIES[c][1] for c in city_idx],
            # [1] is the full state name here, not the code. Note that this
            # table stores the full name while distributors stores st_cd —
            # the same information in two shapes, in two tables. Real
            # databases are inconsistent like this, and the agent has to
            # cope.
            "city_tier": tiers,
            # 1, 2 or 3. This column is what makes pattern 4 answerable
            # without any external lookup of which city is which tier.
            "kyc_status": rng.choice(["Verified", "Pending", "Rejected"], n_inv, p=[0.96, 0.03, 0.01]),
            # Most investors are verified. The 4% who are not give queries
            # something real to filter on.
            "onboarded_date": onboarded,
            # VALIDATION ANCHOR: no folio below may open before this date,
            # for the same investor.
        }
    )

    # =========================================================================
    # TABLE 3 OF 4 — folios
    # =========================================================================
    # A folio is one investor's account in one scheme. Transactions hang off
    # folios, not off investors — so this is the table that connects people
    # to money.
    #
    # Built in two passes: first a DRAFT with more folios than we will keep,
    # then a filter that drops the impossible ones. The draft-then-filter
    # shape appears because some constraints can only be checked after the
    # dates are chosen.

    folios_per_investor = rng.choice([1, 2, 3], n_inv, p=[0.6, 0.3, 0.1])
    # 60% of investors hold one fund, 30% hold two, 10% hold three. Average
    # about 1.5, which is why 40,000 investors produce roughly 60,000
    # folios.

    owner = np.repeat(investor_ids, folios_per_investor)
    # THE KEY LINE OF THIS SECTION.
    #
    # np.repeat takes each investor_id and repeats it as many times as that
    # investor has folios:
    #
    #     investor_ids        [1, 2, 3, 4]
    #     folios_per_investor [1, 3, 2, 1]
    #     owner               [1, 2, 2, 2, 3, 3, 4]
    #
    # Each slot in `owner` is now one folio, labelled with its investor. The
    # array is naturally sorted by investor, which the is_first_folio trick
    # below depends on.

    n_folio_draft = len(owner)

    popularity = np.array([POPULAR.get(s[0], DEFAULT_POPULARITY) for s in SCHEMES], dtype=float)
    # Look up each scheme's weight by name. s[0] is the scheme name;
    # .get(name, DEFAULT) returns 2 for any scheme not in the dict, so
    # renaming a scheme in fund_house.py cannot crash this — it just
    # silently becomes unpopular. Worth knowing when a scheme mysteriously
    # has few investors.

    scheme_pick = rng.choice(np.arange(1, len(SCHEMES) + 1), n_folio_draft, p=popularity / popularity.sum())
    # Which scheme each folio is in, weighted by popularity. The draw is
    # over 1..40, which are the scheme_ids, because position in SCHEMES is
    # the id.

    launch = np.array([s[3] for s in SCHEMES], dtype="datetime64[D]")[scheme_pick - 1]
    # Each picked scheme's launch date. s[3] is the launch date in the
    # SCHEMES tuple. The "- 1" converts a 1-based scheme_id into a 0-based
    # array position, which is the single most common off-by-one in this
    # whole project.

    later = rng.integers(0, 500, n_folio_draft).astype("timedelta64[D]")
    # Up to 500 days of delay, for second and third folios.

    is_first_folio = np.r_[True, owner[1:] != owner[:-1]]
    # True where a folio is its investor's FIRST.
    #
    # How it works: compare the owner array with itself shifted by one
    # position. Where they differ, the investor has changed, so this is a
    # new investor's first folio. np.r_ sticks a True on the front, because
    # row 0 is always somebody's first.
    #
    #     owner           [1, 2, 2, 2, 3]
    #     owner[1:]       [   2, 2, 2, 3]
    #     owner[:-1]      [1, 2, 2, 2   ]
    #     differ?         [   T, F, F, T]
    #     with True front [T, T, F, F, T]
    #
    # This only works because `owner` is sorted by investor, which np.repeat
    # guaranteed.

    opened = onboarded[owner - 1] + np.where(is_first_folio, np.timedelta64(0, "D"), later)
    # The first folio opens the DAY the investor is onboarded — which is how
    # it really works: you complete KYC in order to buy a fund. Later folios
    # open some time after.
    #
    # onboarded[owner - 1] is the "- 1" conversion again: owner holds
    # investor_ids starting at 1, and onboarded is a 0-based array.

    opened = roll_forward(np.maximum(opened, launch))
    # TWO VALIDATION RULES IN ONE LINE.
    #
    # np.maximum(opened, launch) — a folio cannot open before its scheme
    # exists. Where the computed date is earlier than the launch, the launch
    # date wins. An investor onboarded in 2021 who picks a scheme launched
    # in 2023 gets a 2023 folio.
    #
    # roll_forward(...) — a folio cannot be opened on a market holiday.
    #
    # Note the order: maximum first, then roll_forward. If we rolled first
    # and then took the maximum, the result could be a weekend again.

    keep = opened <= end - np.timedelta64(21, "D")
    owner, scheme_pick, opened = owner[keep], scheme_pick[keep], opened[keep]
    # Drop folios that would open in the last three weeks of the window.
    #
    # WHY: such a folio has no time to transact. It would be a row in folios
    # with no rows in transactions — and that then shows up as a scheme with
    # a folio count but no AUM, which looks like a bug when a student
    # queries it.
    #
    # All three arrays must be filtered with the SAME mask, in the same
    # statement. Filtering one and forgetting another would silently
    # misalign every folio with somebody else's scheme — the kind of bug
    # that produces data which loads fine and is entirely wrong.

    n_folio = len(owner)
    folio_ids = np.arange(1, n_folio + 1)
    # Ids assigned AFTER the filter, so they are a gapless 1..n. Assigning
    # them before and then filtering would leave holes.

    plan_type = np.where(rng.random(n_folio) < 0.55, "Direct", "Regular")
    # 55% Direct, 45% Regular.
    #
    # Direct means the investor bought from the AMC with no agent, and pays
    # a lower expense ratio. Regular means an agent sold it and earns
    # commission. The split has been moving toward Direct in India, and 55%
    # is a reasonable present-day figure for an AMC with an online presence.
    #
    # This single choice decides whether arn_code is filled in, which
    # decides whether hidden link 3 has anything to match on.

    option = np.where(rng.random(n_folio) < 0.85, "Growth", "IDCW")
    # Growth means gains stay in the fund. IDCW — Income Distribution cum
    # Capital Withdrawal, formerly called Dividend — pays out. Most retail
    # investors choose Growth.

    plan_lookup = {
        (r["scheme_id"], r["plan_type"], r["option_type"]): r["plan_id"]
        for r in plans.iter_rows(named=True)
    }
    # A dictionary from (scheme, Direct/Regular, Growth/IDCW) to plan_id.
    #
    # Built once, outside the loop below. Doing the lookup by filtering the
    # plans DataFrame 60,000 times instead would take minutes; a dict lookup
    # is instant.

    plan_id = np.array([
        plan_lookup.get((int(s), t, o), plan_lookup[(int(s), t, "Growth")])
        for s, t, o in zip(scheme_pick, plan_type, option)
    ])
    # Each folio's plan_id.
    #
    # THE FALLBACK IS THE INTERESTING PART. Not every scheme offers an IDCW
    # option — a liquid fund or an index fund typically has Growth only. So
    # if the (scheme, type, IDCW) combination does not exist, .get() falls
    # back to the Growth plan for the same scheme and plan type.
    #
    # Without that fallback, .get() would return None, plan_id would contain
    # a null, and the load would fail on a NOT NULL violation — hundreds of
    # lines later, with an error message that says nothing about IDCW.
    #
    # Note the inner lookup uses [] and not .get(): if the GROWTH plan is
    # missing too, something is genuinely broken in fund_house.py and we
    # want a KeyError here rather than a silent null travelling onwards.

    dist_pick = rng.choice(n_dist, n_folio, p=dist_weight)
    # Which distributor is attached to each folio, weighted by the Pareto
    # book sizes computed earlier. This is where pattern 5's distributor
    # picks up its large share of folios.

    folio_arn = np.where(plan_type == "Regular", np.array(arns)[dist_pick], None)
    # THE LINE THAT CREATES HIDDEN LINK 3.
    #
    # Regular folios get the ARN of their distributor. Direct folios get
    # None, which becomes a NULL in the database.
    #
    # Two consequences to notice:
    #
    #   1. About 45% of folios — and therefore of transactions — have a
    #      NULL arn_code. Every query and every test that touches this
    #      column has to say "arn_code IS NOT NULL" or it will treat a
    #      direct investment as a broken link. The Stage 2 orphan check
    #      does exactly that.
    #
    #   2. There is NO FOREIGN KEY from here to distributors.arn_no. That
    #      is deliberate and it is the whole point. The metadata pipeline
    #      has to discover this link by noticing that the values overlap.

    folios = pl.DataFrame(
        {
            "folio_id": folio_ids,
            # PRIMARY KEY.
            "folio_number": [f"SAH{i:09d}" for i in folio_ids],
            # The number printed on the statement — "SAH000000042". A
            # human-facing identifier alongside the machine one, which is
            # how registrar systems work.
            "investor_id": owner,
            # FOREIGN KEY to investors. Declared in the schema.
            "scheme_id": scheme_pick,
            # FOREIGN KEY to schemes. Declared.
            "opened_date": opened,
            "holding_mode": rng.choice(["Single", "Joint", "Anyone or Survivor"], n_folio, p=[0.80, 0.08, 0.12]),
            # Who can operate the folio. Not used by any pattern — it is
            # here because real folios have it, and because a column nobody
            # queries is itself realistic.
            "bank_account_masked": blank_one_percent([f"XXXXXXXX{n:04d}" for n in rng.integers(0, 10000, n_folio)]),
            # Masked at generation, and the column NAME says masked. Listed
            # in config/pii.yaml too.
        }
    )
    # NOTE WHAT IS NOT IN THIS TABLE: arn_code. The ARN lives on the
    # TRANSACTION, not on the folio, because in reality an investor can
    # change advisor mid-life and past transactions keep the old ARN. The
    # folio-level ARN is carried in folio_facts below purely so
    # transactions.py can stamp each row.

    folio_facts = pl.DataFrame(
        {
            "folio_id": folio_ids,
            "scheme_id": scheme_pick,
            "plan_id": plan_id,
            # transactions.py needs the PLAN, because the NAV it divides by
            # is a plan's NAV, not a scheme's.
            "opened_date": opened,
            # So no transaction can be dated before its folio existed.
            "arn_code": folio_arn,
            # So every transaction on a Regular folio carries the ARN.
            "city_tier": tiers[owner - 1],
            # The investor's tier, carried down to the folio. Pattern 4 uses
            # it a few lines below without having to join back to investors.
        }
    )
    # THIS TABLE NEVER REACHES POSTGRESQL. It is a work-in-progress handoff
    # between two generator files. Everything in it is derivable from the
    # real tables by joining; carrying it directly is simply faster.

    # =========================================================================
    # TABLE 4 OF 4 — sip_registrations
    # =========================================================================
    # A SIP is a standing instruction: invest a fixed amount every month on a
    # fixed day. It is how most Indian retail investors actually buy funds,
    # and it is the thing an AMC watches most closely, because a cancelled
    # SIP is lost future revenue.
    #
    # Pattern 4 lives here.

    sip_ok_scheme = np.array([asset_class_of(s[0]) != "Debt" for s in SCHEMES])
    # SIPs are offered in equity and hybrid schemes, not in debt ones. A
    # monthly SIP into a liquid fund makes little sense — people park lump
    # sums there.

    eligible = np.where(sip_ok_scheme[scheme_pick - 1] & (opened <= end - np.timedelta64(60, "D")))[0]
    # Which FOLIO POSITIONS could have a SIP. Two conditions:
    #   - the scheme is not a debt scheme
    #   - the folio opened at least 60 days before the window ends, so there
    #     is room for at least one or two payments
    #
    # np.where(...)[0] returns POSITIONS, not folio_ids. Positions are what
    # we need, because we index the parallel arrays (opened, plan_id, owner)
    # with them. Mixing the two up is the classic bug here.

    n_sip = min(settings.scaled(120_000), len(eligible))
    # How many SIPs to create — but never more than there are eligible
    # folios. Without the min(), rng.choice(replace=False) below would raise
    # at small scale factors, which is a confusing failure to debug.

    chosen = np.sort(rng.choice(eligible, n_sip, replace=False))
    # Pick the folios that get a SIP. replace=False means one folio gets at
    # most one SIP. np.sort keeps the result in folio order, which makes the
    # data easier to read when debugging.

    sip_day = rng.choice([1, 5, 10, 15, 20, 25], n_sip)
    # The day of the month the money is taken. Real AMCs offer a fixed set
    # of SIP dates, not any day — this is why.

    earliest = opened[chosen] + rng.integers(0, 120, n_sip).astype("timedelta64[D]")
    # The SIP cannot start before the folio opened; people usually set one up
    # within a few months of opening.

    first_pay = _add_months(earliest, np.zeros(n_sip, dtype=int), sip_day)
    # Snap that date to the SIP day of the SAME month: 0 months later, on
    # day sip_day.

    first_pay = np.where(first_pay < earliest, _add_months(earliest, np.ones(n_sip, dtype=int), sip_day), first_pay)
    # Fix the ones that went backwards.
    #
    # Say earliest is the 20th and sip_day is the 5th. Snapping to the 5th of
    # the same month lands BEFORE the folio was ready — which would break
    # the "SIP starts on or after folio opened" validation. So in that case
    # push to the 5th of the NEXT month.
    #
    # This is the kind of line that is easy to leave out and produces a
    # validation failure in a handful of rows out of 24,000.

    first_pay = np.minimum(first_pay, end - np.timedelta64(30, "D"))
    # And never later than 30 days before the window closes, so every SIP
    # gets at least one payment.

    frequency = np.where(rng.random(n_sip) < 0.92, "Monthly", "Quarterly")
    # Almost all SIPs are monthly.

    amount = rng.choice([500, 1000, 1500, 2000, 2500, 3000, 5000, 7500, 10000, 15000, 25000], n_sip,
                        p=[0.10, 0.18, 0.08, 0.14, 0.10, 0.10, 0.14, 0.05, 0.07, 0.02, 0.02]).astype(float)
    # Rupees per instalment. Note these are ROUND NUMBERS with the weight
    # piled on 1,000, 2,000 and 5,000. People choose round amounts. A
    # uniform draw between 500 and 25,000 would produce SIPs of Rs 3,847,
    # which no investor has ever set up, and students notice.
    #
    # .astype(float) because the column is numeric in the schema and we do
    # not want integer division surprises later.

    has_end = rng.random(n_sip) < 0.35
    # 35% of SIPs are set up for a fixed number of instalments; the rest run
    # until cancelled ("perpetual").

    tenure = rng.choice([12, 24, 36, 60], n_sip)
    # 1, 2, 3 or 5 years, for the ones that have an end.

    end_date = np.where(has_end, _add_months(first_pay, tenure, sip_day) - np.timedelta64(1, "D"),
                        np.datetime64("NaT"))
    # The end date, or NaT ("Not a Time" — numpy's missing date) for
    # perpetual SIPs. NaT becomes a NULL in the database.
    #
    # The "- 1 day" is because the end date is the day BEFORE the instalment
    # after the last one would fall due.

    # ---- PATTERN 4: when, if ever, each SIP is cancelled --------------------
    monthly_rate = 0.011
    # About 1.1% of running SIPs are cancelled each month. That is roughly
    # the real industry figure, and over 5 years it means a large minority
    # of SIPs end up cancelled — enough for the pattern to sit against a
    # believable background.

    months_to_cancel = rng.geometric(monthly_rate, n_sip)
    # THE GEOMETRIC DISTRIBUTION IS THE RIGHT TOOL HERE, and worth a minute.
    #
    # It answers: "if something has a 1.1% chance of happening each month,
    # how many months until it happens?" Most answers are large (SIPs
    # usually survive a long time), but some are 1 or 2 (a few cancel
    # immediately). That is exactly the shape of real cancellations.
    #
    # The alternative — picking a random month uniformly — would say a SIP
    # is as likely to be cancelled in month 1 as in month 50, which is
    # wrong: a SIP that has already survived 4 years is a committed
    # investor.

    cancel = _add_months(first_pay, months_to_cancel, np.ones(n_sip, dtype=int)) + rng.integers(0, 27, n_sip).astype("timedelta64[D]")
    # Turn that month count into a date. Land on the 1st of the month, then
    # add 0-26 random days, so cancellations are spread through the month
    # rather than all falling on the 1st. A cancellation date is a customer
    # service event, not a scheduled one — it has no reason to be on a
    # fixed day.

    in_tier = tiers[owner[chosen] - 1] == P.P4_TIER
    # Which of the chosen SIPs belong to tier-3 investors.
    #
    # Read the indexing carefully:
    #   chosen          positions of the folios that got a SIP
    #   owner[chosen]   the investor_id behind each of those folios
    #   ... - 1         convert that id to a 0-based array position
    #   tiers[...]      that investor's city tier
    #
    # Three steps of indirection in one line. This is the kind of expression
    # worth saying out loud on camera rather than reading silently.

    elevated_from = np.maximum(first_pay, P.P4_FROM)
    # The pattern does not start at the beginning of time. It begins at
    # P4_FROM. For a SIP that started after that date, the elevated period
    # starts when the SIP does.
    #
    # WHY THIS MATTERS: if the elevated cancellation clock started at
    # first_pay for everyone, tier-3 SIPs would be cancelling at 3.5x from
    # day one of the five-year window, and the pattern would be a constant
    # rather than a CHANGE. A constant is not a finding. The interesting
    # question an analyst asks is "when did this start?" — and there has to
    # be a date for them to find.

    months_elevated = rng.geometric(monthly_rate * P.P4_MULTIPLIER, n_sip) - 1
    # The same geometric draw, with the rate multiplied by 3.5. A higher
    # rate means a smaller number of months, which means earlier
    # cancellation.
    #
    # The "- 1" lets the answer be 0 months, i.e. cancellation in the very
    # first month of the elevated period. rng.geometric returns 1 or more.

    cancel_elevated = _add_months(elevated_from, months_elevated, np.ones(n_sip, dtype=int)) + rng.integers(0, 27, n_sip).astype("timedelta64[D]")
    cancel_elevated = np.maximum(cancel_elevated, first_pay + np.timedelta64(1, "D"))
    # A SIP cannot be cancelled before — or on — the day it started. With
    # months_elevated allowed to be 0, the arithmetic could land on or
    # before first_pay, so this clamps it to at least the next day.
    #
    # A SIP with cancelled_date equal to start_date is the kind of row that
    # makes a student lose trust in the whole dataset.

    cancel = np.where(in_tier, np.minimum(cancel, cancel_elevated), cancel)
    # Apply the pattern.
    #
    # For tier-3 SIPs take whichever cancellation comes FIRST — the normal
    # one or the elevated one. For everyone else, leave the normal one
    # alone.
    #
    # Taking the MINIMUM, not just replacing, is what keeps the pattern
    # honest: a tier-3 SIP that was always going to cancel early still
    # cancels early. The elevated rate only ever makes cancellation sooner,
    # never later. If we simply replaced, a few tier-3 SIPs would end up
    # cancelling LATER than they otherwise would, which would dilute the
    # very effect we are planting.

    # ---- Turn those dates into a status -----------------------------------
    ends_before_cancel = has_end & (end_date < cancel)
    # A fixed-tenure SIP that reaches its end date before any cancellation
    # was never cancelled — it finished.

    is_cancelled = (cancel <= end) & ~ends_before_cancel
    # Cancelled only if the cancellation date falls INSIDE our window. A SIP
    # whose geometric draw says "cancelled in 2031" is simply still running
    # as far as this database is concerned. This is what makes most SIPs
    # Active.

    is_completed = has_end & (end_date <= end) & ~is_cancelled
    # Completed: had an end date, reached it inside the window, and was not
    # cancelled first. The ~is_cancelled is what stops a SIP being both.

    status = np.where(is_cancelled, "Cancelled", np.where(is_completed, "Completed", "Active"))
    # A nested np.where — the array equivalent of if/elif/else. Order
    # matters: Cancelled wins over Completed, and Active is the fallback.

    sip_ids = np.arange(1, n_sip + 1)
    sip_registrations = pl.DataFrame(
        {
            "sip_id": sip_ids,
            # PRIMARY KEY.
            "folio_id": folio_ids[chosen],
            # FOREIGN KEY to folios. Declared.
            "plan_id": plan_id[chosen],
            # FOREIGN KEY to plans. Declared.
            "sip_amount": amount,
            "frequency": frequency,
            "start_date": first_pay,
            "end_date": pl.Series(end_date).cast(pl.Date),
            # NaT becomes NULL on the cast.
            "sip_status": status,
            "cancelled_date": pl.Series(np.where(is_cancelled, cancel, np.datetime64("NaT"))).cast(pl.Date),
            # Filled in only where the status is Cancelled. An Active SIP
            # with a cancellation date would contradict itself, and the
            # consistency tests in Stage 2 check exactly that.
            "distributor_arn": folio_arn[chosen],
            # The agent on the folio, copied onto the SIP. Note this column
            # is called distributor_arn here, arn_code in transactions and
            # arn_no in distributors — the SAME information under three
            # names in three tables. That is not sloppiness in the design;
            # it is the condition the metadata pipeline exists to handle.
        }
    )

    # ---- The payment window, for transactions.py ---------------------------
    last_pay = np.full(n_sip, end)
    # Start by assuming every SIP pays right up to the end of the window.

    last_pay = np.where(is_cancelled, cancel - np.timedelta64(1, "D"), last_pay)
    # A cancelled SIP's last payment is the day BEFORE cancellation. Paying
    # on the cancellation date itself would be a transaction after the
    # instruction was withdrawn.

    last_pay = np.where(has_end, np.minimum(last_pay, end_date), last_pay)
    # A fixed-tenure SIP stops at its end date, or earlier if it was
    # cancelled — hence minimum, not replacement.
    #
    # The order of these three lines is the logic. Window end, then
    # cancellation, then tenure, each one only ever pulling the date
    # earlier.

    sip_schedule = pl.DataFrame(
        {
            "sip_id": sip_ids,
            "folio_id": folio_ids[chosen],
            "plan_id": plan_id[chosen],
            "sip_amount": amount,
            "step_months": [FREQUENCY_MONTHS[f] for f in frequency],
            # "Monthly" → 1, "Quarterly" → 3. transactions.py adds this
            # repeatedly to list the payment dates.
            "sip_day": sip_day,
            "first_pay": first_pay,
            "last_pay": last_pay,
        }
    )
    # ALSO NEVER REACHES POSTGRESQL. Like folio_facts, this exists only to
    # be handed to transactions.py.
    #
    # Everything transactions.py needs to generate SIP payment rows is in
    # here: when to start, when to stop, how often, which day, how much, and
    # which folio and plan to attach it to.

    tables = {
        "distributors": distributors,
        "investors": investors,
        "folios": folios,
        "sip_registrations": sip_registrations,
    }
    # The dictionary keys are the TABLE NAMES in PostgreSQL. copy_loader.py
    # uses them to decide which table to COPY each frame into, so a typo
    # here surfaces as "relation does not exist" at load time, not here.

    return tables, folio_facts, sip_schedule
    # Three things out. run.py keeps all three: the tables for loading, the
    # two helper frames for passing on to transactions.py.