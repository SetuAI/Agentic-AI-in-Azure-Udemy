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

# numpy: random choices and fast arrays of numbers and dates.
import numpy as np

# polars: builds the tables (DataFrames).
import polars as pl

# The planted-pattern values (P.P4_..., P.P5_...).
from dataplane.generator import patterns as P

# Shared name lists and date helpers from common.py.
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

# The scheme list and the "which asset class is this scheme" helper.
from dataplane.generator.fund_house import SCHEMES, asset_class_of

# How popular each kind of scheme is when an investor picks one.
# Equity and hybrid funds attract the most investors; niche debt funds few.
# Scheme name -> relative popularity (higher = picked more often).
POPULAR = {
    "Sahyadri Flexi Cap Fund": 9, "Sahyadri Large Cap Fund": 8, "Sahyadri Mid Cap Fund": 7,
    "Sahyadri Small Cap Fund": 7, "Sahyadri ELSS Tax Saver Fund": 7,
    "Sahyadri Balanced Advantage Fund": 6, "Sahyadri Liquid Fund": 5,
    "Sahyadri Deccan 50 Index Fund": 5, "Sahyadri Aggressive Hybrid Fund": 5,
    "Sahyadri Credit Risk Fund": 4, "Sahyadri Medium Duration Fund": 3,
}
# Popularity of every scheme not listed above.
DEFAULT_POPULARITY = 2

# Months between SIP payments.
FREQUENCY_MONTHS = {"Monthly": 1, "Quarterly": 3}


def _add_months(dates: np.ndarray, months: np.ndarray, day_of_month: np.ndarray) -> np.ndarray:
    """
    Date that is `months` whole months after each date, on the given day of
    the month. Example: (2024-01-20, 2 months, day 5) -> 2024-03-05.
    Works on whole arrays at once.
    """
    # Round every date down to its month, then add the number of months.
    month = dates.astype("datetime64[M]") + months.astype("timedelta64[M]")
    # Back to a day date (the 1st of that month), then move to the wanted day.
    return month.astype("datetime64[D]") + (day_of_month - 1).astype("timedelta64[D]")


# The main function of this file; run.py calls it in step 3 with the scheme_plans table.
def build(settings: Settings, plans: pl.DataFrame) -> tuple[dict[str, pl.DataFrame], pl.DataFrame, pl.DataFrame]:
    """
    Returns three things:
      1. the 4 investor tables
      2. folio facts   -> one row per folio, used by transactions.py
      3. SIP schedule  -> one row per SIP with its payment window, used by transactions.py
    """
    # Short name for the shared random number generator.
    rng = settings.rng
    # The first and last day of the data window.
    start, end = settings.start, settings.end

    # ---------------------------------------------------------------------
    # distributors
    # ---------------------------------------------------------------------
    # How many distributors: 3,000 at scale 1, 600 at scale 0.2.
    n_dist = settings.scaled(3000)
    # Unique 6-digit ARN numbers. The pattern 5 distributor's ARN is fixed,
    # so it is removed from the pool others are drawn from.
    # P5_ARN[4:] cuts off "ARN-" to get the number. Pick n_dist - 1 different numbers.
    arn_numbers = rng.choice(np.setdiff1d(np.arange(100000, 1000000), [int(P.P5_ARN[4:])]), n_dist - 1, replace=False)
    # The pattern-5 distributor comes first (position 0), then everyone else.
    arns = [P.P5_ARN] + [f"ARN-{n}" for n in arn_numbers]

    # A random type per distributor; p=[...] gives each type's probability (85% IFA ...).
    dist_types = rng.choice(["IFA", "Bank", "National Distributor", "Online Platform"],
                            n_dist, p=[0.85, 0.05, 0.03, 0.07])
    # Build a name for every distributor based on its type.
    dist_names = []
    # i = position, kind = the distributor's type.
    for i, kind in enumerate(dist_types):
        # Position 0 is the pattern-5 distributor: use its fixed name.
        if i == 0:
            dist_names.append(P.P5_DISTRIBUTOR_NAME)
        elif kind == "Bank":
            dist_names.append(f"{rng.choice(NAME_WORDS)} Bank Ltd")
        elif kind == "Online Platform":
            dist_names.append(f"{rng.choice(NAME_WORDS)}Invest Online Pvt Ltd")
        elif kind == "National Distributor":
            dist_names.append(f"{rng.choice(NAME_WORDS)} Wealth Management Ltd")
        # IFA (an individual advisor): e.g. "Patil Wealth Advisors".
        else:
            dist_names.append(f"{rng.choice(SURNAMES)} {rng.choice(['Financial Services', 'Wealth Advisors', 'Investments', 'Capital'])}")
    # Make sure the pattern-5 distributor is an IFA, whatever was drawn.
    dist_types[0] = "IFA"
    # A random city (position in CITIES) for each distributor.
    dist_city = rng.integers(0, len(CITIES), n_dist)

    # The distributors table, with its old-style column names.
    distributors = pl.DataFrame(
        {
            "arn_no": arns,
            "dist_nm": dist_names,
            "dist_typ": dist_types,
            # Item 0 of the city tuple is the city name; item 2 is the state code.
            "city_nm": [CITIES[c][0] for c in dist_city],
            "st_cd": [CITIES[c][2] for c in dist_city],
            # Empanelled on a random day from 2008 onwards (0-4899 days after 1 Jan 2008).
            "empnl_dt": np.datetime64("2008-01-01") + rng.integers(0, 4900, n_dist).astype("timedelta64[D]"),
            # 95% are active ('Y').
            "actv_flg": np.where(rng.random(n_dist) < 0.95, "Y", "N"),
        }
    )
    # Force the pattern-5 distributor to be active: when its ARN -> 'Y', otherwise keep the drawn value.
    distributors = distributors.with_columns(pl.when(pl.col("arn_no") == P.P5_ARN).then(pl.lit("Y")).otherwise(pl.col("actv_flg")).alias("actv_flg"))

    # How much business each distributor brings. A few large ones, many
    # small ones (a "Pareto" spread, as in real distribution). The pattern 5
    # distributor is made one of the larger ones, so its rejection rate is
    # based on plenty of transactions and stands out clearly.
    # One random "size" per distributor; a Pareto draw gives a few big values and many small ones.
    dist_weight = rng.pareto(1.3, n_dist) + 0.05
    # Give the pattern-5 distributor the 20th largest size.
    dist_weight[0] = np.sort(dist_weight)[-20]
    # Divide by the total so the weights add up to 1 (needed for rng.choice's p=).
    dist_weight = dist_weight / dist_weight.sum()

    # ---------------------------------------------------------------------
    # investors
    # ---------------------------------------------------------------------
    # How many investors (200,000 at scale 1). "200_000" is just 200000 written readably.
    n_inv = settings.scaled(200_000)
    # investor_id values 1 .. n_inv.
    investor_ids = np.arange(1, n_inv + 1)

    # When each investor joined. u ** 1.3 puts slightly more joiners in the
    # earlier years, so the AMC has a solid base that keeps growing.
    # Number of days from the start until 60 days before the end (nobody joins in the last 60 days).
    span_days = int((end - np.timedelta64(60, "D") - start).astype(int))
    # A random joining day for each investor, moved to a business day.
    onboarded = roll_forward(start + np.floor(rng.random(n_inv) ** 1.3 * span_days).astype("timedelta64[D]"))

    # City: first pick a tier by TIER_SHARE, then a city within that tier.
    # A tier per investor: 50% tier 1, 30% tier 2, 20% tier 3.
    tiers = rng.choice([1, 2, 3], n_inv, p=[TIER_SHARE[1], TIER_SHARE[2], TIER_SHARE[3]])
    # An empty array to hold each investor's city position.
    city_idx = np.empty(n_inv, dtype=int)
    # Fill in the cities tier by tier.
    for tier in (1, 2, 3):
        # Positions of the cities in this tier.
        in_tier = [i for i, c in enumerate(CITIES) if c[3] == tier]
        # True for the investors in this tier.
        mask = tiers == tier
        # Give each of those investors a random city from the tier.
        city_idx[mask] = rng.choice(in_tier, mask.sum())

    # A random first name per investor.
    first = rng.choice(FIRST_NAMES, n_inv)
    # A random surname per investor.
    last = rng.choice(SURNAMES, n_inv)

    # A masked PAN that is still unique. Real PANs are 5 letters, 4 digits,
    # 1 letter, with the 4th letter "P" for a person. We build the letters
    # from the investor_id (so no two investors can share one) and hide the
    # digits.
    # The 26 letters A-Z as an array, so letters[0] = "A", letters[25] = "Z".
    letters = np.array(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    # Build the masked PAN for investor number i.
    def pan_for(i):
        # Write i in "base 26": five digits 0-25, each turned into a letter below.
        a, b, c, d, e = (i // 26**4) % 26, (i // 26**3) % 26, (i // 26**2) % 26, (i // 26) % 26, i % 26
        # e.g. "ABCPX****F": 3 letters, "P", 1 letter, 4 hidden digits, 1 letter.
        return f"{letters[a]}{letters[b]}{letters[c]}P{letters[d]}****{letters[e]}"

    # Helper used for optional columns (email, mobile, birth date, bank account).
    def blank_one_percent(values):
        """Replace about 1% of the values with None (a missing value)."""
        # Each value has a 1% chance of becoming None.
        return [None if rng.random() < 0.01 else v for v in values]

    # The investors table (most columns are PII, all generated already masked).
    investors = pl.DataFrame(
        {
            "investor_id": investor_ids,
            "pan": [pan_for(int(i)) for i in investor_ids],
            # First name + surname.
            "full_name": [f"{a} {b}" for a, b in zip(first, last)],
            # A masked email, e.g. "a*****17@mail.example" (mail.example is a reserved fake domain).
            "email": blank_one_percent([f"{a[0].lower()}*****{i % 100:02d}@mail.example" for a, i in zip(first, investor_ids)]),
            # A masked mobile number, e.g. "9XXXXXX042".
            "mobile": blank_one_percent([f"9XXXXXX{n:03d}" for n in rng.integers(0, 1000, n_inv)]),
            # A random birth date from 1955 onwards (0-17999 days after 1 Jan 1955), about 1% empty.
            "date_of_birth": pl.Series(
                to_dates(blank_one_percent(list(np.datetime64("1955-01-01") + rng.integers(0, 18000, n_inv).astype("timedelta64[D]")))),
                dtype=pl.Date,
            ),
            # City name (item 0) and state name (item 1) of each investor's city.
            "city": [CITIES[c][0] for c in city_idx],
            "state": [CITIES[c][1] for c in city_idx],
            "city_tier": tiers,
            # 96% verified, 3% pending, 1% rejected.
            "kyc_status": rng.choice(["Verified", "Pending", "Rejected"], n_inv, p=[0.96, 0.03, 0.01]),
            "onboarded_date": onboarded,
        }
    )

    # ---------------------------------------------------------------------
    # folios: each investor opens 1 to 3
    # ---------------------------------------------------------------------
    # 60% of investors have 1 folio, 30% have 2, 10% have 3.
    folios_per_investor = rng.choice([1, 2, 3], n_inv, p=[0.6, 0.3, 0.1])
    # np.repeat writes each investor_id once per folio they own.
    owner = np.repeat(investor_ids, folios_per_investor)          # investor of each folio
    # Number of folios before removing the late ones (below).
    n_folio_draft = len(owner)

    # Which scheme: picked by popularity.
    # Popularity of every scheme; .get(name, default) uses the default for unlisted schemes.
    popularity = np.array([POPULAR.get(s[0], DEFAULT_POPULARITY) for s in SCHEMES], dtype=float)
    # A scheme_id for every folio, chosen in proportion to popularity.
    scheme_pick = rng.choice(np.arange(1, len(SCHEMES) + 1), n_folio_draft, p=popularity / popularity.sum())
    # The launch date of each folio's scheme.
    launch = np.array([s[3] for s in SCHEMES], dtype="datetime64[D]")[scheme_pick - 1]

    # Opened on or after the investor joined; 2nd and 3rd folios come later.
    # Never before the scheme launched.
    # A random delay of 0-499 days for 2nd and 3rd folios.
    later = rng.integers(0, 500, n_folio_draft).astype("timedelta64[D]")
    # True where the owner changes from the previous row = that investor's first folio.
    is_first_folio = np.r_[True, owner[1:] != owner[:-1]]
    # First folio opens on the joining day; later folios open after the delay.
    opened = onboarded[owner - 1] + np.where(is_first_folio, np.timedelta64(0, "D"), later)
    # Never before the scheme's launch, and always on a business day.
    opened = roll_forward(np.maximum(opened, launch))

    # Drop folios that would open in the last 3 weeks: too late to transact.
    # True for folios opened at least 21 days before the end.
    keep = opened <= end - np.timedelta64(21, "D")
    # Keep only those folios, in all three arrays.
    owner, scheme_pick, opened = owner[keep], scheme_pick[keep], opened[keep]
    # The final number of folios.
    n_folio = len(owner)
    # folio_id values 1 .. n_folio.
    folio_ids = np.arange(1, n_folio + 1)

    # Plan: 55% Direct, 45% Regular; 85% Growth where IDCW exists.
    # 55% Direct, 45% Regular.
    plan_type = np.where(rng.random(n_folio) < 0.55, "Direct", "Regular")
    # 85% Growth, 15% IDCW.
    option = np.where(rng.random(n_folio) < 0.85, "Growth", "IDCW")
    # Lookup dict: (scheme_id, plan_type, option_type) -> plan_id.
    plan_lookup = {
        (r["scheme_id"], r["plan_type"], r["option_type"]): r["plan_id"]
        for r in plans.iter_rows(named=True)
    }
    # The plan of each folio. If the scheme has no IDCW plan, fall back to its Growth plan.
    plan_id = np.array([
        plan_lookup.get((int(s), t, o), plan_lookup[(int(s), t, "Growth")])
        for s, t, o in zip(scheme_pick, plan_type, option)
    ])

    # Regular plans are sold by a distributor; Direct plans have none.
    # A distributor (position) per folio, bigger distributors picked more often.
    dist_pick = rng.choice(n_dist, n_folio, p=dist_weight)
    # Regular folios get that distributor's ARN; Direct folios get None.
    folio_arn = np.where(plan_type == "Regular", np.array(arns)[dist_pick], None)

    # The folios table.
    folios = pl.DataFrame(
        {
            "folio_id": folio_ids,
            # Statement account numbers, e.g. SAH000000001.
            "folio_number": [f"SAH{i:09d}" for i in folio_ids],
            "investor_id": owner,
            "scheme_id": scheme_pick,
            "opened_date": opened,
            # 80% single owner, 8% joint, 12% "anyone or survivor".
            "holding_mode": rng.choice(["Single", "Joint", "Anyone or Survivor"], n_folio, p=[0.80, 0.08, 0.12]),
            # A masked bank account (only the last 4 digits shown), about 1% empty.
            "bank_account_masked": blank_one_percent([f"XXXXXXXX{n:04d}" for n in rng.integers(0, 10000, n_folio)]),
        }
    )

    # A helper table for transactions.py (NOT loaded into the database).
    folio_facts = pl.DataFrame(
        {
            "folio_id": folio_ids,
            "scheme_id": scheme_pick,
            "plan_id": plan_id,
            "opened_date": opened,
            # The distributor's ARN (None for Direct folios).
            "arn_code": folio_arn,
            # The owner's city tier (owner - 1 = position in the tiers array).
            "city_tier": tiers[owner - 1],
        }
    )

    # ---------------------------------------------------------------------
    # sip_registrations
    # ---------------------------------------------------------------------
    # SIPs are set up mostly in equity and hybrid schemes, not in debt.
    # True for every non-debt scheme (SIPs are allowed there).
    sip_ok_scheme = np.array([asset_class_of(s[0]) != "Debt" for s in SCHEMES])
    # Positions of folios in a non-debt scheme opened at least 60 days before the end.
    eligible = np.where(sip_ok_scheme[scheme_pick - 1] & (opened <= end - np.timedelta64(60, "D")))[0]
    # How many SIPs: 120,000 at scale 1, but never more than the eligible folios.
    n_sip = min(settings.scaled(120_000), len(eligible))
    # Pick that many different folios at random (one SIP each).
    chosen = np.sort(rng.choice(eligible, n_sip, replace=False))    # positions in the folio arrays

    # The SIP date is one of six days of the month; the first payment is on
    # that day, 0 to 120 days after the folio opened.
    # Each SIP's day of the month.
    sip_day = rng.choice([1, 5, 10, 15, 20, 25], n_sip)
    # The earliest day the first payment may happen.
    earliest = opened[chosen] + rng.integers(0, 120, n_sip).astype("timedelta64[D]")
    # Try the SIP day in the same month as "earliest".
    first_pay = _add_months(earliest, np.zeros(n_sip, dtype=int), sip_day)
    # If that day has already passed, use the SIP day of the next month.
    first_pay = np.where(first_pay < earliest, _add_months(earliest, np.ones(n_sip, dtype=int), sip_day), first_pay)
    # Never later than 30 days before the end of the window.
    first_pay = np.minimum(first_pay, end - np.timedelta64(30, "D"))

    # 92% monthly, 8% quarterly.
    frequency = np.where(rng.random(n_sip) < 0.92, "Monthly", "Quarterly")
    # The instalment amount in rupees, with realistic probabilities (p=), as decimals (float).
    amount = rng.choice([500, 1000, 1500, 2000, 2500, 3000, 5000, 7500, 10000, 15000, 25000], n_sip,
                        p=[0.10, 0.18, 0.08, 0.14, 0.10, 0.10, 0.14, 0.05, 0.07, 0.02, 0.02]).astype(float)

    # 35% of SIPs are set up for a fixed number of months; the rest run
    # until cancelled.
    # True for the 35% of SIPs with a fixed end.
    has_end = rng.random(n_sip) < 0.35
    # Their length in months.
    tenure = rng.choice([12, 24, 36, 60], n_sip)
    # End date = first payment + tenure months - 1 day; "NaT" (no date) for open-ended SIPs.
    end_date = np.where(has_end, _add_months(first_pay, tenure, sip_day) - np.timedelta64(1, "D"),
                        np.datetime64("NaT"))

    # When (if ever) each SIP is cancelled.
    # Normal rate: about 1.1% of running SIPs cancel each month.
    # PATTERN 4: tier-3 investors, from P4_FROM onwards, cancel at 3.5x that rate.
    # 1.1% chance per month that a running SIP is cancelled.
    monthly_rate = 0.011
    # geometric() draws "how many months until the first cancellation" at that rate.
    months_to_cancel = rng.geometric(monthly_rate, n_sip)
    # The cancellation date: that many months after the first payment, on a random day 1-27.
    cancel = _add_months(first_pay, months_to_cancel, np.ones(n_sip, dtype=int)) + rng.integers(0, 27, n_sip).astype("timedelta64[D]")

    # True for SIPs whose investor lives in the pattern-4 tier.
    in_tier = tiers[owner[chosen] - 1] == P.P4_TIER
    # The higher rate applies from P4_FROM (or the first payment, if later).
    elevated_from = np.maximum(first_pay, P.P4_FROM)
    # Months until cancellation at the higher (3.5x) rate; "- 1" allows the same month.
    months_elevated = rng.geometric(monthly_rate * P.P4_MULTIPLIER, n_sip) - 1
    # The cancellation date under the higher rate.
    cancel_elevated = _add_months(elevated_from, months_elevated, np.ones(n_sip, dtype=int)) + rng.integers(0, 27, n_sip).astype("timedelta64[D]")
    # A SIP can only be cancelled after its first payment.
    cancel_elevated = np.maximum(cancel_elevated, first_pay + np.timedelta64(1, "D"))
    # For pattern-4 SIPs take whichever cancellation comes first; others keep the normal date.
    cancel = np.where(in_tier, np.minimum(cancel, cancel_elevated), cancel)

    # Decide each SIP's status from those dates, as of the window end.
    # The SIP reached its end date before it would have been cancelled.
    ends_before_cancel = has_end & (end_date < cancel)
    # Cancelled = the cancel date falls inside the window and the SIP had not ended. "~" means NOT.
    is_cancelled = (cancel <= end) & ~ends_before_cancel
    # Completed = it has a fixed end that falls inside the window, and was not cancelled.
    is_completed = has_end & (end_date <= end) & ~is_cancelled
    # Everything else is still Active.
    status = np.where(is_cancelled, "Cancelled", np.where(is_completed, "Completed", "Active"))

    # sip_id values 1 .. n_sip.
    sip_ids = np.arange(1, n_sip + 1)
    # The sip_registrations table.
    sip_registrations = pl.DataFrame(
        {
            "sip_id": sip_ids,
            "folio_id": folio_ids[chosen],
            "plan_id": plan_id[chosen],
            "sip_amount": amount,
            "frequency": frequency,
            "start_date": first_pay,
            # NaT becomes an empty (NULL) date.
            "end_date": pl.Series(end_date).cast(pl.Date),
            "sip_status": status,
            # Only cancelled SIPs get a cancellation date (the schema CHECK requires this).
            "cancelled_date": pl.Series(np.where(is_cancelled, cancel, np.datetime64("NaT"))).cast(pl.Date),
            # The SIP's distributor is the folio's distributor (None for Direct).
            "distributor_arn": folio_arn[chosen],
        }
    )

    # The last date a payment can happen: the day before cancellation, the
    # end date, or the window end, whichever is earliest.
    # Start with the window end for every SIP ...
    last_pay = np.full(n_sip, end)
    # ... cancelled SIPs stop the day before cancellation ...
    last_pay = np.where(is_cancelled, cancel - np.timedelta64(1, "D"), last_pay)
    # ... and fixed-term SIPs stop at their end date.
    last_pay = np.where(has_end, np.minimum(last_pay, end_date), last_pay)

    # A helper table for transactions.py (NOT loaded): when each SIP pays.
    sip_schedule = pl.DataFrame(
        {
            "sip_id": sip_ids,
            "folio_id": folio_ids[chosen],
            "plan_id": plan_id[chosen],
            "sip_amount": amount,
            # Months between payments: 1 (monthly) or 3 (quarterly).
            "step_months": [FREQUENCY_MONTHS[f] for f in frequency],
            "sip_day": sip_day,
            "first_pay": first_pay,
            "last_pay": last_pay,
        }
    )

    # The 4 tables to load, keyed by table name.
    tables = {
        "distributors": distributors,
        "investors": investors,
        "folios": folios,
        "sip_registrations": sip_registrations,
    }
    # Return all three; run.py unpacks them.
    return tables, folio_facts, sip_schedule