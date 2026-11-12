##############################################################################
# dataplane/generator/summaries.py
#
# PURPOSE:
#   Builds the last 2 tables — the two that SUMMARISE the others.
#
#     scheme_aum_monthly   ~660 rows   total money in each scheme, per month
#     portfolio_holdings ~24,000 rows  what each scheme HOLDS, per month
#
#   Small tables. They are built last and they are the most important ones
#   in the whole database, because they are what an analyst actually asks
#   about. Nobody asks "show me transaction 4,182,993". They ask "how much
#   does this fund manage, and what is it holding?"
#
# WHAT PROBLEM DOES THIS SOLVE:
#   In a real fund house, AUM — Assets Under Management — is NOT a number
#   somebody types into a form. It is computed, every month end, from the
#   units investors hold and the NAV on that day:
#
#       AUM of a plan   = units held at month end  x  NAV at month end
#       AUM of a scheme = the sum over its plans
#
#   So we compute it the same way, from the transactions generated in the
#   previous file. Which means:
#
#       IF THE TRANSACTIONS ARE WRONG, THE AUM IS WRONG IN THE SAME WAY.
#
#   That is the property we want. An invented AUM column would be internally
#   inconsistent with the transactions, and the first student who tried to
#   reconcile them would find the dataset is fiction.
#
#   Then each scheme's AUM is SPLIT ACROSS SECURITIES to produce the
#   holdings table, so there are two independent routes to the same number:
#
#       SUM(market_value) in portfolio_holdings  ==  aum_amount in scheme_aum_monthly
#
#   That equality is one of the Stage 2 validation checks, and it is the
#   single best test in the suite: it can only pass if both tables were
#   derived from the same underlying transactions.
#
# WHY THERE IS A HIDDEN LINK HERE:
#   scheme_aum_monthly.scheme_id points at schemes.scheme_id, with NO
#   FOREIGN KEY declared. This is HIDDEN LINK 1 of 3.
#
#   It is the EASY one of the three to discover, because the column names
#   match exactly — the metadata pipeline's same-name source finds it
#   immediately. Hidden links 2 and 3 have different column names and can
#   only be found by comparing values. Having one easy and two hard ones is
#   what makes the link-discovery scoring in Stage 5 meaningful: a pipeline
#   that only does name matching scores 1 out of 3.
#
# WHICH PATTERNS ARE PLANTED HERE:
#   PATTERN 1 — the downgraded bond's weight in the schemes holding it, and
#               the fact that the weight DROPS after the downgrade
#   PATTERN 2 — five companies from one business group, each held at a
#               modest weight, which together exceed a group concentration
#               limit
#   PATTERN 6 — one scheme's exposure to a single bank crossing the 10%
#               single-issuer limit from a certain month onwards
#
#   All three are expressed as FIXED WEIGHTS. The random holdings then fill
#   whatever percentage is left over, which is why a planted weight never
#   pushes a month past 100%.
#
# WHAT THIS FILE RETURNS:
#   Nothing is returned as a pair or triple here. Two separate functions:
#
#       build_aum(...)      -> scheme_aum_monthly
#       build_holdings(...) -> portfolio_holdings
#
#   They must be called IN THAT ORDER, because holdings needs the AUM to
#   split. run.py calls them in order.
##############################################################################


import numpy as np
# Used for the weight arithmetic — the capping, the drift and the rescaling
# that keep each month's weights adding to exactly 100.

import polars as pl
# Does the heavy work in build_aum: group-bys, a cumulative sum over a
# group, and the joins that attach NAVs and plans.

from dataplane.generator import patterns as P
# Pattern 1's weights and dates, pattern 2's five companies, pattern 6's
# scheme, issuer and before/after weights.

from dataplane.generator.fund_house import SCHEMES
# The 40 schemes in order. Position in the list is the scheme_id, and the
# holdings loop below walks it with enumerate(start=1) for exactly that
# reason.

from dataplane.generator.market import SECTOR_ID
# Sector name → sector_id, used to work out which sectors a sectoral fund
# is allowed to invest in.


MONEY_OUT = ["Redemption", "Switch Out"]
# THE SAME DEFINITION AS transactions.py, repeated here on purpose.
#
# This is the one piece of duplication in the generator, and it is worth
# naming: these two strings decide the SIGN of every unit movement. If this
# list and the one in transactions.py ever disagreed, AUM would be computed
# with the wrong sign on some transaction type, and the number would be
# wrong in a way that no test catches directly — it would just be too big.

MAX_RANDOM_WEIGHT = 8.0
# The largest weight any RANDOMLY chosen holding may have, in percent.
#
# WHY 8 AND NOT 10: Indian regulation caps a scheme's exposure to a single
# issuer at 10%. If random holdings could reach 10%, several schemes would
# brush against the limit by accident, and pattern 6 — the one scheme that
# deliberately breaches it — would be lost in the noise.
#
# Capping the random ones at 8% means that when a student asks "which
# schemes breach the single-issuer limit?", the answer is exactly one
# scheme, and it is the planted one. The pattern is only findable because
# the background is quiet.

SECTORAL = {
    "Sahyadri Banking & Financial Services Fund": ["Banking", "NBFC & Housing Finance", "Insurance"],
    "Sahyadri Infrastructure Fund": ["Construction", "Capital Goods", "Cement", "Power", "Logistics"],
    "Sahyadri Technology Fund": ["Information Technology", "Telecom", "Media & Entertainment"],
    "Sahyadri Manufacturing Opportunities Fund": ["Automobiles", "Auto Components", "Capital Goods",
                                                  "Chemicals", "Metals & Mining", "Consumer Durables"],
}
# Which sectors each SECTORAL fund is allowed to hold.
#
# A sectoral fund is one that promises, in its offer document, to invest
# only in a named part of the market. A banking fund holding pharmaceutical
# shares would be a mandate breach — a serious finding, not a quirk.
#
# So these lists are a constraint, not a preference. The holdings builder
# filters the available securities down to these sectors and picks only from
# what is left.
#
# Note "Capital Goods" appears in TWO lists. An infrastructure fund and a
# manufacturing fund can both legitimately hold a capital goods company, and
# they will end up holding different ones because each draws at random from
# the pool.


# ─────────────────────────────────────────────────────────────────────────────
# A SMALL HELPER THAT GETS USED FOUR TIMES
# ─────────────────────────────────────────────────────────────────────────────

def _month_end(col: str) -> pl.Expr:
    """
    The last day of the month of a date column.

        2024-03-12  ->  2024-03-31
        2024-02-07  ->  2024-02-29   (in a leap year)

    Returns a polars EXPRESSION, not a value — so it can be dropped into any
    with_columns or group_by call.

    WHY EVERYTHING IS SNAPPED TO MONTH END: both tables in this file are
    MONTHLY. A transaction on the 12th and another on the 27th both belong
    to the same monthly bucket, and the only way to group them is to give
    them the same date. Month end is the convention the industry uses,
    because that is when a fund values its portfolio.

    Note it is dt.month_end() and not "the 30th" or "the 31st". February,
    leap years and 30-day months all have to be right, or the join between
    the AUM table and the holdings table would miss rows for some months.
    """
    return pl.col(col).dt.month_end()


# ─────────────────────────────────────────────────────────────────────────────
# TABLE 1 OF 2 — scheme_aum_monthly
# ─────────────────────────────────────────────────────────────────────────────

def build_aum(transactions: pl.DataFrame, nav_history: pl.DataFrame, plans: pl.DataFrame,
              folios: pl.DataFrame) -> pl.DataFrame:
    """
    Compute scheme_aum_monthly from completed transactions and month-end NAVs.

    INPUT:
      transactions  the ~900,000 rows from transactions.py
      nav_history   a NAV per plan per business day, from fund_house.py
      plans         so a plan's AUM can be rolled up to its scheme
      folios        so we can count how many folios each scheme has

    THE CALCULATION, IN FOUR STEPS:
      1. turn every completed transaction into a signed unit movement
      2. add those up per plan per month
      3. run a cumulative sum to get the BALANCE at each month end
      4. multiply by that month's NAV, then roll plans up to schemes

    Step 3 is where people go wrong. A month's net units is a FLOW; AUM
    needs a STOCK. Multiplying the flow by the NAV would give "money that
    moved this month", not "money under management" — and the number would
    be about two orders of magnitude too small.
    """

    signed = transactions.filter(pl.col("txn_status") == "Completed").with_columns(
        # ONLY COMPLETED TRANSACTIONS COUNT. A rejected purchase moved no
        # money. Forgetting this filter overstates AUM by roughly 2%, and
        # nothing errors — the number is just wrong. This is the single most
        # common mistake a student will make against this dataset, which is
        # why the rejections were planted in the first place.
        pl.when(pl.col("txn_type").is_in(MONEY_OUT)).then(-pl.col("units")).otherwise(pl.col("units")).alias("delta"),
        # THE SIGN. Redemptions and switch-outs REMOVE units, so their units
        # become negative. Everything else adds.
        #
        # Note it is the UNITS that are signed, not the amount. Units are
        # what the investor holds; the rupee value changes with the NAV
        # every day, but the unit count only changes when a transaction
        # happens. Trying to compute AUM by adding up rupee amounts would
        # ignore five years of market movement entirely.
        _month_end("txn_date").alias("month_end_date"),
        # Which monthly bucket this transaction falls into.
    )

    net_by_month = signed.group_by("plan_id", "month_end_date").agg(pl.col("delta").sum())
    # Net units moved, per plan, per month. This is the FLOW.
    #
    # Grouped by PLAN, not scheme, because the NAV differs between the
    # Direct and Regular plans of the same scheme — Regular carries a higher
    # expense ratio, so its NAV is lower. Multiplying scheme-level units by
    # one of the two NAVs would be wrong for the other.

    month_nav = (
        nav_history.with_columns(_month_end("nav_date").alias("month_end_date"))
        .sort("nav_date")
        .group_by("plan_id", "month_end_date").agg(pl.col("nav").last())
        # The NAV on the LAST TRADING DAY of each month.
        #
        # .sort("nav_date") then .last() is the whole trick, and the sort is
        # not optional: group_by does not promise an order, so .last()
        # without the sort would return an arbitrary day's NAV from that
        # month. The AUM would then be computed against, say, the 9th of the
        # month — a believable-looking number that is simply not the
        # month-end AUM.
        #
        # And note it is the last TRADING day, not the calendar last day.
        # 31 March can be a Sunday. There is no NAV on a Sunday. The real
        # month-end NAV is Friday the 29th's, and that is what this returns,
        # labelled as the 31st.
    )

    plan_month = (
        month_nav.join(net_by_month, on=["plan_id", "month_end_date"], how="left")
        # START FROM month_nav, NOT FROM net_by_month. This direction is the
        # important design decision in this function.
        #
        # month_nav has a row for EVERY plan and EVERY month it had a NAV.
        # net_by_month only has rows for months where something happened.
        #
        # A plan where nobody transacted for three months still HAS AUM in
        # those months — the investors are still invested. If we started
        # from net_by_month, those three months would be missing from the
        # output, the cumulative sum would skip them, and the AUM chart
        # would have holes exactly where the fund was quietest.
        #
        # Starting from the NAV side and LEFT joining the activity on gives
        # a complete monthly grid.
        .with_columns(pl.col("delta").fill_null(0))
        # A month with no transactions has a net movement of zero, not an
        # unknown one. Leaving the null would make the cumulative sum below
        # return null from that point on, wiping out every later month.
        .sort("plan_id", "month_end_date")
        .with_columns(pl.col("delta").cum_sum().over("plan_id").alias("units_held"))
        # STEP 3 — FLOW TO STOCK. The running total of net units: the
        # balance each plan holds at each month end.
        #
        # .over("plan_id") restarts the total for each plan, like a SQL
        # window function. The .sort immediately above is required: a
        # cumulative sum is meaningless on unordered rows, and polars will
        # happily compute a wrong one.
        .with_columns((pl.col("units_held") * pl.col("nav")).alias("plan_aum"))
        # units x price = rupees under management. The definition of AUM, in
        # one multiplication.
        .join(plans.select("plan_id", "scheme_id"), on="plan_id")
        # Attach the scheme, so plans can be rolled up.
    )

    aum = plan_month.group_by("scheme_id", "month_end_date").agg(pl.col("plan_aum").sum().round(2).alias("aum_amount"))
    # Sum the plans of a scheme. A scheme with Direct-Growth,
    # Direct-IDCW, Regular-Growth and Regular-IDCW contributes four rows
    # that become one.
    #
    # Rounded to 2 decimals — paise. The rounding here, against the
    # separately rounded market_value in holdings, is why the Stage 2
    # cross-table check allows a small tolerance rather than demanding exact
    # equality.

    opened = folios.with_columns(_month_end("opened_date").alias("month_end_date")).group_by(
        "scheme_id", "month_end_date").agg(pl.len().alias("new_folios"))
    # How many folios were OPENED in each scheme in each month. A flow
    # again, which the next block turns into a stock.

    aum = (
        aum.join(opened, on=["scheme_id", "month_end_date"], how="left")
        .with_columns(pl.col("new_folios").fill_null(0))
        # A month where nobody opened a folio in this scheme: zero, not null.
        .sort("scheme_id", "month_end_date")
        .with_columns(pl.col("new_folios").cum_sum().over("scheme_id").cast(pl.Int64).alias("folio_count"))
        # The SAME cumulative-sum pattern as units, for a completely
        # different quantity. folio_count is the total number of folios ever
        # opened in the scheme by that month.
        #
        # AN HONEST LIMITATION, worth saying on camera: this count never
        # goes DOWN. A folio that was fully redeemed is still counted,
        # because folios has no closure date — in reality a folio stays open
        # with a zero balance until the investor formally closes it, which
        # most never do. So folio_count is "folios ever opened", and that is
        # also how the real registrar number behaves.
        #
        # .cast(pl.Int64) because the column is BIGINT; a cumulative sum can
        # come back as a different integer width.
    )

    return aum.select("scheme_id", "month_end_date", "aum_amount", "folio_count")
    # Four columns, in the order 01_fund_house.sql declares them. COPY
    # matches by position, so this order is a contract and not a comment.
    #
    # And remember: scheme_id here has NO DECLARED FOREIGN KEY to schemes.
    # That is hidden link 1, and it is deliberate.


# ─────────────────────────────────────────────────────────────────────────────
# WHICH SECURITIES MAY A SCHEME HOLD?
# ─────────────────────────────────────────────────────────────────────────────

def _pools(facts: pl.DataFrame, category_code: str, scheme_name: str) -> list[tuple[pl.DataFrame, int, float]]:
    """
    The securities a scheme is allowed to hold, as a list of

        (pool of securities, how many to pick, share of the fund in %)

    Most schemes get ONE entry — an equity fund holds only shares. HYBRID
    schemes get TWO: an equity pool and a debt pool, with the percentage
    split between them.

    THIS FUNCTION IS WHERE FUND MANDATES LIVE. A liquid fund may not hold
    equity. A gilt fund holds only government paper. A credit risk fund
    deliberately holds LOWER-rated bonds — that is its whole product. Each
    of those rules is one line below.

    WHY IT MATTERS FOR THE COURSE: these rules are what make questions like
    "which schemes hold below-investment-grade paper?" have a real,
    non-obvious answer. If every scheme held a random mix, the holdings
    table would be noise and no portfolio question would be interesting.
    """
    eq = facts.filter(pl.col("security_type") == "Equity")
    bond = facts.filter(pl.col("security_type") == "Corporate Bond")
    gsec = facts.filter(pl.col("security_type") == "Government Security")
    money_market = facts.filter(pl.col("security_type").is_in(["Treasury Bill", "Commercial Paper", "Certificate of Deposit"]))
    # Four pools, carved out of the security facts handed over by market.py.
    #
    # "Money market" means very short-dated paper — weeks to a year. The
    # three types grouped here are what liquid and overnight funds live on.

    good_bonds = pl.concat([bond.filter(pl.col("first_rating_pos") <= 2), gsec])
    # "Good" bonds: the top two rating positions (AAA and AA+), plus all
    # government securities.
    #
    # first_rating_pos is a POSITION in the rating ladder, where 0 is the
    # best. Comparing positions rather than rating STRINGS is what makes
    # this a one-line filter — "AAA" < "AA+" is false as a string
    # comparison, because 'A' sorts before '+'. Any code that compares
    # rating text alphabetically is wrong, and it is a mistake students make
    # constantly.
    #
    # Government securities are included with no rating filter because a
    # sovereign is the top of the ladder by definition, and market.py
    # therefore gives them no rating at all.

    if scheme_name in SECTORAL:
        ids = [SECTOR_ID[s] for s in SECTORAL[scheme_name]]
        return [(eq.filter(pl.col("sector_id").is_in(ids)), 32, 100.0)]
        # A sectoral fund: equity only, restricted to its sectors, about 32
        # holdings, 100% of the fund.
        #
        # Checked FIRST, before the category-code table below, because a
        # sectoral fund's category code would otherwise route it to the
        # generic equity pool and it would hold shares outside its mandate.

    if "Index" in scheme_name:
        return [(eq, 50, 100.0)]
        # An index fund: 50 holdings, because it tracks a 50-stock index.
        #
        # Matching on the NAME is crude, and it is the one place in this
        # file that does so rather than using a category code. It works
        # because every index scheme in SCHEMES has "Index" in its name.
        # Worth flagging on camera as the kind of shortcut that is fine in a
        # generator and would be a bug in production code.

    return {
        # Everything else, by CATEGORY CODE. These codes come from the
        # schemes table and follow a rough convention: DT = debt,
        # HY = hybrid, SO = solution-oriented.
        "DT-ON": [(money_market.filter(pl.col("security_type") != "Commercial Paper"), 30, 100.0)],
        # Overnight fund. Money market only, and NOT commercial paper —
        # which is a company's short-term IOU and carries credit risk. An
        # overnight fund is meant to be the safest thing in the lineup.
        "DT-LQ": [(money_market, 45, 100.0)],
        # Liquid fund. All money market paper, up to 91 days.
        "DT-MM": [(money_market, 45, 100.0)],
        # Money market fund.
        "DT-US": [(pl.concat([money_market, bond.filter(pl.col("first_rating_pos") <= 1)]), 45, 100.0)],
        # Ultra short duration. Money market plus the very best bonds only
        # (position <= 1, so AAA).
        "DT-LD": [(pl.concat([money_market, bond.filter(pl.col("first_rating_pos") <= 1)]), 45, 100.0)],
        "DT-FL": [(pl.concat([money_market, bond.filter(pl.col("first_rating_pos") <= 1)]), 40, 100.0)],
        "DT-SD": [(good_bonds, 45, 100.0)],
        "DT-CB": [(good_bonds, 45, 100.0)],
        # Corporate bond fund. Regulation requires at least 80% in the
        # highest-rated paper, which is what good_bonds represents.
        "DT-DB": [(good_bonds, 40, 100.0)],
        "DT-BP": [(pl.concat([bond.filter(pl.col("issuer_type").is_in(["Bank", "PSU"])),
                              money_market.filter(pl.col("security_type") == "Certificate of Deposit")]), 40, 100.0)],
        # Banking & PSU fund. Filtered by WHO ISSUED the paper rather than by
        # rating — the only pool in this table defined that way, because
        # that is how the category itself is defined.
        "DT-MD": [(pl.concat([bond.filter(pl.col("first_rating_pos") <= 4), gsec]), 45, 100.0)],
        # Medium duration. A wider rating band, down to position 4.
        "DT-CR": [(bond.filter(pl.col("first_rating_pos").is_between(3, 8)), 40, 100.0)],
        # CREDIT RISK FUND — and this is the interesting one.
        #
        # Note the band is BETWEEN 3 and 8: it EXCLUDES the top-rated bonds
        # entirely. That is not an oversight. A credit risk fund is required
        # to hold mostly below-top-rated paper, because taking credit risk
        # in exchange for higher yield is the product. It is also the scheme
        # that holds pattern 1's bond, which is why its investors have
        # something to panic about.
        "DT-GL": [(gsec, 30, 100.0)],
        "DT-LG": [(gsec, 30, 100.0)],
        "DT-ML": [(gsec, 30, 100.0)],
        # Gilt funds. Government securities only, no corporate credit at all.
        "HY-CH": [(eq, 20, 25.0), (good_bonds, 30, 75.0)],
        # THE FIRST HYBRID, and the first entry with TWO pools.
        #
        # Conservative hybrid: 25% equity, 75% debt. Read the tuples as
        # (pool, how many holdings, percent of the fund). The two
        # percentages must add to 100.
        "HY-AH": [(eq, 45, 70.0), (good_bonds, 20, 30.0)],
        # Aggressive hybrid: the mirror image, 70% equity.
        "HY-DA": [(eq, 45, 60.0), (good_bonds, 20, 40.0)],
        "HY-MA": [(eq, 40, 60.0), (good_bonds, 20, 40.0)],
        "HY-AR": [(eq, 50, 70.0), (money_market, 20, 30.0)],
        # Arbitrage fund. Note the debt side is MONEY MARKET, not bonds — an
        # arbitrage fund parks its cash leg in very short paper.
        "HY-ES": [(eq, 35, 40.0), (good_bonds, 25, 60.0)],
        "SO-RT": [(eq, 40, 65.0), (good_bonds, 20, 35.0)],
        # Retirement fund.
        "SO-CH": [(eq, 40, 65.0), (good_bonds, 20, 35.0)],
        # Children's fund.
    }.get(category_code, [(eq, 55, 100.0)])
    # THE FALLBACK: any category code not listed becomes a plain equity fund
    # with 55 holdings.
    #
    # This means adding a scheme to fund_house.py with a new category code
    # does NOT crash the generator — it quietly becomes an equity fund. Good
    # for robustness, and a trap worth knowing about: if a new debt scheme
    # mysteriously holds shares, a missing entry in this dictionary is why.


# ─────────────────────────────────────────────────────────────────────────────
# MAKING WEIGHTS ADD UP
# ─────────────────────────────────────────────────────────────────────────────

def _cap_and_scale(weights: np.ndarray, total: float) -> np.ndarray:
    """
    Scale a set of weights so they add up to `total`, with none above
    MAX_RANDOM_WEIGHT.

    WHY THIS NEEDS A LOOP — and it is not obvious.

    Say three weights come out as 50, 30 and 20 and must total 100 with a cap
    of 8. Clip them all to 8 and the total is 24, not 100. So the clipped
    excess has to be REDISTRIBUTED to the others. But redistributing makes
    the others BIGGER — and some of those may now breach the cap themselves.

    So: clip, redistribute, check again. Repeat until nothing is over. Each
    pass moves less excess than the last, so it converges quickly — usually
    in two or three passes.

    The 20-iteration ceiling is a safety stop, not an expectation. It can
    only be hit in a pathological case: if a pool had fewer securities than
    total/MAX_RANDOM_WEIGHT, the target would be mathematically unreachable
    and the loop would spin. With a pool of 30+ securities and a total of
    100 at an 8% cap, 13 securities would suffice, so there is plenty of
    headroom. If it ever did exhaust the 20 passes, the weights would come
    back summing slightly under the target — and the rounding fix in the
    caller would silently absorb it, which is exactly the kind of quiet
    wrongness worth knowing about.
    """
    w = weights / weights.sum() * total
    # Normalise to the target total first. After this the weights sum to
    # `total` but some may be over the cap.

    for _ in range(20):
        over = w > MAX_RANDOM_WEIGHT
        if not over.any():
            break
            # Nothing over the cap: done. The common case exits here on the
            # first or second pass.

        excess = (w[over] - MAX_RANDOM_WEIGHT).sum()
        # How much weight has to be moved off the over-cap holdings.

        w[over] = MAX_RANDOM_WEIGHT
        # Clip them to the cap exactly.

        w[~over] += excess * w[~over] / w[~over].sum()
        # Spread the excess over the ones UNDER the cap, in PROPORTION to
        # their current size.
        #
        # Proportionally, not equally. Spreading it equally would flatten
        # the portfolio — every holding drifting toward the same weight,
        # which is not what a real portfolio looks like. Proportional
        # redistribution preserves the shape: the big holdings stay big.
        #
        # ~over is the logical NOT of the mask. Note w[~over].sum() cannot
        # be zero in practice, because if every holding were over the cap
        # the target total would be unreachable in the first place.

    return w


# ─────────────────────────────────────────────────────────────────────────────
# TABLE 2 OF 2 — portfolio_holdings
# ─────────────────────────────────────────────────────────────────────────────

def build_holdings(settings, aum: pl.DataFrame, facts: pl.DataFrame) -> pl.DataFrame:
    """
    Build portfolio_holdings: each scheme's securities and weights at each
    month end.

    INPUT:
      aum    the table build_aum just produced. Needed because a holding's
             rupee value is a SHARE of the scheme's AUM — so this cannot run
             before build_aum.
      facts  the security facts from market.py: type, sector, issuer, group
             and starting rating for all 1,500 securities.

    THE STRUCTURE, PER SCHEME:
      1. work out the PLANTED weights, month by month (patterns 1, 2, 6)
      2. pick the random holdings from the scheme's allowed pools
      3. for each month, drift the random weights a little, then scale them
         to fill whatever percentage the planted ones left
      4. fix the rounding so the month totals exactly 100
    """
    rng = settings.rng

    def equity_of(issuer_name):
        """
        The security_id of a named company's SHARE.

        Patterns 2 and 6 are defined in terms of COMPANIES — "this scheme
        holds too much of this bank" — but the holdings table stores
        SECURITIES. A company can have a share and several bonds, so we have
        to say which one, and for these patterns it is the equity.

        The [0] at the end takes the first match. market.py creates exactly
        one equity security per listed company, so there is exactly one.
        If that ever changed, this would silently pick an arbitrary one
        rather than erroring — a KeyError-shaped risk hiding behind an index.
        """
        return int(facts.filter((pl.col("issuer_name") == issuer_name) & (pl.col("security_type") == "Equity"))["security_id"][0])

    p1_bond = int(facts.filter(pl.col("is_pattern1_bond"))["security_id"][0])
    # PATTERN 1's bond. Found by a FLAG, not by name — market.py marks it
    # with a boolean column when it creates it. A flag is safer than a name
    # match, because renaming the issuer would break a name lookup silently.

    p2 = {equity_of(name): weight for name, _sector, weight in P.P2_COMPANIES}
    # PATTERN 2: five companies in one business group, each with its own
    # planted weight. The sector is in the tuple but not needed here, hence
    # the underscore.
    #
    # Each weight is modest — under the 8% single-issuer cap — but TOGETHER
    # they exceed a group concentration limit. That is what makes this
    # pattern interesting: no single row looks wrong. You have to join
    # holdings to securities to issuers to issuer_groups and sum by group
    # before anything shows up. Four tables to find one problem.

    p6 = equity_of(P.P6_ISSUER_NAME)
    # PATTERN 6: the bank one scheme becomes over-exposed to.

    planted_ids = {p1_bond, p6, *p2}
    # Every security with a planted weight, as a set.
    #
    # Used below to EXCLUDE these from the random draw. Without that
    # exclusion, a scheme could pick pattern 6's bank at random AND have the
    # planted weight applied, producing two rows for the same
    # (scheme, security, month) — which violates the primary key and fails
    # the load.

    price = dict(zip(
        facts["security_id"].to_list(),
        np.where(facts["security_type"].to_numpy() == "Equity",
                 rng.uniform(50, 4000, len(facts)), rng.uniform(95, 105, len(facts))),
    ))
    # A FIXED price per security, used only to turn a rupee value into a
    # quantity.
    #
    # Shares Rs 50-4,000, which is the real spread on Indian exchanges. Debt
    # paper around Rs 100, because bonds are issued at a face value of 100
    # and trade near it.
    #
    # AN HONEST SIMPLIFICATION, and worth stating on camera: this price does
    # not move over time. A real holdings table's quantity stays constant
    # while the market value moves with the price; here the market value is
    # driven by AUM and the quantity is back-computed. The two tables remain
    # consistent with each other, which is what the validation checks, but
    # the quantity column is not a realistic share count over time. We chose
    # consistency over realism in that one column, because the questions in
    # this course are about weights and values, never about quantities.

    frames = []
    for scheme_id, (name, category_code, *_rest) in enumerate(SCHEMES, start=1):
        # ONE SCHEME AT A TIME. start=1 because position in SCHEMES is the
        # scheme_id, and ids begin at 1.
        #
        # The tuple unpacking takes the first two fields and dumps the rest
        # into _rest, so adding a field to SCHEMES later does not break this
        # loop.

        months = aum.filter((pl.col("scheme_id") == scheme_id) & (pl.col("aum_amount") > 0)).sort("month_end_date")
        # The months this scheme actually had money in it.
        #
        # THE aum_amount > 0 FILTER MATTERS. A scheme launched in year three
        # has no AUM in years one and two. Generating holdings for a month
        # with zero AUM would produce rows whose market_value is zero but
        # whose weight_pct is not — a contradiction, and a failed validation
        # check.

        if len(months) == 0:
            continue
            # A scheme with no AUM in any month gets no holdings at all.
            # Should not happen with the default settings, but a very small
            # scale factor could produce a scheme nobody invested in.

        month_dates = months["month_end_date"].to_numpy().astype("datetime64[D]")
        n_months = len(month_dates)

        # ---- STEP 1: the planted weights, month by month -------------------
        fixed: dict[int, np.ndarray] = {}
        # security_id → an array of one weight PER MONTH. An array, not a
        # single number, because patterns 1 and 6 CHANGE at a date.

        if name in P.P1_HOLDERS:
            before, after, _share, _drop = P.P1_HOLDERS[name]
            fixed[p1_bond] = np.where(month_dates >= np.datetime64("2024-03-31"), after, before)
            # PATTERN 1. The scheme held the bond at `before` weight, and at
            # `after` — a smaller number — from the downgrade month onwards.
            #
            # The weight DROPS because two things happened at once: the bond
            # lost value, and the fund sold some of it. Both reduce its share
            # of the portfolio.
            #
            # `_share` is the proportion of investors who panicked, used in
            # transactions.py. `_drop` is the NAV hit, used in fund_house.py.
            # The same tuple feeds three files, each taking the field it
            # needs — which is why they all tell the same story.

        if name == P.P2_SCHEME:
            for sec_id, weight in p2.items():
                fixed[sec_id] = np.full(n_months, weight)
                # PATTERN 2. A CONSTANT weight for all five companies, every
                # month, for five years.
                #
                # Constant on purpose. This is not an event, it is a standing
                # condition — a portfolio that has quietly been too
                # concentrated in one business group the whole time. Nobody
                # is looking for a date; they are looking for a breach that
                # has always been there and was never flagged.

        if name == P.P6_SCHEME:
            fixed[p6] = np.where(month_dates >= P.P6_FROM, P.P6_WEIGHT_AFTER, P.P6_WEIGHT_BEFORE)
            # PATTERN 6. The exposure crosses the 10% single-issuer limit
            # from P6_FROM onwards. WEIGHT_BEFORE is under 10, WEIGHT_AFTER
            # is over.
            #
            # Having a before and an after is what makes the question
            # answerable as "WHEN did this scheme breach the limit?" rather
            # than just "which scheme is in breach?" — a date is a much more
            # useful finding than a flag.

        fixed_total = sum(fixed.values()) if fixed else np.zeros(n_months)
        # Total planted weight per month. Summing a dict's values where each
        # value is a numpy array adds them ELEMENTWISE, giving one total per
        # month.
        #
        # The `if fixed else` guard matters: sum() of an empty sequence is
        # the integer 0, and a scalar 0 would break the per-month indexing
        # below. np.zeros(n_months) keeps the shape right for the eight
        # schemes with no planted holdings at all.

        # ---- STEP 2: pick the random holdings ------------------------------
        chosen_ids, chosen_budget = [], []
        for pool, count, share in _pools(facts, category_code, name):
            pool = (pool.filter(~pl.col("security_id").is_in(list(planted_ids)))
                    # Exclude planted securities, as explained above.
                    .sample(fraction=1.0, shuffle=True, seed=int(rng.integers(1e9)))
                    # Shuffle the whole pool. fraction=1.0 means "all of it",
                    # so this is a shuffle rather than a sample.
                    #
                    # The seed is drawn from our own seeded generator, which
                    # is what keeps the whole run reproducible. Passing no
                    # seed, or a fixed one, would either break
                    # reproducibility or make every scheme shuffle
                    # identically.
                    .unique(subset="issuer_id", keep="first", maintain_order=True)
                    # AT MOST ONE SECURITY PER ISSUER — the subtlest line in
                    # this file.
                    #
                    # WHY: the 8% cap is per SECURITY. A company with four
                    # bonds in the pool could end up with four holdings of
                    # 8% each, which is 32% exposure to one company — a
                    # far worse breach than pattern 6, created entirely by
                    # accident, and it would drown the planted pattern.
                    #
                    # Deduplicating by ISSUER means the per-security cap
                    # genuinely acts as a per-company cap.
                    #
                    # keep="first" after a shuffle means the surviving
                    # security per issuer is a random one.
                    # maintain_order=True keeps the shuffled order, so the
                    # .head() below is still a random selection.
                    .head(count))
                    # Take the first `count` — a random `count`, because of
                    # the shuffle.
            chosen_ids.append(pool["security_id"].to_numpy())
            chosen_budget.append(share)
            # Parallel lists: one entry per pool. A hybrid scheme ends up
            # with two entries in each.

        # ---- STEP 3: weights, month by month -------------------------------
        rows_sec, rows_date, rows_w = [], [], []
        base = [rng.gamma(4.0, 1.0, len(ids)) for ids in chosen_ids]
        # THE STARTING WEIGHTS, one array per pool.
        #
        # A gamma distribution with shape 4 gives positive numbers clustered
        # around 4, with a mild right skew. Scaled to sum to 100 across ~50
        # holdings, that produces a portfolio where most positions are
        # roughly 2% and a few are 4-5%.
        #
        # WHY NOT UNIFORM: a uniform draw would give a portfolio where the
        # largest and smallest holdings are equally likely at any size,
        # which no fund manager has ever built. The mild skew of the gamma
        # is what makes the portfolio look managed.

        for m in range(n_months):
            room = 100.0 - fixed_total[m]
            # How much percentage is LEFT for the random holdings this month,
            # after the planted ones have taken their share.
            #
            # This is the mechanism that keeps a planted weight from pushing
            # a month over 100%. Pattern 6 taking 11% means the random
            # holdings get 89% to share, not 100%.

            month_weights = []
            for g, ids in enumerate(chosen_ids):
                base[g] = base[g] * np.exp(rng.normal(0, 0.05, len(ids)))
                # DRIFT. Each weight is multiplied by a number very close to
                # 1 — exp of a draw with standard deviation 0.05, so
                # typically between 0.90 and 1.11.
                #
                # Note base[g] is REASSIGNED, not recomputed. The drift
                # accumulates month on month, so a holding that grew last
                # month starts this month larger. That is what makes the
                # weight history look like a portfolio being managed over
                # time, rather than 60 unrelated random portfolios.
                #
                # Multiplying by exp() rather than adding keeps every weight
                # positive, however many months of drift accumulate.
                month_weights.append(_cap_and_scale(base[g].copy(), room * chosen_budget[g] / 100.0))
                # Scale this pool's weights to its share of the room: a 70%
                # equity pool in a month with 89% of room gets 62.3%.
                #
                # .copy() IS LOAD-BEARING. _cap_and_scale modifies its
                # argument in place (w[over] = ... writes into the array). If
                # base[g] were passed directly, it would come back capped and
                # scaled — and next month's drift would start from a capped
                # value instead of the drifting one. The accumulated history
                # would be destroyed, and nothing would error.

            ids_all = np.concatenate(chosen_ids + [np.array(list(fixed), dtype=int)])
            # All the security ids for this month: the random ones, then the
            # planted ones. list(fixed) gives the dict's KEYS, which are the
            # security ids.
            #
            # dtype=int is explicit because an empty dict would otherwise
            # produce a float array, and concatenating float ids onto integer
            # ones would turn every id into a float.

            w_all = np.concatenate(month_weights + [np.array([v[m] for v in fixed.values()])])
            # The matching weights, in the SAME order. v[m] picks this
            # month's value from each planted weight array.
            #
            # ids_all and w_all must stay aligned. Both are built by
            # concatenating in the same sequence — random pools first, then
            # planted — which is the only thing keeping them in step.

            w_all = np.round(w_all, 4)
            # Round to 4 decimal places, which is what the column stores.

            n_random = sum(len(ids) for ids in chosen_ids)
            largest = int(np.argmax(w_all[:n_random]))
            w_all[largest] = round(w_all[largest] + (100.0 - w_all.sum()), 4)
            # THE ROUNDING FIX. Rounding 50 weights to 4 decimals leaves the
            # total at something like 99.9997, not 100. The leftover is put
            # onto the LARGEST holding.
            #
            # TWO DETAILS THAT MATTER:
            #
            #   w_all[:n_random] — only the RANDOM holdings are candidates.
            #   The planted weights must stay exactly as specified, because
            #   the patterns are defined by their precise values. Nudging
            #   pattern 6's weight by 0.0003 to fix a rounding error would
            #   be harmless numerically and would mean the planted weight in
            #   the database no longer matches the constant in patterns.py.
            #
            #   The LARGEST one — because a 0.0003 adjustment on a 5%
            #   holding is invisible, while the same adjustment on a 0.001%
            #   holding would be a 30% change to that row.
            #
            # And this is why every month sums to EXACTLY 100.0000, which is
            # a Stage 2 validation check.

            rows_sec.append(ids_all)
            rows_date.append(np.full(len(ids_all), month_dates[m]))
            rows_w.append(w_all)
            # Accumulate three parallel lists of arrays, concatenated below.
            # Appending arrays and concatenating once at the end is far
            # faster than growing a DataFrame month by month.

        frame = pl.DataFrame({
            "scheme_id": scheme_id,
            # A single integer, broadcast to every row by polars.
            "security_id": np.concatenate(rows_sec),
            "as_of_date": np.concatenate(rows_date),
            "weight_pct": np.concatenate(rows_w),
        })
        frames.append(frame)

    holdings = pl.concat(frames).join(aum.select("scheme_id", "month_end_date", "aum_amount"),
                                      left_on=["scheme_id", "as_of_date"], right_on=["scheme_id", "month_end_date"])
    # Attach each scheme's AUM for that month.
    #
    # left_on / right_on because the date column has a different name on
    # each side — as_of_date here, month_end_date in the AUM table. Two
    # names for the same concept in two tables, which is realistic and is
    # also another thing the metadata layer has to reconcile.

    holdings = holdings.with_columns(
        (pl.col("weight_pct") / 100 * pl.col("aum_amount")).round(2).alias("market_value"),
        # THE LINE THAT MAKES THE TWO TABLES AGREE.
        #
        # Rupee value = this holding's share of the scheme's AUM. Since the
        # weights sum to exactly 100 for every scheme-month, the market
        # values must sum to the AUM — which is the cross-table validation
        # check. The agreement is not tested into existence; it is
        # arithmetically guaranteed by construction, up to the rounding.
        pl.col("security_id").replace_strict(price).alias("price"),
        # Look up the fixed price. replace_strict RAISES if a security_id is
        # missing from the dict, rather than leaving a null.
        #
        # That strictness is the right choice: a missing price would produce
        # a null quantity, the load would fail on a NOT NULL violation
        # hundreds of lines later, and the error message would say nothing
        # about prices. Failing here names the problem.
    ).with_columns(
        pl.max_horizontal((pl.col("market_value") / pl.col("price")).round(3), pl.lit(0.001)).alias("quantity"),
        # Quantity = value / price, with a FLOOR of 0.001.
        #
        # WHY THE FLOOR: a tiny holding in a small scheme can produce a
        # value/price under 0.0005, which rounds to 0.000. A holding of zero
        # units with a non-zero market value is a contradiction, and the
        # quantity column is CHECK'd greater than zero in the schema — so
        # the load would reject the row.
        #
        # max_horizontal compares two columns ROW BY ROW, taking the larger
        # of the two. The plain max() would collapse the column to a single
        # value.
    )

    return holdings.select("scheme_id", "security_id", "as_of_date", "quantity", "market_value", "weight_pct")
    # Six columns, in schema order, with the three-part primary key
    # (scheme_id, security_id, as_of_date) first.
    #
    # And note what is NOT returned: the `price` column we just computed. It
    # was working state, used to derive quantity, and it is not a column in
    # the table. A real holdings table does carry a price, and ours does not
    # — which is the honest consequence of the simplification noted at the
    # price dictionary above.