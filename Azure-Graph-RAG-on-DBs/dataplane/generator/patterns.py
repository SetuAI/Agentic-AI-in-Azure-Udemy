##############################################################################
# dataplane/generator/patterns.py
#
# PURPOSE:
#   The six stories we deliberately plant in otherwise random data.
#
#   This file contains NO LOGIC. It is a list of fixed values — company
#   names, dates, weights, multipliers — that the generator files read and
#   build their data around.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Random data has no story in it. Generate a million transactions from
#   plausible distributions and every interesting question gets a dull
#   answer: nothing correlates with anything, no distributor stands out, no
#   month is unusual.
#
#   Which makes the whole project impossible to demonstrate. "Why did net
#   flows drop in March 2024?" has to have an answer, and that answer has to
#   be findable by joining tables rather than by reading one column.
#
#   So each of these six is a cause-and-effect chain planted across SEVERAL
#   tables. Finding one requires the joins the graph exists to supply.
#
# WHY THIS FILE IS NOT IN config/ — AND THIS IS THE POINT OF THE FILE:
#   Stage 5's metadata pipeline reads everything in config/. If these values
#   lived there, the pipeline could read the answers instead of discovering
#   them in the data, and every score it produces would be meaningless.
#
#   The same values ARE written down for scoring, in
#   evals/answer_key/planted_patterns.yaml — and the pipeline never reads
#   that folder either. That separation is what makes the score mean
#   something.
#
#   So this file sits inside dataplane/, which only the generator reads.
#
# THE SIX, AND HOW MANY TABLES EACH ONE SPANS:
#
#   1. Bond downgrade      -> redemptions spike afterwards
#                             market + transactions + holdings + NAV   (4)
#   2. Group concentration -> one scheme holds too much of one group
#                             holdings + securities + issuers + groups (4)
#   3. Manager change      -> performance changes afterwards
#                             assignments + managers + NAV             (3)
#   4. SIP cancellations   -> rising in one city tier
#                             SIPs + folios + investors                (3)
#   5. Rejection rate      -> one distributor far above the rest
#                             transactions + distributors              (2)
#   6. Issuer limit        -> one scheme above 10% in one company
#                             holdings + securities + issuers          (3)
#
#   NOTE THAT NONE OF THEM IS A ONE-TABLE PATTERN. That is deliberate: a
#   finding visible in a single table needs no graph, no join path and no
#   metadata — and would prove nothing about this architecture.
#
# HOW TO USE:
#   Imported, never run:
#
#       from dataplane.generator import patterns as P
#       ... P.P1_DOWNGRADE_DATE ...
#
#   The short alias `P` is used in every generator file, so a reader can see
#   at a glance which values are planted and which are random.
##############################################################################


import numpy as np
# Used only for np.datetime64, so the dates below are the same type as every
# other date in the generator.
#
# WHY NOT PLAIN STRINGS: a date written as "2024-03-12" would have to be
# converted at each use site, and a comparison between a string and a numpy
# date fails silently in some contexts rather than erroring. Storing them as
# dates here means every comparison downstream is date-to-date.


# ── PATTERN 1 · BOND DOWNGRADE, THEN A REDEMPTION SPIKE ──────────────────────
#
# THE STORY: a housing finance company's bond is cut from AA- to BBB- — three
# notches at once, which is a severe downgrade rather than a routine one.
# Investors in the two schemes holding it redeem heavily over the following
# four weeks, so March 2024 net flows turn sharply negative for those
# schemes. The Credit Risk Fund's NAV also drops on the downgrade day,
# because the bond's price is marked down.
#
# THE CHAIN, AND WHICH FILE WRITES EACH LINK:
#
#   market.py       creates the issuer and the bond, and writes the two
#                   rating rows into credit_rating_history
#   summaries.py    gives the bond a higher weight before the date and a
#                   lower one after, in portfolio_holdings
#   fund_house.py   applies the one-day NAV drop
#   transactions.py creates the extra redemptions in the four weeks after
#
# FOUR FILES, FOUR TABLES, ONE STORY. And the question that finds it —
# "which schemes were affected by the Vardhaman downgrade?" — has to cross
# hidden link 2, because credit_rating_history joins to securities only by
# value.
#
# WHY THIS IS THE FLAGSHIP PATTERN: it is the one in the README screenshot,
# and the only one where a single event propagates into four different
# tables with a visible date boundary in each.

P1_ISSUER_NAME = "Vardhaman Housing Finance Ltd"
# The company. INVENTED — assembled from common.py's NAME_WORDS.
#
# Note what this row is accused of: being downgraded three notches. That is a
# reputational claim, and attaching it to a real company would be making a
# false statement about a real business. Every company in a planted pattern
# is fictitious for exactly that reason.
#
# tests/test_stage5_metadata.py looks this name up to check that vector
# search can match "vardhaman housing" to the full name. So the string here
# is load-bearing: change it and that test fails.

P1_GROUP_NAME = "Vardhaman Group"
# The business group it belongs to. Gives the downgrade a second dimension —
# an analyst can ask whether other companies in the same group are also held.

P1_DOWNGRADE_DATE = np.datetime64("2024-03-12")
# The downgrade. A Tuesday, and that is checked rather than assumed: the NAV
# drop has to land on a trading day, and a weekend date would be rolled
# forward by the generator and no longer match this constant.
#
# Mid-March was chosen so the reaction falls inside one month. A downgrade on
# the 28th would split the redemption spike across two months and make
# "March net flows" a much weaker signal.

P1_EARLIER_CUT_DATE = np.datetime64("2023-05-15")      # AA -> AA-, a warning sign
# A SMALLER DOWNGRADE TEN MONTHS EARLIER, and it is the most interesting
# value in this file.
#
# It makes credit_rating_history a genuine HISTORY rather than a single
# event. The bond's path is AA -> AA- -> BBB-, so:
#
#   - "what is this bond rated?" has three answers depending on the date,
#     and the correct query is "the most recent row on or before X"
#   - the earlier cut is a warning sign that preceded the severe one, which
#     is how credit deterioration actually looks
#   - a query that takes MAX(rtg) or just any row gets a different answer
#     from one that takes the latest by rtg_dt
#
# Without this row the table would have one rating per bond and the
# "latest-by-date" skill would never be exercised.

P1_SPIKE_END = np.datetime64("2024-04-10")             # redemptions spike until here
# Redemptions are spread between the downgrade and this date — about four
# weeks.
#
# NOT ALL ON ONE DAY, deliberately. A single-day spike reads as a data error.
# A four-week ramp reads as news spreading, advisors calling clients, and
# people acting over days — which is what actually happens.

P1_HOLDERS = {
    "Sahyadri Credit Risk Fund": (6.5, 4.0, 0.35, -0.018),
    "Sahyadri Medium Duration Fund": (2.5, 1.5, 0.15, -0.006),
}
# WHICH SCHEMES HELD THE BOND, AND HOW HARD EACH WAS HIT.
#
# Each value is a 4-tuple, and THREE DIFFERENT FILES each take one field:
#
#   (weight % before, weight % from March 2024, share of folios that
#    redeem in the spike, one-day NAV drop)
#      │                │                      │                  │
#      summaries.py     summaries.py           transactions.py     fund_house.py
#
# That is why the tuple is unpacked with underscores in each file — each one
# names the field it needs and ignores the rest. The shared tuple is what
# keeps the four tables telling the SAME story: if the weights lived in
# summaries.py and the redemption share in transactions.py, the two could
# drift apart with nothing to catch it.
#
# READ THE NUMBERS AS A PROPORTIONAL RESPONSE:
#
#   Credit Risk Fund held 6.5% of the bond, dropping to 4.0%. 35% of its
#   folios redeemed, and its NAV fell 1.8% in a day.
#   Medium Duration Fund held only 2.5%, dropping to 1.5%. 15% of folios
#   redeemed, and its NAV fell 0.6%.
#
# The fund with more exposure saw more panic and a bigger NAV hit. That
# proportionality is what makes the pattern believable — and it means
# "which fund was worst affected?" has a correct answer that matches the
# exposure, rather than being arbitrary.
#
# WHY THE WEIGHT DROPS: two things happen at once. The bond loses value, and
# the fund sells some of it. Both reduce its share of the portfolio. A weight
# that stayed at 6.5% after a three-notch downgrade would mean the fund did
# nothing and the market did not reprice, which is not credible.
#
# A Credit Risk Fund is the right scheme to hit, because holding lower-rated
# paper for extra yield is its stated product. A liquid fund holding a
# BBB- bond would be a mandate breach rather than a credit event.


# ── PATTERN 2 · HIGH EXPOSURE TO ONE BUSINESS GROUP ──────────────────────────
#
# THE STORY: the Mid Cap Fund holds five companies that all belong to the
# same business group. Each holding is comfortably under 10% on its own, so
# every single-company check passes. Together they are about 24% of the fund.
#
# THIS IS THE HARDEST OF THE SIX TO FIND, and the reason is worth stating:
# NO SINGLE ROW LOOKS WRONG. There is no outlier to spot, no threshold
# crossed, nothing a per-row rule would flag. The finding only exists once
# five rows are grouped and summed.
#
# The route is portfolio_holdings -> securities -> issuers -> issuer_groups,
# then SUM(weight_pct) GROUP BY group_name. Four tables and an aggregate
# before anything is visible.
#
# WHICH IS EXACTLY THE SHAPE OF A REAL COMPLIANCE FINDING, and exactly the
# shape a single-table query can never produce.
#
# UNLIKE PATTERNS 1, 4 AND 6, THIS ONE HAS NO DATE. It is a standing
# condition rather than an event — a portfolio that has quietly been too
# concentrated for five years and was never flagged. Nobody is looking for
# when it started; they are looking for the fact that nobody noticed.

P2_GROUP_NAME = "Trident Group"
P2_SCHEME = "Sahyadri Mid Cap Fund"
# A mid cap fund is the right place for this. Mid cap managers hold fewer,
# larger positions than large cap managers do, so a concentrated book is
# plausible there in a way it would not be in an index fund.

P2_COMPANIES = [                          # (company name, sector, weight %)
    ("Trident Chemicals Ltd", "Chemicals", 5.5),
    ("Trident Power Ltd", "Power", 5.0),
    ("Trident Polymers Ltd", "Chemicals", 4.8),
    ("Trident Logistics Ltd", "Logistics", 4.5),
    ("Trident Realty Ltd", "Real Estate", 4.2),
]
# FIVE COMPANIES, FIVE WEIGHTS, AND THE ARITHMETIC IS THE WHOLE PATTERN.
#
#   5.5 + 5.0 + 4.8 + 4.5 + 4.2  =  24.0%
#
# Every individual weight is under 8%, which is the cap summaries.py applies
# to randomly chosen holdings — so none of these five stands out as unusual
# even against the background. And all are under 10%, the single-issuer
# limit, so pattern 6 and pattern 2 do not interfere with each other.
#
# THE SECTORS ARE DELIBERATELY SPREAD — Chemicals, Power, Logistics, Real
# Estate. Which means a SECTOR concentration check also passes: no single
# sector is overweight either. The only grouping that reveals the problem is
# the business group, and the business group is the one dimension a person
# has to think to look at.
#
# Note two companies share the Chemicals sector. So even the sector totals
# look ordinary: 10.3% in Chemicals is unremarkable.
#
# The names all share the "Trident" prefix, which makes the pattern visible
# to a human reading the holdings list — and invisible to a query that does
# not join to issuer_groups. That gap between "obvious when you look" and
# "invisible to the system" is the finding.


# ── PATTERN 3 · MANAGER CHANGE, THEN A CHANGE IN PERFORMANCE ─────────────────
#
# THE STORY: the Focused Fund's primary manager changes in October 2023.
# Before the change the fund trails its benchmark by about 3% a year; after
# it, the fund beats the benchmark by about 6% a year.
#
# THE CHAIN: scheme_manager_assignments holds the change date.
# fund_house.py applies the two different performance rates to the NAV
# series either side of it. So answering "did performance change when the
# manager changed?" needs assignments -> schemes -> scheme_plans ->
# nav_history, plus benchmark_values to compare against.
#
# AND A CAUTION WORTH SAYING ON CAMERA: this pattern is a CORRELATION, and
# the project's own agent is instructed never to claim one thing caused
# another. The manager changed and performance changed. Whether the manager
# caused it is not something this data can show — markets rotated, the
# strategy may have changed, the sample is 10 months.
#
# That restraint is written into the commentary step's prompt, and this
# pattern is the clearest case for why it is there.

P3_SCHEME = "Sahyadri Focused Fund"
# A focused fund holds a small number of positions by design — 20 to 30
# stocks — so a single manager's choices move the result much more than they
# would in a 50-stock index fund. The pattern is plausible here and would
# not be in an index fund, where the manager cannot deviate at all.

P3_OLD_MANAGER = "Rajesh Kulkarni"
P3_NEW_MANAGER = "Meera Iyer"
# Both assembled from common.py's FIRST_NAMES and SURNAMES. Fictitious
# people, and the same reasoning as the companies: this row is a claim about
# somebody's professional performance.

P3_CHANGE_DATE = np.datetime64("2023-10-03")           # new manager's first day
# The new manager's FIRST day, not the old manager's last. So the old
# assignment's end_date is 2023-10-02 and the new one's start_date is this,
# with no gap and no overlap.
#
# Placed about two years into the five-year window, which leaves roughly 25
# months before and 35 months after — enough history on both sides for an
# annualised return to mean anything. A change in month 58 would give two
# months of "after", and any performance difference would be noise.

P3_ALPHA_BEFORE = -0.03                                # -3% a year vs benchmark
P3_ALPHA_AFTER = 0.06                                  # +6% a year vs benchmark
# ALPHA IS RETURN RELATIVE TO THE BENCHMARK, as a yearly rate. Negative means
# trailing the index; positive means beating it.
#
# fund_house.py builds each plan's NAV series by tracking its benchmark and
# adding alpha, so these two numbers are applied as a daily drift either side
# of the change date.
#
# A NINE-POINT SWING, which is large but not absurd — a focused fund changing
# strategy can move that far. It has to be large enough to be visible above
# the day-to-day noise in a NAV series, or the pattern would not be findable
# in a 10-month sample.
#
# And note it is a RATE, not a one-off jump. The NAV does not step on the
# change date; it drifts differently afterwards. A step would be obviously
# artificial, and would also look like a data error rather than a
# performance change.


# ── PATTERN 4 · SIP CANCELLATIONS RISING IN ONE CITY TIER ────────────────────
#
# THE STORY: in the last twelve months of the window, investors in tier-3
# cities cancel their SIPs at 3.5 times the normal rate. Tiers 1 and 2 stay
# at the normal rate.
#
# THE CHAIN: sip_registrations -> folios -> investors, because the city tier
# is on the INVESTOR and the cancellation is on the SIP, with the folio in
# between. Three tables, and the two declared foreign keys that connect them.
#
# WHY A FUND HOUSE CARES MORE ABOUT THIS THAN ABOUT ALMOST ANYTHING ELSE: a
# cancelled SIP is not a lost sale, it is lost FUTURE revenue, every month,
# indefinitely. A rise concentrated in one segment is the kind of thing that
# changes distribution strategy.

P4_TIER = 3
# Tier 3 is smaller towns — 20% of investors, per common.py's TIER_SHARE.
#
# THAT 20% IS WHY THE PATTERN WORKS. Big enough that a 3.5x rise is
# statistically visible; small enough that it is a finding about a SEGMENT
# rather than about the whole book. If tier 3 were 2% of investors the signal
# would be noise; at 50% it would not be a segment story.

P4_FROM = np.datetime64("2025-09-01")
# THE PATTERN STARTS HERE, NOT AT THE BEGINNING OF TIME, and that is the
# design decision in this pattern.
#
# Exactly twelve months before the window closes on 2026-08-31. So there are
# four years of normal behaviour and one year of elevated behaviour.
#
# WHY THAT MATTERS: if tier-3 SIPs cancelled at 3.5x from day one, the
# pattern would be a CONSTANT rather than a CHANGE — and a constant is not a
# finding. The interesting question an analyst asks is "when did this start?",
# and there has to be a date for them to find.
#
# investors.py implements this with np.maximum(first_pay, P4_FROM), so a SIP
# that started after this date has its elevated clock begin when the SIP
# does.

P4_MULTIPLIER = 3.5
# Against a background cancellation rate of about 1.1% of running SIPs per
# month, this makes tier 3 roughly 3.85% per month.
#
# Large enough to be unmistakable in a monthly chart, small enough that most
# tier-3 SIPs still survive the year — a multiplier of 20 would empty the
# segment and the pattern would look like a data error rather than customer
# behaviour.


# ── PATTERN 5 · ONE DISTRIBUTOR WITH A HIGH REJECTION RATE ───────────────────
#
# THE STORY: most distributors see about 2% of transactions rejected. This
# one sees about 17%, mostly for KYC and mandate problems.
#
# THE SIMPLEST OF THE SIX — two tables, one join, one GROUP BY. It is the
# demo question used in the project overview, precisely because it is easy to
# follow end to end.
#
# AND IT IS THE ONE THAT CROSSES HIDDEN LINK 3. transactions.arn_code to
# distributors.arn_no, undeclared, different column names, 45% NULL. So the
# easiest question in the project is unanswerable until the metadata pipeline
# has discovered that link — which is the cleanest possible demonstration of
# why the pipeline exists.

P5_DISTRIBUTOR_NAME = "Konkan Wealth Partners"
# The distributor. Invented, and investors.py forces it to be an IFA — an
# individual advisor — rather than a bank.
#
# A bank with a 17% rejection rate is not a believable story; an individual
# advisor with sloppy mandate paperwork is. The story has to survive a
# student reading the row.
#
# investors.py also forces its active flag to 'Y'. Without that, a 5% chance
# of being marked inactive would sometimes hide it from the obvious query —
# "which ACTIVE distributor has the worst rejection rate?"

P5_ARN = "ARN-104417"
# A FIXED, KNOWN ARN, and this constant does more work than it looks.
#
# transactions.py finds this distributor's rows by comparing arn_code against
# this exact string, and tests/test_stage6_agent.py asserts on the result. So
# the value has to be fixed rather than drawn at random.
#
# And investors.py has to REMOVE this number from the pool other
# distributors' ARNs are drawn from — arn_no is the primary key, so a
# collision would fail the load with a unique violation.

P5_REJECTION_RATE = 0.17
NORMAL_REJECTION_RATE = 0.02
# A rate, not a count. transactions.py flips one weighted coin per
# transaction, so the OBSERVED rate will be near 17% rather than exactly 17%.
#
# THAT IS WHY IT LOOKS LIKE REAL DATA, and why the test asserts a RANGE
# rather than an exact figure. A distributor with exactly 17.0000% of its
# transactions rejected would be the one thing in the database that was
# obviously placed.
#
# NORMAL_REJECTION_RATE has no P5_ prefix because it is not part of pattern 5
# — it is the background every other distributor gets, and summaries and
# tests both refer to it. Worth knowing when searching the codebase for it.
#
# A ~8x difference is far outside anything that could happen by chance in
# thousands of transactions, which is what makes the finding defensible
# rather than suggestive.


# ── PATTERN 6 · ONE SCHEME ABOVE THE 10% SINGLE-ISSUER LIMIT ──────────────────
#
# THE STORY: for the last six months of the window, the Banking & Financial
# Services Fund holds 12.4% of its money in one bank — above the 10% limit
# that normally applies to a single company.
#
# THE CHAIN: portfolio_holdings -> securities -> issuers. Three tables, and
# the question is "which schemes breach the single-issuer limit, and from
# when?"
#
# THE ANSWER IS EXACTLY ONE SCHEME, and that is engineered. summaries.py caps
# every randomly chosen holding at 8%, well under the limit, so nothing else
# brushes against it by accident. The pattern is findable only because the
# background is quiet — and that is a general principle worth stating: a
# planted signal needs a deliberately clean baseline, or it is just noise.

P6_SCHEME = "Sahyadri Banking & Financial Services Fund"
# A SECTORAL FUND, and that is the point. It may only hold banking, NBFC and
# insurance companies, so its universe is small — a few dozen candidates
# rather than 520.
#
# Which makes the breach PLAUSIBLE rather than careless. A fund restricted to
# one sector genuinely struggles to stay diversified, and this is the real
# reason sectoral funds breach concentration limits more often than
# diversified ones. The pattern is not "somebody made a mistake"; it is a
# structural consequence of the mandate.
#
# Note the ampersand in the name. It is the kind of character that breaks
# naive string handling, and it travels through CSV, COPY, JSON prompts and
# an embedding without special treatment — a small, real test of the
# pipeline.

P6_ISSUER_NAME = "Malabar Commercial Bank Ltd"
# Invented, and a bank — so it legitimately falls inside the fund's mandate.
# A breach on a company the fund should not have held at all would be two
# findings at once and would muddle the lesson.

P6_WEIGHT_BEFORE = 8.5
P6_WEIGHT_AFTER = 12.4
# 8.5% BEFORE, WHICH IS UNDER THE LIMIT. 12.4% AFTER, WHICH IS OVER IT.
#
# The before value matters as much as the after. The fund always had a large
# position in this bank — so the story is a position that GREW past a
# threshold, not one that appeared out of nowhere. That is how limit breaches
# actually happen: a holding you already had went up in value.
#
# 8.5% is also just above summaries.py's 8.0% cap on random holdings, so even
# before the breach this is the fund's largest position and visibly so.

P6_FROM = np.datetime64("2026-03-31")                  # first month end above 10%
# A MONTH END, because portfolio_holdings only has month-end rows. A
# mid-month date here would never match any as_of_date and the pattern would
# silently never appear.
#
# The last six month ends of the window: March, April, May, June, July,
# August 2026.
#
# SIX MONTHS IS LONG ENOUGH TO BE A SUSTAINED BREACH rather than a one-month
# blip that might be a valuation artefact. And having a start date makes the
# question answerable as "WHEN did this scheme breach the limit?" — a date is
# a far more useful finding than a flag.