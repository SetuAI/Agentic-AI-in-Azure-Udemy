"""
dataplane/generator/investors.py
================================

WHAT THIS FILE IS FOR
---------------------
Builds the 4 investor tables (see dataplane/schema/03_investors.sql):

    distributors, investors, folios, sip_registrations

These are the tables that grow with the scale factor. At scale 0.2:
600 distributors, 40,000 investors, about 60,000 folios, 24,000 SIPs.

HOW THE PIECES CONNECT
----------------------
    investor  -> opens 1 to 3 folios, each in one scheme and one plan
    folio     -> Regular plan: sold by a distributor (has an ARN)
                 Direct plan:  bought directly (no ARN)
    folio     -> may have one SIP, which invests a fixed amount every month

PERSONAL DATA
-------------
PAN, email, mobile and bank account are generated already masked
(e.g. PAN "ABCPX****F"), so no realistic personal data exists anywhere,
even on a developer's laptop. About 1% of optional fields are left empty,
as in real data.

Pattern 4 (SIP cancellations in one city tier) is planted here.
"""

import numpy as np
import polars as pl

from dataplane.generator import patterns as P
from dataplane.generator.common import (
    CITIES,
    FIRST_NAMES,
    NAME_WORDS,
    SURNAMES,
    TIER_SHARE,
    Settings,
    roll_forward,
    to_dates,
)
from dataplane.generator.fund_house import SCHEMES, asset_class_of

# How popular each kind of scheme is when an investor picks one.
# Equity and hybrid funds attract the most investors; niche debt funds few.
POPULAR = {
    "Sahyadri Flexi Cap Fund": 9, "Sahyadri Large Cap Fund": 8, "Sahyadri Mid Cap Fund": 7,
    "Sahyadri Small Cap Fund": 7, "Sahyadri ELSS Tax Saver Fund": 7,
    "Sahyadri Balanced Advantage Fund": 6, "Sahyadri Liquid Fund": 5,
    "Sahyadri Deccan 50 Index Fund": 5, "Sahyadri Aggressive Hybrid Fund": 5,
    "Sahyadri Credit Risk Fund": 4, "Sahyadri Medium Duration Fund": 3,
}
DEFAULT_POPULARITY = 2

# Months between SIP payments.
FREQUENCY_MONTHS = {"Monthly": 1, "Quarterly": 3}


def _add_months(dates: np.ndarray, months: np.ndarray, day_of_month: np.ndarray) -> np.ndarray:
    """
    Date that is `months` whole months after each date, on the given day of
    the month. Example: (2024-01-20, 2 months, day 5) -> 2024-03-05.
    Works on whole arrays at once.
    """
    month = dates.astype("datetime64[M]") + months.astype("timedelta64[M]")
    return month.astype("datetime64[D]") + (day_of_month - 1).astype("timedelta64[D]")


def build(settings: Settings, plans: pl.DataFrame) -> tuple[dict[str, pl.DataFrame], pl.DataFrame, pl.DataFrame]:
    """
    Returns three things:
      1. the 4 investor tables
      2. folio facts   -> one row per folio, used by transactions.py
      3. SIP schedule  -> one row per SIP with its payment window, used by transactions.py
    """
    rng = settings.rng
    start, end = settings.start, settings.end

    # ---------------------------------------------------------------------
    # distributors
    # ---------------------------------------------------------------------
    n_dist = settings.scaled(3000)
    # Unique 6-digit ARN numbers. The pattern 5 distributor's ARN is fixed,
    # so it is removed from the pool others are drawn from.
    arn_numbers = rng.choice(np.setdiff1d(np.arange(100000, 1000000), [int(P.P5_ARN[4:])]), n_dist - 1, replace=False)
    arns = [P.P5_ARN] + [f"ARN-{n}" for n in arn_numbers]

    dist_types = rng.choice(["IFA", "Bank", "National Distributor", "Online Platform"],
                            n_dist, p=[0.85, 0.05, 0.03, 0.07])
    dist_names = []
    for i, kind in enumerate(dist_types):
        if i == 0:
            dist_names.append(P.P5_DISTRIBUTOR_NAME)
        elif kind == "Bank":
            dist_names.append(f"{rng.choice(NAME_WORDS)} Bank Ltd")
        elif kind == "Online Platform":
            dist_names.append(f"{rng.choice(NAME_WORDS)}Invest Online Pvt Ltd")
        elif kind == "National Distributor":
            dist_names.append(f"{rng.choice(NAME_WORDS)} Wealth Management Ltd")
        else:
            dist_names.append(f"{rng.choice(SURNAMES)} {rng.choice(['Financial Services', 'Wealth Advisors', 'Investments', 'Capital'])}")
    dist_types[0] = "IFA"
    dist_city = rng.integers(0, len(CITIES), n_dist)

    distributors = pl.DataFrame(
        {
            "arn_no": arns,
            "dist_nm": dist_names,
            "dist_typ": dist_types,
            "city_nm": [CITIES[c][0] for c in dist_city],
            "st_cd": [CITIES[c][2] for c in dist_city],
            "empnl_dt": np.datetime64("2008-01-01") + rng.integers(0, 4900, n_dist).astype("timedelta64[D]"),
            "actv_flg": np.where(rng.random(n_dist) < 0.95, "Y", "N"),
        }
    )
    distributors = distributors.with_columns(pl.when(pl.col("arn_no") == P.P5_ARN).then(pl.lit("Y")).otherwise(pl.col("actv_flg")).alias("actv_flg"))

    # How much business each distributor brings. A few large ones, many
    # small ones (a "Pareto" spread, as in real distribution). The pattern 5
    # distributor is made one of the larger ones, so its rejection rate is
    # based on plenty of transactions and stands out clearly.
    dist_weight = rng.pareto(1.3, n_dist) + 0.05
    dist_weight[0] = np.sort(dist_weight)[-20]
    dist_weight = dist_weight / dist_weight.sum()

    # ---------------------------------------------------------------------
    # investors
    # ---------------------------------------------------------------------
    n_inv = settings.scaled(200_000)
    investor_ids = np.arange(1, n_inv + 1)

    # When each investor joined. u ** 1.3 puts slightly more joiners in the
    # earlier years, so the AMC has a solid base that keeps growing.
    span_days = int((end - np.timedelta64(60, "D") - start).astype(int))
    onboarded = roll_forward(start + np.floor(rng.random(n_inv) ** 1.3 * span_days).astype("timedelta64[D]"))

    # City: first pick a tier by TIER_SHARE, then a city within that tier.
    tiers = rng.choice([1, 2, 3], n_inv, p=[TIER_SHARE[1], TIER_SHARE[2], TIER_SHARE[3]])
    city_idx = np.empty(n_inv, dtype=int)
    for tier in (1, 2, 3):
        in_tier = [i for i, c in enumerate(CITIES) if c[3] == tier]
        mask = tiers == tier
        city_idx[mask] = rng.choice(in_tier, mask.sum())

    first = rng.choice(FIRST_NAMES, n_inv)
    last = rng.choice(SURNAMES, n_inv)

    # A masked PAN that is still unique. Real PANs are 5 letters, 4 digits,
    # 1 letter, with the 4th letter "P" for a person. We build the letters
    # from the investor_id (so no two investors can share one) and hide the
    # digits.
    letters = np.array(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    def pan_for(i):
        a, b, c, d, e = (i // 26**4) % 26, (i // 26**3) % 26, (i // 26**2) % 26, (i // 26) % 26, i % 26
        return f"{letters[a]}{letters[b]}{letters[c]}P{letters[d]}****{letters[e]}"

    def blank_one_percent(values):
        """Replace about 1% of the values with None (a missing value)."""
        return [None if rng.random() < 0.01 else v for v in values]

    investors = pl.DataFrame(
        {
            "investor_id": investor_ids,
            "pan": [pan_for(int(i)) for i in investor_ids],
            "full_name": [f"{a} {b}" for a, b in zip(first, last)],
            "email": blank_one_percent([f"{a[0].lower()}*****{i % 100:02d}@mail.example" for a, i in zip(first, investor_ids)]),
            "mobile": blank_one_percent([f"9XXXXXX{n:03d}" for n in rng.integers(0, 1000, n_inv)]),
            "date_of_birth": pl.Series(
                to_dates(blank_one_percent(list(np.datetime64("1955-01-01") + rng.integers(0, 18000, n_inv).astype("timedelta64[D]")))),
                dtype=pl.Date,
            ),
            "city": [CITIES[c][0] for c in city_idx],
            "state": [CITIES[c][1] for c in city_idx],
            "city_tier": tiers,
            "kyc_status": rng.choice(["Verified", "Pending", "Rejected"], n_inv, p=[0.96, 0.03, 0.01]),
            "onboarded_date": onboarded,
        }
    )

    # ---------------------------------------------------------------------
    # folios: each investor opens 1 to 3
    # ---------------------------------------------------------------------
    folios_per_investor = rng.choice([1, 2, 3], n_inv, p=[0.6, 0.3, 0.1])
    owner = np.repeat(investor_ids, folios_per_investor)          # investor of each folio
    n_folio_draft = len(owner)

    # Which scheme: picked by popularity.
    popularity = np.array([POPULAR.get(s[0], DEFAULT_POPULARITY) for s in SCHEMES], dtype=float)
    scheme_pick = rng.choice(np.arange(1, len(SCHEMES) + 1), n_folio_draft, p=popularity / popularity.sum())
    launch = np.array([s[3] for s in SCHEMES], dtype="datetime64[D]")[scheme_pick - 1]

    # Opened on or after the investor joined; 2nd and 3rd folios come later.
    # Never before the scheme launched.
    later = rng.integers(0, 500, n_folio_draft).astype("timedelta64[D]")
    is_first_folio = np.r_[True, owner[1:] != owner[:-1]]
    opened = onboarded[owner - 1] + np.where(is_first_folio, np.timedelta64(0, "D"), later)
    opened = roll_forward(np.maximum(opened, launch))

    # Drop folios that would open in the last 3 weeks: too late to transact.
    keep = opened <= end - np.timedelta64(21, "D")
    owner, scheme_pick, opened = owner[keep], scheme_pick[keep], opened[keep]
    n_folio = len(owner)
    folio_ids = np.arange(1, n_folio + 1)

    # Plan: 55% Direct, 45% Regular; 85% Growth where IDCW exists.
    plan_type = np.where(rng.random(n_folio) < 0.55, "Direct", "Regular")
    option = np.where(rng.random(n_folio) < 0.85, "Growth", "IDCW")
    plan_lookup = {
        (r["scheme_id"], r["plan_type"], r["option_type"]): r["plan_id"]
        for r in plans.iter_rows(named=True)
    }
    plan_id = np.array([
        plan_lookup.get((int(s), t, o), plan_lookup[(int(s), t, "Growth")])
        for s, t, o in zip(scheme_pick, plan_type, option)
    ])

    # Regular plans are sold by a distributor; Direct plans have none.
    dist_pick = rng.choice(n_dist, n_folio, p=dist_weight)
    folio_arn = np.where(plan_type == "Regular", np.array(arns)[dist_pick], None)

    folios = pl.DataFrame(
        {
            "folio_id": folio_ids,
            "folio_number": [f"SAH{i:09d}" for i in folio_ids],
            "investor_id": owner,
            "scheme_id": scheme_pick,
            "opened_date": opened,
            "holding_mode": rng.choice(["Single", "Joint", "Anyone or Survivor"], n_folio, p=[0.80, 0.08, 0.12]),
            "bank_account_masked": blank_one_percent([f"XXXXXXXX{n:04d}" for n in rng.integers(0, 10000, n_folio)]),
        }
    )

    folio_facts = pl.DataFrame(
        {
            "folio_id": folio_ids,
            "scheme_id": scheme_pick,
            "plan_id": plan_id,
            "opened_date": opened,
            "arn_code": folio_arn,
            "city_tier": tiers[owner - 1],
        }
    )

    # ---------------------------------------------------------------------
    # sip_registrations
    # ---------------------------------------------------------------------
    # SIPs are set up mostly in equity and hybrid schemes, not in debt.
    sip_ok_scheme = np.array([asset_class_of(s[0]) != "Debt" for s in SCHEMES])
    eligible = np.where(sip_ok_scheme[scheme_pick - 1] & (opened <= end - np.timedelta64(60, "D")))[0]
    n_sip = min(settings.scaled(120_000), len(eligible))
    chosen = np.sort(rng.choice(eligible, n_sip, replace=False))    # positions in the folio arrays

    # The SIP date is one of six days of the month; the first payment is on
    # that day, 0 to 120 days after the folio opened.
    sip_day = rng.choice([1, 5, 10, 15, 20, 25], n_sip)
    earliest = opened[chosen] + rng.integers(0, 120, n_sip).astype("timedelta64[D]")
    first_pay = _add_months(earliest, np.zeros(n_sip, dtype=int), sip_day)
    first_pay = np.where(first_pay < earliest, _add_months(earliest, np.ones(n_sip, dtype=int), sip_day), first_pay)
    first_pay = np.minimum(first_pay, end - np.timedelta64(30, "D"))

    frequency = np.where(rng.random(n_sip) < 0.92, "Monthly", "Quarterly")
    amount = rng.choice([500, 1000, 1500, 2000, 2500, 3000, 5000, 7500, 10000, 15000, 25000], n_sip,
                        p=[0.10, 0.18, 0.08, 0.14, 0.10, 0.10, 0.14, 0.05, 0.07, 0.02, 0.02]).astype(float)

    # 35% of SIPs are set up for a fixed number of months; the rest run
    # until cancelled.
    has_end = rng.random(n_sip) < 0.35
    tenure = rng.choice([12, 24, 36, 60], n_sip)
    end_date = np.where(has_end, _add_months(first_pay, tenure, sip_day) - np.timedelta64(1, "D"),
                        np.datetime64("NaT"))

    # When (if ever) each SIP is cancelled.
    # Normal rate: about 1.1% of running SIPs cancel each month.
    # PATTERN 4: tier-3 investors, from P4_FROM onwards, cancel at 3.5x that rate.
    monthly_rate = 0.011
    months_to_cancel = rng.geometric(monthly_rate, n_sip)
    cancel = _add_months(first_pay, months_to_cancel, np.ones(n_sip, dtype=int)) + rng.integers(0, 27, n_sip).astype("timedelta64[D]")

    in_tier = tiers[owner[chosen] - 1] == P.P4_TIER
    elevated_from = np.maximum(first_pay, P.P4_FROM)
    months_elevated = rng.geometric(monthly_rate * P.P4_MULTIPLIER, n_sip) - 1
    cancel_elevated = _add_months(elevated_from, months_elevated, np.ones(n_sip, dtype=int)) + rng.integers(0, 27, n_sip).astype("timedelta64[D]")
    cancel_elevated = np.maximum(cancel_elevated, first_pay + np.timedelta64(1, "D"))
    cancel = np.where(in_tier, np.minimum(cancel, cancel_elevated), cancel)

    # Decide each SIP's status from those dates, as of the window end.
    ends_before_cancel = has_end & (end_date < cancel)
    is_cancelled = (cancel <= end) & ~ends_before_cancel
    is_completed = has_end & (end_date <= end) & ~is_cancelled
    status = np.where(is_cancelled, "Cancelled", np.where(is_completed, "Completed", "Active"))

    sip_ids = np.arange(1, n_sip + 1)
    sip_registrations = pl.DataFrame(
        {
            "sip_id": sip_ids,
            "folio_id": folio_ids[chosen],
            "plan_id": plan_id[chosen],
            "sip_amount": amount,
            "frequency": frequency,
            "start_date": first_pay,
            "end_date": pl.Series(end_date).cast(pl.Date),
            "sip_status": status,
            "cancelled_date": pl.Series(np.where(is_cancelled, cancel, np.datetime64("NaT"))).cast(pl.Date),
            "distributor_arn": folio_arn[chosen],
        }
    )

    # The last date a payment can happen: the day before cancellation, the
    # end date, or the window end, whichever is earliest.
    last_pay = np.full(n_sip, end)
    last_pay = np.where(is_cancelled, cancel - np.timedelta64(1, "D"), last_pay)
    last_pay = np.where(has_end, np.minimum(last_pay, end_date), last_pay)

    sip_schedule = pl.DataFrame(
        {
            "sip_id": sip_ids,
            "folio_id": folio_ids[chosen],
            "plan_id": plan_id[chosen],
            "sip_amount": amount,
            "step_months": [FREQUENCY_MONTHS[f] for f in frequency],
            "sip_day": sip_day,
            "first_pay": first_pay,
            "last_pay": last_pay,
        }
    )

    tables = {
        "distributors": distributors,
        "investors": investors,
        "folios": folios,
        "sip_registrations": sip_registrations,
    }
    return tables, folio_facts, sip_schedule