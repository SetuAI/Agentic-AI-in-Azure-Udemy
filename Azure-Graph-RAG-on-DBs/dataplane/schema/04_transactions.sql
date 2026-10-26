-- =============================================================================
-- dataplane/schema/04_transactions.sql
-- =============================================================================
--
-- WHAT THIS FILE IS FOR
-- ---------------------
-- Creates the 1 "transactions" table: every purchase, sale, SIP instalment
-- and switch that happened inside a folio. It is by far the largest table
-- (millions of rows at scale 1), so it is split into 60 monthly pieces.
--
--   1. transactions        -> one row per buy / sell / SIP / switch
--
-- WHERE THIS FITS
-- ---------------
-- File 4 of 4. Must run last, because transactions points to folios (file 03)
-- and scheme_plans (file 01).
--
-- WHY THE TABLE IS SPLIT INTO MONTHS ("PARTITIONED")
-- --------------------------------------------------
-- Postgres stores a partitioned table as one parent (transactions) plus one
-- child table per range (transactions_2021_09, transactions_2021_10, ...).
-- Queries and inserts go to the parent; Postgres routes each row to the
-- right child by txn_date. Benefits:
--   - A question like "SIP inflows in March 2024" reads ONE child, not all 60.
--   - Loading and deleting a month of data is a whole-table operation.
--   - This is how a real fund house would store it, so Stage 5's metadata
--     pipeline must learn to describe a partitioned table, not just plain ones.
--
-- The 60 months are Sep 2021 to Aug 2026 inclusive: the 5-year window every
-- other dated table (nav_history, benchmark_values, scheme_aum_monthly) uses.
--
-- HIDDEN LINK 3 LIVES HERE
-- ------------------------
-- transactions.arn_code holds the distributor's ARN and matches
-- distributors.arn_no, but it is deliberately NOT declared with REFERENCES.
-- The names differ (arn_code vs arn_no) and NULL means "direct, no
-- distributor". Stage 5 must find this link by comparing values.
-- See evals/answer_key/hidden_links.yaml. Do not add a foreign key here;
-- tests/test_stage1_schema.py fails if you do.
--
-- PLANTED PATTERN 5 (from file 03): one distributor's transactions are
-- rejected far more often than everyone else's. The generator writes that
-- through txn_status = 'Rejected' on this table.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- CLEAN START (same reasoning as file 01)
-- -----------------------------------------------------------------------------
-- Dropping the parent drops all 60 monthly pieces with it.
DROP TABLE IF EXISTS transactions CASCADE;


-- =============================================================================
-- 1. transactions                                (millions of rows at scale 1)
-- =============================================================================
-- Create the PARENT table. It holds no rows itself; the monthly pieces below do.
CREATE TABLE transactions (
    -- The transaction's own ID number (part of the primary key, see below).
    transaction_id  bigint NOT NULL,

    -- The date the transaction was accepted. This is the partition key, so
    -- it must be part of the primary key (a Postgres rule for partitioned
    -- tables: the key must include every partition column).
    txn_date        date NOT NULL,

    -- Which account, and which plan inside the scheme. Both declared.
    folio_id        bigint NOT NULL REFERENCES folios (folio_id),
    plan_id         integer NOT NULL REFERENCES scheme_plans (plan_id),

    -- What happened: a purchase, a redemption (sale), a SIP instalment, or a switch
    -- between schemes (Switch Out of one scheme, Switch In to another).
    txn_type        text NOT NULL
        CHECK (txn_type IN ('Purchase', 'Redemption', 'SIP', 'Switch In', 'Switch Out')),

    -- Money and units. Units = amount / nav on txn_date; kept in the row so
    -- questions do not need to join nav_history to answer "how many units".
    amount          numeric(18, 2) NOT NULL CHECK (amount > 0),
    units           numeric(18, 4) NOT NULL CHECK (units > 0),
    nav             numeric(12, 4) NOT NULL CHECK (nav > 0),

    -- A rejected row stays in the table, as in a real system, but moves no
    -- money and no units. Only rejected rows carry a reason.
    txn_status      text NOT NULL CHECK (txn_status IN ('Completed', 'Rejected')),
    -- Why it was rejected (empty for completed transactions).
    rejection_reason text,

    -- HIDDEN LINK 3: matches distributors.arn_no by value, no REFERENCES.
    -- NULL = direct transaction with no distributor.
    arn_code        text,

    -- How the transaction came in.
    channel         text NOT NULL
        CHECK (channel IN ('Distributor', 'Branch', 'Online', 'Mobile App')),

    -- System timestamp, office hours on txn_date, Indian time.
    created_at      timestamptz NOT NULL,

    -- The ID plus the date identifies one row (see the txn_date note above).
    PRIMARY KEY (transaction_id, txn_date),

    -- A reason is filled in if, and only if, the transaction was rejected.
    CHECK ((txn_status = 'Rejected') = (rejection_reason IS NOT NULL))
-- PARTITION BY RANGE (txn_date): split rows into pieces by date ranges.
) PARTITION BY RANGE (txn_date);

-- Indexes on the parent are created automatically on every monthly piece.
CREATE INDEX transactions_folio_idx ON transactions (folio_id);
CREATE INDEX transactions_plan_idx  ON transactions (plan_id);
CREATE INDEX transactions_arn_idx   ON transactions (arn_code);


-- -----------------------------------------------------------------------------
-- THE 60 MONTHLY PIECES
-- -----------------------------------------------------------------------------
-- A loop, so nobody has to type 60 nearly-identical CREATE TABLE lines (and
-- nobody can mistype one of them). Each piece covers [first of month, first
-- of next month), so a date belongs to exactly one piece.
-- DO $$ ... $$ runs a small one-off program (PL/pgSQL) inside the database.
DO $$
-- DECLARE lists the program's variables.
DECLARE
    -- month_start = first day of the month being created; starts at Sep 2021.
    month_start date := DATE '2021-09-01';
    -- month_end = first day of the NEXT month (the piece's end, not included).
    month_end   date;
-- BEGIN ... END is the body of the program.
BEGIN
    -- Repeat until we pass the last month (Aug 2026).
    WHILE month_start < DATE '2026-09-01' LOOP
        -- Work out the first day of the next month.
        month_end := month_start + INTERVAL '1 month';
        -- Build and run a CREATE TABLE command. In format():
        --   %I = a table name (safely quoted), %L = a value (safely quoted).
        -- Example result: CREATE TABLE transactions_2021_09 PARTITION OF transactions
        --                 FOR VALUES FROM ('2021-09-01') TO ('2021-10-01')
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF transactions FOR VALUES FROM (%L) TO (%L)',
            -- The piece's name: "transactions_" + year_month, e.g. transactions_2021_09.
            'transactions_' || to_char(month_start, 'YYYY_MM'),
            -- The piece's first day (included).
            month_start,
            -- The piece's end day (NOT included - it belongs to the next piece).
            month_end
        );
        -- Move on to the next month.
        month_start := month_end;
    -- End of the WHILE loop.
    END LOOP;
-- End of the program.
END $$;
