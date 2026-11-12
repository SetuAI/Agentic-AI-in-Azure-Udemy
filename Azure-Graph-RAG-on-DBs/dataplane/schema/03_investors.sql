-- ############################################################################
-- dataplane/schema/03_investors.sql
--
-- PURPOSE:
--   Creates the 4 "investor" tables — the demand side. Files 01 and 02
--   described the funds and what they invest in; nobody has put in a rupee
--   yet. This file creates the people who do, and the agents who sell to
--   them.
--
--   This is FILE 3 OF 4. It must run after files 01 and 02, because folios
--   and sip_registrations point at schemes and scheme_plans.
--
--   1. distributors        ~3,000 rows    agents and platforms (OLD-STYLE NAMES)
--   2. investors         ~200,000 rows    the people (CONTAINS PERSONAL DATA)
--   3. folios            ~300,000 rows    an investor's account in one scheme
--   4. sip_registrations ~120,000 rows    standing monthly instructions
--
--   Those counts are at the generator's full scale of 1.0. At the default
--   0.2 it is 600 distributors, 40,000 investors, ~60,000 folios and 24,000
--   SIPs.
--
-- WHAT PROBLEM DOES THIS SOLVE:
--   THESE ARE THE ONLY TABLES THAT GROW WITH SCALE. The fund house has 40
--   schemes and the market has 1,500 securities whatever the scale factor —
--   a fund house does not launch more schemes because more people buy. But
--   investors, their accounts and their SIPs grow without limit, and
--   everything in file 04 hangs off them.
--
--   Which is why the id types change in this file. Up to now every primary
--   key has been `integer`. Here they become `bigint`, and the reason is
--   below on investor_id.
--
-- HOW THE 4 TABLES CONNECT:
--
--   investors ──> folios ──> sip_registrations
--                   │  │            │
--                   │  └────────────┴──> distributors
--                   │                    (declared on sip_registrations,
--                   │                     and NOT declared on file 04's
--                   │                     transactions — hidden link 3)
--                   │
--                   └──> schemes (file 01)
--
--   One investor opens 1 to 3 folios. Each folio is in exactly one scheme,
--   and may have one SIP. A Regular-plan folio was sold by a distributor; a
--   Direct-plan folio was not.
--
-- PERSONAL DATA — READ THIS BEFORE RUNNING ANYTHING:
--   `investors` and `folios` hold personal data: name, PAN, email, mobile,
--   date of birth, bank account. PAN is India's tax identifier.
--
--   THREE SEPARATE PROTECTIONS APPLY, and we use all three rather than
--   relying on any one of them:
--
--     1. The generator creates these values ALREADY MASKED. A PAN here looks
--        like 'ABCPX****F' — right shape, middle digits absent. A mobile is
--        '9XXXXXX417'. There is no later step that masks them; they are
--        never real in the first place. So a full copy of this database on a
--        student's laptop holds nothing resembling a person's identity.
--
--     2. The columns are listed in config/pii.yaml. Stage 5 never turns
--        their values into embeddings and never sends them to a model. So
--        even masked, they do not travel.
--
--     3. They never enter long-term conversation memory.
--
--   Masking at generation and listing in pii.yaml are not alternatives. The
--   first protects the data at rest; the second protects it in transit to a
--   model. A real client's database would have only the second, which is
--   exactly why the second has to work.
--
-- WHICH PLANTED PATTERNS LIVE HERE:
--   PATTERN 4 — SIP cancellations rise sharply among investors in one city
--               tier, from a certain month onwards. Needs
--               sip_registrations -> folios -> investors to find.
--   PATTERN 5 — one distributor's transactions are rejected far more often
--               than everyone else's. The distributor is created here; the
--               rejections are written in file 04.
--
-- HOW TO RUN:
--   Never on its own — apply_schema.py runs all four files in one
--   transaction:
--
--       uv run python dataplane/schema/apply_schema.py
-- ############################################################################


-- ── CLEAN START ────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS
    sip_registrations,
    folios,
    investors,
    distributors
CASCADE;
-- Reverse dependency order again: sip_registrations points at folios, which
-- points at investors, which is independent. So they go in that order.
--
-- Same IF EXISTS and CASCADE reasoning as file 01, and the same warning:
-- this deletes data.


-- ── TABLE 1 OF 4 · distributors ────────────────────────────────────────────
--
-- The agents, banks and online platforms that sell schemes to investors.
--
-- In India every one of them is registered with AMFI — the Association of
-- Mutual Funds in India — and identified by an ARN, an AMFI Registration
-- Number. That ARN, rather than a name, is what appears on a transaction.
--
-- OLD-STYLE TABLE 2 OF 2
--   Short, unclear column names, exactly as in file 02's
--   credit_rating_history:
--
--     arn_no    ->  the ARN, the distributor's registration number
--     dist_nm   ->  distributor name
--     dist_typ  ->  distributor type
--     city_nm   ->  city name
--     st_cd     ->  state code
--     empnl_dt  ->  empanelment date — the day AMFI approved them to sell
--     actv_flg  ->  active flag, 'Y' or 'N'
--
--   `empnl_dt` is the one worth dwelling on. Nothing in that name says
--   "empanelment", and nothing says what empanelment is. A model will guess
--   "employment date" or "enrolment date", and it will be close enough to
--   sound right and wrong enough to matter.
--
--   A human writes "the date AMFI registered this distributor to sell mutual
--   funds" once, approves it, and from then on the agent knows. The schema
--   cannot explain itself; a reviewed note can.
--
-- PLANTED PATTERN 5 LIVES HERE. One distributor — with a fixed, known ARN —
-- has about a 17% transaction rejection rate against a 2% background. The
-- generator makes it one of the larger distributors on purpose, so the rate
-- is computed over thousands of transactions and is statistically solid
-- rather than a few unlucky rows.

CREATE TABLE distributors (
    arn_no      text PRIMARY KEY CHECK (arn_no LIKE 'ARN-%'),
    -- THE PRIMARY KEY IS TEXT, NOT AN INTEGER, and it is the real-world ARN
    -- itself rather than a number we invented.
    --
    -- Everywhere else in this database the key is an integer we control.
    -- Here it is an outside identifier, because that is what older registry
    -- systems did — and because the real world already has a unique id for a
    -- distributor, inventing a second one adds a column and guarantees
    -- nothing.
    --
    -- AND IT IS WHAT MAKES HIDDEN LINK 3 A VALUE-MATCHING PROBLEM. File 04's
    -- transactions stores this ARN in a column called `arn_code`. Two
    -- different names for the same thing, both text, with no foreign key —
    -- so the only way to discover the link is to compare the values. If the
    -- key here were an integer id, the link would be an integer-to-integer
    -- comparison and far easier to spot.
    --
    -- CHECK (arn_no LIKE 'ARN-%')
    --   The % means "anything after this", so every value must start with
    --   'ARN-'. A bare number is rejected.
    --
    --   This is the first LIKE in a CHECK in the project. It encodes a fact
    --   about the format of an outside identifier, so a malformed value is
    --   refused at insertion rather than discovered by whoever tries to join
    --   on it later.

    dist_nm     text NOT NULL,
    -- Distributor name. NOT UNIQUE — two advisory firms can share a name.

    dist_typ    text NOT NULL
        CHECK (dist_typ IN ('Bank', 'National Distributor', 'IFA', 'Online Platform')),
    -- What kind of seller. IFA is an Independent Financial Advisor — an
    -- individual, and about 85% of the Indian distribution business.
    --
    -- The generator forces pattern 5's distributor to be an IFA. A bank with
    -- a 17% rejection rate is not a believable story; an individual advisor
    -- with sloppy mandate paperwork is. The story has to survive a student
    -- reading the row.

    city_nm     text NOT NULL,

    st_cd       text NOT NULL CHECK (length(st_cd) = 2),
    -- The two-letter state code — 'MH' for Maharashtra, 'KA' for Karnataka.
    --
    -- NOTE THE INCONSISTENCY WITH THE NEXT TABLE, and it is deliberate.
    -- `investors` stores the FULL state name in a column called `state`.
    -- This table stores a two-letter CODE in a column called `st_cd`.
    --
    -- The same information, in two shapes, under two names, in two tables in
    -- the same database. Real databases are like this, because the two
    -- tables were built by different people at different times. Any question
    -- that spans both has to reconcile them, and nothing in the schema says
    -- how.

    empnl_dt    date NOT NULL,
    -- The day AMFI empanelled them.
    --
    -- The generator places these in 2008 onwards, so every distributor was
    -- empanelled BEFORE the five-year transaction window opens. An agent
    -- whose empanelment date falls after a transaction they sold would be an
    -- obvious data bug, and one a student would spot.

    actv_flg    char(1) NOT NULL DEFAULT 'Y' CHECK (actv_flg IN ('Y', 'N'))
    -- Whether they are still selling. 'Y' or 'N'.
    --
    -- char(1) AND A 'Y'/'N' FLAG, NOT A BOOLEAN. PostgreSQL has a proper
    -- boolean type, and this column does not use it — again because that is
    -- what registry systems actually do.
    --
    -- The cost is real: a query must say "actv_flg = 'Y'" rather than
    -- "is_active", and a model has to be told that 'Y' means yes. Another
    -- thing the metadata layer has to explain.
    --
    -- DEFAULT 'Y' means an insert that omits this column gets an active
    -- distributor rather than an empty flag.
);


-- ── TABLE 2 OF 4 · investors ───────────────────────────────────────────────
--
-- One row per person. About 200,000 at full scale, 40,000 at the default.
--
-- THIS IS THE TABLE THAT HOLDS PERSONAL DATA. Every such column is marked
-- below and is listed in config/pii.yaml.

CREATE TABLE investors (
    investor_id     bigint PRIMARY KEY,
    -- bigint, NOT integer — and this is the first bigint in the project.
    --
    -- integer stops at about 2.1 billion. bigint goes to about 9.2 billion
    -- billion, which in practice never runs out.
    --
    -- WHY THE CHANGE HERE: this is the first table that grows without bound.
    -- 40 schemes will never need more than an integer. An investor table in
    -- a real fund house, with years of history and closed accounts never
    -- deleted, genuinely can approach the integer limit — and the day it
    -- does, every table pointing at it has to be altered too, on a live
    -- system, under pressure.
    --
    -- The cost of bigint is four extra bytes per row. The cost of running out
    -- of integers is an outage. This is the cheapest insurance in the file.

    pan             text NOT NULL UNIQUE,    -- PII, stored masked
    -- PAN — Permanent Account Number — is India's tax identifier, and the
    -- real unique identifier of an investor. Two people share a name; nobody
    -- shares a PAN.
    --
    -- STORED MASKED: 'ABCPX****F'. The real format is 5 letters, 4 digits, 1
    -- letter, with the 4th letter 'P' for an individual person. The
    -- generator builds the LETTERS from the investor_id, which makes them
    -- unique by construction, and replaces the DIGITS with asterisks, which
    -- makes the whole thing unusable as an identity.
    --
    -- UNIQUE is therefore still enforceable even though the value is masked,
    -- which is the point of masking it that way rather than replacing it
    -- wholesale.

    full_name       text NOT NULL,           -- PII
    -- NOT UNIQUE, deliberately. Real investor tables are full of repeated
    -- names — which is exactly why PAN exists as the identifier and the name
    -- does not.

    email           text,                    -- PII
    mobile          text,                    -- PII
    date_of_birth   date,                    -- PII
    -- ALL THREE ARE NULL-ABLE, and that is realistic rather than careless.
    -- An email was never captured, a mobile is missing, a date of birth was
    -- skipped. The generator leaves about 1% of each empty on purpose.
    --
    -- WHY DELIBERATELY INCOMPLETE DATA: because if every optional column
    -- were 100% filled, a student's SQL would never need a NULL check, and
    -- they would write queries that fall over on the first real database
    -- they touch. It also makes COUNT(email) differ from COUNT(*), which is a
    -- lesson in itself.
    --
    -- Note the columns above these three — pan, full_name — are NOT NULL, so
    -- the generator never passes them through its blanking step. Doing so
    -- would make the load fail.

    city            text NOT NULL,
    state           text NOT NULL,
    -- CITY AND STATE ARE NOT TREATED AS PERSONAL DATA HERE, and the
    -- reasoning is worth stating because it is a judgement rather than a
    -- rule.
    --
    -- On their own they cannot identify a person — a city has millions of
    -- people in it. And questions genuinely need them: "where are we losing
    -- SIPs?" is a city-level question, and pattern 4 cannot be found without
    -- geography.
    --
    -- They are not in pii.yaml, so their values ARE embedded and a model
    -- does see them. That is a deliberate line, and it is the kind of line
    -- worth drawing explicitly rather than by default.

    city_tier       smallint NOT NULL CHECK (city_tier IN (1, 2, 3)),
    -- 1 = large metro, 2 = mid-size city, 3 = smaller town. Indian cities are
    -- classified this way, and it drives everything from distribution
    -- strategy to pricing.
    --
    -- PLANTED PATTERN 4 LIVES ON THIS COLUMN. From a certain month onwards,
    -- tier-3 investors cancel their SIPs at about three and a half times the
    -- normal rate.
    --
    -- Storing the tier directly, rather than deriving it from the city name,
    -- is what makes the pattern answerable without an external lookup of
    -- which city is which tier. A question needing that lookup would be
    -- unanswerable from the database alone.
    --
    -- smallint because there are exactly three values and there always will
    -- be. The CHECK makes a fourth impossible.

    kyc_status      text NOT NULL
        CHECK (kyc_status IN ('Verified', 'Pending', 'Rejected')),
    -- KYC — Know Your Customer — is the identity verification a regulator
    -- requires before someone may invest.
    --
    -- The generator makes about 96% Verified. The remaining 4% give queries
    -- something real to filter on, and introduce the idea that not every row
    -- in a customer table is a customer who can transact.

    onboarded_date  date NOT NULL
    -- When they joined the fund house.
    --
    -- A VALIDATION ANCHOR: no folio may open before this date for the same
    -- investor, because you complete KYC in order to buy a fund. The
    -- generator enforces it; tests/test_stage2_data.py checks it.
);


-- ── TABLE 3 OF 4 · folios ──────────────────────────────────────────────────
--
-- A folio is one investor's account in one scheme.
--
-- THE KEY IDEA: an investor holding three different funds has THREE folios,
-- and that is the normal case, not an edge case. Transactions hang off
-- folios, never off investors — so this is the table that connects people to
-- money.
--
-- About 300,000 at full scale, which is roughly 1.5 per investor.

CREATE TABLE folios (
    folio_id            bigint PRIMARY KEY,
    -- bigint again, same reasoning as investor_id. This table grows faster
    -- than investors do.

    folio_number        text NOT NULL UNIQUE,
    -- The account number printed on the investor's statement — 'SAH000000042'.
    --
    -- A HUMAN-FACING IDENTIFIER ALONGSIDE THE MACHINE ONE. folio_id is ours
    -- and appears in joins; folio_number is what the investor quotes on the
    -- phone. Registrar systems work exactly this way, and a model asked
    -- "show me folio SAH000000042" has to know to search this column rather
    -- than folio_id.

    investor_id         bigint NOT NULL REFERENCES investors (investor_id),
    scheme_id           integer NOT NULL REFERENCES schemes (scheme_id),
    -- Both declared. Note the TYPES DIFFER — bigint for the investor,
    -- integer for the scheme — and each matches the type of the column it
    -- points at. A foreign key between mismatched integer widths would still
    -- work, but it would make the join slower and the intent less clear.

    opened_date         date NOT NULL,
    -- TWO RULES THE GENERATOR ENFORCES ON THIS COLUMN, neither of which the
    -- schema can express:
    --   - not before the investor's onboarded_date
    --   - not before the scheme's launch_date
    --
    -- Both are cross-table comparisons, and a CHECK can only see one row of
    -- one table. So they are enforced in the generator and verified by
    -- tests/test_stage2_data.py. A rule the schema cannot state has to be
    -- enforced somewhere else and tested, or it is not enforced at all.

    holding_mode        text NOT NULL
        CHECK (holding_mode IN ('Single', 'Joint', 'Anyone or Survivor')),
    -- Who may operate the folio.
    --
    -- NO PATTERN USES THIS COLUMN and no demo question touches it. It is
    -- here because real folios have it — and a column nobody queries is
    -- itself realistic. The metadata pipeline still has to describe it, and
    -- the table-notes test still requires an approved note for it.

    bank_account_masked text                 -- PII, stored masked (e.g. 'XXXXXX4417')
    -- The bank account money is paid into.
    --
    -- THE COLUMN NAME SAYS MASKED, which is a small thing worth copying.
    -- Anyone reading the schema knows what they are looking at without
    -- consulting pii.yaml. A column called `bank_account` holding masked
    -- values would be correct and misleading.
    --
    -- NULL-able, like the other optional personal columns.
);

CREATE INDEX folios_investor_idx ON folios (investor_id);
CREATE INDEX folios_scheme_idx   ON folios (scheme_id);
-- TWO INDEXES, AND THE REASON THEY ARE NEEDED SURPRISES PEOPLE.
--
-- A FOREIGN KEY DOES NOT CREATE AN INDEX. PostgreSQL indexes the column
-- being pointed AT (because it must be a primary key or unique, and those
-- are indexed), but it does NOT index the column doing the pointing.
--
-- So the declarations above enforce that every investor_id exists. They do
-- nothing to make "find all folios for this investor" fast — without these
-- two lines, that query would scan all 300,000 rows.
--
-- DECLARING A LINK AND MAKING THE LINK FAST TO FOLLOW ARE TWO SEPARATE JOBS,
-- and you have to do both. This is probably the most common performance
-- mistake in relational design, precisely because the foreign key looks like
-- it should have handled it.


-- ── TABLE 4 OF 4 · sip_registrations ───────────────────────────────────────
--
-- A SIP — Systematic Investment Plan — is a standing instruction to invest a
-- fixed amount every month or quarter.
--
-- It is how most Indian retail investors actually buy funds, and it is the
-- thing a fund house watches most closely, because a cancelled SIP is lost
-- future revenue rather than a lost sale.
--
-- Each registration here produces ONE 'SIP' transaction per period in file
-- 04. So 24,000 SIPs paying for up to 60 months is around 600,000
-- transaction rows — the single largest source of rows in the database.

CREATE TABLE sip_registrations (
    sip_id              bigint PRIMARY KEY,

    folio_id            bigint NOT NULL REFERENCES folios (folio_id),

    plan_id             integer NOT NULL REFERENCES scheme_plans (plan_id),
    -- A SIP buys into one specific PLAN — 'Direct Growth', say — not just a
    -- scheme. Because the money buys units, units need a price, and the
    -- price belongs to a plan. This is file 01's scheme_plans distinction
    -- showing up again.

    sip_amount          numeric(18, 2) NOT NULL CHECK (sip_amount > 0),
    -- Rupees per instalment. The generator draws from round numbers — 1,000,
    -- 2,000, 5,000 — because people choose round amounts. A uniform draw
    -- would produce SIPs of Rs 3,847, which no investor has ever set up, and
    -- students notice.

    frequency           text NOT NULL CHECK (frequency IN ('Monthly', 'Quarterly')),
    -- Almost all are Monthly. This column is what file 04's generator turns
    -- into a step size: Monthly means add 1 month between payments,
    -- Quarterly means add 3.

    start_date          date NOT NULL,

    end_date            date,               -- NULL = runs until cancelled
    -- Same open-ended-period pattern as file 01's
    -- scheme_manager_assignments. About 35% of SIPs are set up for a fixed
    -- number of instalments; the rest run until cancelled, and those have
    -- NULL here.

    sip_status          text NOT NULL
        CHECK (sip_status IN ('Active', 'Cancelled', 'Completed', 'Paused')),
    -- FOUR VALUES, AND THE GENERATOR ONLY EVER PRODUCES THREE. 'Paused'
    -- never appears in our data.
    --
    -- Worth saying out loud rather than leaving as a trap: the schema allows
    -- a status the data does not contain. That is normal — a schema is
    -- written for the business, not for this month's data — but it means a
    -- model that reads only the CHECK constraint will believe paused SIPs
    -- exist, while one that looks at actual values will not. The metadata
    -- pipeline samples real values for exactly this reason.

    cancelled_date      date,

    distributor_arn     text REFERENCES distributors (arn_no),
    -- THE MOST IMPORTANT LINE IN THIS FILE, and it is important for what it
    -- has rather than what it lacks.
    --
    -- THIS IS A DECLARED FOREIGN KEY. The SIP's distributor is properly
    -- linked to the distributors table. The database knows about it, it
    -- enforces it, and link discovery will find it in the catalog and
    -- approve it automatically.
    --
    -- NOW HOLD THAT, AND READ FILE 04. transactions.arn_code points at
    -- exactly the same place — distributors.arn_no — and has NO foreign key.
    --
    -- THE SAME RELATIONSHIP EXISTS TWICE IN THIS DATABASE. DECLARED IN ONE
    -- TABLE, UNDECLARED IN THE OTHER.
    --
    -- That is not inconsistency in our design. It is the most realistic
    -- thing in this entire schema. In every client database I have worked
    -- on, the same relationship is properly declared in the tables somebody
    -- designed carefully, and silently undeclared in the tables that got
    -- added later under deadline. And the undeclared one is always in the
    -- biggest table, because the biggest table is the one people are most
    -- afraid to add constraints to.
    --
    -- NULL means the investor registered the SIP directly, with no
    -- distributor involved.

    CHECK ((sip_status = 'Cancelled') = (cancelled_date IS NOT NULL)),
    -- A CLEVER CONSTRAINT, AND WORTH READING SLOWLY.
    --
    -- It compares two TRUE/FALSE expressions with = , and insists they agree:
    --
    --   (sip_status = 'Cancelled')      is it marked cancelled?
    --   (cancelled_date IS NOT NULL)    is there a cancellation date?
    --
    -- Both true, or both false. Anything else is rejected.
    --
    -- SO IT CATCHES BOTH DIRECTIONS AT ONCE:
    --   Active with a cancellation date   -> REFUSED
    --   Cancelled with no date            -> REFUSED
    --
    -- One line, two bugs made impossible. Writing it as two separate CHECKs
    -- would take four lines and express the same thing less clearly.
    --
    -- A row where the status and the date disagree is a self-contradicting
    -- row, and the kind of thing that survives in a real database for years
    -- because nothing ever errors on it.

    CHECK (end_date IS NULL OR end_date >= start_date)
    -- A SIP cannot end before it began. Same two-case pattern as file 01's
    -- scheme_manager_assignments, and the IS NULL branch is needed for the
    -- same reason: NULL >= anything evaluates to NULL, which a CHECK treats
    -- as passing, so the intention has to be written out rather than relied
    -- upon.
);

CREATE INDEX sip_registrations_folio_idx ON sip_registrations (folio_id);
-- One index, on the foreign key, for the same reason as the two on folios:
-- the declaration does not create it, and "all SIPs for this folio" is a
-- question the generator and the demo both ask.
--
-- Note there is deliberately NO index on plan_id or distributor_arn here.
-- Every index slows down every insert, and no question in this project looks
-- SIPs up by plan or by distributor. You index the questions you know will
-- be asked — not every foreign key, and not just in case.