"""
dataplane/generator/summaries.py
================================

WHAT THIS FILE IS FOR
---------------------
Builds the two tables that summarise other tables:

    scheme_aum_monthly  -> total money in each scheme at each month end
    portfolio_holdings  -> what each scheme holds at each month end

WHY THESE ARE CALCULATED, NOT INVENTED
--------------------------------------
In a real fund house, AUM is not a separate number someone types in. It is
the units investors hold, multiplied by the NAV. So we calculate it the
same way from the transactions we already generated:

    AUM of a plan at month end = units held at month end x NAV at month end
    AUM of a scheme            = sum over its plans

Then each scheme's holdings are split out of that AUM, so the two routes to
AUM agree:

    sum of market_value in portfolio_holdings  =  aum_amount in scheme_aum_monthly

That agreement is one of the Stage 3 validation checks.

Patterns 1, 2 and 6 fix certain holdings to planted weights here.
"""

# numpy: random numbers and fast arrays.
import numpy as np

# polars: groups, joins and sums the tables.
import polars as pl

# The planted-pattern values (P.P1_..., P.P2_..., P.P6_...).
from dataplane.generator import patterns as P

# The scheme list (name, category code, ...).
from dataplane.generator.fund_house import SCHEMES

# Lookup: sector name -> sector_id.
from dataplane.generator.market import SECTOR_ID

# Transaction types that REMOVE units from a folio.
MONEY_OUT = ["Redemption", "Switch Out"]

# Largest weight any randomly chosen holding may have, in %. Kept well under
# the 10% single-issuer limit, so the only scheme above 10% is pattern 6.
MAX_RANDOM_WEIGHT = 8.0

# Sectors each sectoral fund invests in.
# Scheme name -> list of sector names it may invest in.
SECTORAL = {
    "Sahyadri Banking & Financial Services Fund": ["Banking", "NBFC & Housing Finance", "Insurance"],
    "Sahyadri Infrastructure Fund": ["Construction", "Capital Goods", "Cement", "Power", "Logistics"],
    "Sahyadri Technology Fund": ["Information Technology", "Telecom", "Media & Entertainment"],
    "Sahyadri Manufacturing Opportunities Fund": ["Automobiles", "Auto Components", "Capital Goods",
                                                  "Chemicals", "Metals & Mining", "Consumer Durables"],
}


def _month_end(col: str) -> pl.Expr:
    """The last day of the month of a date column, e.g. 2024-03-12 -> 2024-03-31."""
    # .dt.month_end() moves each date to the last day of its month.
    return pl.col(col).dt.month_end()


# Called by run.py in step 5 (first).
def build_aum(transactions: pl.DataFrame, nav_history: pl.DataFrame, plans: pl.DataFrame,
              folios: pl.DataFrame) -> pl.DataFrame:
    """Calculate scheme_aum_monthly from completed transactions and month-end NAVs."""

    # Units each completed transaction adds (+) or removes (-).
    # Keep only completed transactions (rejected ones move nothing), then add two columns:
    signed = transactions.filter(pl.col("txn_status") == "Completed").with_columns(
        # delta = -units for money out, +units for money in.
        pl.when(pl.col("txn_type").is_in(MONEY_OUT)).then(-pl.col("units")).otherwise(pl.col("units")).alias("delta"),
        # month_end_date = the month end the transaction falls in.
        _month_end("txn_date").alias("month_end_date"),
    )
    # Net change in units per plan per month.
    net_by_month = signed.group_by("plan_id", "month_end_date").agg(pl.col("delta").sum())

    # The NAV on the last trading day of each month, for each plan.
    # For every plan and month: sort by date, then take the LAST NAV of the month.
    month_nav = (
        nav_history.with_columns(_month_end("nav_date").alias("month_end_date"))
        .sort("nav_date")
        .group_by("plan_id", "month_end_date").agg(pl.col("nav").last())
    )

    # month_nav has a row for every plan and every month it had a NAV. Joining
    # the monthly net units onto it (0 where nothing happened) and adding them
    # up month by month gives the units held at each month end.
    # Steps (one per line below): left-join the net units -> 0 where none -> sort ->
    # running total = units held -> x NAV = plan AUM -> add each plan's scheme_id.
    plan_month = (
        month_nav.join(net_by_month, on=["plan_id", "month_end_date"], how="left")
        .with_columns(pl.col("delta").fill_null(0))
        .sort("plan_id", "month_end_date")
        .with_columns(pl.col("delta").cum_sum().over("plan_id").alias("units_held"))
        .with_columns((pl.col("units_held") * pl.col("nav")).alias("plan_aum"))
        .join(plans.select("plan_id", "scheme_id"), on="plan_id")
    )

    # Add up the plans of each scheme: the scheme's AUM per month end, to 2 decimals.
    aum = plan_month.group_by("scheme_id", "month_end_date").agg(pl.col("plan_aum").sum().round(2).alias("aum_amount"))

    # Number of folios opened in each scheme by each month end.
    # New folios per scheme per month (pl.len() counts rows).
    opened = folios.with_columns(_month_end("opened_date").alias("month_end_date")).group_by(
        "scheme_id", "month_end_date").agg(pl.len().alias("new_folios"))
    # Add the new-folio counts and keep a running total per scheme = folio_count.
    aum = (
        aum.join(opened, on=["scheme_id", "month_end_date"], how="left")
        .with_columns(pl.col("new_folios").fill_null(0))
        .sort("scheme_id", "month_end_date")
        .with_columns(pl.col("new_folios").cum_sum().over("scheme_id").cast(pl.Int64).alias("folio_count"))
    )
    # Return the 4 columns of scheme_aum_monthly, in the table's order.
    return aum.select("scheme_id", "month_end_date", "aum_amount", "folio_count")


def _pools(facts: pl.DataFrame, category_code: str, scheme_name: str) -> list[tuple[pl.DataFrame, int, float]]:
    """
    Which securities a scheme may hold, as a list of (pool, how many to pick,
    share of the fund in %). Most schemes have one pool; hybrids have an
    equity pool and a debt pool.
    """
    # All shares.
    eq = facts.filter(pl.col("security_type") == "Equity")
    # All corporate bonds.
    bond = facts.filter(pl.col("security_type") == "Corporate Bond")
    # All government securities.
    gsec = facts.filter(pl.col("security_type") == "Government Security")
    # All short-term (money-market) papers.
    money_market = facts.filter(pl.col("security_type").is_in(["Treasury Bill", "Commercial Paper", "Certificate of Deposit"]))
    # Safe bonds: rated AA or better (position 0-2) plus government securities.
    good_bonds = pl.concat([bond.filter(pl.col("first_rating_pos") <= 2), gsec])

    # Sectoral funds: 32 shares from their own sectors only.
    if scheme_name in SECTORAL:
        # The sector IDs this fund may invest in.
        ids = [SECTOR_ID[s] for s in SECTORAL[scheme_name]]
        return [(eq.filter(pl.col("sector_id").is_in(ids)), 32, 100.0)]
    # Index funds: 50 shares from the whole market.
    if "Index" in scheme_name:
        return [(eq, 50, 100.0)]

    # Everything else is decided by the category code (see CATEGORIES in fund_house.py).
    # Each entry: [(pool, how many to pick, % of the fund)]. Lower first_rating_pos = safer.
    return {
        "DT-ON": [(money_market.filter(pl.col("security_type") != "Commercial Paper"), 30, 100.0)],
        "DT-LQ": [(money_market, 45, 100.0)],
        "DT-MM": [(money_market, 45, 100.0)],
        "DT-US": [(pl.concat([money_market, bond.filter(pl.col("first_rating_pos") <= 1)]), 45, 100.0)],
        "DT-LD": [(pl.concat([money_market, bond.filter(pl.col("first_rating_pos") <= 1)]), 45, 100.0)],
        "DT-FL": [(pl.concat([money_market, bond.filter(pl.col("first_rating_pos") <= 1)]), 40, 100.0)],
        "DT-SD": [(good_bonds, 45, 100.0)],
        "DT-CB": [(good_bonds, 45, 100.0)],
        "DT-DB": [(good_bonds, 40, 100.0)],
        "DT-BP": [(pl.concat([bond.filter(pl.col("issuer_type").is_in(["Bank", "PSU"])),
                              money_market.filter(pl.col("security_type") == "Certificate of Deposit")]), 40, 100.0)],
        "DT-MD": [(pl.concat([bond.filter(pl.col("first_rating_pos") <= 4), gsec]), 45, 100.0)],
        "DT-CR": [(bond.filter(pl.col("first_rating_pos").is_between(3, 8)), 40, 100.0)],
        "DT-GL": [(gsec, 30, 100.0)],
        "DT-LG": [(gsec, 30, 100.0)],
        "DT-ML": [(gsec, 30, 100.0)],
        "HY-CH": [(eq, 20, 25.0), (good_bonds, 30, 75.0)],
        "HY-AH": [(eq, 45, 70.0), (good_bonds, 20, 30.0)],
        "HY-DA": [(eq, 45, 60.0), (good_bonds, 20, 40.0)],
        "HY-MA": [(eq, 40, 60.0), (good_bonds, 20, 40.0)],
        "HY-AR": [(eq, 50, 70.0), (money_market, 20, 30.0)],
        "HY-ES": [(eq, 35, 40.0), (good_bonds, 25, 60.0)],
        "SO-RT": [(eq, 40, 65.0), (good_bonds, 20, 35.0)],
        "SO-CH": [(eq, 40, 65.0), (good_bonds, 20, 35.0)],
    # .get(code, default): plain equity funds not listed above hold 55 shares.
    }.get(category_code, [(eq, 55, 100.0)])


def _cap_and_scale(weights: np.ndarray, total: float) -> np.ndarray:
    """
    Scale weights so they add up to `total`, with none above MAX_RANDOM_WEIGHT.
    Clipping one weight makes the others grow when rescaled, so we repeat a
    few times until nothing is above the cap.
    """
    # Scale the weights so they add up to "total".
    w = weights / weights.sum() * total
    # Repeat at most 20 times.
    for _ in range(20):
        # True for weights above the cap.
        over = w > MAX_RANDOM_WEIGHT
        # Nothing above the cap: finished.
        if not over.any():
            break
        # How much weight sits above the cap in total.
        excess = (w[over] - MAX_RANDOM_WEIGHT).sum()
        # Cut those weights down to the cap.
        w[over] = MAX_RANDOM_WEIGHT
        # Share the excess among the other weights, in proportion to their size.
        w[~over] += excess * w[~over] / w[~over].sum()
    # The capped weights, still adding up to "total".
    return w


# Called by run.py in step 5 (second), after build_aum.
def build_holdings(settings, aum: pl.DataFrame, facts: pl.DataFrame) -> pl.DataFrame:
    """Build portfolio_holdings: each scheme's securities and weights at each month end."""
    # Short name for the shared random number generator.
    rng = settings.rng

    # The securities with a planted weight, looked up by issuer name.
    # Helper: the security_id of a company's share, found by the company's name.
    def equity_of(issuer_name):
        return int(facts.filter((pl.col("issuer_name") == issuer_name) & (pl.col("security_type") == "Equity"))["security_id"][0])

    # The downgraded bond (pattern 1).
    p1_bond = int(facts.filter(pl.col("is_pattern1_bond"))["security_id"][0])
    # The five Trident shares (pattern 2): security_id -> fixed weight.
    p2 = {equity_of(name): weight for name, _sector, weight in P.P2_COMPANIES}
    # The bank share (pattern 6).
    p6 = equity_of(P.P6_ISSUER_NAME)
    # All planted security IDs in one set ("*p2" unpacks the dict's keys).
    planted_ids = {p1_bond, p6, *p2}

    # A fixed price per security (shares Rs 50-4,000, debt papers around Rs 100),
    # used only to turn a rupee value into a quantity.
    # Lookup dict: security_id -> price.
    price = dict(zip(
        facts["security_id"].to_list(),
        np.where(facts["security_type"].to_numpy() == "Equity",
                 rng.uniform(50, 4000, len(facts)), rng.uniform(95, 105, len(facts))),
    ))

    # One holdings table per scheme, joined at the end.
    frames = []
    # Build the holdings scheme by scheme.
    for scheme_id, (name, category_code, *_rest) in enumerate(SCHEMES, start=1):
        # The month ends at which this scheme had money.
        months = aum.filter((pl.col("scheme_id") == scheme_id) & (pl.col("aum_amount") > 0)).sort("month_end_date")
        # A scheme with no money yet holds nothing: skip it.
        if len(months) == 0:
            continue
        # Those month-end dates as a numpy array.
        month_dates = months["month_end_date"].to_numpy().astype("datetime64[D]")
        # How many month ends.
        n_months = len(month_dates)

        # ---- Planted holdings, as a weight per month ---------------------
        # security_id -> array of its planted weight for every month.
        fixed: dict[int, np.ndarray] = {}
        if name in P.P1_HOLDERS:                                           # pattern 1
            # The bond's weight before and after March 2024.
            before, after, _share, _drop = P.P1_HOLDERS[name]
            fixed[p1_bond] = np.where(month_dates >= np.datetime64("2024-03-31"), after, before)
        if name == P.P2_SCHEME:                                            # pattern 2
            # The five Trident shares at the same weight every month.
            for sec_id, weight in p2.items():
                fixed[sec_id] = np.full(n_months, weight)
        if name == P.P6_SCHEME:                                            # pattern 6
            fixed[p6] = np.where(month_dates >= P.P6_FROM, P.P6_WEIGHT_AFTER, P.P6_WEIGHT_BEFORE)
        # Total planted weight per month (0 for schemes with no planted holdings).
        fixed_total = sum(fixed.values()) if fixed else np.zeros(n_months)

        # ---- Randomly chosen holdings ------------------------------------
        # For each pool: the chosen security IDs, and the pool's % of the fund.
        chosen_ids, chosen_budget = [], []
        # Go through the scheme's pools (one, or two for hybrids).
        for pool, count, share in _pools(facts, category_code, name):
            # Shuffle, keep at most ONE security per issuer (so no company can
            # add up to more than the cap through several bonds), then take
            # the first `count`. Planted securities are excluded here.
            pool = (pool.filter(~pl.col("security_id").is_in(list(planted_ids)))
                    .sample(fraction=1.0, shuffle=True, seed=int(rng.integers(1e9)))
                    .unique(subset="issuer_id", keep="first", maintain_order=True)
                    .head(count))
            # Remember the chosen IDs ...
            chosen_ids.append(pool["security_id"].to_numpy())
            # ... and the pool's share of the fund.
            chosen_budget.append(share)

        # Starting weights are random. Each month every weight drifts a
        # little (the fund buys and sells), then all are rescaled to fit.
        # Collected per month: security IDs, dates and weights.
        rows_sec, rows_date, rows_w = [], [], []
        # Random positive starting weights for each pool (gamma gives positive values around 4).
        base = [rng.gamma(4.0, 1.0, len(ids)) for ids in chosen_ids]
        # m = month number 0, 1, 2, ...
        for m in range(n_months):
            room = 100.0 - fixed_total[m]                      # % left after planted holdings
            # This month's weights, one array per pool.
            month_weights = []
            # g = pool number.
            for g, ids in enumerate(chosen_ids):
                # Drift every weight by a few % up or down.
                base[g] = base[g] * np.exp(rng.normal(0, 0.05, len(ids)))
                # Scale the pool to its share of the room left, capping single weights.
                month_weights.append(_cap_and_scale(base[g].copy(), room * chosen_budget[g] / 100.0))
            # All IDs this month: random ones first, planted ones last.
            ids_all = np.concatenate(chosen_ids + [np.array(list(fixed), dtype=int)])
            # Their weights, in the same order.
            w_all = np.concatenate(month_weights + [np.array([v[m] for v in fixed.values()])])

            # Round to 4 decimals, then put any rounding leftover onto the
            # largest random holding, so the month adds up to exactly 100.
            w_all = np.round(w_all, 4)
            # How many of them are random (they come first).
            n_random = sum(len(ids) for ids in chosen_ids)
            # Position of the largest random weight (argmax = position of the maximum).
            largest = int(np.argmax(w_all[:n_random]))
            # Add the leftover (100 - total) to it.
            w_all[largest] = round(w_all[largest] + (100.0 - w_all.sum()), 4)

            # Store this month's IDs, dates and weights.
            rows_sec.append(ids_all)
            rows_date.append(np.full(len(ids_all), month_dates[m]))
            rows_w.append(w_all)

        # This scheme's holdings for every month, as one table.
        frame = pl.DataFrame({
            "scheme_id": scheme_id,
            "security_id": np.concatenate(rows_sec),
            "as_of_date": np.concatenate(rows_date),
            "weight_pct": np.concatenate(rows_w),
        })
        # Keep it.
        frames.append(frame)

    # Stack all schemes and add each month's AUM (matching as_of_date to month_end_date).
    holdings = pl.concat(frames).join(aum.select("scheme_id", "month_end_date", "aum_amount"),
                                      left_on=["scheme_id", "as_of_date"], right_on=["scheme_id", "month_end_date"])
    # Rupee value = weight x the scheme's AUM that month. Quantity = value / price.
    # market_value = weight % / 100 x AUM; price looked up per security; then
    # quantity = value / price, at least 0.001 (the schema requires quantity > 0).
    holdings = holdings.with_columns(
        (pl.col("weight_pct") / 100 * pl.col("aum_amount")).round(2).alias("market_value"),
        pl.col("security_id").replace_strict(price).alias("price"),
    ).with_columns(
        pl.max_horizontal((pl.col("market_value") / pl.col("price")).round(3), pl.lit(0.001)).alias("quantity"),
    )
    # Return the 6 columns of portfolio_holdings, in the table's order.
    return holdings.select("scheme_id", "security_id", "as_of_date", "quantity", "market_value", "weight_pct")