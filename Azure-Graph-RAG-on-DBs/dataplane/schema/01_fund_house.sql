-- =============================================================================
-- dataplane/schema/01_fund_house.sql
-- =============================================================================
--
-- WHAT THIS FILE IS FOR
-- ---------------------
-- Creates the 9 "fund house" tables. These describe the mutual fund company's
-- own products: which schemes it runs, the plans inside each scheme, who
-- manages them, what they are compared against, and their daily prices.
--
--   1. scheme_categories           -> the SEBI category of a scheme (Large Cap, Liquid, ...)
--   2. benchmarks                  -> the index a scheme is compared against (e.g. a Nifty-style index)
--   3. benchmark_values            -> the daily value of each benchmark
--   4. schemes                     -> the funds themselves
--   5. scheme_plans                -> Direct/Regular x Growth/IDCW versions of each scheme
--   6. fund_managers               -> the people who manage schemes
--   7. scheme_manager_assignments  -> who managed which scheme, and when
--   8. nav_history                 -> the daily price (NAV) of each plan
--   9. scheme_aum_monthly          -> total money managed per scheme, per month end
--
-- WHERE THIS FITS
-- ---------------
-- This is file 1 of 4. apply_schema.py runs them in order 01 -> 04, because
-- later files point to tables created here (for example, portfolio_holdings in
-- file 02 points to schemes, which is created in this file).
--
-- WHICH DATABASE
-- --------------
-- These tables go into mf_data, the database that stands in for the client's
-- own fund database. They do NOT go into agent_meta.
--
-- A NOTE ON THE COMMENTS IN THIS FILE
-- -----------------------------------
-- Comments starting with "--" live only in this file. They are NOT stored in
-- the database. That matters: in Stage 5 our pipeline reads the database to
-- write its own notes about each table, exactly as it will on the client's
-- database. If we stored descriptions in the database, the pipeline would be
-- copying our answers instead of working them out, and its score would mean
-- nothing.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- CLEAN START
-- -----------------------------------------------------------------------------
-- While building, we will run this file many times. These lines delete the
-- tables if they already exist, so each run starts from empty.
--
-- IF EXISTS -> do not fail on the very first run, when the tables do not exist yet.
-- CASCADE   -> also remove anything in other files that points at these tables
--              (for example, the link from portfolio_holdings to schemes).
--              Those other files recreate their own tables and links anyway.
--
-- This is safe ONLY because mf_data holds synthetic data. The client deletes
-- the whole dataplane/ folder; this file is never run on their database.
-- DROP TABLE removes each table listed below (one name per line, separated by commas).
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
-- CASCADE (explained above) ends the DROP statement; ";" marks the end of an SQL statement.
CASCADE;


-- =============================================================================
-- 1. scheme_categories                                         (about 36 rows)
-- =============================================================================
-- SEBI puts every mutual fund scheme into one category, such as "Large Cap
-- Fund" or "Liquid Fund". Questions like "net flows into debt funds" are
-- answered by grouping schemes through this table.
-- CREATE TABLE makes a new, empty table. Each line inside the brackets is one column:
--   column_name   data_type   rules
CREATE TABLE scheme_categories (
    -- The table's own ID number. Other tables store this number to say
    -- "this scheme belongs to category 7".
    -- PRIMARY KEY means: must be filled in, and no two rows may share it.
    category_id     integer PRIMARY KEY,

    -- A short code a person can read, e.g. 'EQ-LC' for Equity Large Cap.
    -- UNIQUE: two categories may not share a code.
    category_code   text NOT NULL UNIQUE,

    -- The full name, e.g. 'Large Cap Fund'.
    category_name   text NOT NULL UNIQUE,

    -- The broad group the category belongs to.
    -- CHECK stops any value outside this list from being saved, so a typo
    -- like 'Equtiy' is rejected instead of silently creating a sixth group.
    asset_class     text NOT NULL
        CHECK (asset_class IN ('Equity', 'Debt', 'Hybrid', 'Solution Oriented', 'Other')),

    -- SEBI's riskometer level. Six fixed levels, lowest to highest.
    risk_level      text NOT NULL
        CHECK (risk_level IN ('Low', 'Low to Moderate', 'Moderate',
                              'Moderately High', 'High', 'Very High'))
-- The closing bracket and ";" finish the CREATE TABLE statement.
);


-- =============================================================================
-- 2. benchmarks                                                (about 15 rows)
-- =============================================================================
-- Every scheme is compared against an index. A scheme "beat its benchmark"
-- if its return was higher than the index's return over the same period.
-- Names are fictitious, so nothing in the demo can be mistaken for a real index.
CREATE TABLE benchmarks (
    -- The benchmark's own ID number (unique, never empty).
    benchmark_id    integer PRIMARY KEY,
    -- The benchmark's name. text = any length of characters; NOT NULL = must be filled in.
    benchmark_name  text NOT NULL UNIQUE,

    -- What kind of market the index tracks. Used to check that an equity
    -- scheme is compared against an equity index, not a bond index.
    benchmark_type  text NOT NULL
        CHECK (benchmark_type IN ('Equity', 'Debt', 'Hybrid')),

    -- The company that publishes the index.
    provider        text NOT NULL
);


-- =============================================================================
-- 3. benchmark_values                                      (about 18,750 rows)
-- =============================================================================
-- One row per benchmark per trading day: 15 benchmarks x about 1,250 trading
-- days in 5 years.
CREATE TABLE benchmark_values (
    -- REFERENCES benchmarks -> this is a foreign key. The database refuses a
    -- value here unless that benchmark_id exists in the benchmarks table.
    -- It prevents "orphan" rows pointing at a benchmark that does not exist.
    benchmark_id    integer NOT NULL REFERENCES benchmarks (benchmark_id),

    -- "date", not "timestamptz": this is a business date (a trading day),
    -- with no time of day and no time zone.
    value_date      date NOT NULL,

    -- The index level on that day. numeric(14,4) = up to 14 digits in total,
    -- 4 of them after the decimal point. numeric stores exact decimals.
    -- CHECK > 0: an index can never be zero or negative.
    index_value     numeric(14, 4) NOT NULL CHECK (index_value > 0),

    -- A benchmark has only one value per day. Making the pair
    -- (benchmark_id, value_date) the primary key enforces that.
    PRIMARY KEY (benchmark_id, value_date)
);


-- =============================================================================
-- 4. schemes                                                   (about 40 rows)
-- =============================================================================
-- The funds themselves, e.g. "Sahyadri Credit Risk Fund".
CREATE TABLE schemes (
    -- The scheme's own ID number. Many other tables point to it.
    scheme_id       integer PRIMARY KEY,

    -- The fund house's internal code for the scheme.
    scheme_code     text NOT NULL UNIQUE,

    -- The full scheme name. This is what users type in questions, and in
    -- Stage 5 these names are embedded into pgvector so that a question
    -- saying "Sahyadri credit fund" can be matched to this exact row.
    scheme_name     text NOT NULL UNIQUE,

    -- Which SEBI category this scheme belongs to (foreign key).
    category_id     integer NOT NULL REFERENCES scheme_categories (category_id),

    -- Which index this scheme is compared against (foreign key).
    benchmark_id    integer NOT NULL REFERENCES benchmarks (benchmark_id),

    -- The day the scheme opened to investors. Used by the validation checks:
    -- no transaction may be dated before its scheme's launch_date.
    launch_date     date NOT NULL,

    -- Is the scheme still running? DEFAULT 'Active' is used when no value is given.
    scheme_status   text NOT NULL DEFAULT 'Active'
        CHECK (scheme_status IN ('Active', 'Merged', 'Closed')),

    -- Charge for redeeming early, as a percentage (1.0000 = 1%).
    -- numeric(7,4) = up to 999.9999; 4 decimals is enough for any real rate.
    exit_load_pct   numeric(7, 4) NOT NULL DEFAULT 0 CHECK (exit_load_pct >= 0),

    -- Smallest SIP instalment allowed, in rupees. numeric(18,2) = rupees and paise.
    min_sip_amount  numeric(18, 2) NOT NULL CHECK (min_sip_amount > 0),

    -- When this row was created in the system. "timestamptz" (not "date")
    -- because this is a system time, with a time of day and a time zone.
    created_at      timestamptz NOT NULL DEFAULT now()
);


-- =============================================================================
-- 5. scheme_plans                                             (about 150 rows)
-- =============================================================================
-- Each scheme is sold in up to 4 versions:
--   Direct  or Regular -> bought directly, or through a distributor (who earns commission)
--   Growth  or IDCW    -> profits stay invested, or are paid out
-- Each version has its own NAV, its own expense ratio and its own ISIN.
-- Transactions, NAVs and SIPs all point to a PLAN, not to a scheme.
CREATE TABLE scheme_plans (
    -- The plan's own ID number.
    plan_id             integer PRIMARY KEY,
    -- Which scheme this plan belongs to (foreign key to schemes).
    scheme_id           integer NOT NULL REFERENCES schemes (scheme_id),

    -- Direct (no distributor) or Regular (through a distributor).
    plan_type           text NOT NULL CHECK (plan_type IN ('Direct', 'Regular')),
    -- Growth (profits reinvested) or IDCW (profits paid out to the investor).
    option_type         text NOT NULL CHECK (option_type IN ('Growth', 'IDCW')),

    -- ISIN: the 12-character international code that identifies this plan
    -- on exchanges and registrars. Real-world codes are kept in their own
    -- column, separate from plan_id, so our internal numbers never depend
    -- on outside codes.
    isin                text NOT NULL UNIQUE CHECK (length(isin) = 12),

    -- Yearly fee as a percentage of the money invested (0.7500 = 0.75%).
    -- Regular plans cost more than Direct plans, because of the commission.
    expense_ratio_pct   numeric(7, 4) NOT NULL CHECK (expense_ratio_pct >= 0),

    -- A scheme cannot have two "Direct Growth" plans.
    UNIQUE (scheme_id, plan_type, option_type)
);


-- =============================================================================
-- 6. fund_managers                                             (about 25 rows)
-- =============================================================================
-- The people who decide what each scheme buys and sells.
CREATE TABLE fund_managers (
    -- The manager's own ID number.
    manager_id          integer PRIMARY KEY,
    -- The manager's full name.
    manager_name        text NOT NULL,
    -- Years of experience. smallint = a small whole number; it can never be negative.
    experience_years    smallint NOT NULL CHECK (experience_years >= 0),
    -- Degrees / certificates, e.g. 'CFA'. No NOT NULL, so it may be left empty.
    qualification       text,
    -- The day the manager joined this fund house (AMC = Asset Management Company).
    -- No comma after the last column, because nothing follows it.
    joined_amc_date     date NOT NULL
);


-- =============================================================================
-- 7. scheme_manager_assignments                                (about 70 rows)
-- =============================================================================
-- Who managed which scheme, and for which period. One scheme can have
-- several managers over time, and one manager can run several schemes.
--
-- PLANTED PATTERN 3 lives here: one scheme changes its primary manager
-- partway through the 5 years, and its performance changes after that date.
CREATE TABLE scheme_manager_assignments (
    -- The assignment's own ID number.
    assignment_id   integer PRIMARY KEY,
    -- Which scheme was managed (foreign key to schemes).
    scheme_id       integer NOT NULL REFERENCES schemes (scheme_id),
    -- Who managed it (foreign key to fund_managers).
    manager_id      integer NOT NULL REFERENCES fund_managers (manager_id),

    -- The lead manager ('Primary') or a helper ('Co-manager').
    manager_role    text NOT NULL CHECK (manager_role IN ('Primary', 'Co-manager')),

    -- The first day of this assignment.
    start_date      date NOT NULL,

    -- NULL (empty) means "still managing today". A filled date means the
    -- assignment ended that day.
    end_date        date,

    -- An assignment cannot end before it starts.
    CHECK (end_date IS NULL OR end_date >= start_date)
);


-- =============================================================================
-- 8. nav_history                                          (about 170,000 rows)
-- =============================================================================
-- NAV (Net Asset Value) is the price of one unit of a plan on a given day.
-- Returns are calculated from it: 1-year return = (NAV today / NAV a year ago) - 1.
CREATE TABLE nav_history (
    -- Which plan this price is for (foreign key to scheme_plans).
    plan_id     integer NOT NULL REFERENCES scheme_plans (plan_id),
    -- The trading day of this price.
    nav_date    date NOT NULL,

    -- numeric(12,4): NAVs are published to 4 decimal places.
    nav         numeric(12, 4) NOT NULL CHECK (nav > 0),

    -- One NAV per plan per day.
    PRIMARY KEY (plan_id, nav_date)
);


-- =============================================================================
-- 9. scheme_aum_monthly                                      (about 2,400 rows)
-- =============================================================================
-- AUM (Assets Under Management): the total money a scheme manages, at each
-- month end. 40 schemes x 60 months = 2,400 rows.
--
-- ROUTE 1 OF 2 TO AUM
-- AUM can also be worked out by adding up the market value of a scheme's
-- holdings in portfolio_holdings (file 02). The two routes are kept on
-- purpose: in Stage 5, metrics.yaml says which one is the approved way to
-- answer "what is the AUM of X", so the model never has to guess.
--
-- HIDDEN LINK 1 OF 3
-- scheme_id below points to schemes.scheme_id, but we deliberately do NOT
-- declare it with REFERENCES. Real client databases often have links like
-- this that nobody declared. Stage 5 must find it on its own; this one is
-- findable because the column name matches exactly.
-- The expected answer is recorded in evals/answer_key/hidden_links.yaml.
CREATE TABLE scheme_aum_monthly (
    scheme_id       integer NOT NULL,          -- hidden link: no REFERENCES on purpose

    -- Always the last day of a month.
    month_end_date  date NOT NULL
        CHECK (month_end_date = (date_trunc('month', month_end_date) + interval '1 month - 1 day')::date),

    -- Total money managed, in rupees.
    aum_amount      numeric(18, 2) NOT NULL CHECK (aum_amount >= 0),

    -- How many investor accounts (folios) held this scheme that month.
    folio_count     integer NOT NULL CHECK (folio_count >= 0),

    -- One AUM row per scheme per month end.
    PRIMARY KEY (scheme_id, month_end_date)
);