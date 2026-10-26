-- =============================================================================
-- dataplane/schema/03_investors.sql
-- =============================================================================
--
-- WHAT THIS FILE IS FOR
-- ---------------------
-- Creates the 4 "investor" tables. These describe the people who invest,
-- the distributors who sell to them, the accounts they hold, and the monthly
-- investments (SIPs) they have set up.
--
--   1. distributors        -> agents and platforms that sell schemes (OLD-STYLE NAMES)
--   2. investors           -> the people investing (contains personal data)
--   3. folios              -> an investor's account in a scheme
--   4. sip_registrations   -> standing instructions to invest every month
--
-- WHERE THIS FITS
-- ---------------
-- File 3 of 4. Must run after files 01 and 02, because folios and
-- sip_registrations point to schemes and scheme_plans.
--
-- These tables grow with the generator's scale factor. At scale 1:
-- 3,000 distributors, 200,000 investors, 300,000 folios, 120,000 SIPs.
--
-- PERSONAL DATA
-- -------------
-- investors and folios contain personal data (PII): names, PAN, email,
-- phone, date of birth, bank account. Three rules apply, all enforced later:
--   - The generator creates it already masked (e.g. PAN like 'ABCPX****F'),
--     so no realistic personal data exists even in dev.
--   - The columns are listed in config/pii.yaml. Stage 5 never embeds them
--     into pgvector, and the model never sees their values.
--   - They never go into long-term memory.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- CLEAN START (same reasoning as file 01)
-- -----------------------------------------------------------------------------
-- Delete these 4 tables if they already exist (CASCADE also drops links pointing at them).
DROP TABLE IF EXISTS
    sip_registrations,
    folios,
    investors,
    distributors
CASCADE;


-- =============================================================================
-- 1. distributors                                           (about 3,000 rows)
-- =============================================================================
-- Agents, banks and online platforms that sell schemes to investors. Each is
-- registered with AMFI and identified by an ARN (AMFI Registration Number).
--
-- OLD-STYLE TABLE 2 OF 2
-- Short, unclear column names, as in file 02's credit_rating_history:
--     arn_no    -> ARN, the distributor's registration number
--     dist_nm   -> distributor name
--     dist_typ  -> distributor type
--     city_nm   -> city name
--     st_cd     -> state code
--     empnl_dt  -> date the distributor was empanelled (approved to sell)
--     actv_flg  -> active flag, 'Y' or 'N'
--
-- The primary key is the real-world ARN itself, not a separate number.
-- Older systems often did this. It also makes HIDDEN LINK 3 (in file 04)
-- a value-matching problem rather than a name-matching one.
--
-- PLANTED PATTERN 5 lives here: one distributor's transactions are rejected
-- far more often than everyone else's.
CREATE TABLE distributors (
    -- arn_no = the ARN; LIKE 'ARN-%' means it must start with "ARN-" (% = anything after).
    arn_no      text PRIMARY KEY CHECK (arn_no LIKE 'ARN-%'),
    -- dist_nm = the distributor's name.
    dist_nm     text NOT NULL,
    -- dist_typ = the kind of distributor (IFA = Independent Financial Advisor).
    dist_typ    text NOT NULL
        CHECK (dist_typ IN ('Bank', 'National Distributor', 'IFA', 'Online Platform')),
    -- city_nm = the city the distributor is based in.
    city_nm     text NOT NULL,
    -- st_cd = a 2-letter state code, e.g. 'MH'.
    st_cd       text NOT NULL CHECK (length(st_cd) = 2),
    -- empnl_dt = the date the distributor was approved to sell.
    empnl_dt    date NOT NULL,
    -- actv_flg = still active? char(1) = exactly one character, 'Y' or 'N'.
    actv_flg    char(1) NOT NULL DEFAULT 'Y' CHECK (actv_flg IN ('Y', 'N'))
);


-- =============================================================================
-- 2. investors                                            (about 200,000 rows)
-- =============================================================================
-- One row per person. Columns marked PII are listed in config/pii.yaml.
CREATE TABLE investors (
    -- bigint (not integer) for tables that grow with the scale factor.
    -- integer stops at about 2.1 billion; bigint never runs out in practice.
    investor_id     bigint PRIMARY KEY,

    -- Personal details (all PII). email, mobile and date_of_birth may be empty.
    pan             text NOT NULL UNIQUE,    -- PII, stored masked
    full_name       text NOT NULL,           -- PII
    email           text,                    -- PII
    mobile          text,                    -- PII
    date_of_birth   date,                    -- PII

    -- City and state are NOT treated as PII here: on their own they cannot
    -- identify a person, and questions like "SIPs by city" need them.
    city            text NOT NULL,
    state           text NOT NULL,

    -- 1 = large metro, 2 = mid-size city, 3 = smaller town.
    -- PLANTED PATTERN 4 lives here: SIP cancellations rise among
    -- investors in one city tier during the last year.
    city_tier       smallint NOT NULL CHECK (city_tier IN (1, 2, 3)),

    -- KYC ("Know Your Customer") identity check: has the investor been verified?
    kyc_status      text NOT NULL
        CHECK (kyc_status IN ('Verified', 'Pending', 'Rejected')),

    -- The day the investor first joined the fund house.
    onboarded_date  date NOT NULL
);


-- =============================================================================
-- 3. folios                                               (about 300,000 rows)
-- =============================================================================
-- A folio is an investor's account in a scheme. One investor can hold several
-- folios; every transaction happens inside a folio.
CREATE TABLE folios (
    -- The folio's own ID number (bigint because there are many folios).
    folio_id            bigint PRIMARY KEY,

    -- The account number printed on the investor's statement.
    folio_number        text NOT NULL UNIQUE,

    -- Who owns the folio (foreign key to investors).
    investor_id         bigint NOT NULL REFERENCES investors (investor_id),
    -- Which scheme the folio invests in (foreign key to schemes).
    scheme_id           integer NOT NULL REFERENCES schemes (scheme_id),

    -- The day the folio was opened.
    opened_date         date NOT NULL,

    -- How the account is owned: by one person, jointly, or by either holder.
    holding_mode        text NOT NULL
        CHECK (holding_mode IN ('Single', 'Joint', 'Anyone or Survivor')),

    bank_account_masked text                 -- PII, stored masked (e.g. 'XXXXXX4417')
);

-- The foreign keys above do not create indexes by themselves. These make
-- "all folios of this investor" and "all folios in this scheme" fast.
-- CREATE INDEX name ON table (column) builds a lookup list on that column.
CREATE INDEX folios_investor_idx ON folios (investor_id);
CREATE INDEX folios_scheme_idx   ON folios (scheme_id);


-- =============================================================================
-- 4. sip_registrations                                    (about 120,000 rows)
-- =============================================================================
-- A SIP (Systematic Investment Plan) is an instruction to invest a fixed
-- amount every month (or quarter). Each registration here produces one
-- 'SIP' transaction per period in the transactions table.
CREATE TABLE sip_registrations (
    -- The SIP's own ID number.
    sip_id              bigint PRIMARY KEY,
    -- The folio the SIP invests into (foreign key to folios).
    folio_id            bigint NOT NULL REFERENCES folios (folio_id),

    -- A SIP buys into one specific plan (e.g. Direct Growth).
    plan_id             integer NOT NULL REFERENCES scheme_plans (plan_id),

    -- The amount invested each period, in rupees (must be more than 0).
    sip_amount          numeric(18, 2) NOT NULL CHECK (sip_amount > 0),
    -- How often the SIP invests.
    frequency           text NOT NULL CHECK (frequency IN ('Monthly', 'Quarterly')),

    -- The first instalment date; end_date is the last one (if any).
    start_date          date NOT NULL,
    end_date            date,               -- NULL = runs until cancelled

    -- The SIP's current state.
    sip_status          text NOT NULL
        CHECK (sip_status IN ('Active', 'Cancelled', 'Completed', 'Paused')),

    -- The day the SIP was cancelled (only filled in for cancelled SIPs).
    cancelled_date      date,

    -- The distributor who registered the SIP. Declared with REFERENCES,
    -- unlike transactions.arn_code in file 04 (that one is a hidden link).
    -- NULL means the investor registered it directly, with no distributor.
    distributor_arn     text REFERENCES distributors (arn_no),

    -- A cancelled SIP must have a cancellation date, and only a cancelled
    -- SIP may have one. This keeps the two columns consistent.
    CHECK ((sip_status = 'Cancelled') = (cancelled_date IS NOT NULL)),
    -- A SIP cannot end before it starts.
    CHECK (end_date IS NULL OR end_date >= start_date)
);

-- Makes "all SIPs of this folio" fast.
CREATE INDEX sip_registrations_folio_idx ON sip_registrations (folio_id);