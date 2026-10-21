"""
dataplane/generator/market.py
=============================

WHAT THIS FILE IS FOR
---------------------
Builds 5 of the 6 market tables (see dataplane/schema/02_market.sql):

    sectors, issuer_groups, issuers, securities, credit_rating_history

The 6th, portfolio_holdings, is NOT built here. A holding's rupee value is
a share of the scheme's AUM, and AUM is only known after transactions
exist, so holdings are built later in summaries.py.

WHAT GETS CREATED
-----------------
    25 sectors, 60 business groups, 800 issuers, 1,500 securities:
        520 shares (one per listed company)
        600 corporate bonds, 120 commercial papers, 60 certificates of deposit
        150 government securities, 50 treasury bills
    about 2,000 credit rating records for the 780 rated debt securities

The special companies used by patterns 1, 2 and 6 are created first, with
fixed names, so the other files can find them by name.
"""

import numpy as np
import polars as pl

from dataplane.generator import patterns as P
from dataplane.generator.common import NAME_WORDS, SECOND_WORDS, Settings, to_dates

SECTORS = [
    "Banking", "NBFC & Housing Finance", "Insurance", "Information Technology",
    "Pharmaceuticals", "Healthcare Services", "FMCG", "Automobiles", "Auto Components",
    "Capital Goods", "Cement", "Construction", "Metals & Mining", "Oil & Gas", "Power",
    "Telecom", "Chemicals", "Textiles", "Real Estate", "Consumer Durables", "Retail",
    "Media & Entertainment", "Logistics", "Agriculture", "Government",
]
SECTOR_ID = {name: i + 1 for i, name in enumerate(SECTORS)}

# The rating scale, best to worst. A downgrade moves to a later position.
RATING_SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
                "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B", "C", "D"]

# Invented rating agencies, so no real agency's opinion is implied.
AGENCIES = ["Meridian Credit Ratings", "Indus Ratings & Research",
            "Konark Rating Services", "Apex Rating Agency"]

# The 19 state governments that issue state development loans.
STATES = ["Maharashtra", "Karnataka", "Tamil Nadu", "Gujarat", "Rajasthan", "Uttar Pradesh",
          "West Bengal", "Kerala", "Telangana", "Andhra Pradesh", "Madhya Pradesh", "Punjab",
          "Haryana", "Bihar", "Odisha", "Assam", "Chhattisgarh", "Jharkhand", "Uttarakhand"]

# Which sectors each kind of company belongs to.
CORPORATE_SECTORS = [s for s in SECTORS if s not in ("Banking", "NBFC & Housing Finance", "Government")]


def build(settings: Settings) -> tuple[dict[str, pl.DataFrame], pl.DataFrame]:
    """
    Returns two things:
      1. the 5 market tables, as table name -> DataFrame
      2. a helper table, one row per security, with the facts summaries.py
         needs to choose holdings (type, sector, group, starting rating)
    """
    rng = settings.rng

    # ---------------------------------------------------------------------
    # sectors
    # ---------------------------------------------------------------------
    sectors = pl.DataFrame(
        {
            "sector_id": list(range(1, len(SECTORS) + 1)),
            # A short code from the first letters, e.g. "NBFC & Housing Finance" -> "NHF".
            "sector_code": [
                "".join(w[0] for w in s.replace("&", "").split()).upper() + f"{i + 1:02d}"
                for i, s in enumerate(SECTORS)
            ],
            "sector_name": SECTORS,
        }
    )

    # ---------------------------------------------------------------------
    # issuer_groups: 60 business groups
    # ---------------------------------------------------------------------
    # Patterns 1 and 2 need two specific groups; the rest take the other words.
    group_names = [P.P1_GROUP_NAME, P.P2_GROUP_NAME]
    for word in NAME_WORDS:
        name = f"{word} Group"
        if name not in group_names and len(group_names) < 60:
            group_names.append(name)
    group_id = {name: i + 1 for i, name in enumerate(group_names)}
    issuer_groups = pl.DataFrame({"group_id": list(range(1, 61)), "group_name": group_names})

    # ---------------------------------------------------------------------
    # issuers: 800 companies and governments
    # ---------------------------------------------------------------------
    # Each entry: (name, group name or None, sector, issuer_type, has shares listed)
    issuers: list[tuple] = []
    taken: set[str] = set()

    def add(name, group, sector, kind, listed):
        issuers.append((name, group, sector, kind, listed))
        taken.add(name)

    # Governments first: they issue G-Secs and T-Bills, and have no shares.
    add("Government of India", None, "Government", "Government", False)
    for state in STATES:
        add(f"State Government of {state}", None, "Government", "Government", False)

    # The fixed companies for the patterns.
    add(P.P1_ISSUER_NAME, P.P1_GROUP_NAME, "NBFC & Housing Finance", "NBFC", True)     # pattern 1
    for name, sector, _weight in P.P2_COMPANIES:                                         # pattern 2
        add(name, P.P2_GROUP_NAME, sector, "Corporate", True)
    add(P.P6_ISSUER_NAME, None, "Banking", "Bank", True)                                 # pattern 6

    # Fill the rest up to 800 with invented names.
    # About 40% of companies belong to a group (excluding the two pattern
    # groups, so those stay exactly as designed).
    other_groups = group_names[2:]
    while len(issuers) < 800:
        roll = rng.random()
        word = str(rng.choice(NAME_WORDS))
        if roll < 0.07:
            kind, sector, name = "Bank", "Banking", f"{word} {rng.choice(['Commercial', 'Co-operative', 'City', 'National'])} Bank Ltd"
        elif roll < 0.17:
            kind, sector = "NBFC", "NBFC & Housing Finance"
            name = f"{word} {rng.choice(['Finance', 'Capital', 'Housing Finance', 'Fincorp'])} Ltd"
        elif roll < 0.25:
            kind, sector = "PSU", str(rng.choice(["Power", "Oil & Gas", "Metals & Mining", "Logistics", "Capital Goods"]))
            name = f"{word} {sector.split()[0]} Corporation Ltd"
        else:
            kind, sector = "Corporate", str(rng.choice(CORPORATE_SECTORS))
            name = f"{word} {rng.choice(SECOND_WORDS)} Ltd"
        if name in taken:
            continue
        group = str(rng.choice(other_groups)) if rng.random() < 0.40 else None
        add(name, group, sector, kind, False)   # "listed" is decided just below

    issuer_df = pl.DataFrame(
        {
            "issuer_id": list(range(1, 801)),
            "issuer_name": [i[0] for i in issuers],
            "group_id": [group_id[i[1]] if i[1] else None for i in issuers],
            "sector_id": [SECTOR_ID[i[2]] for i in issuers],
            "issuer_type": [i[3] for i in issuers],
        }
    )

    # ---------------------------------------------------------------------
    # securities: 1,500 shares, bonds and money-market papers
    # ---------------------------------------------------------------------
    n_gov = 20                                         # issuers 1-20 are governments
    company_ids = np.arange(n_gov + 1, 801)            # issuers 21-800 are companies
    bank_ids = issuer_df.filter(pl.col("issuer_type") == "Bank")["issuer_id"].to_numpy()

    # 520 listed companies get one share each. The pattern companies
    # (issuers 21-27) are always listed; the rest are picked at random.
    fixed_listed = np.arange(21, 28)
    others = rng.choice(np.setdiff1d(company_ids, fixed_listed), 520 - len(fixed_listed), replace=False)
    listed_ids = np.concatenate([fixed_listed, others])

    securities = []   # (issuer_id, security_type, coupon %, maturity date)

    def maturity(low_year, high_year):
        year = int(rng.integers(low_year, high_year + 1))
        return np.datetime64(f"{year}-{int(rng.integers(1, 13)):02d}-15")

    for issuer_id in listed_ids:
        securities.append((int(issuer_id), "Equity", None, None))

    # 600 corporate bonds. The pattern 1 company gets exactly one: the bond
    # that is downgraded. It is created first so it is easy to find.
    securities.append((21, "Corporate Bond", 8.9, np.datetime64("2028-06-15")))
    for issuer_id in rng.choice(np.setdiff1d(company_ids, [21]), 599):
        securities.append((int(issuer_id), "Corporate Bond", round(float(rng.uniform(6.8, 10.5)), 4), maturity(2027, 2036)))

    # 120 commercial papers (short-term company borrowing) and 60 certificates
    # of deposit (short-term bank borrowing). They pay no coupon: they are
    # bought below face value instead, so coupon is left empty.
    for issuer_id in rng.choice(np.setdiff1d(company_ids, [21]), 120):
        securities.append((int(issuer_id), "Commercial Paper", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 300)), "D")))
    for issuer_id in rng.choice(bank_ids, 60):
        securities.append((int(issuer_id), "Certificate of Deposit", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 300)), "D")))

    # 150 government securities: 100 central, 50 state. And 50 treasury bills.
    for _ in range(100):
        securities.append((1, "Government Security", round(float(rng.uniform(6.8, 7.6)), 4), maturity(2027, 2053)))
    for _ in range(50):
        securities.append((int(rng.integers(2, 21)), "Government Security", round(float(rng.uniform(7.0, 7.8)), 4), maturity(2027, 2040)))
    for _ in range(50):
        securities.append((1, "Treasury Bill", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 180)), "D")))

    n_sec = len(securities)
    ids = np.arange(1, n_sec + 1)
    securities_df = pl.DataFrame(
        {
            "security_id": ids,
            "security_code": [f"SEC{i:06d}" for i in ids],
            # Shares start with INE, government papers with IN00, both 12 characters.
            "isin": [
                (f"IN00{i:08d}" if s[1] in ("Government Security", "Treasury Bill") else f"INE{i:07d}10")
                for i, s in zip(ids, securities)
            ],
            "issuer_id": [s[0] for s in securities],
            "security_type": [s[1] for s in securities],
            "coupon_rate_pct": [s[2] for s in securities],
            "maturity_date": pl.Series(to_dates([s[3] for s in securities]), dtype=pl.Date),
        }
    )

    # ---------------------------------------------------------------------
    # credit_rating_history: ratings of every company debt security
    # ---------------------------------------------------------------------
    # Government papers are not rated (they are treated as risk-free), and
    # shares have no credit rating. That leaves 780 rated securities.
    issuer_type = dict(zip(issuer_df["issuer_id"].to_list(), issuer_df["issuer_type"].to_list()))
    pattern1_bond_id = int(
        securities_df.filter((pl.col("issuer_id") == 21) & (pl.col("security_type") == "Corporate Bond"))["security_id"][0]
    )

    rating_rows = []   # (security_code, agency, rating, date, outlook)
    first_rating = {}  # security_id -> starting rating position, used for holdings

    for sec in securities_df.iter_rows(named=True):
        if sec["security_type"] in ("Equity", "Government Security", "Treasury Bill"):
            continue
        code = sec["security_code"]
        agency = str(rng.choice(AGENCIES))

        if sec["security_id"] == pattern1_bond_id:
            # PATTERN 1: AA at the start, cut to AA- (outlook Negative),
            # then cut 3 notches to BBB- on the downgrade date.
            rating_rows.append((code, agency, "AA", settings.start, "Stable"))
            rating_rows.append((code, agency, "AA-", P.P1_EARLIER_CUT_DATE, "Negative"))
            rating_rows.append((code, agency, "BBB-", P.P1_DOWNGRADE_DATE, "Negative"))
            first_rating[sec["security_id"]] = RATING_SCALE.index("AA")
            continue

        # Starting rating depends on who issued it: banks and PSUs are the
        # safest, then NBFCs, then other companies.
        kind = issuer_type[sec["issuer_id"]]
        if kind in ("Bank", "PSU"):
            position = int(rng.choice([0, 0, 0, 1, 1, 2]))
        elif kind == "NBFC":
            position = int(rng.choice([1, 2, 2, 3, 3, 4, 5]))
        else:
            position = int(rng.choice([0, 1, 2, 2, 3, 3, 4, 5, 6, 7, 8]))
        first_rating[sec["security_id"]] = position
        rating_rows.append((code, agency, RATING_SCALE[position], settings.start, "Stable"))

        # Then a few changes over the 5 years (on average about 1.5), each
        # one notch up or down. Downgrades are slightly more common.
        date = settings.start
        for _ in range(int(rng.poisson(1.5))):
            date = date + np.timedelta64(int(rng.integers(60, 500)), "D")
            if date > settings.end:
                break
            step = 1 if rng.random() < 0.55 else -1
            position = int(np.clip(position + step, 0, 9))   # stays within AAA .. BBB-
            outlook = "Negative" if step == 1 else "Positive"
            rating_rows.append((code, agency, RATING_SCALE[position], date, outlook))

    credit_rating_history = pl.DataFrame(
        {
            "rtg_id": list(range(1, len(rating_rows) + 1)),
            "sec_cd": [r[0] for r in rating_rows],
            "rtg_agcy": [r[1] for r in rating_rows],
            "rtg": [r[2] for r in rating_rows],
            "rtg_dt": pl.Series(to_dates([r[3] for r in rating_rows]), dtype=pl.Date),
            "outlk": [r[4] for r in rating_rows],
            # The system time the rating was recorded: 6 pm on the rating date.
            "upd_ts": [f"{r[3]} 18:00:00+05:30" for r in rating_rows],
        }
    )

    # ---------------------------------------------------------------------
    # Helper table for summaries.py (not loaded into the database)
    # ---------------------------------------------------------------------
    security_facts = securities_df.select("security_id", "issuer_id", "security_type").join(
        issuer_df.select("issuer_id", "issuer_name", "group_id", "sector_id", "issuer_type"),
        on="issuer_id",
    ).with_columns(
        pl.col("security_id").replace_strict(first_rating, default=None).alias("first_rating_pos"),
        (pl.col("security_id") == pattern1_bond_id).alias("is_pattern1_bond"),
    )

    tables = {
        "sectors": sectors,
        "issuer_groups": issuer_groups,
        "issuers": issuer_df,
        "securities": securities_df,
        "credit_rating_history": credit_rating_history,
    }
    return tables, security_facts