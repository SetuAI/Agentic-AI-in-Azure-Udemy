##############################################################################
# dataplane/generator/transactions.py
#
# PURPOSE:
#   Builds ONE table — transactions — and it is the biggest table in the
#   database by a wide margin.
#
#     transactions   ~900,000 rows at the default scale factor of 0.2
#                   ~4,500,000 rows at scale 1.0
#
#   Every purchase, every SIP instalment, every redemption and every switch
#   across the whole 5-year window. About 80% of all the rows in this
#   project are in this one table.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Everything up to now is static. Schemes exist, people exist, folios
#   exist — but no money has moved. This file is where money moves, and
#   almost every interesting question in the course ends up reading it:
#
#     "which distributor has the worst rejection rate?"
#     "did redemptions spike after the downgrade?"
#     "how much did SIP collections grow last year?"
#     "what is this scheme's AUM?"   ← even this, indirectly
#
#   AUM is not stored as a typed-in number anywhere. summaries.py computes
#   it from the units in this table. So an error here propagates into the
#   AUM table, into portfolio holdings, and into the validation that checks
#   the two agree.
#
# WHY THIS TABLE IS PARTITIONED:
#   900,000 rows is not large by database standards, but 4.5 million starts
#   to be. The schema splits this table into 60 monthly children — one per
#   month of the window — so that a query with a date filter reads one
#   month instead of five years. See dataplane/schema/04_transactions.sql.
#
#   That is also why the agent's SQL guardrail REQUIRES a date filter on any
#   query touching this table. Without one, PostgreSQL scans all 60
#   partitions, and the query that was meant to take 200ms takes 20 seconds.
#   The guardrail is not pedantry; it is the reason the partitioning pays.
#
# HOW IT WORKS, IN ORDER — THREE PHASES:
#
#   PHASE 1. MONEY IN.
#       Three kinds of row, built independently and then stacked:
#           SIP        one row per scheduled instalment
#           Purchase   a first lump sum when the folio opens, plus later ones
#           Switch In  money arriving from another scheme
#       Each row gets the plan's NAV for its date, and
#           units = amount / NAV
#
#   PHASE 2. MONEY OUT.
#       Redemptions and switch-outs. This phase is HARDER, and the reason is
#       worth stating plainly:
#
#           An investor cannot redeem units they do not hold.
#
#       So we cannot just pick a random amount. For each withdrawal we have
#       to work out how many units that folio actually holds on that date,
#       then take a share of THAT. And a second withdrawal has to know what
#       the first one already took.
#
#       Getting this wrong produces folios with negative unit balances,
#       which is both impossible and one of the Stage 2 validation checks.
#
#   PHASE 3. REJECTIONS, IDs AND FINISHING.
#       About 2% of all transactions are rejected — a failed bank mandate,
#       an unregistered autopay, a KYC problem. The row STAYS in the table,
#       exactly as it would in a real registrar system, but it moves no
#       money and no units.
#
#       That detail matters more than it looks. It means every single query
#       about money has to say
#
#           WHERE txn_status = 'Completed'
#
#       and a query that forgets it overstates collections by about 2%. This
#       is deliberately planted so students meet the problem.
#
# WHICH PATTERNS ARE PLANTED HERE:
#   PATTERN 1 — the redemption spike. After a bond is downgraded, a share of
#               the investors in the schemes holding it redeem heavily
#               within about four weeks. The downgrade is in market.py; the
#               human reaction to it is here.
#   PATTERN 5 — one distributor's transactions are rejected about 17% of the
#               time against a 2% background. The distributor is created in
#               investors.py; its behaviour is here.
#
# WHAT THIS FILE RETURNS:
#   One polars DataFrame — the finished transactions table. Unlike the other
#   generator files it returns no helper frames, because nothing downstream
#   needs intermediate state: summaries.py reads the finished table.
##############################################################################


import numpy as np
# Used for the date arithmetic and the row-expansion tricks — np.repeat,
# np.cumsum — that turn 24,000 SIPs into 600,000 payment rows without a
# Python loop.

import polars as pl
# Used for everything involving joins and group-bys. Phase 2 in particular
# leans on polars heavily: join_asof, cum_sum over a group, and a full outer
# join are all doing real work there.

from dataplane.generator import patterns as P
# Pattern 1's downgrade date, spike window and per-scheme share; pattern 5's
# ARN and rejection rate.

from dataplane.generator.common import Settings, random_dates, roll_forward
# Settings   scale factor, date window, seeded generator
# random_dates  a random BUSINESS DAY between two dates, per row
# roll_forward  move a date to the next business day if it is a holiday

from dataplane.generator.fund_house import SCHEME_ID, SCHEMES, asset_class_of
# SCHEME_ID       name → scheme_id, for pattern 1's named schemes
# SCHEMES         the 40 schemes in order
# asset_class_of  Equity / Debt / Hybrid, used to size lump sums


MONEY_IN = ["Purchase", "SIP", "Switch In"]
MONEY_OUT = ["Redemption", "Switch Out"]
# The two directions money can move.
#
# THIS IS THE MOST IMPORTANT DEFINITION IN THE PROJECT, and it is repeated
# in summaries.py and in tests/test_stage2_data.py. "Switch Out" is money
# LEAVING a scheme and "Switch In" is money ARRIVING in another — a switch
# is two rows, in two different schemes.
#
# Anyone who treats a switch as neutral, or forgets that a switch-out
# reduces units, computes the wrong AUM. Which is why the constant is named
# and shared rather than written out at each use site.

REJECT_REASONS_IN = ["Insufficient funds in bank account", "Mandate not registered",
                     "KYC not compliant", "Payment not received", "Bank account mismatch"]
REJECT_REASONS_OUT = ["Signature mismatch", "KYC not compliant", "Bank account mismatch"]
# Why a transaction failed, and the two lists are DIFFERENT on purpose.
#
# Money coming in fails for payment reasons — the bank said no. Money going
# out cannot fail for "insufficient funds", because the fund is paying the
# investor. It fails on paperwork: a signature that does not match, a bank
# account that is not the registered one.
#
# A single shared list would put "Insufficient funds" on a redemption, and
# any student who has worked in financial services would spot it instantly.


# ─────────────────────────────────────────────────────────────────────────────
# HOW BIG IS A LUMP SUM?
# ─────────────────────────────────────────────────────────────────────────────

def _lumpsum_amounts(rng, scheme_ids: np.ndarray) -> np.ndarray:
    """
    Random one-off investment amounts, in rupees, one per scheme_id passed in.

    THE SHAPE OF THE DISTRIBUTION IS THE POINT.

    If amounts were drawn uniformly between Rs 1,000 and Rs 50 lakh, the
    average investment would be about Rs 25 lakh — which is not how retail
    investing works. Most people invest modest amounts; a few invest very
    large ones; the average is dragged upward by the few.

    That shape is called LOG-NORMAL, and it is what rng.normal inside an
    exp() produces. Here is the chain:

        rng.normal(0, 1.0)   a bell curve centred on 0, mostly between -2 and 2
        np.exp(that)         mostly between 0.14 and 7.4, centred on 1
        typical * that       mostly 0.14x to 7.4x the typical amount

    So the typical amount is the middle, and the tail stretches upward much
    further than it stretches down. That is a real investment-size curve.

    DEBT FUNDS GET BIGGER CHEQUES. Rs 1 lakh typical against Rs 20,000 for
    equity, because debt funds are where people park large sums for short
    periods — a company's spare cash, proceeds from a property sale. Equity
    is where they invest small amounts steadily. Mixing these up would make
    every query about average ticket size wrong in a way that is hard to
    see.
    """
    is_debt = np.array([asset_class_of(SCHEMES[s - 1][0]) == "Debt" for s in scheme_ids])
    # The "- 1" again: scheme_ids start at 1, SCHEMES is a 0-based list.

    typical = np.where(is_debt, 100_000, 20_000)
    # The middle of the distribution for each row.

    amounts = typical * np.exp(rng.normal(0, 1.0, len(scheme_ids)))
    # The log-normal draw described above. The 1.0 is the spread — raising
    # it would create a few absurd investments; lowering it would make
    # everyone invest nearly the same.

    return np.clip(np.round(amounts, -2), 1_000, 5_000_000)
    # Three finishing touches, and all three matter:
    #
    #   np.round(amounts, -2)  round to the nearest Rs 100. A negative
    #                          decimals argument rounds to the LEFT of the
    #                          decimal point. People invest Rs 25,000, not
    #                          Rs 24,873.41.
    #
    #   np.clip(..., 1_000, 5_000_000)  floor of Rs 1,000 (below which a
    #                          fund will not accept a purchase) and ceiling
    #                          of Rs 50 lakh. Without the ceiling, the
    #                          log-normal tail eventually produces a
    #                          Rs 400 crore retail purchase, which would
    #                          dominate the AUM of whichever scheme it landed
    #                          in and make the whole chart nonsense.


# ─────────────────────────────────────────────────────────────────────────────
# THE BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build(settings: Settings, folio_facts: pl.DataFrame, sip_schedule: pl.DataFrame,
          nav_history: pl.DataFrame) -> pl.DataFrame:
    """
    Build and return the transactions table.

    INPUT — three frames, from two earlier files:
      folio_facts    from investors.py: one row per folio, with its plan,
                     opening date and the ARN of the agent who sold it
      sip_schedule   from investors.py: one row per SIP, with the payment
                     window, the day of the month and the amount
      nav_history    from fund_house.py: a NAV for every plan for every
                     business day. This is what turns rupees into units.

    RETURNS one DataFrame, ready to load.
    """
    rng = settings.rng
    end = settings.end

    folio_ids = folio_facts["folio_id"].to_numpy()
    opened = folio_facts["opened_date"].to_numpy().astype("datetime64[D]")
    scheme_of_folio = folio_facts["scheme_id"].to_numpy()
    n_folio = len(folio_ids)
    # Pull the frequently used columns out into numpy arrays once, up front.
    #
    # WHY: the phases below index these arrays thousands of times. Going
    # back to the DataFrame each time would be far slower, and the numpy
    # indexing syntax is what makes the row-expansion tricks readable.
    #
    # .astype("datetime64[D]") pins the date unit to DAYS. polars may hand
    # back microseconds or milliseconds depending on version, and mixing
    # units in numpy date arithmetic produces answers that are quietly
    # wrong by a factor of a million.

    # =========================================================================
    # PHASE 1 OF 3 — MONEY IN
    # =========================================================================

    # ---- 1a. SIP instalments ------------------------------------------------
    # The biggest single source of rows: 24,000 SIPs, each paying for up to
    # 60 months, is around 600,000 rows on its own.
    #
    # The job: turn ONE SIP row into MANY payment rows, without a Python
    # loop. The next twenty lines are the most dense in this file.

    first_pay = sip_schedule["first_pay"].to_numpy().astype("datetime64[D]")
    last_pay = sip_schedule["last_pay"].to_numpy().astype("datetime64[D]")
    step = sip_schedule["step_months"].to_numpy()
    sip_day = sip_schedule["sip_day"].to_numpy()

    month_gap = (last_pay.astype("datetime64[M]") - first_pay.astype("datetime64[M]")).astype(int)
    # How many whole MONTHS between the first payment and the last possible
    # one. Casting both dates to a month unit before subtracting is what
    # gives a month count rather than a day count.

    n_payments = np.maximum(month_gap // step + 1, 0)
    # How many payments each SIP makes.
    #
    #   month_gap // step   whole steps that fit (12 months, step 3 → 4)
    #   + 1                 because the FIRST payment counts too
    #   np.maximum(..., 0)  never negative
    #
    # The maximum() guard matters: if last_pay somehow ended up before
    # first_pay, month_gap is negative, the division gives a negative, and
    # np.repeat below would raise a confusing error about negative repeats.
    # Clamping to 0 means that SIP simply produces no rows.

    row_of = np.repeat(np.arange(len(first_pay)), n_payments)
    # One slot per PAYMENT, labelled with which SIP it belongs to.
    #
    #     n_payments  [3, 1, 2]
    #     row_of      [0, 0, 0, 1, 2, 2]
    #
    # Same np.repeat trick as investors.py used for folios.

    k = np.arange(len(row_of)) - np.repeat(np.cumsum(n_payments) - n_payments, n_payments)
    # The instalment NUMBER within each SIP: 0, 1, 2, then back to 0 for the
    # next SIP.
    #
    # This line is worth walking through slowly on camera, because it is the
    # cleverest thing in the file and it looks like noise.
    #
    #   np.arange(len(row_of))                 a flat counter: 0 1 2 3 4 5
    #   np.cumsum(n_payments)                  running total:  3 4 6
    #   np.cumsum(n_payments) - n_payments     where each SIP STARTS: 0 3 4
    #   np.repeat(that, n_payments)            that start, per row: 0 0 0 3 4 4
    #   subtract                               0 1 2 0 0 1
    #                                          ^^^^^ ^ ^^^
    #                                          SIP0  S1 SIP2
    #
    # So we take a global counter and subtract each group's starting offset.
    # The result restarts at 0 for every SIP. No loop, no Python, 600,000
    # values in one expression.

    pay_month = first_pay[row_of].astype("datetime64[M]") + (k * step[row_of]).astype("timedelta64[M]")
    # Which MONTH each instalment falls in: the first payment's month, plus
    # k steps. For a quarterly SIP, k=2 means 6 months later.

    pay_date = roll_forward(pay_month.astype("datetime64[D]") + (sip_day[row_of] - 1).astype("timedelta64[D]"))
    # Put the SIP day back on, and roll weekends and holidays forward.
    #
    # The "- 1" is the same off-by-one as in investors._add_months: casting a
    # month to a day gives the 1st, so day 5 needs + 4 days.
    #
    # roll_forward matters here more than anywhere else in the project. A
    # SIP on the 5th will land on a Saturday several times over five years,
    # and we join on NAV date below — NAVs exist only on business days, so a
    # Saturday payment would find no NAV and be silently dropped by the
    # inner join. Students would then see SIPs with missing months and have
    # no way to know why.

    ok = (pay_date <= last_pay[row_of]) & (pay_date <= end)
    # Keep only payments inside the SIP's window AND inside the overall
    # 5-year window.
    #
    # The first condition can fail even after all the care above, because
    # roll_forward moves dates FORWARD: a payment on the last valid day
    # could roll past it.

    sip_rows = pl.DataFrame(
        {
            "folio_id": sip_schedule["folio_id"].to_numpy()[row_of][ok],
            # Two index operations in sequence: [row_of] expands one row per
            # SIP into one per payment, then [ok] drops the out-of-window
            # ones. Order matters — [ok] has the expanded length.
            "txn_date": pay_date[ok],
            "txn_type": "SIP",
            # A single string, not an array. polars broadcasts it to every
            # row, which is both shorter and cheaper than building an array
            # of 600,000 identical strings.
            "amount": sip_schedule["sip_amount"].to_numpy()[row_of][ok],
            # The same instalment amount every month. Real SIPs can step up
            # annually; we do not model that, and it is worth saying so out
            # loud rather than letting a student assume we did.
        }
    )

    # ---- 1b. The first purchase, when a folio opens ------------------------
    has_sip = np.isin(folio_ids, sip_schedule["folio_id"].to_numpy())
    # Which folios have a SIP. np.isin is a set membership test over whole
    # arrays — much faster than a Python "in" per folio.

    buys_first = np.where(has_sip, rng.random(n_folio) < 0.30, True)
    # EVERY folio without a SIP starts with a lump sum. A folio with a SIP
    # starts with one only 30% of the time.
    #
    # WHY: a folio with no SIP and no opening purchase would be an account
    # that was opened and never used — a row in folios with no money in it,
    # which then shows up as a scheme with folios but less AUM than expected.
    # Making the lump sum unconditional there closes that hole.
    #
    # The 30% for SIP folios is the investor who seeds the account with a
    # one-time amount and then starts a monthly instruction. Common, but not
    # universal.

    first_buy = pl.DataFrame(
        {
            "folio_id": folio_ids[buys_first],
            "txn_date": opened[buys_first],
            # ON the opening date, not after. In reality the purchase is
            # what causes the folio to be created.
            "txn_type": "Purchase",
            "amount": _lumpsum_amounts(rng, scheme_of_folio[buys_first]),
        }
    )

    # ---- 1c. Later purchases and switch-ins --------------------------------
    def extra_rows(mean_per_folio: float, txn_type: str) -> pl.DataFrame:
        """
        A random number of extra rows per folio, on random business days
        after it opened.

        WHY A POISSON DRAW: the question is "how many times did this folio
        top up over five years?" The answer is a count, most often 0, 1 or
        2, occasionally 5. That is exactly what a Poisson distribution
        describes — the number of independent events in a period.

        Drawing a uniform integer 0-5 instead would say a folio is as likely
        to top up five times as never, which is wrong: most folios are
        bought once and left alone.
        """
        counts = rng.poisson(mean_per_folio, n_folio)
        who = np.repeat(np.arange(n_folio), counts)
        # The np.repeat expansion once more. counts can legitimately be 0 for
        # a folio, and np.repeat handles that by emitting nothing for it.
        return pl.DataFrame(
            {
                "folio_id": folio_ids[who],
                "txn_date": random_dates(settings, opened[who], np.full(len(who), end)),
                # A random BUSINESS DAY between the folio opening and the
                # window end, per row. random_dates handles the
                # business-day part, so these dates will find a NAV.
                "txn_type": txn_type,
                "amount": _lumpsum_amounts(rng, scheme_of_folio[who]),
            }
        )

    money_in = pl.concat([sip_rows, first_buy, extra_rows(1.2, "Purchase"), extra_rows(0.1, "Switch In")])
    # Stack all four sources into one frame.
    #
    # The means say a lot about investor behaviour: 1.2 extra purchases per
    # folio over five years, but only 0.1 switch-ins — switching between
    # schemes is something 1 folio in 10 ever does. Switch-ins being rare is
    # why they are easy to forget in a query, and why forgetting them is a
    # good lesson.
    #
    # pl.concat requires all four frames to have the SAME columns in the
    # same order. They do, because each was built with the same four keys.

    nav = nav_history.rename({"nav_date": "txn_date"})
    # Rename the NAV date column so it matches, letting us join on
    # ["plan_id", "txn_date"] directly rather than with a left_on/right_on
    # pair. Purely for readability of the join below.

    money_in = (
        money_in.join(folio_facts.select("folio_id", "plan_id", "arn_code"), on="folio_id")
        # Attach each folio's PLAN and its agent's ARN. The plan is needed
        # for the NAV; the ARN is needed for pattern 5 and for hidden link 3.
        .join(nav, on=["plan_id", "txn_date"], how="inner")
        # Attach the NAV for that plan on that date.
        #
        # AN INNER JOIN, DELIBERATELY. If a transaction date has no NAV, the
        # row is DROPPED rather than kept with a null NAV. A transaction
        # with no NAV cannot have units, and a row with null units would
        # poison the AUM calculation downstream.
        #
        # In practice this should never drop anything, because every date
        # above is a business day on or after the scheme's launch. If row
        # counts come out lower than expected, this join is the first place
        # to look.
        .with_columns((pl.col("amount") / pl.col("nav")).round(3).alias("units"))
        # units = rupees / price per unit.
        #
        # Rounded to 3 decimals. The column itself is numeric(18,4), so it
        # has room for a fourth — we simply never use it. Registrars publish
        # unit balances to 3 or 4 places depending on the registrar.
        # The rounding is not cosmetic: it is why the Stage 2 unit-balance
        # check has to allow a small tolerance instead of demanding exact
        # equality, and why the redemption logic below uses floor() rather
        # than round().
    )

    # ---- 1d. Rejections, including PATTERN 5 -------------------------------
    def mark_rejections(frame: pl.DataFrame, reasons: list[str]) -> pl.DataFrame:
        """
        Add txn_status and rejection_reason to a batch of rows.

        PATTERN 5 LIVES IN THESE SIX LINES. The distributor whose ARN is
        P5_ARN gets a ~17% rejection rate; everybody else gets ~2%.

        Defined as a nested function because it is called TWICE — once for
        all of money_in, and once per rank inside the money_out loop — and
        it needs access to `rng` from the enclosing scope.
        """
        is_p5 = (frame["arn_code"] == P.P5_ARN).fill_null(False).to_numpy()
        # Which rows belong to pattern 5's distributor.
        #
        # .fill_null(False) IS LOAD-BEARING. About 45% of folios are Direct
        # and have a NULL arn_code. In polars, NULL == "ARN-123456" is NULL,
        # not False. Feeding nulls into np.where below gives undefined
        # behaviour. This turns "unknown" into "no", which is correct here:
        # a direct transaction has no distributor, so it is not pattern 5's.

        rate = np.where(is_p5, P.P5_REJECTION_RATE, P.NORMAL_REJECTION_RATE)
        # A per-row rejection probability: 0.17 for the planted distributor,
        # 0.02 for everyone else.

        rejected = rng.random(len(frame)) < rate
        # One coin flip per row, against that row's own rate. This is what
        # makes the pattern statistical rather than exact — the distributor's
        # observed rate will be near 17%, not exactly 17%, which is what
        # makes it look like real data and why the test asserts a range.

        reason = rng.choice(reasons, len(frame))
        # A reason is drawn for EVERY row, including the completed ones.
        # Slightly wasteful, but vectorised and simpler than drawing only
        # for the rejected subset and scattering the results back.

        return frame.with_columns(
            pl.Series("txn_status", np.where(rejected, "Rejected", "Completed")),
            pl.Series("rejection_reason", [r if x else None for r, x in zip(reason, rejected)], dtype=pl.Utf8),
            # A reason ONLY where rejected; NULL everywhere else.
            #
            # A completed transaction carrying a rejection reason would be a
            # self-contradicting row, and Stage 2 has a consistency test for
            # exactly this. dtype=pl.Utf8 is given explicitly because a list
            # that happens to be all None would otherwise infer as a Null
            # column, which the loader cannot write to a text column.
        )

    money_in = mark_rejections(money_in, REJECT_REASONS_IN)

    # =========================================================================
    # PHASE 2 OF 3 — MONEY OUT
    # =========================================================================
    # The hard phase. Restating the constraint, because everything here
    # exists to satisfy it:
    #
    #     a folio can never redeem more units than it holds on that date
    #
    # Which means a redemption amount cannot be chosen freely. We choose a
    # FRACTION of holdings, then compute the units from the actual balance.

    earliest_out = opened + np.timedelta64(30, "D")
    # No withdrawal within 30 days of opening. Partly realistic — people do
    # not usually buy and sell within a month — and partly practical: it
    # guarantees there is at least one purchase on the books before any
    # withdrawal, so the balance is never zero.

    can_redeem = earliest_out < end
    # A folio that opened in the last 30 days of the window cannot redeem at
    # all. investors.py already dropped folios opening in the last 21 days,
    # so this catches the narrow band between 21 and 30 days.

    n_redeem = np.where(can_redeem, rng.choice([0, 1, 2], n_folio, p=[0.45, 0.40, 0.15]), 0)
    # 45% of folios never redeem, 40% redeem once, 15% twice.
    n_switch = np.where(can_redeem, (rng.random(n_folio) < 0.08).astype(int), 0)
    # 8% switch out once. .astype(int) turns True/False into 1/0, which is
    # what np.repeat needs as a repeat count.

    who_r = np.repeat(np.arange(n_folio), n_redeem)
    who_s = np.repeat(np.arange(n_folio), n_switch)

    frac_r = np.where(rng.random(len(who_r)) < 0.30, 1.0, rng.uniform(0.10, 0.60, len(who_r)))
    # HOW MUCH of the holding is taken out.
    #
    # 30% of redemptions are a FULL exit — frac of exactly 1.0. The rest
    # take 10% to 60%.
    #
    # The full-exit spike is real behaviour: people either trim a position
    # or leave entirely, and "leave entirely" is a common choice. A plain
    # uniform draw would never produce a clean 100% exit, so the data would
    # contain no folios with a zero balance — and "how many investors fully
    # exited last quarter?" would have no answer.

    frac_s = rng.uniform(0.20, 0.80, len(who_s))
    # Switch-outs are partial by nature — you are moving part of your money
    # to a different fund, not leaving the AMC.

    out_parts = [
        pl.DataFrame({"folio_id": folio_ids[who_r],
                      "txn_date": random_dates(settings, earliest_out[who_r], np.full(len(who_r), end)),
                      "txn_type": "Redemption", "frac": frac_r}),
        pl.DataFrame({"folio_id": folio_ids[who_s],
                      "txn_date": random_dates(settings, earliest_out[who_s], np.full(len(who_s), end)),
                      "txn_type": "Switch Out", "frac": frac_s}),
    ]
    # Note the column is "frac", not "amount". At this stage we only know
    # the SHARE to take; the actual units and rupees cannot be computed
    # until we know the balance.

    # ---- PATTERN 1: the redemption spike after the downgrade ---------------
    for scheme_name, (_before, _after, share, _drop) in P.P1_HOLDERS.items():
        # P1_HOLDERS maps a scheme name to a 4-tuple. Only `share` is used
        # here — the weight before, the weight after and the NAV drop are
        # used in summaries.py and fund_house.py. The leading underscores
        # mark the three we are deliberately ignoring, so a reader does not
        # go looking for where they are used in this file.

        in_scheme = (scheme_of_folio == SCHEME_ID[scheme_name]) & (opened < P.P1_DOWNGRADE_DATE)
        # Folios in an affected scheme that already existed when the
        # downgrade happened.
        #
        # THE SECOND CONDITION IS THE ONE PEOPLE FORGET. An investor who
        # opened a folio AFTER the downgrade cannot have panicked about it.
        # Without this, the data would contain folios opened in 2025
        # redeeming in reaction to a 2024 event — and any student who
        # checked the dates would find the pattern was fabricated.

        idx = np.where(in_scheme & (rng.random(n_folio) < share))[0]
        # A random share of those folios react. `share` differs per scheme —
        # a scheme with a 6% holding in the downgraded bond sees more panic
        # than one with 1%.

        out_parts.append(pl.DataFrame({
            "folio_id": folio_ids[idx],
            "txn_date": random_dates(settings, np.full(len(idx), P.P1_DOWNGRADE_DATE), np.full(len(idx), P.P1_SPIKE_END)),
            # Spread across the roughly four weeks after the downgrade.
            #
            # NOT all on one day. A single-day spike reads as a data error.
            # A four-week ramp reads as news spreading, advisors calling
            # clients, and people acting over days — which is what actually
            # happens.
            "txn_type": "Redemption",
            "frac": rng.uniform(0.40, 1.0, len(idx)),
            # 40% to 100% of holdings. Much heavier than the normal 10-60%,
            # because this is a reaction and not a planned rebalance. That
            # difference in SIZE, on top of the difference in COUNT, is what
            # makes the spike visible in a rupee chart as well as a row count.
        }))

    money_out = (
        pl.concat(out_parts)
        .join(folio_facts.select("folio_id", "plan_id", "arn_code"), on="folio_id")
        .join(nav, on=["plan_id", "txn_date"], how="inner")
        .sort("folio_id", "txn_date")
        .with_columns(pl.col("txn_date").rank("ordinal").over("folio_id").alias("rank"))
        # RANK IS THE KEY TO THIS WHOLE PHASE.
        #
        # Within each folio, number the withdrawals in date order: 1 for the
        # earliest, 2 for the next, and so on. "ordinal" means ties get
        # distinct ranks rather than sharing one, so two withdrawals on the
        # same date still get 1 and 2 — which is what we need, because we
        # process one rank at a time and a shared rank would mean two
        # withdrawals computing their balance from the same starting point
        # and together taking more than is held.
        #
        # .over("folio_id") makes the ranking restart per folio, the same way
        # a SQL window function with PARTITION BY would.
    )

    bought = (
        money_in.filter(pl.col("txn_status") == "Completed")
        # ONLY COMPLETED PURCHASES ADD UNITS. A rejected purchase moved no
        # money, so it must not increase the balance available to redeem.
        # Omitting this filter would let folios redeem units that were never
        # actually bought.
        .group_by("folio_id", "txn_date").agg(pl.col("units").sum())
        # Several purchases can land on the same date — a SIP instalment and
        # a top-up, say. Collapse them to one row per folio per date, or the
        # cumulative sum below would be computed over duplicate dates and
        # join_asof would pick an arbitrary one of them.
        .sort("folio_id", "txn_date")
        .with_columns(pl.col("units").cum_sum().over("folio_id").alias("units_bought"))
        # The running total of units bought by each date, per folio. This is
        # the "balance before any withdrawals" curve.
        .select("folio_id", "txn_date", "units_bought")
        .sort("txn_date")
        # Sorted by DATE at the end, because join_asof requires its right
        # side sorted on the asof key.
    )

    taken_so_far = pl.DataFrame({"folio_id": [], "units_taken": []},
                                schema={"folio_id": pl.Int64, "units_taken": pl.Float64})
    # A running ledger of how many units each folio has already withdrawn.
    # Starts empty, and grows by one rank per loop iteration.
    #
    # The explicit schema= is required: polars cannot infer column types
    # from empty lists, and the join below would fail on a type mismatch
    # between Null and Int64.

    done = []
    for r in range(1, int(money_out["rank"].max() or 0) + 1):
        # ONE PASS PER RANK — all the first withdrawals, then all the
        # seconds, and so on. Typically only 2 or 3 iterations.
        #
        # WHY NOT ONE PASS OVER EVERYTHING: a folio's second withdrawal has
        # to know what its first one took. That is a dependency between rows
        # of the same group, which no single vectorised expression can
        # express. Ranking and looping over ranks is how we turn a row-by-row
        # dependency into a handful of whole-array operations.
        #
        # `or 0` guards the case where money_out is empty — .max() returns
        # None, and int(None) would raise.

        batch = (
            money_out.filter(pl.col("rank") == r)
            .sort("txn_date")
            .join_asof(bought, on="txn_date", by="folio_id", strategy="backward", check_sortedness=False)
            # JOIN_ASOF IS THE TOOL THAT MAKES THIS WORK, and it deserves a
            # minute on camera.
            #
            # A normal join needs an exact match. Here there is nothing to
            # match exactly on: a redemption on 14 March needs the purchase
            # total as of 14 March, and there was probably no purchase that
            # day.
            #
            # join_asof with strategy="backward" finds the LATEST row on or
            # before the key. So a redemption on 14 March picks up the
            # cumulative total from, say, 5 March. Which is exactly the
            # balance the investor held that day.
            #
            # by="folio_id" scopes the search to the same folio — without
            # it, a redemption would match another investor's purchases.
            #
            # check_sortedness=False: both sides ARE sorted by txn_date (see
            # the .sort above and at the end of `bought`), but with a `by`
            # key polars cannot verify that cheaply, so it would warn on
            # every iteration. We assert the sortedness ourselves and turn
            # the check off.
            .join(taken_so_far, on="folio_id", how="left")
            # What earlier ranks already withdrew. A LEFT join, because a
            # folio's first withdrawal has no earlier ones and must survive
            # the join with a null.
            .with_columns(
                (pl.col("units_bought").fill_null(0) - pl.col("units_taken").fill_null(0)).alias("held")
            )
            # THE BALANCE: everything bought up to this date, minus
            # everything already taken out.
            #
            # Both fill_null(0) calls are needed. units_bought is null if
            # join_asof found no purchase on or before this date (possible if
            # the only purchase was rejected); units_taken is null on a first
            # withdrawal. Either null would make `held` null, the filter
            # below would drop the row, and the redemption would vanish
            # without explanation.
            .with_columns(((pl.col("frac") * pl.col("held") * 1000).floor() / 1000).alias("units"))
            # Take the chosen share of the balance, rounded DOWN to 3
            # decimals.
            #
            # FLOOR, NOT ROUND. Units are produced to 3 decimals, and a
            # full exit asks for frac = 1.0 — the entire balance. If the
            # arithmetic rounded UP by even 0.0005 units, the folio would
            # end the day with a negative balance, and the Stage 2
            # unit-balance test would fail.
            #
            # The × 1000 / 1000 is how you floor to 3 decimals: scale up,
            # floor to a whole number, scale back down.
            .filter(pl.col("units") > 0)
            # Drop withdrawals of nothing. A folio whose purchases were all
            # rejected, or which has already been fully redeemed by an
            # earlier rank, has nothing left to take. Keeping a 0-unit row
            # would be a transaction that moved no money and was not
            # rejected either — meaningless, and it would fail the
            # amount > 0 check at the end anyway.
        )
        batch = mark_rejections(batch, REJECT_REASONS_OUT)
        # Rejections are applied PER BATCH, and with the outgoing reason
        # list. Pattern 5 applies here too — that distributor's redemptions
        # fail at the elevated rate as well, which is right: a sloppy
        # advisor is sloppy in both directions.
        done.append(batch)

        taken_now = batch.filter(pl.col("txn_status") == "Completed").group_by("folio_id").agg(pl.col("units").sum().alias("u"))
        # ONLY COMPLETED WITHDRAWALS REDUCE THE BALANCE.
        #
        # This is the mirror of the filter on `bought`, and it is the
        # subtler of the two. A REJECTED redemption still occupies a rank —
        # it was attempted — but it took no units. So the next rank must see
        # the balance as if the rejected one never happened. Including
        # rejected rows here would make later redemptions too small, and the
        # planted full-exit behaviour would quietly stop producing zero
        # balances.

        taken_so_far = (
            taken_so_far.join(taken_now, on="folio_id", how="full", coalesce=True)
            # A FULL OUTER join, because both sides have folios the other
            # does not: taken_so_far has folios that withdrew in earlier
            # ranks but not this one, and taken_now has folios withdrawing
            # for the first time at this rank.
            #
            # coalesce=True merges the two folio_id columns into one.
            # Without it polars keeps folio_id and folio_id_right, and the
            # next iteration's join would break.
            .with_columns((pl.col("units_taken").fill_null(0) + pl.col("u").fill_null(0)).alias("units_taken"))
            # Add this rank's withdrawals to the running total, treating a
            # missing side as 0.
            .select("folio_id", "units_taken")
            # Drop "u" so the frame has exactly the two columns the next
            # iteration expects.
        )

    money_out = pl.concat(done).with_columns((pl.col("units") * pl.col("nav")).round(2).alias("amount"))
    # Only NOW can the rupee amount be computed, because only now are the
    # units known.
    #
    # Note the direction is the reverse of money_in: there, amount was
    # chosen and units derived; here, units are forced by the balance and
    # amount is derived. Rounded to 2 decimals, because rupees have paise.

    # =========================================================================
    # PHASE 3 OF 3 — COMBINE, NUMBER AND FINISH
    # =========================================================================

    columns = ["folio_id", "plan_id", "txn_date", "txn_type", "amount", "units", "nav",
               "txn_status", "rejection_reason", "arn_code"]
    # The columns both halves share. money_in has no "frac" or "rank";
    # money_out has those extra working columns. Selecting an explicit list
    # from each is what lets them be concatenated, and it guarantees the
    # column ORDER matches — pl.concat aligns by position, so a different
    # order would silently put plan_ids into the folio_id column.

    txns = pl.concat([money_in.select(columns), money_out.select(columns)]).filter(pl.col("amount") > 0)
    # One final amount > 0 guard. Rounding to 2 decimals can turn a
    # sub-paisa redemption into exactly 0.00, and a transaction of zero
    # rupees is not a transaction.

    txns = txns.sort("txn_date", "folio_id").with_row_index("transaction_id", offset=1)
    # Sort by DATE FIRST, then assign ids 1, 2, 3...
    #
    # TWO REASONS THE SORT ORDER IS DATE-FIRST:
    #
    #   1. transaction_id then increases with time, like a real sequence
    #      from a registrar system. An id that jumps around in time looks
    #      wrong to anyone who reads the table.
    #
    #   2. The table is partitioned by month. Loading rows in date order
    #      means each partition is filled and then left alone, which is
    #      markedly faster than jumping between 60 partitions row by row.
    #
    # offset=1 because with_row_index starts at 0 by default, and this is a
    # primary key.

    n = len(txns)
    has_arn = txns["arn_code"].is_not_null().to_numpy()

    channel = np.where(
        has_arn,
        rng.choice(["Distributor", "Branch"], n, p=[0.8, 0.2]),
        rng.choice(["Online", "Mobile App", "Branch"], n, p=[0.5, 0.4, 0.1]),
    )
    # HOW the transaction reached the AMC, and it is CONSISTENT with the ARN.
    #
    # A transaction with an ARN came through an agent, so its channel is
    # "Distributor" or, occasionally, a branch visit the agent arranged. A
    # transaction with no ARN is the investor acting alone — website, app or
    # walking into a branch themselves.
    #
    # It would be far easier to draw the channel independently, and it would
    # be wrong: the data would contain "Online" transactions carrying a
    # distributor ARN, which contradicts itself. Students who cross-check
    # two columns against each other should find they agree.
    #
    # Note both rng.choice calls generate n values even though only part of
    # each is used. Wasteful, vectorised, and clear — the right trade here.

    seconds = rng.integers(9 * 3600, 18 * 3600, n)
    # A time of day in seconds past midnight, between 09:00 and 18:00.
    # Office hours — registrar systems do not stamp transactions at 3am.

    times = [f"{h:02d}:{m:02d}:{s:02d}" for h, m, s in zip(seconds // 3600, (seconds % 3600) // 60, seconds % 60)]
    # Seconds → "HH:MM:SS". The :02d pads single digits with a leading zero,
    # so 9 becomes "09" — without it the timestamp string would not parse.

    return txns.with_columns(
        pl.col("transaction_id").cast(pl.Int64),
        # with_row_index produces an unsigned 32-bit integer. PostgreSQL has
        # no unsigned types, and the column is BIGINT, so cast it explicitly
        # rather than leaving the loader to guess.
        pl.Series("channel", channel),
        (pl.col("txn_date").cast(pl.Utf8) + " " + pl.Series(times) + "+05:30").alias("created_at"),
        # The system timestamp, built as a STRING: "2024-03-14 11:42:07+05:30".
        #
        # The "+05:30" is India Standard Time, written explicitly. The column
        # is TIMESTAMPTZ, and PostgreSQL will parse this string on load. If
        # the offset were omitted, PostgreSQL would interpret the time in the
        # SERVER's timezone — which on Azure is UTC — and every transaction
        # would be recorded five and a half hours late. Nothing would error.
        # The data would simply be wrong, and a query grouping by hour of day
        # would show the business opening at 03:30.
    ).select(
        "transaction_id", "txn_date", "folio_id", "plan_id", "txn_type", "amount", "units", "nav",
        "txn_status", "rejection_reason", "arn_code", "channel", "created_at",
    )
    # The final column order, matching 04_transactions.sql exactly. The
    # loader uses PostgreSQL's COPY, which matches columns BY POSITION, not
    # by name — so this list is not documentation, it is the contract. A
    # column out of order here loads silently wrong or fails on a type
    # mismatch, depending on luck.