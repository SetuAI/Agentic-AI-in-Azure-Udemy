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

import numpy as np
import polars as pl

from dataplane.generator import patterns as P
from dataplane.generator.fund_house import SCHEMES
from dataplane.generator.market import SECTOR_ID

MONEY_OUT = ["Redemption", "Switch Out"]

# Largest weight any randomly chosen holding may have, in %. Kept well under
# the 10% single-issuer limit, so the only scheme above 10% is pattern 6.
MAX_RANDOM_WEIGHT = 8.0

# Sectors each sectoral fund invests in.
SECTORAL = {
    "Sahyadri Banking & Financial Services Fund": ["Banking", "NBFC & Housing Finance", "Insurance"],
    "Sahyadri Infrastructure Fund": ["Construction", "Capital Goods", "Cement", "Power", "Logistics"],
    "Sahyadri Technology Fund": ["Information Technology", "Telecom", "Media & Entertainment"],
    "Sahyadri Manufacturing Opportunities Fund": ["Automobiles", "Auto Components", "Capital Goods",
                                                  "Chemicals", "Metals & Mining", "Consumer Durables"],
}


def _month_end(col: str) -> pl.Expr:
    """The last day of the month of a date column, e.g. 2024-03-12 -> 2024-03-31."""
    return pl.col(col).dt.month_end()


def build_aum(transactions: pl.DataFrame, nav_history: pl.DataFrame, plans: pl.DataFrame,
              folios: pl.DataFrame) -> pl.DataFrame:
    """Calculate scheme_aum_monthly from completed transactions and month-end NAVs."""

    # Units each completed transaction adds (+) or removes (-).
    signed = transactions.filter(pl.col("txn_status") == "Completed").with_columns(
        pl.when(pl.col("txn_type").is_in(MONEY_OUT)).then(-pl.col("units")).otherwise(pl.col("units")).alias("delta"),
        _month_end("txn_date").alias("month_end_date"),
    )
    net_by_month = signed.group_by("plan_id", "month_end_date").agg(pl.col("delta").sum())

    # The NAV on the last trading day of each month, for each plan.
    month_nav = (
        nav_history.with_columns(_month_end("nav_date").alias("month_end_date"))
        .sort("nav_date")
        .group_by("plan_id", "month_end_date").agg(pl.col("nav").last())
    )

    # month_nav has a row for every plan and every month it had a NAV. Joining
    # the monthly net units onto it (0 where nothing happened) and adding them
    # up month by month gives the units held at each month end.
    plan_month = (
        month_nav.join(net_by_month, on=["plan_id", "month_end_date"], how="left")
        .with_columns(pl.col("delta").fill_null(0))
        .sort("plan_id", "month_end_date")
        .with_columns(pl.col("delta").cum_sum().over("plan_id").alias("units_held"))
        .with_columns((pl.col("units_held") * pl.col("nav")).alias("plan_aum"))
        .join(plans.select("plan_id", "scheme_id"), on="plan_id")
    )

    aum = plan_month.group_by("scheme_id", "month_end_date").agg(pl.col("plan_aum").sum().round(2).alias("aum_amount"))

    # Number of folios opened in each scheme by each month end.
    opened = folios.with_columns(_month_end("opened_date").alias("month_end_date")).group_by(
        "scheme_id", "month_end_date").agg(pl.len().alias("new_folios"))
    aum = (
        aum.join(opened, on=["scheme_id", "month_end_date"], how="left")
        .with_columns(pl.col("new_folios").fill_null(0))
        .sort("scheme_id", "month_end_date")
        .with_columns(pl.col("new_folios").cum_sum().over("scheme_id").cast(pl.Int64).alias("folio_count"))
    )
    return aum.select("scheme_id", "month_end_date", "aum_amount", "folio_count")


def _pools(facts: pl.DataFrame, category_code: str, scheme_name: str) -> list[tuple[pl.DataFrame, int, float]]:
    """
    Which securities a scheme may hold, as a list of (pool, how many to pick,
    share of the fund in %). Most schemes have one pool; hybrids have an
    equity pool and a debt pool.
    """
    eq = facts.filter(pl.col("security_type") == "Equity")
    bond = facts.filter(pl.col("security_type") == "Corporate Bond")
    gsec = facts.filter(pl.col("security_type") == "Government Security")
    money_market = facts.filter(pl.col("security_type").is_in(["Treasury Bill", "Commercial Paper", "Certificate of Deposit"]))
    good_bonds = pl.concat([bond.filter(pl.col("first_rating_pos") <= 2), gsec])

    if scheme_name in SECTORAL:
        ids = [SECTOR_ID[s] for s in SECTORAL[scheme_name]]
        return [(eq.filter(pl.col("sector_id").is_in(ids)), 32, 100.0)]
    if "Index" in scheme_name:
        return [(eq, 50, 100.0)]

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
    }.get(category_code, [(eq, 55, 100.0)])


def _cap_and_scale(weights: np.ndarray, total: float) -> np.ndarray:
    """
    Scale weights so they add up to `total`, with none above MAX_RANDOM_WEIGHT.
    Clipping one weight makes the others grow when rescaled, so we repeat a
    few times until nothing is above the cap.
    """
    w = weights / weights.sum() * total
    for _ in range(20):
        over = w > MAX_RANDOM_WEIGHT
        if not over.any():
            break
        excess = (w[over] - MAX_RANDOM_WEIGHT).sum()
        w[over] = MAX_RANDOM_WEIGHT
        w[~over] += excess * w[~over] / w[~over].sum()
    return w


def build_holdings(settings, aum: pl.DataFrame, facts: pl.DataFrame) -> pl.DataFrame:
    """Build portfolio_holdings: each scheme's securities and weights at each month end."""
    rng = settings.rng

    # The securities with a planted weight, looked up by issuer name.
    def equity_of(issuer_name):
        return int(facts.filter((pl.col("issuer_name") == issuer_name) & (pl.col("security_type") == "Equity"))["security_id"][0])

    p1_bond = int(facts.filter(pl.col("is_pattern1_bond"))["security_id"][0])
    p2 = {equity_of(name): weight for name, _sector, weight in P.P2_COMPANIES}
    p6 = equity_of(P.P6_ISSUER_NAME)
    planted_ids = {p1_bond, p6, *p2}

    # A fixed price per security (shares Rs 50-4,000, debt papers around Rs 100),
    # used only to turn a rupee value into a quantity.
    price = dict(zip(
        facts["security_id"].to_list(),
        np.where(facts["security_type"].to_numpy() == "Equity",
                 rng.uniform(50, 4000, len(facts)), rng.uniform(95, 105, len(facts))),
    ))

    frames = []
    for scheme_id, (name, category_code, *_rest) in enumerate(SCHEMES, start=1):
        months = aum.filter((pl.col("scheme_id") == scheme_id) & (pl.col("aum_amount") > 0)).sort("month_end_date")
        if len(months) == 0:
            continue
        month_dates = months["month_end_date"].to_numpy().astype("datetime64[D]")
        n_months = len(month_dates)

        # ---- Planted holdings, as a weight per month ---------------------
        fixed: dict[int, np.ndarray] = {}
        if name in P.P1_HOLDERS:                                           # pattern 1
            before, after, _share, _drop = P.P1_HOLDERS[name]
            fixed[p1_bond] = np.where(month_dates >= np.datetime64("2024-03-31"), after, before)
        if name == P.P2_SCHEME:                                            # pattern 2
            for sec_id, weight in p2.items():
                fixed[sec_id] = np.full(n_months, weight)
        if name == P.P6_SCHEME:                                            # pattern 6
            fixed[p6] = np.where(month_dates >= P.P6_FROM, P.P6_WEIGHT_AFTER, P.P6_WEIGHT_BEFORE)
        fixed_total = sum(fixed.values()) if fixed else np.zeros(n_months)

        # ---- Randomly chosen holdings ------------------------------------
        chosen_ids, chosen_budget = [], []
        for pool, count, share in _pools(facts, category_code, name):
            # Shuffle, keep at most ONE security per issuer (so no company can
            # add up to more than the cap through several bonds), then take
            # the first `count`. Planted securities are excluded here.
            pool = (pool.filter(~pl.col("security_id").is_in(list(planted_ids)))
                    .sample(fraction=1.0, shuffle=True, seed=int(rng.integers(1e9)))
                    .unique(subset="issuer_id", keep="first", maintain_order=True)
                    .head(count))
            chosen_ids.append(pool["security_id"].to_numpy())
            chosen_budget.append(share)

        # Starting weights are random. Each month every weight drifts a
        # little (the fund buys and sells), then all are rescaled to fit.
        rows_sec, rows_date, rows_w = [], [], []
        base = [rng.gamma(4.0, 1.0, len(ids)) for ids in chosen_ids]
        for m in range(n_months):
            room = 100.0 - fixed_total[m]                      # % left after planted holdings
            month_weights = []
            for g, ids in enumerate(chosen_ids):
                base[g] = base[g] * np.exp(rng.normal(0, 0.05, len(ids)))
                month_weights.append(_cap_and_scale(base[g].copy(), room * chosen_budget[g] / 100.0))
            ids_all = np.concatenate(chosen_ids + [np.array(list(fixed), dtype=int)])
            w_all = np.concatenate(month_weights + [np.array([v[m] for v in fixed.values()])])

            # Round to 4 decimals, then put any rounding leftover onto the
            # largest random holding, so the month adds up to exactly 100.
            w_all = np.round(w_all, 4)
            n_random = sum(len(ids) for ids in chosen_ids)
            largest = int(np.argmax(w_all[:n_random]))
            w_all[largest] = round(w_all[largest] + (100.0 - w_all.sum()), 4)

            rows_sec.append(ids_all)
            rows_date.append(np.full(len(ids_all), month_dates[m]))
            rows_w.append(w_all)

        frame = pl.DataFrame({
            "scheme_id": scheme_id,
            "security_id": np.concatenate(rows_sec),
            "as_of_date": np.concatenate(rows_date),
            "weight_pct": np.concatenate(rows_w),
        })
        frames.append(frame)

    holdings = pl.concat(frames).join(aum.select("scheme_id", "month_end_date", "aum_amount"),
                                      left_on=["scheme_id", "as_of_date"], right_on=["scheme_id", "month_end_date"])
    # Rupee value = weight x the scheme's AUM that month. Quantity = value / price.
    holdings = holdings.with_columns(
        (pl.col("weight_pct") / 100 * pl.col("aum_amount")).round(2).alias("market_value"),
        pl.col("security_id").replace_strict(price).alias("price"),
    ).with_columns(
        pl.max_horizontal((pl.col("market_value") / pl.col("price")).round(3), pl.lit(0.001)).alias("quantity"),
    )
    return holdings.select("scheme_id", "security_id", "as_of_date", "quantity", "market_value", "weight_pct")