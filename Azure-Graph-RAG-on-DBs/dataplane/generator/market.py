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

# numpy: random choices and fast arrays.
import numpy as np

# polars: builds the tables (DataFrames).
import polars as pl

# The planted-pattern values (P.P1_..., P.P2_..., P.P6_...).
from dataplane.generator import patterns as P

# Shared word lists and helpers from common.py.
from dataplane.generator.common import NAME_WORDS, SECOND_WORDS, Settings, to_dates

# The 25 industry sectors. sector_id is the position + 1.
SECTORS = [
    "Banking", "NBFC & Housing Finance", "Insurance", "Information Technology",
    "Pharmaceuticals", "Healthcare Services", "FMCG", "Automobiles", "Auto Components",
    "Capital Goods", "Cement", "Construction", "Metals & Mining", "Oil & Gas", "Power",
    "Telecom", "Chemicals", "Textiles", "Real Estate", "Consumer Durables", "Retail",
    "Media & Entertainment", "Logistics", "Agriculture", "Government",
]
# Lookup dict: sector name -> sector_id.
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
# Ordinary companies can be in any sector except these three special ones.
CORPORATE_SECTORS = [s for s in SECTORS if s not in ("Banking", "NBFC & Housing Finance", "Government")]


# The main function of this file; run.py calls it in step 2.
def build(settings: Settings) -> tuple[dict[str, pl.DataFrame], pl.DataFrame]:
    """
    Returns two things:
      1. the 5 market tables, as table name -> DataFrame
      2. a helper table, one row per security, with the facts summaries.py
         needs to choose holdings (type, sector, group, starting rating)
    """
    # Short name for the shared random number generator.
    rng = settings.rng

    # ---------------------------------------------------------------------
    # sectors
    # ---------------------------------------------------------------------
    # The sectors table.
    sectors = pl.DataFrame(
        {
            # IDs 1 .. 25.
            "sector_id": list(range(1, len(SECTORS) + 1)),
            # A short code from the first letters, e.g. "NBFC & Housing Finance" -> "NHF".
            "sector_code": [
                # Remove "&", split into words, take each word's first letter, uppercase, add a 2-digit number.
                "".join(w[0] for w in s.replace("&", "").split()).upper() + f"{i + 1:02d}"
                for i, s in enumerate(SECTORS)
            ],
            # The names themselves.
            "sector_name": SECTORS,
        }
    )

    # ---------------------------------------------------------------------
    # issuer_groups: 60 business groups
    # ---------------------------------------------------------------------
    # Patterns 1 and 2 need two specific groups; the rest take the other words.
    # Start with the two pattern groups, so they get IDs 1 and 2.
    group_names = [P.P1_GROUP_NAME, P.P2_GROUP_NAME]
    # Try every name word as "<word> Group".
    for word in NAME_WORDS:
        # e.g. "Kaveri Group".
        name = f"{word} Group"
        # Skip duplicates and stop at 60 groups.
        if name not in group_names and len(group_names) < 60:
            # Keep this group name.
            group_names.append(name)
    # Lookup dict: group name -> group_id.
    group_id = {name: i + 1 for i, name in enumerate(group_names)}
    # The issuer_groups table: IDs 1 .. 60 and their names.
    issuer_groups = pl.DataFrame({"group_id": list(range(1, 61)), "group_name": group_names})

    # ---------------------------------------------------------------------
    # issuers: 800 companies and governments
    # ---------------------------------------------------------------------
    # Each entry: (name, group name or None, sector, issuer_type, has shares listed)
    # The list of issuers being built (": list[tuple]" is only a type hint for readers).
    issuers: list[tuple] = []
    # Names already used, so no two issuers share a name (a set gives fast "in" checks).
    taken: set[str] = set()

    # A small helper defined inside build(): records one issuer and marks its name as taken.
    def add(name, group, sector, kind, listed):
        # Store the issuer's facts as one tuple.
        issuers.append((name, group, sector, kind, listed))
        # Remember the name.
        taken.add(name)

    # Governments first: they issue G-Secs and T-Bills, and have no shares.
    # Issuer 1: the central government.
    add("Government of India", None, "Government", "Government", False)
    # Issuers 2-20: the 19 state governments.
    for state in STATES:
        add(f"State Government of {state}", None, "Government", "Government", False)

    # The fixed companies for the patterns.
    # Issuer 21: the company whose bond is downgraded.
    add(P.P1_ISSUER_NAME, P.P1_GROUP_NAME, "NBFC & Housing Finance", "NBFC", True)     # pattern 1
    # The five Trident Group companies (issuers 22-26).
    for name, sector, _weight in P.P2_COMPANIES:                                         # pattern 2
        add(name, P.P2_GROUP_NAME, sector, "Corporate", True)
    # Issuer 27: the bank one scheme holds too much of.
    add(P.P6_ISSUER_NAME, None, "Banking", "Bank", True)                                 # pattern 6

    # Fill the rest up to 800 with invented names.
    # About 40% of companies belong to a group (excluding the two pattern
    # groups, so those stay exactly as designed).
    # Every group except the first two (the pattern groups).
    other_groups = group_names[2:]
    # Keep inventing issuers until there are 800.
    while len(issuers) < 800:
        # A random number 0..1 decides the kind of issuer: 7% bank, 10% NBFC, 8% PSU, 75% corporate.
        roll = rng.random()
        # A random first word for the name.
        word = str(rng.choice(NAME_WORDS))
        # Bank: e.g. "Kaveri City Bank Ltd".
        if roll < 0.07:
            kind, sector, name = "Bank", "Banking", f"{word} {rng.choice(['Commercial', 'Co-operative', 'City', 'National'])} Bank Ltd"
        # NBFC: e.g. "Kaveri Finance Ltd".
        elif roll < 0.17:
            kind, sector = "NBFC", "NBFC & Housing Finance"
            name = f"{word} {rng.choice(['Finance', 'Capital', 'Housing Finance', 'Fincorp'])} Ltd"
        # PSU: a government-owned company, e.g. "Kaveri Power Corporation Ltd".
        elif roll < 0.25:
            kind, sector = "PSU", str(rng.choice(["Power", "Oil & Gas", "Metals & Mining", "Logistics", "Capital Goods"]))
            name = f"{word} {sector.split()[0]} Corporation Ltd"
        # Ordinary company: e.g. "Kaveri Steel Ltd".
        else:
            kind, sector = "Corporate", str(rng.choice(CORPORATE_SECTORS))
            name = f"{word} {rng.choice(SECOND_WORDS)} Ltd"
        # If the name was already used, throw it away and try again.
        if name in taken:
            continue
        # 40% chance the company belongs to a (non-pattern) group.
        group = str(rng.choice(other_groups)) if rng.random() < 0.40 else None
        add(name, group, sector, kind, False)   # "listed" is decided just below

    # The issuers table.
    issuer_df = pl.DataFrame(
        {
            # IDs 1 .. 800, in the order the issuers were added.
            "issuer_id": list(range(1, 801)),
            # Item 0 of each tuple is the name.
            "issuer_name": [i[0] for i in issuers],
            # Look up the group's ID; None (empty) if the issuer has no group.
            "group_id": [group_id[i[1]] if i[1] else None for i in issuers],
            # Look up the sector's ID.
            "sector_id": [SECTOR_ID[i[2]] for i in issuers],
            # Bank / NBFC / PSU / Corporate / Government.
            "issuer_type": [i[3] for i in issuers],
        }
    )

    # ---------------------------------------------------------------------
    # securities: 1,500 shares, bonds and money-market papers
    # ---------------------------------------------------------------------
    n_gov = 20                                         # issuers 1-20 are governments
    company_ids = np.arange(n_gov + 1, 801)            # issuers 21-800 are companies
    # The IDs of every bank (only banks issue certificates of deposit).
    bank_ids = issuer_df.filter(pl.col("issuer_type") == "Bank")["issuer_id"].to_numpy()

    # 520 listed companies get one share each. The pattern companies
    # (issuers 21-27) are always listed; the rest are picked at random.
    # Issuers 21, 22, ..., 27 (arange stops before 28).
    fixed_listed = np.arange(21, 28)
    # setdiff1d = companies NOT in fixed_listed; pick 513 of them, each only once (replace=False).
    others = rng.choice(np.setdiff1d(company_ids, fixed_listed), 520 - len(fixed_listed), replace=False)
    # All 520 listed companies.
    listed_ids = np.concatenate([fixed_listed, others])

    securities = []   # (issuer_id, security_type, coupon %, maturity date)

    # Helper: a random maturity date on the 15th of a random month, in the given year range.
    def maturity(low_year, high_year):
        # A random year from low_year to high_year (inclusive).
        year = int(rng.integers(low_year, high_year + 1))
        # Random month 1-12, padded to 2 digits, e.g. "2031-04-15".
        return np.datetime64(f"{year}-{int(rng.integers(1, 13)):02d}-15")

    # One share (Equity) per listed company; shares have no coupon and no maturity.
    for issuer_id in listed_ids:
        # Record the share.
        securities.append((int(issuer_id), "Equity", None, None))

    # 600 corporate bonds. The pattern 1 company gets exactly one: the bond
    # that is downgraded. It is created first so it is easy to find.
    # The pattern-1 bond: issuer 21, 8.9% coupon, matures June 2028.
    securities.append((21, "Corporate Bond", 8.9, np.datetime64("2028-06-15")))
    # 599 more bonds from random companies (never issuer 21; a company may get several).
    for issuer_id in rng.choice(np.setdiff1d(company_ids, [21]), 599):
        # Coupon between 6.8% and 10.5%, maturing between 2027 and 2036.
        securities.append((int(issuer_id), "Corporate Bond", round(float(rng.uniform(6.8, 10.5)), 4), maturity(2027, 2036)))

    # 120 commercial papers (short-term company borrowing) and 60 certificates
    # of deposit (short-term bank borrowing). They pay no coupon: they are
    # bought below face value instead, so coupon is left empty.
    # 120 commercial papers, maturing within about 300 days of 15 Sep 2026.
    for issuer_id in rng.choice(np.setdiff1d(company_ids, [21]), 120):
        securities.append((int(issuer_id), "Commercial Paper", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 300)), "D")))
    # 60 certificates of deposit, issued by banks only.
    for issuer_id in rng.choice(bank_ids, 60):
        securities.append((int(issuer_id), "Certificate of Deposit", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 300)), "D")))

    # 150 government securities: 100 central, 50 state. And 50 treasury bills.
    # 100 central government securities ("_" = we do not need the loop counter).
    for _ in range(100):
        securities.append((1, "Government Security", round(float(rng.uniform(6.8, 7.6)), 4), maturity(2027, 2053)))
    # 50 state government securities (issuers 2-20).
    for _ in range(50):
        securities.append((int(rng.integers(2, 21)), "Government Security", round(float(rng.uniform(7.0, 7.8)), 4), maturity(2027, 2040)))
    # 50 treasury bills from the central government.
    for _ in range(50):
        securities.append((1, "Treasury Bill", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 180)), "D")))

    # How many securities were created (1,500).
    n_sec = len(securities)
    # security_id values 1 .. 1,500.
    ids = np.arange(1, n_sec + 1)
    # The securities table.
    securities_df = pl.DataFrame(
        {
            "security_id": ids,
            # Codes SEC000001, SEC000002, ...
            "security_code": [f"SEC{i:06d}" for i in ids],
            # Shares start with INE, government papers with IN00, both 12 characters.
            "isin": [
                (f"IN00{i:08d}" if s[1] in ("Government Security", "Treasury Bill") else f"INE{i:07d}10")
                # zip pairs every ID with its security tuple.
                for i, s in zip(ids, securities)
            ],
            # Item 0: issuer; item 1: type; item 2: coupon; item 3: maturity.
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
    # Lookup dict: issuer_id -> issuer_type.
    issuer_type = dict(zip(issuer_df["issuer_id"].to_list(), issuer_df["issuer_type"].to_list()))
    # Find the security_id of the pattern-1 bond (the only bond of issuer 21).
    pattern1_bond_id = int(
        securities_df.filter((pl.col("issuer_id") == 21) & (pl.col("security_type") == "Corporate Bond"))["security_id"][0]
    )

    rating_rows = []   # (security_code, agency, rating, date, outlook)
    first_rating = {}  # security_id -> starting rating position, used for holdings

    # Go through every security; "sec" is a dict of its columns.
    for sec in securities_df.iter_rows(named=True):
        # Shares and government papers get no rating.
        if sec["security_type"] in ("Equity", "Government Security", "Treasury Bill"):
            # Skip to the next security.
            continue
        # The rating table refers to securities by this code (the hidden link).
        code = sec["security_code"]
        # One random agency rates this security throughout.
        agency = str(rng.choice(AGENCIES))

        # The pattern-1 bond gets a fixed rating story.
        if sec["security_id"] == pattern1_bond_id:
            # PATTERN 1: AA at the start, cut to AA- (outlook Negative),
            # then cut 3 notches to BBB- on the downgrade date.
            rating_rows.append((code, agency, "AA", settings.start, "Stable"))
            rating_rows.append((code, agency, "AA-", P.P1_EARLIER_CUT_DATE, "Negative"))
            rating_rows.append((code, agency, "BBB-", P.P1_DOWNGRADE_DATE, "Negative"))
            # Its starting rating position (AA = position 2).
            first_rating[sec["security_id"]] = RATING_SCALE.index("AA")
            continue

        # Starting rating depends on who issued it: banks and PSUs are the
        # safest, then NBFCs, then other companies.
        # The issuer's type.
        kind = issuer_type[sec["issuer_id"]]
        # Position 0 = AAA, 1 = AA+, 2 = AA ... (repeated numbers make them more likely).
        if kind in ("Bank", "PSU"):
            position = int(rng.choice([0, 0, 0, 1, 1, 2]))
        elif kind == "NBFC":
            position = int(rng.choice([1, 2, 2, 3, 3, 4, 5]))
        else:
            position = int(rng.choice([0, 1, 2, 2, 3, 3, 4, 5, 6, 7, 8]))
        # Remember the starting position for summaries.py.
        first_rating[sec["security_id"]] = position
        # The first rating record, dated at the start of the window.
        rating_rows.append((code, agency, RATING_SCALE[position], settings.start, "Stable"))

        # Then a few changes over the 5 years (on average about 1.5), each
        # one notch up or down. Downgrades are slightly more common.
        # Changes happen at random gaps after the start.
        date = settings.start
        # poisson(1.5) gives a random count (0, 1, 2, ...) averaging 1.5.
        for _ in range(int(rng.poisson(1.5))):
            # The next change is 60-499 days after the previous one.
            date = date + np.timedelta64(int(rng.integers(60, 500)), "D")
            # Stop if that falls after the end of the window.
            if date > settings.end:
                break
            # +1 = one notch worse (55% chance), -1 = one notch better.
            step = 1 if rng.random() < 0.55 else -1
            position = int(np.clip(position + step, 0, 9))   # stays within AAA .. BBB-
            # The outlook matches the direction of the change.
            outlook = "Negative" if step == 1 else "Positive"
            # Record the change.
            rating_rows.append((code, agency, RATING_SCALE[position], date, outlook))

    # The credit_rating_history table, with its old-style column names.
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
    # Join securities to their issuers so each row has the issuer's name, group, sector and type.
    security_facts = securities_df.select("security_id", "issuer_id", "security_type").join(
        issuer_df.select("issuer_id", "issuer_name", "group_id", "sector_id", "issuer_type"),
        on="issuer_id",
    # Add two helper columns:
    ).with_columns(
        # first_rating_pos: the starting rating position (None for unrated securities).
        pl.col("security_id").replace_strict(first_rating, default=None).alias("first_rating_pos"),
        # is_pattern1_bond: True only for the downgraded bond.
        (pl.col("security_id") == pattern1_bond_id).alias("is_pattern1_bond"),
    )

    # The 5 tables to load, keyed by table name.
    tables = {
        "sectors": sectors,
        "issuer_groups": issuer_groups,
        "issuers": issuer_df,
        "securities": securities_df,
        "credit_rating_history": credit_rating_history,
    }
    # Return both; run.py unpacks them into two variables.
    return tables, security_facts