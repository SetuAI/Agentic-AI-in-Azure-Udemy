"""
dataplane/generator/patterns.py
===============================

WHAT THIS FILE IS FOR
---------------------
The 6 patterns we deliberately plant in the synthetic data. Random data on
its own has no story, so every demo question would get a dull answer.
These patterns give the demo questions something real to find.

Each pattern is a set of fixed values (names, dates, sizes). The generator
files read them from here and build the data around them.

WHY THIS FILE IS NOT IN config/
-------------------------------
Stage 5's metadata pipeline reads config/. If these values were there, the
pipeline could read the answers instead of discovering them in the data,
and every score would be meaningless. The answers are also written out for
scoring in evals/answer_key/planted_patterns.yaml.

THE 6 PATTERNS
--------------
    1. Bond downgrade      -> redemptions spike afterwards (market + transactions)
    2. Group concentration -> one scheme holds a lot of one business group (holdings)
    3. Manager change      -> performance changes afterwards (managers + NAV)
    4. SIP cancellations   -> rising in one city tier (SIPs)
    5. Rejection rate      -> one distributor far above the rest (transactions)
    6. Issuer limit        -> one scheme above 10% in one company (holdings)
"""

# numpy is used here only for its date type, np.datetime64.
import numpy as np

# ---------------------------------------------------------------------------
# PATTERN 1: BOND DOWNGRADE, THEN A REDEMPTION SPIKE
# ---------------------------------------------------------------------------
# A housing finance company's bond is cut from AA- to BBB- (3 notches).
# Investors in the two schemes holding it redeem heavily in the following
# weeks, so March 2024 net flows turn sharply negative for those schemes.
# The Credit Risk Fund's NAV also drops on the downgrade day, because the
# bond's price is marked down.
# The company whose bond gets downgraded.
P1_ISSUER_NAME = "Vardhaman Housing Finance Ltd"
# The business group that company belongs to.
P1_GROUP_NAME = "Vardhaman Group"
# The day of the big downgrade (AA- -> BBB-).
P1_DOWNGRADE_DATE = np.datetime64("2024-03-12")
P1_EARLIER_CUT_DATE = np.datetime64("2023-05-15")      # AA -> AA-, a warning sign
P1_SPIKE_END = np.datetime64("2024-04-10")             # redemptions spike until here

# scheme name -> (weight % before downgrade, weight % from March 2024,
#                 share of folios that redeem in the spike, one-day NAV drop)
P1_HOLDERS = {
    # Credit Risk Fund: 6.5% -> 4.0% weight, 35% of folios redeem, NAV drops 1.8% that day.
    "Sahyadri Credit Risk Fund": (6.5, 4.0, 0.35, -0.018),
    # Medium Duration Fund: 2.5% -> 1.5% weight, 15% of folios redeem, NAV drops 0.6%.
    "Sahyadri Medium Duration Fund": (2.5, 1.5, 0.15, -0.006),
}

# ---------------------------------------------------------------------------
# PATTERN 2: HIGH EXPOSURE TO ONE BUSINESS GROUP
# ---------------------------------------------------------------------------
# The Mid Cap Fund holds five companies from the same group. Each holding is
# under 10% on its own, so a single-company check misses it. Together they
# are about 24% of the fund.
# The group the Mid Cap Fund is over-exposed to.
P2_GROUP_NAME = "Trident Group"
# The scheme that holds too much of that group.
P2_SCHEME = "Sahyadri Mid Cap Fund"
P2_COMPANIES = [                          # (company name, sector, weight %)
    ("Trident Chemicals Ltd", "Chemicals", 5.5),
    ("Trident Power Ltd", "Power", 5.0),
    ("Trident Polymers Ltd", "Chemicals", 4.8),
    ("Trident Logistics Ltd", "Logistics", 4.5),
    ("Trident Realty Ltd", "Real Estate", 4.2),
]

# ---------------------------------------------------------------------------
# PATTERN 3: MANAGER CHANGE, THEN A CHANGE IN PERFORMANCE
# ---------------------------------------------------------------------------
# The Focused Fund's primary manager changes. Before the change, the fund
# trails its benchmark; after it, the fund beats its benchmark.
# The scheme whose manager changes.
P3_SCHEME = "Sahyadri Focused Fund"
# Primary manager before the change.
P3_OLD_MANAGER = "Rajesh Kulkarni"
# Primary manager after the change.
P3_NEW_MANAGER = "Meera Iyer"
P3_CHANGE_DATE = np.datetime64("2023-10-03")           # new manager's first day
P3_ALPHA_BEFORE = -0.03                                # -3% a year vs benchmark
P3_ALPHA_AFTER = 0.06                                  # +6% a year vs benchmark

# ---------------------------------------------------------------------------
# PATTERN 4: SIP CANCELLATIONS RISING IN ONE CITY TIER
# ---------------------------------------------------------------------------
# In the last 12 months, investors in tier-3 cities cancel SIPs at 3.5 times
# the normal rate. Other tiers stay at the normal rate.
# The city tier where cancellations rise (3 = smaller towns).
P4_TIER = 3
# The rise starts on this date (the last 12 months of the window).
P4_FROM = np.datetime64("2025-09-01")
# Cancellations are 3.5 times the normal rate from that date.
P4_MULTIPLIER = 3.5

# ---------------------------------------------------------------------------
# PATTERN 5: ONE DISTRIBUTOR WITH A HIGH REJECTION RATE
# ---------------------------------------------------------------------------
# Most distributors see about 2% of transactions rejected. This one sees
# about 16-18%, mostly for KYC and mandate problems.
# The distributor with the high rejection rate.
P5_DISTRIBUTOR_NAME = "Konkan Wealth Partners"
# Its ARN (registration number), used as its ID in the data.
P5_ARN = "ARN-104417"
# 17% of its transactions are rejected.
P5_REJECTION_RATE = 0.17
# Everyone else: 2% rejected.
NORMAL_REJECTION_RATE = 0.02

# ---------------------------------------------------------------------------
# PATTERN 6: ONE SCHEME ABOVE THE 10% SINGLE-ISSUER LIMIT
# ---------------------------------------------------------------------------
# For the last 6 months, the Banking & Financial Services Fund holds 12.4%
# in one bank, above the usual 10% limit for a single company.
# The scheme that breaks the 10% limit.
P6_SCHEME = "Sahyadri Banking & Financial Services Fund"
# The bank it holds too much of.
P6_ISSUER_NAME = "Malabar Commercial Bank Ltd"
# Weight % in that bank before P6_FROM (inside the limit).
P6_WEIGHT_BEFORE = 8.5
# Weight % from P6_FROM onwards (above the 10% limit).
P6_WEIGHT_AFTER = 12.4
P6_FROM = np.datetime64("2026-03-31")                  # first month end above 10%