"""
dataplane/generator/transactions.py
===================================

WHAT THIS FILE IS FOR
---------------------
Builds the transactions table (see dataplane/schema/04_transactions.sql):
every purchase, SIP payment, redemption and switch, over 5 years.

HOW IT WORKS, IN ORDER
----------------------
1. MONEY IN. Four kinds of rows are created first:
       SIP        -> one row per scheduled SIP payment
       Purchase   -> a first purchase when a folio opens, plus a few later ones
       Switch In  -> money moved in from another scheme (a few)
   Each gets the plan's NAV for its date, and units = amount / NAV.

2. MONEY OUT. Then Redemption and Switch Out rows are created. An
   investor can only take out units they already hold, so for each one we
   first work out how many units the folio holds on that date, then take a
   share of those. This guarantees no folio ever goes below zero units,
   which is one of the Stage 3 validation checks.

3. REJECTIONS. About 2% of all transactions are rejected (bad mandate,
   failed payment, KYC problem). A rejected row stays in the table, as it
   would in a real system, but it moves no money and no units.

Patterns 1 (redemption spike after the downgrade) and 5 (one distributor's
high rejection rate) are planted here.
"""

import numpy as np
import polars as pl

from dataplane.generator import patterns as P
from dataplane.generator.common import Settings, random_dates, roll_forward
from dataplane.generator.fund_house import SCHEME_ID, SCHEMES, asset_class_of

MONEY_IN = ["Purchase", "SIP", "Switch In"]
MONEY_OUT = ["Redemption", "Switch Out"]

REJECT_REASONS_IN = ["Insufficient funds in bank account", "Mandate not registered",
                     "KYC not compliant", "Payment not received", "Bank account mismatch"]
REJECT_REASONS_OUT = ["Signature mismatch", "KYC not compliant", "Bank account mismatch"]


def _lumpsum_amounts(rng, scheme_ids: np.ndarray) -> np.ndarray:
    """
    Random one-off investment amounts, in rupees.

    Amounts follow a "log-normal" spread: most are modest, a few are very
    large, as in real investing. Debt funds attract bigger cheques than
    equity funds. Rounded to the nearest Rs 100, between Rs 1,000 and Rs 50 lakh.
    """
    is_debt = np.array([asset_class_of(SCHEMES[s - 1][0]) == "Debt" for s in scheme_ids])
    typical = np.where(is_debt, 100_000, 20_000)
    amounts = typical * np.exp(rng.normal(0, 1.0, len(scheme_ids)))
    return np.clip(np.round(amounts, -2), 1_000, 5_000_000)


def build(settings: Settings, folio_facts: pl.DataFrame, sip_schedule: pl.DataFrame,
          nav_history: pl.DataFrame) -> pl.DataFrame:
    """Build and return the transactions table."""
    rng = settings.rng
    end = settings.end

    folio_ids = folio_facts["folio_id"].to_numpy()
    opened = folio_facts["opened_date"].to_numpy().astype("datetime64[D]")
    scheme_of_folio = folio_facts["scheme_id"].to_numpy()
    n_folio = len(folio_ids)

    # =====================================================================
    # 1. MONEY IN
    # =====================================================================

    # ---- SIP payments ---------------------------------------------------
    # For each SIP, list its payment dates: first_pay, then every
    # step_months months, on the SIP day, until last_pay.
    first_pay = sip_schedule["first_pay"].to_numpy().astype("datetime64[D]")
    last_pay = sip_schedule["last_pay"].to_numpy().astype("datetime64[D]")
    step = sip_schedule["step_months"].to_numpy()
    sip_day = sip_schedule["sip_day"].to_numpy()

    # How many payments each SIP can make: whole steps between the months, plus one.
    month_gap = (last_pay.astype("datetime64[M]") - first_pay.astype("datetime64[M]")).astype(int)
    n_payments = np.maximum(month_gap // step + 1, 0)

    # np.repeat makes one row per payment; k counts 0, 1, 2, ... within each SIP.
    row_of = np.repeat(np.arange(len(first_pay)), n_payments)
    k = np.arange(len(row_of)) - np.repeat(np.cumsum(n_payments) - n_payments, n_payments)
    pay_month = first_pay[row_of].astype("datetime64[M]") + (k * step[row_of]).astype("timedelta64[M]")
    pay_date = roll_forward(pay_month.astype("datetime64[D]") + (sip_day[row_of] - 1).astype("timedelta64[D]"))
    # Drop payments that roll past the SIP's last date or the window end.
    ok = (pay_date <= last_pay[row_of]) & (pay_date <= end)

    sip_rows = pl.DataFrame(
        {
            "folio_id": sip_schedule["folio_id"].to_numpy()[row_of][ok],
            "txn_date": pay_date[ok],
            "txn_type": "SIP",
            "amount": sip_schedule["sip_amount"].to_numpy()[row_of][ok],
        }
    )

    # ---- First purchase when a folio opens -------------------------------
    # Every folio without a SIP starts with a lump sum. Folios with a SIP
    # start with one only sometimes (30%).
    has_sip = np.isin(folio_ids, sip_schedule["folio_id"].to_numpy())
    buys_first = np.where(has_sip, rng.random(n_folio) < 0.30, True)
    first_buy = pl.DataFrame(
        {
            "folio_id": folio_ids[buys_first],
            "txn_date": opened[buys_first],
            "txn_type": "Purchase",
            "amount": _lumpsum_amounts(rng, scheme_of_folio[buys_first]),
        }
    )

    # ---- Later purchases and switch-ins ----------------------------------
    def extra_rows(mean_per_folio: float, txn_type: str) -> pl.DataFrame:
        """A random number of extra rows per folio, on random dates after it opened."""
        counts = rng.poisson(mean_per_folio, n_folio)
        who = np.repeat(np.arange(n_folio), counts)
        return pl.DataFrame(
            {
                "folio_id": folio_ids[who],
                "txn_date": random_dates(settings, opened[who], np.full(len(who), end)),
                "txn_type": txn_type,
                "amount": _lumpsum_amounts(rng, scheme_of_folio[who]),
            }
        )

    money_in = pl.concat([sip_rows, first_buy, extra_rows(1.2, "Purchase"), extra_rows(0.1, "Switch In")])

    # Attach each folio's plan, and the plan's NAV on the transaction date.
    # Every date is a business day on or after the scheme's first NAV, so
    # the NAV always exists.
    nav = nav_history.rename({"nav_date": "txn_date"})
    money_in = (
        money_in.join(folio_facts.select("folio_id", "plan_id", "arn_code"), on="folio_id")
        .join(nav, on=["plan_id", "txn_date"], how="inner")
        # units = amount / NAV, rounded to 3 decimals as registrars do.
        .with_columns((pl.col("amount") / pl.col("nav")).round(3).alias("units"))
    )

    # ---- Rejections (money in) -------------------------------------------
    # PATTERN 5: the chosen distributor's transactions are rejected about
    # 17% of the time; everyone else's about 2%.
    def mark_rejections(frame: pl.DataFrame, reasons: list[str]) -> pl.DataFrame:
        """Add txn_status and rejection_reason columns to a batch of rows."""
        is_p5 = (frame["arn_code"] == P.P5_ARN).fill_null(False).to_numpy()
        rate = np.where(is_p5, P.P5_REJECTION_RATE, P.NORMAL_REJECTION_RATE)
        rejected = rng.random(len(frame)) < rate
        reason = rng.choice(reasons, len(frame))
        return frame.with_columns(
            pl.Series("txn_status", np.where(rejected, "Rejected", "Completed")),
            # A reason only where rejected; None (empty) everywhere else.
            pl.Series("rejection_reason", [r if x else None for r, x in zip(reason, rejected)], dtype=pl.Utf8),
        )

    money_in = mark_rejections(money_in, REJECT_REASONS_IN)

    # =====================================================================
    # 2. MONEY OUT
    # =====================================================================
    # Each folio makes 0, 1 or 2 redemptions and sometimes a switch-out,
    # on random dates at least 30 days after it opened.
    earliest_out = opened + np.timedelta64(30, "D")
    can_redeem = earliest_out < end
    n_redeem = np.where(can_redeem, rng.choice([0, 1, 2], n_folio, p=[0.45, 0.40, 0.15]), 0)
    n_switch = np.where(can_redeem, (rng.random(n_folio) < 0.08).astype(int), 0)

    who_r = np.repeat(np.arange(n_folio), n_redeem)
    who_s = np.repeat(np.arange(n_folio), n_switch)
    # Fraction of current holdings taken out: 30% of redemptions take
    # everything; the rest take 10% to 60%. Switch-outs take 20% to 80%.
    frac_r = np.where(rng.random(len(who_r)) < 0.30, 1.0, rng.uniform(0.10, 0.60, len(who_r)))
    frac_s = rng.uniform(0.20, 0.80, len(who_s))

    out_parts = [
        pl.DataFrame({"folio_id": folio_ids[who_r],
                      "txn_date": random_dates(settings, earliest_out[who_r], np.full(len(who_r), end)),
                      "txn_type": "Redemption", "frac": frac_r}),
        pl.DataFrame({"folio_id": folio_ids[who_s],
                      "txn_date": random_dates(settings, earliest_out[who_s], np.full(len(who_s), end)),
                      "txn_type": "Switch Out", "frac": frac_s}),
    ]

    # PATTERN 1: after the bond downgrade, a share of folios in the schemes
    # holding it redeem 40-100% of their units within about four weeks.
    for scheme_name, (_before, _after, share, _drop) in P.P1_HOLDERS.items():
        in_scheme = (scheme_of_folio == SCHEME_ID[scheme_name]) & (opened < P.P1_DOWNGRADE_DATE)
        idx = np.where(in_scheme & (rng.random(n_folio) < share))[0]
        out_parts.append(pl.DataFrame({
            "folio_id": folio_ids[idx],
            "txn_date": random_dates(settings, np.full(len(idx), P.P1_DOWNGRADE_DATE), np.full(len(idx), P.P1_SPIKE_END)),
            "txn_type": "Redemption",
            "frac": rng.uniform(0.40, 1.0, len(idx)),
        }))

    money_out = (
        pl.concat(out_parts)
        .join(folio_facts.select("folio_id", "plan_id", "arn_code"), on="folio_id")
        .join(nav, on=["plan_id", "txn_date"], how="inner")
        .sort("folio_id", "txn_date")
        # rank: 1 for a folio's first withdrawal, 2 for its second, and so on.
        .with_columns(pl.col("txn_date").rank("ordinal").over("folio_id").alias("rank"))
    )

    # Running total of units bought (completed only) per folio, by date.
    bought = (
        money_in.filter(pl.col("txn_status") == "Completed")
        .group_by("folio_id", "txn_date").agg(pl.col("units").sum())
        .sort("folio_id", "txn_date")
        .with_columns(pl.col("units").cum_sum().over("folio_id").alias("units_bought"))
        .select("folio_id", "txn_date", "units_bought")
        .sort("txn_date")
    )

    # Handle withdrawals one "rank" at a time: first withdrawals of every
    # folio, then second withdrawals, and so on. Each needs to know what
    # earlier withdrawals already took out.
    taken_so_far = pl.DataFrame({"folio_id": [], "units_taken": []},
                                schema={"folio_id": pl.Int64, "units_taken": pl.Float64})
    done = []
    for r in range(1, int(money_out["rank"].max() or 0) + 1):
        batch = (
            money_out.filter(pl.col("rank") == r)
            .sort("txn_date")
            # join_asof: for each withdrawal, find the latest "units_bought"
            # total on or before its date, for the same folio. Both sides are
            # sorted by txn_date just above, which join_asof requires; with
            # by="folio_id" polars cannot confirm that itself, hence
            # check_sortedness=False.
            .join_asof(bought, on="txn_date", by="folio_id", strategy="backward", check_sortedness=False)
            .join(taken_so_far, on="folio_id", how="left")
            .with_columns(
                (pl.col("units_bought").fill_null(0) - pl.col("units_taken").fill_null(0)).alias("held")
            )
            # Take the chosen share of what is held. floor (not round) so we
            # never take out a fraction of a unit more than is held.
            .with_columns(((pl.col("frac") * pl.col("held") * 1000).floor() / 1000).alias("units"))
            .filter(pl.col("units") > 0)
        )
        batch = mark_rejections(batch, REJECT_REASONS_OUT)
        done.append(batch)

        # Only completed withdrawals reduce what the folio holds.
        taken_now = batch.filter(pl.col("txn_status") == "Completed").group_by("folio_id").agg(pl.col("units").sum().alias("u"))
        taken_so_far = (
            taken_so_far.join(taken_now, on="folio_id", how="full", coalesce=True)
            .with_columns((pl.col("units_taken").fill_null(0) + pl.col("u").fill_null(0)).alias("units_taken"))
            .select("folio_id", "units_taken")
        )

    money_out = pl.concat(done).with_columns((pl.col("units") * pl.col("nav")).round(2).alias("amount"))

    # =====================================================================
    # 3. COMBINE, NUMBER AND FINISH
    # =====================================================================
    columns = ["folio_id", "plan_id", "txn_date", "txn_type", "amount", "units", "nav",
               "txn_status", "rejection_reason", "arn_code"]
    txns = pl.concat([money_in.select(columns), money_out.select(columns)]).filter(pl.col("amount") > 0)
    txns = txns.sort("txn_date", "folio_id").with_row_index("transaction_id", offset=1)

    n = len(txns)
    has_arn = txns["arn_code"].is_not_null().to_numpy()
    # How the transaction came in: distributor-led, or the investor's own.
    channel = np.where(
        has_arn,
        rng.choice(["Distributor", "Branch"], n, p=[0.8, 0.2]),
        rng.choice(["Online", "Mobile App", "Branch"], n, p=[0.5, 0.4, 0.1]),
    )
    # A system time during office hours on the transaction date.
    seconds = rng.integers(9 * 3600, 18 * 3600, n)
    times = [f"{h:02d}:{m:02d}:{s:02d}" for h, m, s in zip(seconds // 3600, (seconds % 3600) // 60, seconds % 60)]

    return txns.with_columns(
        pl.col("transaction_id").cast(pl.Int64),
        pl.Series("channel", channel),
        (pl.col("txn_date").cast(pl.Utf8) + " " + pl.Series(times) + "+05:30").alias("created_at"),
    ).select(
        "transaction_id", "txn_date", "folio_id", "plan_id", "txn_type", "amount", "units", "nav",
        "txn_status", "rejection_reason", "arn_code", "channel", "created_at",
    )