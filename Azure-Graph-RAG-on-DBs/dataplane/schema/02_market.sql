-- ############################################################################
-- dataplane/schema/02_market.sql
--
-- PURPOSE:
--   Creates the 6 "market" tables — everything OUTSIDE the fund house. File
--   01 described the fund company's own products; this file describes the
--   world those products invest in.
--
--   The companies that borrow or sell shares. The individual shares and
--   bonds they issue. The credit ratings of those bonds, over time. And what
--   each scheme actually holds, month by month.
--
--   This is FILE 2 OF 4. It must run after file 01, because
--   portfolio_holdings points at `schemes`, which file 01 creates.
--
--   1. sectors                   ~25 rows    industry of a company
--   2. issuer_groups             ~60 rows    business group a company belongs to
--   3. issuers                  ~800 rows    companies and governments
--   4. securities             ~1,500 rows    individual shares and bonds
--   5. credit_rating_history  ~2,000 rows    every rating, over time (OLD-STYLE NAMES)
--   6. portfolio_holdings   ~144,000 rows    what each scheme holds, monthly
--
-- WHAT PROBLEM DOES THIS SOLVE:
--   These six tables are what make the MULTI-HOP questions possible — and
--   multi-hop questions are the entire reason this project uses a graph
--   rather than a bigger prompt.
--
--   Take one question: "which schemes hold bonds that were downgraded?"
--
--   There is no single table that can answer it. The route is:
--
--     schemes ──> portfolio_holdings ──> securities ──> credit_rating_history
--       │                                    │                   │
--       which fund        what it holds       which bond          its rating
--                                                                 history
--
--   Four tables, three joins. And one of those joins — the last one — is not
--   declared anywhere in the database, so a model reading the schema cannot
--   see it at all.
--
--   In Stage 5 the AGE graph stores that route, so the model is HANDED the
--   join path instead of guessing it. This file is where the route is
--   created; the graph only records what is already here.
--
-- HOW THE 6 TABLES CONNECT:
--
--   sectors ──────────┐
--                     ├──> issuers ──> securities ──> portfolio_holdings <── schemes
--   issuer_groups ────┘                     │                                (file 01)
--   (may be empty)                          │
--                                           └ ─ ─> credit_rating_history
--                                                  (dashed: real link, NOT
--                                                   declared, and the column
--                                                   names differ — hidden
--                                                   link 2 of 3)
--
-- WHICH PLANTED PATTERNS LIVE HERE:
--   PATTERN 1 — one bond is downgraded on a known date, and redemptions
--               spike in the following weeks. The downgrade is one row in
--               credit_rating_history.
--   PATTERN 2 — one scheme holds five companies that all belong to the same
--               business group. No single holding looks wrong.
--   PATTERN 6 — one scheme has more than 10% in a single issuer, which
--               breaks the usual regulatory limit.
--
--   All three need joins across several of these tables to find, which is
--   why they are planted here rather than in a single column somewhere.
--
-- HOW TO RUN:
--   Never on its own — apply_schema.py runs all four files in one
--   transaction:
--
--       uv run python dataplane/schema/apply_schema.py
-- ############################################################################


-- ── CLEAN START ────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS
    portfolio_holdings,
    credit_rating_history,
    securities,
    issuers,
    issuer_groups,
    sectors
CASCADE;
-- Same reasoning as file 01, and the same reverse-dependency order:
-- portfolio_holdings points at securities, so it is dropped first.
--
-- IF EXISTS so the first run on a fresh database does not fail. CASCADE so
-- anything outside this file pointing at these tables is removed too.
--
-- And the same warning: this deletes data. Acceptable only because mf_data
-- holds nothing but synthetic data.


-- ── TABLE 1 OF 6 · sectors ─────────────────────────────────────────────────
--
-- The 25 industries — Banking, Information Technology, Pharmaceuticals,
-- Cement, and so on. Roughly the classification Indian exchanges use.
--
-- WHY THIS TABLE EXISTS: four of the 40 schemes are SECTORAL funds, meaning
-- their offer document promises to invest only in a named part of the
-- market. A banking fund holding pharmaceutical shares would be a mandate
-- breach — a serious finding, not a quirk. The generator enforces that by
-- filtering candidate securities down to the fund's allowed sectors, and the
-- filter works through this table.

CREATE TABLE sectors (
    sector_id       integer PRIMARY KEY,

    sector_code     text NOT NULL UNIQUE,

    sector_name     text NOT NULL UNIQUE
    -- Note "Government" is one of the 25. Governments issue securities too,
    -- and every issuer must have a sector because the column is NOT NULL, so
    -- they get their own rather than being left empty.
);


-- ── TABLE 2 OF 6 · issuer_groups ───────────────────────────────────────────
--
-- Business groups. In India many large companies belong to a family group —
-- several separately listed companies under one ultimate owner.
--
-- WHY A REGULATOR CARES, AND THEREFORE WHY THIS TABLE EXISTS: the limit on
-- how much a fund may hold applies to a GROUP, not only to a company. You
-- can hold 8% of one company and 8% of another and be within the limit on
-- both — and in breach once you notice both belong to the same group.
--
-- PLANTED PATTERN 2 LIVES HERE, and it is the hardest of the six to find.
-- One scheme holds five companies from a single group, each at a modest
-- weight. No single row looks wrong. Finding it requires joining
-- portfolio_holdings to securities to issuers to issuer_groups and summing
-- by group — four tables before anything is visible.
--
-- That is the shape of a real compliance finding, and it is the shape a
-- single-table query can never produce.

CREATE TABLE issuer_groups (
    group_id        integer PRIMARY KEY,

    group_name      text NOT NULL UNIQUE
);


-- ── TABLE 3 OF 6 · issuers ─────────────────────────────────────────────────
--
-- The companies, banks and governments that issue securities. About 800:
-- one central government, 19 state governments, and the rest companies,
-- banks and non-bank lenders.

CREATE TABLE issuers (
    issuer_id       integer PRIMARY KEY,

    issuer_name     text NOT NULL UNIQUE,
    -- The company name.
    --
    -- LIKE scheme_name IN FILE 01, THIS COLUMN GETS SPECIAL TREATMENT in
    -- Stage 5: every value is turned into an embedding and stored in
    -- pgvector, so a question that names a company loosely — "vardhaman
    -- housing" — can be matched to the exact row, "Vardhaman Housing Finance
    -- Ltd", and the SQL can use the real name.
    --
    -- config/agent.yaml lists this table and column as where an "issuer"
    -- kind of name is looked up. tests/test_stage5_metadata.py checks that
    -- exact match works.

    group_id        integer REFERENCES issuer_groups (group_id),
    -- Which business group, if any.
    --
    -- NOTICE WHAT IS MISSING: there is no NOT NULL here. This column MAY BE
    -- EMPTY, and the contrast with the next line is deliberate.

    sector_id       integer NOT NULL REFERENCES sectors (sector_id),
    -- Which industry. NOT NULL — every issuer must have one.
    --
    -- SO: COMPARE THESE TWO LINES. Same kind of column, same kind of foreign
    -- key, and one is required while the other is optional. That is a
    -- modelling decision, not an oversight.
    --
    -- Every company is in some industry. Not every company belongs to a
    -- business group — most are independent. An empty group_id means "no
    -- group", which is a real answer rather than missing information.
    --
    -- AND IT CHANGES HOW EVERY QUERY TOUCHING IT MUST BE WRITTEN. Count
    -- issuers by sector and the numbers add to 800. Count by group and they
    -- do not, because an inner join silently drops the ungrouped ones. A
    -- column that is allowed to be empty is a column every downstream query
    -- has to think about — and the only way to know is to read the schema,
    -- or to be told by an approved column note, which is what Stage 5 exists
    -- to produce.

    issuer_type     text NOT NULL
        CHECK (issuer_type IN ('Corporate', 'PSU', 'Bank', 'NBFC', 'Government', 'Other'))
    -- What kind of entity it is. PSU is a public sector undertaking — a
    -- government-owned company. NBFC is a non-banking financial company — it
    -- lends money but is not a bank.
    --
    -- This column, rather than rating or sector, is how one scheme category
    -- in the generator picks its holdings: a Banking & PSU fund is defined by
    -- WHO issued the paper, not by how it is rated.
);


-- ── TABLE 4 OF 6 · securities ──────────────────────────────────────────────
--
-- The 1,500 individual instruments a scheme can hold. A share, a corporate
-- bond, a commercial paper, a government security.
--
-- Roughly: 520 shares (one per listed company), 600 corporate bonds, 120
-- commercial papers, 60 certificates of deposit, 150 government securities,
-- 50 treasury bills.

CREATE TABLE securities (
    security_id     integer PRIMARY KEY,
    -- Our internal number. portfolio_holdings points at this.

    security_code   text NOT NULL UNIQUE,
    -- The code other systems use to refer to this security.
    --
    -- THIS IS THE TARGET OF HIDDEN LINK 2. credit_rating_history refers to
    -- securities by this CODE, not by security_id — and it calls the column
    -- something different. See table 5.
    --
    -- Why a real database would do this: the ratings feed comes from outside,
    -- from a rating agency, and an outside system has no idea what our
    -- internal security_id is. It can only quote the public code. So the
    -- ratings table stores the code, and the join has to go through text
    -- rather than through our own integer key.

    isin            text NOT NULL UNIQUE CHECK (length(isin) = 12),
    -- The 12-character international identifier, same rule as scheme_plans
    -- in file 01. Present, unique, exactly 12 characters.
    --
    -- So this table has THREE identifiers for the same thing: security_id
    -- (ours), security_code (the market's short code) and isin (the
    -- international standard). All three are real, all three are used by
    -- different systems, and a model has to be told which one to join on.

    issuer_id       integer NOT NULL REFERENCES issuers (issuer_id),
    -- Who issued it. Declared, so link discovery finds it for free.
    --
    -- This is the second hop of the multi-hop question: from a security to
    -- the company behind it, so that "how much do we hold in this company?"
    -- can add up across all its bonds and its share.

    security_type   text NOT NULL
        CHECK (security_type IN ('Equity', 'Corporate Bond', 'Commercial Paper',
                                 'Certificate of Deposit', 'Government Security',
                                 'Treasury Bill')),
    -- Six kinds, and the distinction drives the whole generator. A liquid
    -- fund may hold only the short-dated ones. A gilt fund may hold only
    -- government paper. A credit risk fund deliberately holds lower-rated
    -- corporate bonds, because taking credit risk for extra yield is the
    -- product.
    --
    -- Equity means a share — part ownership. The other five are all debt:
    -- somebody borrowed money and promises to repay it.

    coupon_rate_pct numeric(7, 4) CHECK (coupon_rate_pct >= 0),
    -- The interest rate a bond pays, as a percentage.
    --
    -- NULL-ABLE, AND THE REASON IS IN THE DATA: shares do not pay a fixed
    -- interest rate. So this column is empty for every one of the ~520
    -- equity rows and filled for the ~980 debt rows.
    --
    -- Note the CHECK still applies when the value is present. A CHECK passes
    -- automatically on NULL — in SQL, "NULL >= 0" evaluates to NULL, not
    -- false, and a CHECK treats NULL as passing. So this constraint means
    -- "if there is a rate, it is not negative", which is exactly right.
    --
    -- That NULL-passes-a-CHECK behaviour is worth knowing generally: it is
    -- why a CHECK can never substitute for NOT NULL.

    maturity_date   date
    -- When a bond repays its principal. Also NULL for shares, which never
    -- mature — you hold them until you sell them.
    --
    -- No CHECK here. A maturity date in the past is perfectly normal: the
    -- bond matured, and the row stays as history.
);


-- ── TABLE 5 OF 6 · credit_rating_history ───────────────────────────────────
--
-- Every rating ever given to a bond, by a rating agency, with the date.
--
-- A credit rating is an opinion on whether the borrower will repay. AAA at
-- the top, down through AA and A and below. A drop — AA to BBB, say — is a
-- DOWNGRADE, and it is a significant event: the bond loses value
-- immediately, and funds holding it come under pressure.
--
-- THIS IS A HISTORY TABLE, NOT A CURRENT-STATE TABLE. One row per rating
-- event, not one row per security. Which means "what is this bond rated
-- today?" is not a simple lookup — it is the most recent row by date for
-- that security, and writing that query correctly is a real skill.
--
-- OLD-STYLE TABLE 1 OF 2
--   The column names are deliberately short and unclear, the way systems
--   built thirty years ago were, when column names had length limits:
--
--     rtg_id    ->  rating record ID
--     sec_cd    ->  security code
--     rtg_agcy  ->  rating agency
--     rtg       ->  the rating itself (AAA, AA+, ...)
--     rtg_dt    ->  the date the rating was given
--     outlk     ->  outlook
--     upd_ts    ->  when the row was last updated
--
--   THIS IS A TEST OF STAGE 5. A language model reading `rtg_dt` cannot
--   reliably tell you whether it is the date of the rating or the date the
--   row was loaded. It will guess, it will be right most of the time, and the
--   time it is wrong the SQL will look perfectly reasonable and the answer
--   will be wrong.
--
--   The metadata pipeline has to write a note that distinguishes rtg_dt from
--   upd_ts clearly enough that the SQL-writing model picks the right one. A
--   human approves that note once, and from then on the ambiguity is gone.
--   That is the whole argument for the review step, in one table.
--
--   Names are kept lowercase on purpose. In PostgreSQL, a name containing
--   capital letters must be wrapped in double quotes in every query forever
--   — "rtgDt" would need quoting every single time. Lowercase names never do.
--
-- HIDDEN LINK 2 OF 3
--   sec_cd below holds the same values as securities.security_code. There is
--   no REFERENCES, AND the column names are different.
--
--   THAT SECOND POINT IS WHAT MAKES THIS ONE HARD. Hidden link 1 in file 01
--   is called scheme_id on both sides, so a rule that compares column names
--   finds it immediately. Here, `sec_cd` and `security_code` are not the same
--   string, and no amount of comparing names will connect them.
--
--   The only available evidence is the VALUES. Take every distinct value in
--   sec_cd, take every value in securities.security_code, and measure the
--   overlap. It is 100%. That is the discovery, and metadata/links.py's
--   value_overlap rule is what performs it — with a threshold of 95%, set in
--   config/metadata.yaml.
--
--   A pipeline that only compares names scores 1 out of 3. This column is why.
--
-- PLANTED PATTERN 1 STARTS HERE. One specific bond is downgraded on a
-- specific date. What investors did in the four weeks afterwards is in file
-- 04's transactions. The event itself is one row in this table — and the
-- question that connects the two spans four tables and crosses this hidden
-- link.

CREATE TABLE credit_rating_history (
    rtg_id      integer PRIMARY KEY,
    -- The rating record's own id. Note it identifies the RATING EVENT, not
    -- the security — the same bond has several rows here, one per rating.

    sec_cd      text NOT NULL,               -- hidden link: no REFERENCES on purpose
    -- Which security this rating is about, by its public code.
    --
    -- Compare with issuer_id two tables up, which says "REFERENCES issuers".
    -- Same kind of relationship, no declaration.
    --
    -- Note the type is TEXT, not integer — it holds security_code, not
    -- security_id. That matters for link discovery: one of its guard rails
    -- refuses to compare a text column with an integer column, because such
    -- a comparison can only ever produce a false match. Here both sides are
    -- text, so the comparison is legitimate.
    --
    -- tests/test_stage1_schema.py asserts this column has NO foreign key. It
    -- asserts an ABSENCE, and it does so because a well-meaning "fix" here
    -- would hand the answer to the pipeline and the exercise would quietly
    -- stop testing anything.

    rtg_agcy    text NOT NULL,
    -- Which agency gave the rating.
    --
    -- Worth knowing: two agencies can rate the same bond differently on the
    -- same day. So "the rating" of a bond is not one value — it depends which
    -- agency you ask. Any query about ratings that ignores this column is
    -- quietly picking an arbitrary agency's view.

    rtg         text NOT NULL
        CHECK (rtg IN ('AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-',
                       'BBB+', 'BBB', 'BBB-', 'BB+', 'BB', 'BB-', 'B', 'C', 'D')),
    -- The rating. Sixteen steps, best (AAA) to default (D).
    --
    -- THE ORDER MATTERS AND THE STORAGE DOES NOT PRESERVE IT. This is text,
    -- so sorting it alphabetically gives nonsense: 'AA+' sorts before 'AAA'
    -- because '+' comes before 'A' in character order, and 'B' sorts before
    -- 'BBB'.
    --
    -- So "is this bond rated below A?" cannot be answered by a text
    -- comparison. The ladder has to be mapped to positions first — which is
    -- exactly what the generator does internally with a first_rating_pos
    -- number, and what any correct query against this column must do too.
    --
    -- This is a mistake students make constantly, and it produces answers
    -- that are confidently wrong rather than obviously wrong.

    rtg_dt      date NOT NULL,
    -- The date the rating was given. A BUSINESS date.
    --
    -- THIS IS THE COLUMN THAT ANSWERS "WHEN WAS IT DOWNGRADED?", and it is
    -- the one that must not be confused with upd_ts below.

    outlk       text CHECK (outlk IN ('Stable', 'Positive', 'Negative', 'Watch')),
    -- The agency's outlook — its view on which way the rating may move next.
    --
    -- NULL-ABLE: not every rating comes with an outlook. 'Watch' is the
    -- interesting value, because it is the agency signalling a change may be
    -- imminent. A negative outlook or a watch is often the warning that
    -- precedes a downgrade.

    upd_ts      timestamptz NOT NULL DEFAULT now()
    -- When this ROW was last changed in the system. A SYSTEM timestamp.
    --
    -- AND HERE IS THE TRAP THE WHOLE TABLE IS BUILT AROUND. Two date columns
    -- with similar abbreviated names and completely different meanings:
    --
    --   rtg_dt   when the agency gave the rating    (business date, no time)
    --   upd_ts   when our system stored the row     (system time, with zone)
    --
    -- A question about downgrades needs rtg_dt. A model that picks upd_ts
    -- gets a query that runs, returns rows, and answers a different question
    -- — "when did we load this?" — with no error anywhere.
    --
    -- Nothing in the column names distinguishes them. Only an approved note
    -- can. That is the single clearest argument in this schema for why the
    -- metadata layer exists.
);


-- ── TABLE 6 OF 6 · portfolio_holdings ──────────────────────────────────────
--
-- What each scheme holds at each month end. About 144,000 rows: 40 schemes
-- times roughly 60 securities each times 60 months.
--
-- ROUTE 2 OF 2 TO AUM
--   Add up market_value for one scheme at one month end and you get its
--   AUM. scheme_aum_monthly in file 01 is the other route, and the two
--   agree — summaries.py computes each holding as a share of that AUM, so
--   the agreement is arithmetic rather than hoped for.
--
--   tests/test_stage2_data.py checks it, and it is the best test in the
--   suite: it can only pass if both tables were derived from the same
--   underlying transactions.
--
-- PLANTED PATTERN 6 LIVES HERE. One scheme has more than 10% of its money
-- in a single issuer, breaching the usual regulatory limit, from a known
-- month onwards. The generator caps every randomly chosen holding at 8% so
-- that this one scheme is the only breach — the pattern is findable only
-- because the background is quiet.

CREATE TABLE portfolio_holdings (
    scheme_id       integer NOT NULL REFERENCES schemes (scheme_id),
    -- Declared, pointing at file 01's schemes.
    --
    -- NOTE THE CONTRAST WITH FILE 01's scheme_aum_monthly, where the same
    -- column pointing at the same place has NO foreign key. Two tables, same
    -- relationship, declared in one and not the other. That inconsistency is
    -- the most realistic thing in this schema.

    security_id     integer NOT NULL REFERENCES securities (security_id),
    -- Declared. The first hop of the multi-hop question.

    as_of_date      date NOT NULL,
    -- Always a month end, like scheme_aum_monthly.
    --
    -- NOTE THERE IS NO month-end CHECK HERE, unlike file 01's
    -- scheme_aum_monthly which enforces it with date_trunc. An inconsistency
    -- in our own schema, and worth pointing out rather than hiding: the
    -- generator produces month ends for both, so both are correct in
    -- practice, but only one is guaranteed by the database.
    --
    -- Also note the column is called as_of_date here and month_end_date
    -- there. The same concept under two names in two tables — which is why
    -- the join between them in summaries.py needs left_on and right_on
    -- rather than a plain "on".

    quantity        numeric(18, 3) NOT NULL CHECK (quantity > 0),
    -- How many units, shares or bonds are held.
    --
    -- Three decimal places, because fractional holdings are normal.
    --
    -- CHECK > 0, strictly: a holding of zero is not a holding, it is the
    -- absence of one, and it should be recorded by the row not existing.
    -- This is why summaries.py puts a floor of 0.001 on the computed
    -- quantity — a tiny holding that rounded to 0.000 would be rejected
    -- here, and the load would fail.

    market_value    numeric(18, 2) NOT NULL CHECK (market_value >= 0),
    -- What the holding was worth that day, in rupees.
    --
    -- CHECK >= 0 this time, not > 0. The asymmetry with quantity above is
    -- deliberate: a holding can round to zero rupees while still being a
    -- real holding of a fraction of a unit.

    weight_pct      numeric(7, 4) NOT NULL CHECK (weight_pct >= 0 AND weight_pct <= 100),
    -- This holding's share of the scheme's total money. 12.5000 means 12.5%.
    --
    -- TWO CONDITIONS IN ONE CHECK, joined by AND. A holding cannot be a
    -- negative share of a portfolio, and it cannot be more than all of it.
    --
    -- What the database CANNOT enforce here is the thing that actually
    -- matters: that all the weights for one scheme in one month add up to
    -- 100. A CHECK sees one row at a time, so a cross-row total is beyond
    -- it. That rule is enforced by the generator — summaries.py puts the
    -- rounding leftover onto the largest holding so each month totals
    -- exactly 100.0000 — and verified by tests/test_stage2_data.py.
    --
    -- Worth saying out loud: a constraint the schema cannot express has to
    -- be enforced somewhere else and tested, or it is not enforced at all.

    PRIMARY KEY (scheme_id, security_id, as_of_date)
    -- A THREE-COLUMN composite primary key. A scheme holds a given security
    -- at most once per month end.
    --
    -- This is what makes the generator's "exclude planted securities from the
    -- random draw" step necessary: if a scheme picked pattern 6's bank at
    -- random AND had the planted weight applied, there would be two rows for
    -- the same three values, and the load would fail on a unique violation.
);

CREATE INDEX portfolio_holdings_security_idx ON portfolio_holdings (security_id, as_of_date);
-- A SECOND lookup structure on this table, and this one earns its place.
--
-- WHAT AN INDEX IS: a sorted lookup list the database maintains alongside
-- the table, so it can find matching rows without reading every row.
--
-- The primary key above already indexes (scheme_id, security_id, as_of_date)
-- in that order, which makes "what does THIS SCHEME hold?" fast — the query
-- can start at the scheme and read forward.
--
-- But it does nothing for the REVERSE question, because scheme_id comes
-- first: "which schemes hold THIS SECURITY?" would have to scan all 144,000
-- rows. And that reverse question is exactly the one the multi-hop demo
-- asks, after a bond has been downgraded.
--
-- So this index puts security_id first, and the reverse lookup becomes fast.
--
-- TWO THINGS TO SAY ABOUT INDEXES GENERALLY:
--
--   An index is NOT a rule. It changes nothing about what data is allowed.
--   It only changes how quickly a question can be answered. Every constraint
--   so far in this file changes what is possible; this line does not.
--
--   And it is not free. Every index makes every INSERT slightly slower,
--   because the index has to be updated too. So you add them for questions
--   you know will be asked — not for every column, and not just in case.