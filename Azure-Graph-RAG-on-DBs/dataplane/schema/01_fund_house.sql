-- ############################################################################
-- dataplane/schema/01_fund_house.sql
--
-- PURPOSE:
--   Creates the 9 "fund house" tables — everything about the mutual fund
--   company's own products. Which schemes it runs, the plans inside each
--   scheme, who manages them, what they are measured against, their daily
--   prices, and how much money each one holds.
--
--   This is FILE 1 OF 4. apply_schema.py runs them 01 -> 04.
--
--   1. scheme_categories          ~36 rows    the SEBI category of a scheme
--   2. benchmarks                 ~15 rows    the index a scheme is measured against
--   3. benchmark_values       ~18,750 rows    the daily level of each index
--   4. schemes                    ~40 rows    the funds themselves
--   5. scheme_plans              ~150 rows    Direct/Regular x Growth/IDCW
--   6. fund_managers              ~25 rows    the people who run the money
--   7. scheme_manager_assignments ~70 rows    who ran what, and when
--   8. nav_history           ~170,000 rows    the daily price of each plan
--   9. scheme_aum_monthly      ~2,400 rows    money managed, per scheme, per month
--
-- WHAT PROBLEM DOES THIS SOLVE:
--   Every question this project answers eventually lands on `schemes`. More
--   tables point at it than at anything else in the database. So before any
--   data exists, the shape of a scheme has to be decided: what makes two
--   schemes different, what a scheme cannot exist without, and what is
--   allowed to be missing.
--
--   The other eight tables exist because a scheme on its own is not enough
--   to answer anything useful:
--
--     "is this fund beating the market?"   needs benchmarks + benchmark_values
--     "what did this fund return?"         needs nav_history
--     "how big is this fund?"              needs scheme_aum_monthly
--     "did performance change?"            needs scheme_manager_assignments
--     "is this a debt fund?"               needs scheme_categories
--
--   And every one of those links is declared here, in SQL, so the database
--   itself refuses a row that breaks it.
--
-- HOW THE 9 TABLES CONNECT:
--
--   scheme_categories ──┐
--                       ├──> schemes ──> scheme_plans ──> nav_history
--   benchmarks ─────────┘       │
--        │                      ├──> scheme_manager_assignments <── fund_managers
--        │                      │
--        └──> benchmark_values  └ ─ ─> scheme_aum_monthly
--                                      (dashed: the link is real but
--                                       NOT declared — hidden link 1 of 3)
--
-- WHICH DATABASE:
--   These go into mf_data, which stands in for the client's own fund
--   database. NOT agent_meta. The agent only ever READS mf_data.
--
-- WHY THE COMMENTS IN THIS FILE ARE NOT IN THE DATABASE:
--   Everything after a "--" lives in this file only. PostgreSQL has a
--   COMMENT ON TABLE command that would store these descriptions inside the
--   database, and we deliberately do not use it.
--
--   The reason is the whole second half of this project. In Stage 5 the
--   metadata pipeline reads this database and writes its OWN description of
--   every table and column, exactly as it would on a client's database. If
--   the descriptions were already stored here, the pipeline would be copying
--   our answers rather than working them out, and its score would mean
--   nothing.
--
--   So: a human reading this file sees a full explanation. A program reading
--   the database sees column names, types and constraints, and nothing else.
--
-- HOW TO RUN:
--   Never on its own. apply_schema.py runs all four files together, in one
--   transaction:
--
--       uv run python dataplane/schema/apply_schema.py
-- ############################################################################


-- ── CLEAN START ────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS
    scheme_aum_monthly,
    nav_history,
    scheme_manager_assignments,
    fund_managers,
    scheme_plans,
    schemes,
    benchmark_values,
    benchmarks,
    scheme_categories
CASCADE;
-- Delete these 9 tables if they already exist, so every run of this file
-- starts from empty.
--
-- THE ORDER OF THE NAMES IS THE REVERSE OF THE CREATE ORDER, and that is
-- deliberate. scheme_plans points at schemes, so scheme_plans must go first.
-- Dropping schemes while scheme_plans still points at it would normally be
-- refused.
--
-- IF EXISTS
--   Without this, the very first run on a fresh database would fail: you
--   cannot drop a table that was never created. IF EXISTS turns that error
--   into a silent no-op.
--
-- CASCADE
--   Also remove anything OUTSIDE this file that points at these tables.
--   File 02's portfolio_holdings references schemes, and file 03's folios
--   does too. Without CASCADE, a second run of this file would be refused
--   because those other tables still depend on schemes.
--
--   CASCADE is safe here only because apply_schema.py always runs all four
--   files together, so anything CASCADE removes is recreated moments later.
--   Running this one file by hand against a built database would leave the
--   other files' tables damaged.
--
-- AND THE WARNING THAT MATTERS: this deletes data. Every run of
-- apply_schema.py wipes mf_data and leaves 20 empty tables. That is
-- acceptable here because mf_data holds nothing but synthetic data that the
-- generator rebuilds in about 100 seconds. On a client's database a script
-- that opens with DROP TABLE would be unacceptable, and the client never
-- receives the dataplane/ folder at all.


-- ── TABLE 1 OF 9 · scheme_categories ───────────────────────────────────────
--
-- SEBI — the Indian market regulator — defines the categories every mutual
-- fund scheme must sit in. "Large Cap Fund". "Liquid Fund". "Credit Risk
-- Fund". A fund house does not invent these; it chooses one per scheme.
--
-- WHY A SEPARATE TABLE rather than a text column on schemes: because
-- questions are asked at the category level. "Net flows into debt funds"
-- means grouping 40 schemes into 5 asset classes, and that grouping has to
-- live somewhere a query can join to.
--
-- About 36 rows, and it never grows — SEBI adds a category every few years.

CREATE TABLE scheme_categories (
    category_id     integer PRIMARY KEY,
    -- The table's own identifying number. Other tables store this number to
    -- say "this scheme is in category 7".
    --
    -- PRIMARY KEY does two things at once, and this is its first appearance
    -- in the project so it is worth both:
    --   1. NOT NULL  — this column can never be empty
    --   2. UNIQUE    — no two rows may share a value
    -- It also creates an index automatically, so looking a row up by this
    -- number is fast without anything further being declared.

    category_code   text NOT NULL UNIQUE,
    -- A short code a person can read, such as 'EQ-LC' for Equity Large Cap.
    --
    -- UNIQUE on its own (without PRIMARY KEY) means: no two rows may share a
    -- value, but this is not the table's identity. A table has one primary
    -- key and may have many unique columns.
    --
    -- Why have both an id and a code: the id is ours and will never change.
    -- The code comes from outside and could be revised. Keeping them apart
    -- means a renamed code does not force every other table to be updated.

    category_name   text NOT NULL UNIQUE,
    -- The full name, such as 'Large Cap Fund'.

    asset_class     text NOT NULL
        CHECK (asset_class IN ('Equity', 'Debt', 'Hybrid', 'Solution Oriented', 'Other')),
    -- The broad group the category belongs to. Five values, and only five.
    --
    -- CHECK is the first constraint here that enforces a RULE rather than a
    -- shape. The database tests every value against this list and REJECTS
    -- anything else. Not corrects, not warns — rejects, with an error.
    --
    -- WITHOUT IT, this column would eventually hold 'Equity', 'equity',
    -- 'EQUITY' and 'Equtiy', and every query that groups by asset class
    -- would return four rows where there should be one. The bug would not
    -- look like a typo; it would look like the data being wrong.
    --
    -- A CHECK makes that impossible rather than unlikely. That distinction
    -- is most of what a schema is for.

    risk_level      text NOT NULL
        CHECK (risk_level IN ('Low', 'Low to Moderate', 'Moderate',
                              'Moderately High', 'High', 'Very High'))
    -- SEBI's riskometer level. Six fixed steps, lowest to highest.
    --
    -- NOTE THE ORDER IS MEANINGFUL but the storage is text, so sorting this
    -- column alphabetically gives nonsense: 'High' sorts before 'Low'. Any
    -- question about "riskier than" has to map these to numbers first. That
    -- is a real limitation of storing an ordered scale as text, and it is
    -- how the regulator publishes it, so we keep it.
);


-- ── TABLE 2 OF 9 · benchmarks ──────────────────────────────────────────────
--
-- Every scheme is measured against an index. A scheme "beat its benchmark"
-- if its return was higher than the index's over the same period. That
-- comparison is the single most common performance question in this domain.
--
-- The names here are invented, so nothing in the demo can be mistaken for a
-- real published index.

CREATE TABLE benchmarks (
    benchmark_id    integer PRIMARY KEY,

    benchmark_name  text NOT NULL UNIQUE,

    benchmark_type  text NOT NULL
        CHECK (benchmark_type IN ('Equity', 'Debt', 'Hybrid')),
    -- What kind of market the index tracks.
    --
    -- This exists so a validation check can confirm an equity scheme is
    -- measured against an equity index. A debt fund compared to a stock
    -- index would produce performance numbers that are arithmetically
    -- correct and completely meaningless.

    provider        text NOT NULL
    -- The company that publishes the index.
);


-- ── TABLE 3 OF 9 · benchmark_values ────────────────────────────────────────
--
-- One row per benchmark per trading day. 15 benchmarks times roughly 1,250
-- trading days in five years, so about 18,750 rows.
--
-- This is the first TIME SERIES table in the project — same thing, many
-- dates — and it introduces the composite primary key below.

CREATE TABLE benchmark_values (
    benchmark_id    integer NOT NULL REFERENCES benchmarks (benchmark_id),
    -- Which index this value belongs to.
    --
    -- REFERENCES IS THE FIRST FOREIGN KEY IN THE PROJECT, so here is what it
    -- does in full.
    --
    -- It tells the database: every value in this column must already exist
    -- in benchmarks.benchmark_id. The database then enforces that in BOTH
    -- directions:
    --
    --   inserting a row with an unknown benchmark_id   -> REFUSED
    --   deleting a benchmark that has values pointing at it -> REFUSED
    --
    -- So "orphan" rows — a value for an index that does not exist — cannot
    -- be created, by anyone, including by a bug in our own generator.
    --
    -- It also matters for the metadata pipeline later: a declared foreign key
    -- appears in PostgreSQL's catalog, so link discovery finds it
    -- automatically and approves it without asking a human. There are 19
    -- declared links in this database and 3 undeclared ones, and the whole
    -- point of the exercise is the difference.

    value_date      date NOT NULL,
    -- The trading day.
    --
    -- "date", NOT "timestamptz". This is a BUSINESS date — a trading day —
    -- with no time of day and no time zone. An index has one closing level
    -- for the day; 14:32 on that day is not a thing that exists here.
    --
    -- Using timestamptz would invite a time zone to creep in, and then
    -- "Monday's close" in India would be Sunday's date in UTC. Half this
    -- project's dates are business dates and the other half are system
    -- timestamps, and keeping the two types separate is what stops them
    -- being confused.

    index_value     numeric(14, 4) NOT NULL CHECK (index_value > 0),
    -- The index level on that day.
    --
    -- numeric(14, 4) MEANS: up to 14 digits in total, 4 of them after the
    -- decimal point. So the largest value is 9999999999.9999.
    --
    -- WHY numeric AND NOT float: numeric stores exact decimals. A float
    -- stores an approximation, so 0.1 + 0.2 does not equal 0.3, and sums of
    -- thousands of rows drift. For anything that is money or a published
    -- price, that drift is not acceptable — and this project adds up over a
    -- million transaction amounts later.
    --
    -- CHECK > 0: an index level can never be zero or negative.

    PRIMARY KEY (benchmark_id, value_date)
    -- A COMPOSITE PRIMARY KEY — two columns together.
    --
    -- Neither column is unique on its own. One benchmark appears on 1,250
    -- dates; one date appears for 15 benchmarks. What is unique is the PAIR:
    -- a benchmark has exactly one value on any given day.
    --
    -- Declaring it this way means a duplicate is IMPOSSIBLE rather than
    -- merely unlikely. Without it, re-running the generator could quietly
    -- insert a second value for the same index on the same day, and every
    -- return calculation downstream would be wrong in a way that is very
    -- hard to see.
    --
    -- Note there is no separate id column on this table at all. The data's
    -- own natural identity is the pair, so inventing an extra number would
    -- add a column and guarantee nothing.
);


-- ── TABLE 4 OF 9 · schemes ─────────────────────────────────────────────────
--
-- The funds themselves — "Sahyadri Credit Risk Fund" and 39 others.
--
-- THIS IS THE CENTRE OF THE DATABASE. More tables point at schemes than at
-- anything else: scheme_plans, scheme_manager_assignments, folios,
-- portfolio_holdings, and (undeclared) scheme_aum_monthly.

CREATE TABLE schemes (
    scheme_id       integer PRIMARY KEY,

    scheme_code     text NOT NULL UNIQUE,
    -- The fund house's internal code.

    scheme_name     text NOT NULL UNIQUE,
    -- The full scheme name.
    --
    -- THIS COLUMN GETS SPECIAL TREATMENT LATER. It is what a user types in a
    -- question, and they will not type it exactly. In Stage 5 every value in
    -- this column is turned into an embedding and stored in pgvector, so
    -- that a question saying "sahyadri credit fund" can be matched to this
    -- exact row and the SQL can use the real name.
    --
    -- config/agent.yaml names this table and column as where a "scheme" kind
    -- of name gets looked up.

    category_id     integer NOT NULL REFERENCES scheme_categories (category_id),
    -- Which SEBI category. NOT NULL plus a foreign key together mean: a
    -- scheme without a valid category cannot exist. Not "should not" — cannot.

    benchmark_id    integer NOT NULL REFERENCES benchmarks (benchmark_id),
    -- Which index it is measured against. Same reasoning.

    launch_date     date NOT NULL,
    -- The day the scheme opened to investors.
    --
    -- A VALIDATION ANCHOR, used in three places downstream:
    --   - a folio may not open before its scheme launched
    --   - a NAV may not exist before the launch date
    --   - a transaction may not be dated before it either
    -- The generator enforces all three, and tests/test_stage2_data.py checks
    -- that it did.

    scheme_status   text NOT NULL DEFAULT 'Active'
        CHECK (scheme_status IN ('Active', 'Merged', 'Closed')),
    -- DEFAULT 'Active' means: insert a row without naming this column and it
    -- becomes 'Active' rather than empty. Combined with NOT NULL, there is
    -- no way to end up with a scheme whose status is unknown.

    exit_load_pct   numeric(7, 4) NOT NULL DEFAULT 0 CHECK (exit_load_pct >= 0),
    -- The charge for redeeming early, as a percentage. 1.0000 means 1%.
    --
    -- numeric(7, 4) allows up to 999.9999, which is far more than any real
    -- exit load, and 4 decimal places is enough for any rate a regulator
    -- publishes.
    --
    -- CHECK >= 0, not > 0: zero is a legitimate exit load. Many schemes have
    -- none. The difference between >= and > here is the difference between
    -- "free to redeem" being allowed and being rejected.

    min_sip_amount  numeric(18, 2) NOT NULL CHECK (min_sip_amount > 0),
    -- The smallest monthly instalment allowed, in rupees.
    --
    -- numeric(18, 2) is the money type used throughout this project: 18
    -- digits, 2 after the point. Rupees and paise, with room for amounts far
    -- larger than anything here.
    --
    -- CHECK > 0 this time, not >= 0: a minimum investment of zero is not a
    -- minimum.

    created_at      timestamptz NOT NULL DEFAULT now()
    -- When this row was created in the system.
    --
    -- "timestamptz", NOT "date" — and this is the contrast with value_date
    -- above. This is a SYSTEM time: it has a time of day, it has a time
    -- zone, and it records when a computer did something. launch_date is a
    -- BUSINESS date: a day in the real world, with no clock attached.
    --
    -- DEFAULT now() fills it in automatically, so no insert has to mention it.
);


-- ── TABLE 5 OF 9 · scheme_plans ────────────────────────────────────────────
--
-- The distinction people new to mutual funds get wrong, so this table is
-- worth slowing down for.
--
-- AN INVESTOR DOES NOT BUY A SCHEME. THEY BUY A PLAN WITHIN A SCHEME.
--
-- The same fund is sold in up to four versions, from two independent choices:
--
--   Direct  or Regular  ->  bought from the fund house, or sold by a
--                           distributor who earns commission
--   Growth  or IDCW     ->  profits stay invested, or are paid out
--                           (IDCW = Income Distribution cum Capital
--                            Withdrawal, formerly called Dividend)
--
-- AND HERE IS WHY IT MATTERS FOR EVERY QUERY IN THIS PROJECT: each version
-- has its OWN NAV. A Regular plan carries the distributor's commission
-- inside its expense ratio, so its NAV is permanently lower than the Direct
-- plan of the same fund. Same portfolio, same manager, different price per
-- unit.
--
-- Which is why nav_history, sip_registrations and transactions all point at
-- a PLAN and not at a scheme. Every time you see plan_id rather than
-- scheme_id downstream, this table is the reason.
--
-- About 150 rows: 40 schemes, up to 4 plans each, minus the combinations that
-- do not exist — a liquid fund or an index fund typically offers Growth only.

CREATE TABLE scheme_plans (
    plan_id             integer PRIMARY KEY,

    scheme_id           integer NOT NULL REFERENCES schemes (scheme_id),

    plan_type           text NOT NULL CHECK (plan_type IN ('Direct', 'Regular')),

    option_type         text NOT NULL CHECK (option_type IN ('Growth', 'IDCW')),

    isin                text NOT NULL UNIQUE CHECK (length(isin) = 12),
    -- The International Securities Identification Number: the 12-character
    -- code that identifies this plan on exchanges and at registrars.
    --
    -- THREE RULES ON ONE COLUMN, and each earns its place:
    --   NOT NULL    every plan has one
    --   UNIQUE      no two plans share one
    --   length = 12 an ISIN is always exactly 12 characters, worldwide
    --
    -- The length check encodes a fact about the outside world in the
    -- database, so a truncated or padded value is rejected at the point of
    -- insertion rather than discovered later by whoever tries to use it.
    --
    -- Note it is in its own column, separate from plan_id. Outside codes get
    -- revised; our internal numbers must not depend on them.

    expense_ratio_pct   numeric(7, 4) NOT NULL CHECK (expense_ratio_pct >= 0),
    -- The yearly fee, as a percentage of money invested. 0.7500 is 0.75%.
    --
    -- This is the column that makes Direct and Regular differ. The generator
    -- gives Regular plans a higher value, and the NAV calculation subtracts
    -- it daily — which is what produces the permanent gap between the two
    -- NAVs of the same scheme.

    UNIQUE (scheme_id, plan_type, option_type)
    -- A COMPOSITE UNIQUE across three columns.
    --
    -- Any one of them repeats freely: there are many Direct plans, many
    -- Growth options, many plans per scheme. What cannot repeat is the
    -- combination. One scheme cannot have two "Direct Growth" plans.
    --
    -- Note this is UNIQUE and not PRIMARY KEY. The table already has a
    -- primary key, plan_id, because other tables need a single short number
    -- to point at. This unique constraint sits alongside it to enforce a
    -- business rule the primary key cannot express.
    --
    -- The generator also relies on it: investors.py builds a lookup from
    -- (scheme_id, plan_type, option_type) to plan_id, and that lookup is
    -- only well-defined because this constraint guarantees one answer.
);


-- ── TABLE 6 OF 9 · fund_managers ───────────────────────────────────────────
--
-- The people who run the money. About 25 of them.

CREATE TABLE fund_managers (
    manager_id          integer PRIMARY KEY,

    manager_name        text NOT NULL,
    -- NOT UNIQUE, deliberately. Two people can share a name, and forcing
    -- uniqueness here would be a rule about reality that is not true.

    experience_years    smallint NOT NULL CHECK (experience_years >= 0),
    -- "smallint" rather than "integer": up to 32,767, which is ample for
    -- years of experience and uses half the storage. A small point, and the
    -- right habit — choosing the narrowest type that fits the data is how a
    -- schema documents the range it expects.

    qualification       text,
    -- THE ONLY NULL-ABLE COLUMN IN THIS TABLE. No NOT NULL, so it may be
    -- empty, and that is a modelling decision rather than an oversight: not
    -- every manager's qualification is on record.
    --
    -- Which changes how queries must be written. COUNT(qualification) will
    -- not equal COUNT(*), and an inner join on it silently drops managers.
    -- A column that is allowed to be empty is a column every downstream
    -- query has to think about.

    joined_amc_date     date NOT NULL
    -- When they joined this fund house. AMC = Asset Management Company.
);


-- ── TABLE 7 OF 9 · scheme_manager_assignments ──────────────────────────────
--
-- Who managed which scheme, over which period.
--
-- WHY THIS IS NOT JUST A manager_id COLUMN ON schemes: because managers
-- change. A scheme has one manager this year and a different one next year,
-- and often two at once — a primary and a co-manager.
--
-- A single column on schemes could hold only the current manager, and the
-- history would be lost. This table holds one row per period of
-- responsibility, so the question "who was running this fund in March 2024?"
-- has an answer.
--
-- PLANTED PATTERN 3 LIVES HERE. One scheme changes its primary manager
-- partway through the five years, and its performance changes afterwards.
-- Answering "did performance change when the manager changed?" needs this
-- table joined to schemes joined to scheme_plans joined to nav_history —
-- four tables for one question, which is exactly the kind of question this
-- project exists to answer.
--
-- About 70 rows for 40 schemes, because many schemes have had more than one.

CREATE TABLE scheme_manager_assignments (
    assignment_id   integer PRIMARY KEY,

    scheme_id       integer NOT NULL REFERENCES schemes (scheme_id),

    manager_id      integer NOT NULL REFERENCES fund_managers (manager_id),
    -- TWO foreign keys on one table. This is a "join table" — its job is to
    -- connect two other tables in a many-to-many relationship. One scheme
    -- has many managers over time; one manager runs many schemes.

    manager_role    text NOT NULL CHECK (manager_role IN ('Primary', 'Co-manager')),
    -- Which of the two kinds of responsibility. Pattern 3 is about the
    -- PRIMARY manager changing, so any query looking for it has to filter on
    -- this column — a co-manager change is not the same event.

    start_date      date NOT NULL,

    end_date        date,
    -- EMPTY MEANS "STILL MANAGING TODAY". A filled date means the assignment
    -- ended that day.
    --
    -- This is the standard way to record an open-ended period, and it is
    -- worth naming the alternative: storing a far-future date like
    -- 9999-12-31 instead. That would make some queries simpler, and it would
    -- also mean the database asserts something false — that the assignment
    -- definitely ends in the year 9999. NULL says "we do not know when this
    -- ends", which is the truth.
    --
    -- The cost is that every query about current managers must say
    -- "end_date IS NULL", and forgetting it returns past managers too.

    CHECK (end_date IS NULL OR end_date >= start_date)
    -- A TABLE-LEVEL CHECK — it compares two columns, so it cannot live on
    -- either one.
    --
    -- Read it as two allowed cases:
    --   end_date IS NULL          the assignment is still running
    --   end_date >= start_date    it ended on or after it began
    --
    -- Anything else is rejected. An assignment that ended before it started
    -- is impossible, and without this line it would be merely unlikely.
    --
    -- Note the IS NULL comes first and is essential. In SQL, NULL >= anything
    -- is not false — it is NULL, which a CHECK treats as passing. Writing
    -- only "end_date >= start_date" would therefore accept open-ended rows
    -- by accident rather than by design, which happens to be the behaviour
    -- we want but for the wrong reason. Stating both cases makes the
    -- intention explicit.
);


-- ── TABLE 8 OF 9 · nav_history ─────────────────────────────────────────────
--
-- NAV — Net Asset Value — is the price of one unit of a plan on one day.
-- Every return calculation in this domain comes from it:
--
--     one-year return = (NAV today / NAV one year ago) - 1
--
-- THE LARGEST TABLE IN THIS FILE by a wide margin: about 150 plans times
-- roughly 1,250 trading days, so around 170,000 rows. Only transactions in
-- file 04 is bigger.

CREATE TABLE nav_history (
    plan_id     integer NOT NULL REFERENCES scheme_plans (plan_id),
    -- A PLAN, not a scheme. This is the consequence of table 5: the Direct
    -- and Regular versions of the same fund have different prices, so the
    -- price belongs to the plan.

    nav_date    date NOT NULL,
    -- A trading day. NAVs are not published on weekends or market holidays,
    -- so this column has gaps — and those gaps are why the generator rolls
    -- transaction dates forward to the next business day. A transaction on a
    -- Sunday would find no NAV, and the join that attaches a price to it
    -- would silently drop the row.

    nav         numeric(12, 4) NOT NULL CHECK (nav > 0),
    -- The price per unit.
    --
    -- Four decimal places because that is how Indian fund houses publish
    -- NAVs. Not a rounding choice we made — a convention we are matching.
    --
    -- CHECK > 0: a NAV of zero would mean the fund is worthless, and a
    -- negative one is not a thing. It also protects the arithmetic
    -- downstream, where units = amount / nav. A zero here would be a
    -- division by zero in the generator.

    PRIMARY KEY (plan_id, nav_date)
    -- One NAV per plan per day. The same composite-key reasoning as
    -- benchmark_values: neither column is unique alone, the pair is.
    --
    -- And this one has a second benefit. The generator joins transactions to
    -- this table on exactly (plan_id, txn_date), and a primary key creates an
    -- index — so that join, which happens for every one of a million
    -- transactions, uses the index rather than scanning.
);


-- ── TABLE 9 OF 9 · scheme_aum_monthly ──────────────────────────────────────
--
-- AUM — Assets Under Management — is the total money a scheme holds. One row
-- per scheme per month end: 40 schemes times 60 months, so 2,400 rows.
--
-- This is the number an analyst actually asks about. Nobody asks about a
-- single transaction; they ask how big the fund is and whether it is growing.
--
-- ROUTE 1 OF 2 TO AUM
--   The same number can be reached another way: by adding up the market
--   value of everything the scheme holds, in portfolio_holdings in file 02.
--
--   Both routes are kept, on purpose, and they agree — summaries.py computes
--   the holdings as a share of this AUM, so the agreement is arithmetic
--   rather than hoped for. tests/test_stage2_data.py checks it.
--
--   Having two routes creates a real problem for a language model: which one
--   should it use? It must not choose, because the two could diverge on a
--   client's database. So config/metrics.yaml names the approved route, and
--   the model is handed that definition rather than being asked to decide.
--
-- HIDDEN LINK 1 OF 3
--   scheme_id below points at schemes.scheme_id. We deliberately do NOT
--   declare it with REFERENCES.
--
--   Every value in it is a real scheme. The relationship is entirely real.
--   The database has simply not been told — so deleting a scheme would leave
--   these rows pointing at nothing, and PostgreSQL would not object.
--
--   THIS IS THE EASIEST OF THE THREE to discover, because the column name
--   matches the target exactly. Stage 5's same-name rule finds it
--   immediately. The other two have different names on each side and can
--   only be found by comparing values — see file 02's credit_rating_history
--   and file 04's transactions.
--
--   Having one easy and two hard links is what makes the Stage 5 score
--   meaningful: a pipeline that only compares names scores 1 out of 3.
--
--   WHY A REAL DATABASE LOOKS LIKE THIS: a monthly summary table is exactly
--   the kind of thing added three years after the original design, by a
--   different team, in a hurry, to feed a report. Nobody wrote the foreign
--   key. Nothing broke. It has been like that ever since.
--
--   The expected answer is recorded in evals/answer_key/hidden_links.yaml,
--   and the pipeline is never allowed to read that file.

CREATE TABLE scheme_aum_monthly (
    scheme_id       integer NOT NULL,          -- hidden link: no REFERENCES on purpose
    -- Compare this line with category_id in table 4, which says
    -- "REFERENCES scheme_categories (category_id)". Same kind of column,
    -- same kind of relationship, and here the declaration is missing.
    --
    -- tests/test_stage1_schema.py has a test whose entire job is to assert
    -- that this column still has NO foreign key. It asserts an ABSENCE,
    -- because if somebody helpfully "fixed" this line, link discovery would
    -- read the answer out of the catalog and score 3 out of 3 without doing
    -- any work. The course would still appear to run. The lesson would be
    -- gone.

    month_end_date  date NOT NULL
        CHECK (month_end_date = (date_trunc('month', month_end_date) + interval '1 month - 1 day')::date),
    -- Always the LAST DAY OF A MONTH, and the CHECK enforces it.
    --
    -- Read the expression from the inside out:
    --   date_trunc('month', month_end_date)   throw away the day
    --                                         2024-03-17 -> 2024-03-01
    --   + interval '1 month - 1 day'          add a month, take off a day
    --                                         2024-03-01 -> 2024-03-31
    --   ::date                                cast the result back to a date
    --                                         (date_trunc returns a timestamp)
    --   = month_end_date                      insist the original equals it
    --
    -- So the 31st passes in March, the 30th passes in April, and the 29th
    -- passes in February 2024 but not in February 2025. Leap years are
    -- handled by PostgreSQL's own calendar rather than by us.
    --
    -- WHY BOTHER: this table claims to be monthly. If one row carried a
    -- mid-month date, every query joining monthly data would quietly miss
    -- it, and the total would be wrong with nothing to indicate why. The
    -- constraint turns the table's own promise into something the database
    -- enforces.

    aum_amount      numeric(18, 2) NOT NULL CHECK (aum_amount >= 0),
    -- Total money managed, in rupees. The project's money type again.
    --
    -- CHECK >= 0, not > 0: a scheme can legitimately hold nothing — in the
    -- months before it launched, or after everyone has redeemed. Demanding
    -- > 0 would reject correct data.

    folio_count     integer NOT NULL CHECK (folio_count >= 0),
    -- How many investor accounts held this scheme that month.
    --
    -- AN HONEST LIMITATION worth knowing: summaries.py computes this as the
    -- running total of folios ever OPENED in the scheme, so it never goes
    -- down. A folio that was fully redeemed is still counted. That is also
    -- how the real registrar number behaves — a folio stays open with a zero
    -- balance until the investor formally closes it, which most never do.

    PRIMARY KEY (scheme_id, month_end_date)
    -- One row per scheme per month.
    --
    -- Note the first column of this primary key is the undeclared link.
    -- PostgreSQL will happily index and enforce uniqueness on a column it
    -- knows nothing else about — being part of a primary key says nothing
    -- about where the values came from. Which is precisely why the hidden
    -- link is invisible to anything that only reads the catalog.
);