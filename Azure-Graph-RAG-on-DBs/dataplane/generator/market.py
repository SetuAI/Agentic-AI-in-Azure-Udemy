##############################################################################
# dataplane/generator/market.py
#
# PURPOSE:
#   Builds 5 of the 6 market tables — the outside world the schemes invest
#   in. fund_house.py described the fund company's own products; this file
#   describes the companies, the shares and bonds they issue, and the credit
#   ratings of those bonds over time.
#
#     sectors                    25 rows   industries
#     issuer_groups              60 rows   business groups
#     issuers                   800 rows   companies and governments
#     securities              1,500 rows   shares, bonds, money-market paper
#     credit_rating_history  ~2,000 rows   every rating, over time
#
#   THE 6TH TABLE, portfolio_holdings, IS NOT BUILT HERE. A holding's rupee
#   value is a share of the scheme's AUM, and AUM is not known until
#   transactions exist. It is built in summaries.py.
#
#   This is the SECOND builder run by run.py, and like fund_house.py it
#   depends on nothing.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   These five tables are what make the MULTI-HOP questions possible — and
#   multi-hop questions are the entire reason this project uses a graph
#   rather than a bigger prompt.
#
#   Take one question: "which schemes hold bonds that were downgraded?"
#
#   No single table can answer it. The route is:
#
#     schemes ──> portfolio_holdings ──> securities ──> credit_rating_history
#                                            │                    │
#                                            └── issuers          └── and this
#                                                                     join is
#                                                                     NOT declared
#
#   Four tables, three joins, and the last one is invisible to anything
#   reading only the database catalog. This file is where that route is
#   created; the AGE graph in Stage 5 only records what already exists here.
#
# WHAT GETS CREATED, IN DETAIL:
#
#     800 issuers       1 central government
#                      19 state governments
#                     780 companies, banks and non-bank lenders
#
#   1,500 securities    520 shares         one per listed company
#                       600 corporate bonds
#                       120 commercial papers
#                        60 certificates of deposit
#                       150 government securities
#                        50 treasury bills
#
#   ~2,000 ratings     covering the 780 rated debt securities. Shares and
#                      government paper are not rated — explained below.
#
# WHAT THIS FILE RETURNS, AND WHY IT IS TWO THINGS:
#   Unlike fund_house.py, build() here returns a PAIR:
#
#     1. the five tables, for the database
#     2. a helper frame of facts about every security, for summaries.py
#
#   The second never reaches PostgreSQL. It exists because summaries.py has
#   to decide which securities each scheme holds, and to do that it needs
#   each security's type, sector, group and starting credit rating together.
#   Handing those over directly is simpler and faster than re-deriving them
#   from the finished tables.
#
# WHICH PLANTED PATTERNS ARE SET UP HERE:
#   PATTERN 1 — the bond that gets downgraded, and its full rating history
#   PATTERN 2 — the five companies belonging to one business group
#   PATTERN 6 — the bank one scheme ends up over-exposed to
#
#   All three need SPECIFIC companies with SPECIFIC names, because other
#   files look them up by name. So they are created FIRST, before the random
#   ones, at known positions.
#
# HOW TO USE:
#   Not run directly. run.py calls it second:
#
#       market_tables, security_facts = market.build(settings)
#       tables.update(market_tables)
##############################################################################


import numpy as np
# Used for the random draws, the date arithmetic on maturity dates, and
# np.setdiff1d, which removes specific ids from a pool before sampling.

import polars as pl
# Holds the five tables, and does the join that builds the security_facts
# helper frame at the end.

from dataplane.generator import patterns as P
# Patterns 1, 2 and 6. Their issuer names, group names and the two rating
# dates for the downgraded bond.

from dataplane.generator.common import NAME_WORDS, SECOND_WORDS, Settings, to_dates
# NAME_WORDS    72 first words for invented company names
# SECOND_WORDS  22 industry-suggesting second words
# Settings      the seeded generator and the date window. Like
#               fund_house.py, this file ignores `scale` — the market does
#               not get bigger because more people invest.
# to_dates      converts a list containing None into a polars Date column.
#               Needed twice: maturity dates (empty for shares) and rating
#               dates.


SECTORS = [
    "Banking", "NBFC & Housing Finance", "Insurance", "Information Technology",
    "Pharmaceuticals", "Healthcare Services", "FMCG", "Automobiles", "Auto Components",
    "Capital Goods", "Cement", "Construction", "Metals & Mining", "Oil & Gas", "Power",
    "Telecom", "Chemicals", "Textiles", "Real Estate", "Consumer Durables", "Retail",
    "Media & Entertainment", "Logistics", "Agriculture", "Government",
]
# THE 25 INDUSTRIES. Roughly the sector classification Indian exchanges use.
#
# NOTE "Government" IS IN THE LIST, and it is there for a structural reason
# rather than because a government is an industry. Governments issue
# securities too, and every issuer needs a sector because the column is NOT
# NULL in the schema. So they get their own.
#
# THE FIRST TWO MATTER FOR THE PATTERNS. Pattern 1's company is in "NBFC &
# Housing Finance" and pattern 6's bank is in "Banking" — and the Banking &
# Financial Services Fund is restricted to exactly those two plus Insurance,
# which is what makes pattern 6's concentration breach plausible: a small
# universe to pick from.
#
# Note "NBFC & Housing Finance" contains an AMPERSAND, which travels through
# CSV, COPY, a JSON prompt and an embedding without special treatment. A
# small, real test of the pipeline.

SECTOR_ID = {name: i + 1 for i, name in enumerate(SECTORS)}
# Name to id lookup, same position-plus-one convention as fund_house.py.
#
# EXPORTED AND USED ELSEWHERE: summaries.py imports this to turn the sector
# names in its SECTORAL dictionary into ids, so a sectoral fund can be
# filtered to its allowed sectors. Which means renaming a sector here
# without renaming it there raises a KeyError at generation time — a loud
# failure, which is what we want.

RATING_SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
                "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B", "C", "D"]
# THE 16-STEP RATING LADDER, BEST TO WORST. A downgrade moves to a LATER
# position in this list.
#
# AND THE POSITION IS THE POINT. Everything in this file works with the
# POSITION — an integer — rather than the rating string, because comparing
# rating text is wrong:
#
#     'AA+' < 'AAA'   is TRUE as a string comparison, because '+' comes
#                     before 'A' in character order
#     'B' < 'BBB'     is TRUE as a string comparison
#
# So "is this bond rated below A?" cannot be answered by comparing text.
# Positions can be compared; strings cannot. Any code that sorts or filters
# this column alphabetically is broken, and it is a mistake students make
# constantly.
#
# The first ten positions — AAA down to BBB- — are "investment grade".
# Everything from BB+ down is speculative, commonly called junk. The clip at
# position 9 further down is what keeps every randomly generated security
# inside investment grade.

AGENCIES = ["Meridian Credit Ratings", "Indus Ratings & Research",
            "Konark Rating Services", "Apex Rating Agency"]
# FOUR INVENTED RATING AGENCIES, so no real agency's opinion is implied.
#
# That matters more here than for the companies. A rating is a published
# professional opinion, and putting a real agency's name on an invented
# downgrade of an invented company would be attributing a judgement they
# never made.
#
# WHY FOUR RATHER THAN ONE: because two agencies can rate the same bond
# differently on the same day, so "the rating" of a bond is not one value —
# it depends which agency you ask. Each security here gets ONE agency
# (chosen at random and kept for all its ratings), so that complication
# exists in the schema without exploding the row count.

STATES = ["Maharashtra", "Karnataka", "Tamil Nadu", "Gujarat", "Rajasthan", "Uttar Pradesh",
          "West Bengal", "Kerala", "Telangana", "Andhra Pradesh", "Madhya Pradesh", "Punjab",
          "Haryana", "Bihar", "Odisha", "Assam", "Chhattisgarh", "Jharkhand", "Uttarakhand"]
# THE 19 STATE GOVERNMENTS that issue State Development Loans — bonds issued
# by an Indian state rather than the central government.
#
# Real states, and that is fine: the fact that Maharashtra issues bonds is
# public record, not a claim about anybody. The pattern companies are
# invented because the patterns accuse them of something; a state issuing a
# bond is not an accusation.

CORPORATE_SECTORS = [s for s in SECTORS if s not in ("Banking", "NBFC & Housing Finance", "Government")]
# THE 22 SECTORS AN ORDINARY COMPANY CAN BE IN — everything except the two
# financial sectors and Government.
#
# WHY EXCLUDE THEM: the issuer loop below assigns a sector based on what KIND
# of entity it is creating. A "Bank" gets Banking, an "NBFC" gets NBFC &
# Housing Finance, a "Government" gets Government. So when it creates a
# plain "Corporate", it must not accidentally put it in Banking — a
# corporate issuer in the Banking sector would contradict its own
# issuer_type, and a query filtering on either column would get different
# answers.
#
# Computed once as a list comprehension rather than typed out, so adding a
# sector above automatically makes it available to corporates.


def build(settings: Settings) -> tuple[dict[str, pl.DataFrame], pl.DataFrame]:
    """
    Build the 5 market tables, plus the helper frame summaries.py needs.

    Args:
        settings: the seeded generator and the date window. `scale` is
                  ignored.

    Returns:
        A 2-tuple:
          [0] dict of 5 table names to polars DataFrames, ready to load
          [1] security_facts — one row per security, with the type, sector,
              issuer, group and starting rating that summaries.py needs to
              choose holdings. NEVER loaded into the database.
    """
    rng = settings.rng

    # ---------------------------------------------------------------------
    # sectors
    # ---------------------------------------------------------------------
    sectors = pl.DataFrame(
        {
            "sector_id": list(range(1, len(SECTORS) + 1)),
            "sector_code": [
                "".join(w[0] for w in s.replace("&", "").split()).upper() + f"{i + 1:02d}"
                for i, s in enumerate(SECTORS)
            ],
            # BUILD A SHORT CODE FROM THE FIRST LETTERS OF EACH WORD, plus
            # the id as two digits. Read it inside out, for "NBFC & Housing
            # Finance":
            #
            #   s.replace("&", "")          "NBFC  Housing Finance"
            #   .split()                    ["NBFC", "Housing", "Finance"]
            #   w[0] for w in ...           "N", "H", "F"
            #   "".join(...)                "NHF"
            #   .upper()                    "NHF"
            #   + f"{i + 1:02d}"            "NHF02"
            #
            # WHY THE ID IS APPENDED: without it the codes would collide.
            # "Capital Goods" and "Consumer Durables" both give "CD", and
            # sector_code is UNIQUE in the schema — so the load would fail on
            # the second one. Appending the id guarantees uniqueness however
            # the names change.
            #
            # The .replace("&", "") is needed because splitting "NBFC &
            # Housing Finance" without it yields "&" as a word, and "&"[0]
            # is "&" — giving "N&HF".
            "sector_name": SECTORS,
            # The list itself, used directly as a column.
        }
    )

    # ---------------------------------------------------------------------
    # issuer_groups: 60 business groups
    # ---------------------------------------------------------------------
    group_names = [P.P1_GROUP_NAME, P.P2_GROUP_NAME]
    # PATTERNS 1 AND 2'S GROUPS GO FIRST, at positions 0 and 1 — so they
    # become group_id 1 and 2.
    #
    # Pattern 2 is the one that needs this: its five companies must all be in
    # ONE group, and summaries.py plants weights on them by name. If the
    # group were drawn at random the five companies could end up in
    # different groups and the concentration would not exist.

    for word in NAME_WORDS:
        name = f"{word} Group"
        if name not in group_names and len(group_names) < 60:
            group_names.append(name)
    # Fill to 60 from the word list. "Ashoka Group", "Trident Group", and so
    # on.
    #
    # THE `not in` CHECK MATTERS BECAUSE OF THE TWO PATTERN GROUPS. They are
    # "Vardhaman Group" and "Trident Group", and both "Vardhaman" and
    # "Trident" are in NAME_WORDS — so without the check, each would be
    # added a second time and group_name is UNIQUE in the schema. The load
    # would fail on a unique violation.
    #
    # NAME_WORDS has 72 entries and we stop at 60, so 12 words go unused as
    # group names. They are still used for company names below.

    group_id = {name: i + 1 for i, name in enumerate(group_names)}
    issuer_groups = pl.DataFrame({"group_id": list(range(1, 61)), "group_name": group_names})
    # Note the hard-coded 61 here against len()-based ranges elsewhere. It
    # works because the loop above stops at exactly 60 — but it is the one
    # place in this file where changing the count would need two edits.

    # ---------------------------------------------------------------------
    # issuers: 800 companies and governments
    # ---------------------------------------------------------------------
    issuers: list[tuple] = []
    taken: set[str] = set()
    # A list for the rows, and a SET for the names already used.
    #
    # Why a set alongside the list: checking "is this name taken?" against a
    # list of 800 is a linear scan, done 800-plus times. Against a set it is
    # instant. At this size either works; the habit is the point.

    def add(name, group, sector, kind, listed):
        issuers.append((name, group, sector, kind, listed))
        taken.add(name)
    # A tiny helper so every addition updates BOTH structures. Appending to
    # the list without adding to the set would silently break the duplicate
    # check — exactly the kind of bug a two-line helper prevents.
    #
    # Each issuer is (name, group name or None, sector, issuer_type, whether
    # shares are listed).

    add("Government of India", None, "Government", "Government", False)
    for state in STATES:
        add(f"State Government of {state}", None, "Government", "Government", False)
    # GOVERNMENTS FIRST, so they occupy issuer_id 1 through 20. One central,
    # 19 states.
    #
    # THAT POSITIONING IS RELIED ON BELOW: `n_gov = 20` and the central
    # government is referenced as issuer_id 1 when creating G-Secs and
    # T-Bills. Adding a 20th state would shift every pattern company's id
    # and silently break the hard-coded ranges.
    #
    # group is None — a government belongs to no business group, which is
    # exactly what the nullable group_id column is for.
    #
    # listed is False — a government has no shares.

    add(P.P1_ISSUER_NAME, P.P1_GROUP_NAME, "NBFC & Housing Finance", "NBFC", True)     # pattern 1
    for name, sector, _weight in P.P2_COMPANIES:                                         # pattern 2
        add(name, P.P2_GROUP_NAME, sector, "Corporate", True)
    add(P.P6_ISSUER_NAME, None, "Banking", "Bank", True)                                 # pattern 6
    # THE SEVEN PATTERN COMPANIES, at issuer_id 21 through 27:
    #
    #   21        pattern 1's housing finance company
    #   22 to 26  pattern 2's five Trident companies
    #   27        pattern 6's bank
    #
    # ALL SEVEN HAVE listed=True, and the next section hard-codes
    # np.arange(21, 28) to guarantee it. That hard-coded range is the
    # fragile part of this file: add one government above and these numbers
    # all shift, and the pattern companies would silently stop having
    # shares.
    #
    # The sectors are chosen to make each pattern plausible. Pattern 1 is an
    # NBFC — a non-bank lender, the kind of company that genuinely gets
    # downgraded. Pattern 6 is a Bank, which legitimately falls inside the
    # Banking & Financial Services Fund's mandate, so the breach is a
    # concentration problem and not also a mandate breach.
    #
    # Pattern 2's five take their sectors from the patterns file — Chemicals,
    # Power, Chemicals, Logistics, Real Estate. Deliberately spread, so a
    # SECTOR concentration check passes and only the GROUP check reveals the
    # problem. `_weight` is the third field and is used by summaries.py, not
    # here.
    #
    # Pattern 6's bank has group=None. Giving it a group would add a second
    # dimension to a pattern that is about a single issuer, and could
    # accidentally create a second group concentration.

    other_groups = group_names[2:]
    # The 58 groups EXCLUDING the two pattern groups.
    #
    # THIS SLICE IS WHAT KEEPS PATTERNS 1 AND 2 EXACTLY AS DESIGNED. Random
    # companies are drawn from this list, so no random company can land in
    # "Trident Group" and inflate pattern 2's concentration beyond the
    # planted 24%, or in "Vardhaman Group" and muddle pattern 1.

    while len(issuers) < 800:
        roll = rng.random()
        word = str(rng.choice(NAME_WORDS))
        # ONE RANDOM NUMBER DECIDES WHAT KIND OF COMPANY THIS IS, by falling
        # into one of four bands. The bands are cumulative, so the order of
        # the branches below is the order of the bands.

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
        # THE MIX, AND IT IS A DESIGN DECISION:
        #
        #   7%    Bank          "Teesta National Bank Ltd"
        #   10%   NBFC          "Chambal Housing Finance Ltd"
        #   8%    PSU           "Narmada Power Corporation Ltd"
        #   75%   Corporate     "Tungabhadra Polymers Ltd"
        #
        # 7% banks of 780 companies is about 55 banks, and that number
        # matters: the 60 certificates of deposit below are drawn only from
        # banks, so there have to be enough banks to draw from.
        #
        # NOTICE THE NAME AND THE SECTOR AGREE for the three financial kinds.
        # A Bank is named "... Bank Ltd" and is in Banking. A PSU is named
        # after its own sector — `sector.split()[0]` takes the first word, so
        # "Oil & Gas" gives "Narmada Oil Corporation Ltd".
        #
        # BUT FOR AN ORDINARY CORPORATE, THE NAME AND SECTOR ARE DRAWN
        # INDEPENDENTLY. So "Nilgiri Steel Ltd" can be classified under
        # Chemicals. That looks like a bug and is in fact realistic —
        # company names rarely match their current business after a few
        # decades of diversification.
        #
        # It is also a useful reminder for the course: A NAME IS NOT DATA.
        # The sector column is. Any query that parses a company name to work
        # out its industry will be wrong here, and wrong on a real database
        # too.
        #
        # str(...) around every rng.choice on a list of strings, because
        # numpy returns its own string type and polars would store it as an
        # object column.

        if name in taken:
            continue
        # SKIP AND REDRAW ON A COLLISION. issuer_name is UNIQUE in the
        # schema, so a duplicate would fail the load.
        #
        # 72 words x 22 second words gives 1,584 possible corporate names for
        # ~585 corporates, so collisions are common enough that this check
        # fires regularly. The `while len(issuers) < 800` loop simply runs a
        # few more times.

        group = str(rng.choice(other_groups)) if rng.random() < 0.40 else None
        add(name, group, sector, kind, False)   # "listed" is decided just below
        # ABOUT 40% OF COMPANIES BELONG TO A BUSINESS GROUP; the rest are
        # independent and get None.
        #
        # THAT 60% WITH NO GROUP IS WHY group_id IS NULLABLE in the schema,
        # and it is the thing that changes how every query touching it has to
        # be written: count issuers by sector and the numbers add to 800;
        # count by group and they do not, because an inner join drops the
        # ungrouped ones.
        #
        # The trailing comment flags that `listed=False` here is a
        # placeholder — the actual listing decision is made in the next
        # section, and this field of the tuple is never read. A small wart:
        # the fifth tuple field exists and is unused.

    issuer_df = pl.DataFrame(
        {
            "issuer_id": list(range(1, 801)),
            "issuer_name": [i[0] for i in issuers],
            "group_id": [group_id[i[1]] if i[1] else None for i in issuers],
            # The conditional turns a group NAME into an id, or leaves None.
            # `if i[1]` rather than `if i[1] is not None` — equivalent here
            # because no group is named with an empty string.
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
    # Three pools of issuer ids. `bank_ids` is queried out of the finished
    # frame rather than tracked during the loop — about 55 ids, including
    # pattern 6's bank at 27.

    fixed_listed = np.arange(21, 28)
    others = rng.choice(np.setdiff1d(company_ids, fixed_listed), 520 - len(fixed_listed), replace=False)
    listed_ids = np.concatenate([fixed_listed, others])
    # WHICH 520 COMPANIES HAVE SHARES.
    #
    # The seven pattern companies ALWAYS do — that is the hard-coded
    # np.arange(21, 28). Then 513 more are drawn at random from the rest.
    #
    # np.setdiff1d removes the seven from the pool first, so none can be
    # drawn twice. replace=False means no company gets two shares.
    #
    # WHY THE PATTERN COMPANIES MUST BE LISTED: patterns 2 and 6 are both
    # about EQUITY holdings. summaries.py looks up each pattern company's
    # share by name, and takes [0] of the result — so a company with no
    # share would raise an IndexError there, hundreds of lines later, with
    # no hint that the cause was a random draw here.

    securities = []   # (issuer_id, security_type, coupon %, maturity date)

    def maturity(low_year, high_year):
        year = int(rng.integers(low_year, high_year + 1))
        return np.datetime64(f"{year}-{int(rng.integers(1, 13)):02d}-15")
    # A random maturity date: a random year in the range, a random month,
    # always the 15th.
    #
    # ALWAYS THE 15TH because the day of month is irrelevant to every
    # question in this project and a random day would add noise with no
    # information. Real bonds do mature on specific dates; nothing here asks
    # about them.
    #
    # `high_year + 1` because rng.integers excludes its upper bound.
    # rng.integers(1, 13) for the month, for the same reason.
    # The :02d pads the month, so 3 becomes "03" and np.datetime64 can parse
    # it.

    for issuer_id in listed_ids:
        securities.append((int(issuer_id), "Equity", None, None))
    # 520 SHARES. Both coupon and maturity are None — a share pays no fixed
    # interest and never matures. Which is why both columns are nullable in
    # the schema, and why about a third of the securities table has nothing
    # in either.

    securities.append((21, "Corporate Bond", 8.9, np.datetime64("2028-06-15")))
    for issuer_id in rng.choice(np.setdiff1d(company_ids, [21]), 599):
        securities.append((int(issuer_id), "Corporate Bond", round(float(rng.uniform(6.8, 10.5)), 4), maturity(2027, 2036)))
    # 600 CORPORATE BONDS, AND THE FIRST ONE IS PATTERN 1'S.
    #
    # Issuer 21 gets EXACTLY ONE bond — the one that gets downgraded — and is
    # then REMOVED from the pool the other 599 are drawn from, by
    # np.setdiff1d.
    #
    # WHY THAT MATTERS: if that company had three bonds, "which bond was
    # downgraded?" would have an ambiguous answer, and summaries.py would
    # have to decide which of the three the funds held. One bond, one
    # downgrade, one story.
    #
    # Its coupon is a fixed 8.9% and its maturity a fixed 2028 — so the
    # bond's own details are stable across runs, not just its existence.
    #
    # The other 599 are drawn WITH replacement (no replace=False), so a
    # company can have several bonds. That is realistic — a large company
    # issues bonds repeatedly — and it is exactly the condition that makes
    # summaries.py's "at most one security per issuer" deduplication
    # necessary: without it, a company with four bonds could reach 32% of a
    # portfolio through four separate 8% holdings.
    #
    # Coupons from 6.8% to 10.5%, rounded to 4 decimals to match
    # numeric(7,4).

    for issuer_id in rng.choice(np.setdiff1d(company_ids, [21]), 120):
        securities.append((int(issuer_id), "Commercial Paper", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 300)), "D")))
    for issuer_id in rng.choice(bank_ids, 60):
        securities.append((int(issuer_id), "Certificate of Deposit", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 300)), "D")))
    # 120 COMMERCIAL PAPERS and 60 CERTIFICATES OF DEPOSIT.
    #
    # Commercial paper is short-term COMPANY borrowing; a certificate of
    # deposit is short-term BANK borrowing. Which is why the CDs are drawn
    # only from `bank_ids` — a non-bank cannot issue one.
    #
    # BOTH HAVE NO COUPON. They are bought BELOW face value and repay face
    # value at maturity, so the return is the discount rather than interest
    # payments. The column is left empty, and the schema's CHECK on it
    # passes automatically on NULL.
    #
    # THE MATURITIES ARE ALL JUST AFTER THE WINDOW CLOSES — 2026-09-15 plus
    # up to 300 days. That is deliberate: these are short-dated instruments,
    # and if they matured inside the window they would have to disappear
    # from portfolios partway through, which summaries.py does not model.
    # Keeping every maturity beyond the window means every security is
    # holdable on every month end.
    #
    # Pattern 1's issuer is excluded from commercial paper too, so its only
    # security apart from its share is the one downgraded bond.

    for _ in range(100):
        securities.append((1, "Government Security", round(float(rng.uniform(6.8, 7.6)), 4), maturity(2027, 2053)))
    for _ in range(50):
        securities.append((int(rng.integers(2, 21)), "Government Security", round(float(rng.uniform(7.0, 7.8)), 4), maturity(2027, 2040)))
    for _ in range(50):
        securities.append((1, "Treasury Bill", None,
                           np.datetime64("2026-09-15") + np.timedelta64(int(rng.integers(0, 180)), "D")))
    # 150 GOVERNMENT SECURITIES AND 50 TREASURY BILLS.
    #
    #   100 central G-Secs, issuer 1, maturing 2027 to 2053
    #    50 state loans, issuers 2 to 20, maturing 2027 to 2040
    #    50 T-Bills, issuer 1, maturing just after the window
    #
    # THREE DETAILS WORTH READING:
    #
    # The central government's bonds run out to 2053 — a 30-year maturity.
    # State loans stop at 2040, because states issue shorter paper. That
    # difference is what makes the Long Duration and Gilt funds meaningfully
    # different from the short-duration ones.
    #
    # STATE LOANS PAY SLIGHTLY MORE — 7.0% to 7.8% against the centre's 6.8%
    # to 7.6%. A state is a marginally riskier borrower than the central
    # government, and the market charges it. A small piece of real finance,
    # free to include.
    #
    # A TREASURY BILL HAS NO COUPON, like commercial paper, because it is
    # also sold at a discount. A G-Sec does have one.
    #
    # Note `rng.integers(2, 21)` for state ids — 2 through 20, the 19 states.
    # Another place the governments-first ordering is relied on.

    n_sec = len(securities)
    ids = np.arange(1, n_sec + 1)
    securities_df = pl.DataFrame(
        {
            "security_id": ids,
            "security_code": [f"SEC{i:06d}" for i in ids],
            # "SEC000001" to "SEC001500".
            #
            # THIS COLUMN IS THE TARGET OF HIDDEN LINK 2.
            # credit_rating_history stores this value in a column called
            # `sec_cd`, with no foreign key — so the only way to discover the
            # link is to notice the values overlap.
            #
            # Note the ratings table stores the CODE rather than the
            # security_id. Which is realistic: the ratings feed comes from
            # outside, from an agency, and an outside system has no idea what
            # our internal security_id is. It can only quote the public code.
            "isin": [
                (f"IN00{i:08d}" if s[1] in ("Government Security", "Treasury Bill") else f"INE{i:07d}10")
                for i, s in zip(ids, securities)
            ],
            # TWO ISIN SHAPES, BOTH EXACTLY 12 CHARACTERS:
            #
            #   government  "IN00" + 8 digits        = 4 + 8  = 12
            #   everything  "INE"  + 7 digits + "10" = 3 + 7 + 2 = 12
            #
            # Real Indian ISINs do distinguish government paper this way —
            # government securities start IN00 and corporate ones INE.
            #
            # The schema has CHECK (length(isin) = 12), so both arithmetics
            # have to be exactly right. Getting one digit wrong in either
            # branch would fail the load on a third of the rows, and the
            # error would name the constraint rather than the branch.
            "issuer_id": [s[0] for s in securities],
            "security_type": [s[1] for s in securities],
            "coupon_rate_pct": [s[2] for s in securities],
            # Contains None for every share, commercial paper, CD and T-Bill
            # — about 750 of the 1,500 rows. polars infers a nullable Float64
            # column from a list of floats and Nones, which is what we want.
            "maturity_date": pl.Series(to_dates([s[3] for s in securities]), dtype=pl.Date),
            # to_dates() HERE because this list mixes numpy dates with None
            # — 520 shares have no maturity. polars cannot build a Date
            # column from that mixture; it would fall back to an object
            # column, which the CSV loader cannot write.
        }
    )

    # ---------------------------------------------------------------------
    # credit_rating_history: ratings of every company debt security
    # ---------------------------------------------------------------------
    issuer_type = dict(zip(issuer_df["issuer_id"].to_list(), issuer_df["issuer_type"].to_list()))
    # issuer_id to issuer_type, as a plain dict for fast lookup inside the
    # loop below. Querying the frame 780 times instead would be far slower.

    pattern1_bond_id = int(
        securities_df.filter((pl.col("issuer_id") == 21) & (pl.col("security_type") == "Corporate Bond"))["security_id"][0]
    )
    # FIND PATTERN 1'S BOND, by issuer and type.
    #
    # [0] takes the first match, and there is exactly one because issuer 21
    # was given precisely one bond and excluded from every other draw. If
    # that exclusion were removed, this line would silently pick an
    # arbitrary one of several.
    #
    # Used twice: to give this bond its special rating history below, and to
    # flag it in security_facts so summaries.py can find it.

    rating_rows = []   # (security_code, agency, rating, date, outlook)
    first_rating = {}  # security_id -> starting rating position, used for holdings

    for sec in securities_df.iter_rows(named=True):
        if sec["security_type"] in ("Equity", "Government Security", "Treasury Bill"):
            continue
        # WHO IS NOT RATED, AND WHY.
        #
        # A SHARE has no credit rating at all — you are buying ownership, not
        # lending money, so there is no question of repayment.
        #
        # GOVERNMENT PAPER is treated as risk-free. A sovereign is the top of
        # the ladder by definition, and in practice Indian G-Secs are not
        # rated by domestic agencies.
        #
        # That leaves 780 rated securities out of 1,500: the 600 corporate
        # bonds, 120 commercial papers and 60 CDs.
        #
        # AND IT HAS A CONSEQUENCE FOR summaries.py: government securities
        # have no entry in `first_rating`, so the "good bonds" pool there
        # includes them with no rating filter at all. A rating filter applied
        # to G-Secs would exclude every one of them.

        code = sec["security_code"]
        agency = str(rng.choice(AGENCIES))
        # ONE AGENCY PER SECURITY, chosen once and used for all its ratings.
        #
        # So a bond's rating history is one agency's view over time, rather
        # than a mixture of four agencies' views — which keeps "what is this
        # bond rated?" answerable as "the latest row", instead of requiring
        # a choice of agency first.

        if sec["security_id"] == pattern1_bond_id:
            rating_rows.append((code, agency, "AA", settings.start, "Stable"))
            rating_rows.append((code, agency, "AA-", P.P1_EARLIER_CUT_DATE, "Negative"))
            rating_rows.append((code, agency, "BBB-", P.P1_DOWNGRADE_DATE, "Negative"))
            first_rating[sec["security_id"]] = RATING_SCALE.index("AA")
            continue
        # PATTERN 1'S FULL RATING HISTORY — THREE ROWS, AND THE SHAPE IS THE
        # POINT.
        #
        #   AA   Stable     at the window start
        #   AA-  Negative   May 2023 — one notch down, and the outlook turns
        #   BBB- Negative   March 2024 — SIX notches down, a severe cut
        #
        # THE MIDDLE ROW IS THE MOST VALUABLE LINE IN THIS BLOCK. It makes
        # this table a genuine HISTORY rather than a single event:
        #
        #   - "what is this bond rated?" has three answers depending on the
        #     date, so the correct query is "the most recent row on or before
        #     X" — a skill never exercised if every bond had one rating
        #   - the earlier cut, with a Negative outlook, is a WARNING SIGN
        #     that preceded the severe one, which is how credit
        #     deterioration actually looks
        #   - a query taking MAX(rtg) or any arbitrary row gets a different
        #     answer from one taking the latest by rtg_dt
        #
        # Note the outlook turns Negative at the FIRST cut and stays
        # Negative. An agency signalling further weakness is the thing an
        # analyst would want to have noticed ten months early.
        #
        # first_rating is set to AA's position — position 2 — because
        # summaries.py uses the STARTING rating to decide which schemes may
        # hold it. At AA it qualifies for a Credit Risk Fund's pool; at BBB-
        # it would be excluded from almost everything, and the schemes could
        # never have held it in the first place.
        #
        # `continue` skips the random rating logic entirely, so this bond's
        # history is exactly these three rows and nothing more.

        kind = issuer_type[sec["issuer_id"]]
        if kind in ("Bank", "PSU"):
            position = int(rng.choice([0, 0, 0, 1, 1, 2]))
        elif kind == "NBFC":
            position = int(rng.choice([1, 2, 2, 3, 3, 4, 5]))
        else:
            position = int(rng.choice([0, 1, 2, 2, 3, 3, 4, 5, 6, 7, 8]))
        # THE STARTING RATING DEPENDS ON WHO ISSUED IT, and the hierarchy is
        # real:
        #
        #   Bank / PSU   positions 0-2 (AAA to AA)      the safest
        #   NBFC         positions 1-5 (AA+ to A)       a notch or two lower
        #   Corporate    positions 0-8 (AAA to BBB)     spread much wider
        #
        # A bank is backed by deposits and regulation; a PSU is
        # government-owned. A non-bank lender borrows to lend and is more
        # fragile — which is exactly why pattern 1's company is an NBFC.
        #
        # AND LOOK AT HOW THE WEIGHTING IS DONE: by REPEATING VALUES in the
        # list. [0, 0, 0, 1, 1, 2] means position 0 is three times as likely
        # as position 2.
        #
        # That is a cheap and very readable way to weight a draw without
        # computing probabilities, and it is worth pointing out as a
        # technique. The alternative, rng.choice([0,1,2], p=[0.5,0.333,0.167]),
        # requires the probabilities to sum to exactly 1.0 and is harder to
        # adjust by eye.

        first_rating[sec["security_id"]] = position
        rating_rows.append((code, agency, RATING_SCALE[position], settings.start, "Stable"))
        # The opening rating, dated at the window start, always Stable. Every
        # rated security has at least this one row.

        date = settings.start
        for _ in range(int(rng.poisson(1.5))):
            date = date + np.timedelta64(int(rng.integers(60, 500)), "D")
            if date > settings.end:
                break
            step = 1 if rng.random() < 0.55 else -1
            position = int(np.clip(position + step, 0, 9))   # stays within AAA .. BBB-
            outlook = "Negative" if step == 1 else "Positive"
            rating_rows.append((code, agency, RATING_SCALE[position], date, outlook))
        # THEN A FEW CHANGES OVER THE FIVE YEARS, and four things to note.
        #
        # rng.poisson(1.5) — ABOUT 1.5 CHANGES PER SECURITY ON AVERAGE. A
        # Poisson draw is the right tool for "how many independent events in
        # a period": most securities get one or two, some get none, a few get
        # four or five. A uniform draw would say a bond is as likely to be
        # re-rated five times as never.
        #
        # THE DATE WALKS FORWARD CUMULATIVELY — each change is 60 to 500 days
        # after the last, not after the start. So the ratings of one security
        # are in chronological order and properly spaced, rather than
        # clustered or out of order.
        #
        # The `break` on passing the window end means a security whose draw
        # said "three changes" may only get one, if the gaps ran long. Which
        # is correct: we cannot record a rating change that happens after our
        # data ends.
        #
        # step = +1 at 55%, -1 at 45% — DOWNGRADES SLIGHTLY MORE COMMON than
        # upgrades, which matches the long-run pattern in credit. +1 means
        # moving to a LATER position in the scale, i.e. getting worse. The
        # outlook follows the direction: Negative on a downgrade, Positive on
        # an upgrade.
        #
        # AND THE CLIP IS THE KEY LINE OF THE WHOLE BLOCK:
        #
        #     np.clip(position + step, 0, 9)
        #
        # Positions are held between 0 and 9 — AAA down to BBB-. SO NO
        # RANDOMLY GENERATED SECURITY EVER FALLS BELOW INVESTMENT GRADE.
        #
        # THAT IS WHAT MAKES PATTERN 1 FINDABLE. The downgraded bond goes to
        # BBB- deliberately, and because nothing else in the database is
        # rated worse, a query for "securities downgraded three or more
        # notches" or "bonds now at the bottom of investment grade" returns
        # essentially one answer.
        #
        # A planted signal needs a deliberately clean baseline, or it is just
        # noise. The same principle appears in summaries.py, where random
        # holdings are capped at 8% so that pattern 6 is the only limit
        # breach.
        #
        # The clip also stops the random walk wandering into C or D, which
        # would mean securities in default sitting in ordinary debt funds —
        # a far bigger finding than any of our six, created by accident.

    credit_rating_history = pl.DataFrame(
        {
            "rtg_id": list(range(1, len(rating_rows) + 1)),
            "sec_cd": [r[0] for r in rating_rows],
            # HIDDEN LINK 2. Holds securities.security_code, under a
            # different name, with no foreign key.
            "rtg_agcy": [r[1] for r in rating_rows],
            "rtg": [r[2] for r in rating_rows],
            "rtg_dt": pl.Series(to_dates([r[3] for r in rating_rows]), dtype=pl.Date),
            # THE BUSINESS date — when the agency gave the rating.
            "outlk": [r[4] for r in rating_rows],
            "upd_ts": [f"{r[3]} 18:00:00+05:30" for r in rating_rows],
            # THE SYSTEM timestamp — when our system recorded the row. Built
            # as a STRING: "2024-03-12 18:00:00+05:30".
            #
            # SIX PM ON THE RATING DATE, for every row. Agencies publish
            # after market close, so an evening timestamp is plausible — and
            # a constant time of day is enough, because no question asks
            # about the hour.
            #
            # THE "+05:30" IS NOT OPTIONAL. It is India Standard Time,
            # written explicitly. The column is TIMESTAMPTZ, and without the
            # offset PostgreSQL would interpret the time in the SERVER's
            # timezone — which on Azure is UTC — recording every rating five
            # and a half hours late. Nothing would error.
            #
            # AND THIS PAIR OF COLUMNS IS THE TRAP THE WHOLE TABLE IS BUILT
            # AROUND. rtg_dt and upd_ts are two dates with similar
            # abbreviated names and completely different meanings. A question
            # about downgrades needs rtg_dt; a model that picks upd_ts gets a
            # query that runs, returns rows, and answers "when did we load
            # this?" with no error anywhere.
            #
            # Nothing in the column names distinguishes them. Only an
            # approved column note can — which is the single clearest
            # argument in this schema for why the metadata layer exists.
            #
            # Note both are derived from r[3], the same date. So they always
            # agree on the day and differ only in what they MEAN, which makes
            # the confusion harder to spot rather than easier.
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
    # ONE ROW PER SECURITY, WITH EVERYTHING summaries.py NEEDS TO CHOOSE
    # HOLDINGS. It never reaches PostgreSQL.
    #
    # The join pulls the issuer's name, group, sector and type down onto each
    # security — so summaries.py can filter by sector (for a sectoral fund),
    # by issuer_type (for a Banking & PSU fund) or by group, without joining
    # anything itself.
    #
    # TWO COMPUTED COLUMNS, AND BOTH ARE DESIGN DECISIONS.
    #
    # first_rating_pos IS A POSITION, NOT A RATING STRING. summaries.py
    # filters on it with comparisons like `first_rating_pos <= 2` for "AAA or
    # AA+", and `is_between(3, 8)` for a credit risk fund's band. Those
    # comparisons are only correct because the value is an integer — the same
    # filters on rating TEXT would be wrong, because 'AA+' sorts before
    # 'AAA'.
    #
    # replace_strict with default=None maps each security_id to its starting
    # position, and gives None for the 720 securities with no rating —
    # shares and government paper. Note the DEFAULT is what makes
    # replace_strict tolerable here: without it, replace_strict raises on an
    # unmapped value, which is usually what you want and is not, in this one
    # case, because unrated securities are legitimate.
    #
    # is_pattern1_bond IS A BOOLEAN FLAG, NOT A NAME LOOKUP. summaries.py
    # finds the downgraded bond with
    #
    #     facts.filter(pl.col("is_pattern1_bond"))["security_id"][0]
    #
    # rather than searching for the issuer name. A flag is safer: renaming
    # the issuer in patterns.py could never silently break it, whereas a name
    # match would return nothing and the [0] would raise an IndexError.

    tables = {
        "sectors": sectors,
        "issuer_groups": issuer_groups,
        "issuers": issuer_df,
        "securities": securities_df,
        "credit_rating_history": credit_rating_history,
    }
    return tables, security_facts
    # TWO THINGS OUT. run.py unpacks the pair:
    #
    #     market_tables, security_facts = market.build(settings)
    #     tables.update(market_tables)
    #
    # and holds security_facts until step 5, where it is handed to
    # summaries.build_holdings. The five tables go to the database; the
    # helper frame is working state passed between two generator files.