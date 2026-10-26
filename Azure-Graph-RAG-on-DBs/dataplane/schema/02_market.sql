-- =============================================================================
-- dataplane/schema/02_market.sql
-- =============================================================================
--
-- WHAT THIS FILE IS FOR
-- ---------------------
-- Creates the 6 "market" tables. These describe what the schemes invest in:
-- the companies (issuers), the shares and bonds they issue (securities), the
-- credit ratings of those bonds over time, and how much of each security
-- every scheme holds at each month end.
--
--   1. sectors                -> industry of a company (Banking, Power, ...)
--   2. issuer_groups          -> business group a company belongs to
--   3. issuers                -> the companies and governments that issue securities
--   4. securities             -> individual shares, bonds, commercial paper, ...
--   5. credit_rating_history  -> rating changes on each security over time (OLD-STYLE NAMES)
--   6. portfolio_holdings     -> what each scheme holds at each month end
--
-- WHERE THIS FITS
-- ---------------
-- File 2 of 4. Must run after 01_fund_house.sql, because portfolio_holdings
-- points to schemes, which is created in file 01.
--
-- These tables make the multi-hop questions possible, which are the ones the
-- graph exists for. For example: "Which schemes hold bonds that were
-- downgraded?" needs the route
--     schemes -> portfolio_holdings -> securities -> credit_rating_history
-- The AGE graph in Stage 5 stores that route, so the model does not have to
-- guess how these tables connect.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- CLEAN START (same reasoning as file 01)
-- -----------------------------------------------------------------------------
-- Delete these 6 tables if they already exist (CASCADE also drops links pointing at them).
DROP TABLE IF EXISTS
    portfolio_holdings,
    credit_rating_history,
    securities,
    issuers,
    issuer_groups,
    sectors
CASCADE;


-- =============================================================================
-- 1. sectors                                                   (about 25 rows)
-- =============================================================================
-- The industry a company works in, e.g. Banking or Power.
CREATE TABLE sectors (
    -- The sector's own ID number (unique, never empty).
    sector_id       integer PRIMARY KEY,
    -- A short readable code for the sector; no two sectors may share one.
    sector_code     text NOT NULL UNIQUE,
    -- The sector's full name, e.g. 'Banking'.
    sector_name     text NOT NULL UNIQUE
);


-- =============================================================================
-- 2. issuer_groups                                             (about 60 rows)
-- =============================================================================
-- Many companies belong to one business group. Regulators care about the
-- total a scheme has invested in one GROUP, not just in one company.
--
-- PLANTED PATTERN 2 lives here: one scheme has an unusually high share of
-- its money in companies from a single group.
CREATE TABLE issuer_groups (
    -- The business group's own ID number.
    group_id        integer PRIMARY KEY,
    -- The group's name. Also embedded in Stage 5 so questions can name it loosely.
    group_name      text NOT NULL UNIQUE
);


-- =============================================================================
-- 3. issuers                                                  (about 800 rows)
-- =============================================================================
-- Companies, banks and governments that issue securities.
CREATE TABLE issuers (
    -- The issuer's own ID number.
    issuer_id       integer PRIMARY KEY,

    -- Names are embedded into pgvector in Stage 5, so a question can name
    -- a company loosely and still be matched to the right row.
    issuer_name     text NOT NULL UNIQUE,

    -- Every issuer has a sector. Not every issuer belongs to a group, so
    -- group_id may be empty (NULL).
    group_id        integer REFERENCES issuer_groups (group_id),
    -- Which sector this issuer is in (foreign key, must be filled in).
    sector_id       integer NOT NULL REFERENCES sectors (sector_id),

    -- What kind of issuer this is. PSU = Public Sector Undertaking (government-owned
    -- company); NBFC = Non-Banking Financial Company.
    issuer_type     text NOT NULL
        CHECK (issuer_type IN ('Corporate', 'PSU', 'Bank', 'NBFC', 'Government', 'Other'))
);


-- =============================================================================
-- 4. securities                                             (about 1,500 rows)
-- =============================================================================
-- Each share, bond or money-market paper a scheme can hold.
CREATE TABLE securities (
    -- The security's own ID number.
    security_id     integer PRIMARY KEY,

    -- The code used by other systems to refer to this security.
    -- credit_rating_history refers to securities by THIS code, not by
    -- security_id (see HIDDEN LINK 2 below).
    security_code   text NOT NULL UNIQUE,

    -- The 12-character international code for this security.
    isin            text NOT NULL UNIQUE CHECK (length(isin) = 12),

    -- Which company/government issued it (foreign key to issuers).
    issuer_id       integer NOT NULL REFERENCES issuers (issuer_id),

    -- What kind of security it is (share, bond, money-market paper, ...).
    security_type   text NOT NULL
        CHECK (security_type IN ('Equity', 'Corporate Bond', 'Commercial Paper',
                                 'Certificate of Deposit', 'Government Security',
                                 'Treasury Bill')),

    -- Interest rate for bonds, as a percentage. Empty (NULL) for shares,
    -- which do not pay a fixed interest.
    coupon_rate_pct numeric(7, 4) CHECK (coupon_rate_pct >= 0),

    -- When a bond repays its money. Empty (NULL) for shares, which never mature.
    maturity_date   date
);


-- =============================================================================
-- 5. credit_rating_history                                  (about 2,000 rows)
-- =============================================================================
-- Every rating given to a bond, by a rating agency, over time. A drop in
-- rating (for example AA -> BBB) is a "downgrade".
--
-- OLD-STYLE TABLE 1 OF 2
-- The columns use short, unclear names, the way older systems often did:
--     rtg_id    -> rating record ID
--     sec_cd    -> security code
--     rtg_agcy  -> rating agency
--     rtg       -> the rating itself (AAA, AA+, ...)
--     rtg_dt    -> date the rating was given
--     outlk     -> outlook
--     upd_ts    -> time the row was last updated
-- This tests Stage 5: the notes it writes must explain these names clearly
-- enough that the SQL-writing model uses the right column.
--
-- Names are kept lowercase. In Postgres, a name with capital letters must be
-- wrapped in double quotes in every query forever; lowercase names never do.
--
-- HIDDEN LINK 2 OF 3
-- sec_cd holds the same values as securities.security_code, but there is no
-- REFERENCES and the names do not match. Stage 5 can find it only by
-- checking that the VALUES in the two columns overlap.
--
-- PLANTED PATTERN 1 lives here: one bond is downgraded, and in the weeks
-- after that date, redemptions spike in the schemes that hold it.
CREATE TABLE credit_rating_history (
    -- rtg_id = the rating record's own ID number.
    rtg_id      integer PRIMARY KEY,

    -- sec_cd = which security was rated (same values as securities.security_code).
    sec_cd      text NOT NULL,               -- hidden link: no REFERENCES on purpose

    -- rtg_agcy = the rating agency that gave the rating.
    rtg_agcy    text NOT NULL,

    -- Standard long-term rating scale, best (AAA) to default (D).
    rtg         text NOT NULL
        CHECK (rtg IN ('AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-',
                       'BBB+', 'BBB', 'BBB-', 'BB+', 'BB', 'BB-', 'B', 'C', 'D')),

    -- rtg_dt = the date the rating was given.
    rtg_dt      date NOT NULL,

    -- outlk = the agency's outlook (may be empty).
    outlk       text CHECK (outlk IN ('Stable', 'Positive', 'Negative', 'Watch')),

    -- upd_ts = when the row was last updated; now() fills in the current time.
    upd_ts      timestamptz NOT NULL DEFAULT now()
);


-- =============================================================================
-- 6. portfolio_holdings                                   (about 144,000 rows)
-- =============================================================================
-- What each scheme holds at each month end: 40 schemes x about 60 securities
-- x 60 months.
--
-- ROUTE 2 OF 2 TO AUM
-- Adding up market_value for one scheme at one month end gives its AUM.
-- scheme_aum_monthly (file 01) is the other route.
--
-- PLANTED PATTERN 6 lives here: one scheme has more than 10% of its money in
-- a single issuer, which breaks the usual regulatory limit.
CREATE TABLE portfolio_holdings (
    -- Which scheme holds the security (foreign key to schemes).
    scheme_id       integer NOT NULL REFERENCES schemes (scheme_id),
    -- Which security is held (foreign key to securities).
    security_id     integer NOT NULL REFERENCES securities (security_id),

    -- Always a month end, like scheme_aum_monthly.
    as_of_date      date NOT NULL,

    -- How many units, shares or bonds of the security are held.
    quantity        numeric(18, 3) NOT NULL CHECK (quantity > 0),

    -- What those holdings were worth that day, in rupees.
    market_value    numeric(18, 2) NOT NULL CHECK (market_value >= 0),

    -- Share of the scheme's total money in this security (12.5000 = 12.5%).
    -- The validation check in Stage 3 confirms that for each scheme and
    -- month, these add up to 100.
    weight_pct      numeric(7, 4) NOT NULL CHECK (weight_pct >= 0 AND weight_pct <= 100),

    -- A scheme holds a given security only once per month end.
    PRIMARY KEY (scheme_id, security_id, as_of_date)
);

-- An index is a lookup list the database keeps alongside the table, so it
-- can find matching rows without reading every row.
-- The primary key above already gives fast lookups by scheme. This second
-- index gives fast lookups by security, which the multi-hop question needs:
-- "which schemes hold THIS downgraded bond?"
CREATE INDEX portfolio_holdings_security_idx ON portfolio_holdings (security_id, as_of_date);