-- ############################################################################
-- dataplane/schema/04_transactions.sql
--
-- PURPOSE:
--   Creates ONE table — transactions — and it holds about 80% of all the rows
--   in this database.
--
--   Every purchase, every SIP instalment, every redemption and every switch
--   that happened inside a folio, across the full five-year window. Roughly
--   900,000 rows at the default scale of 0.2, and 4.6 million at scale 1.0.
--
--   Because it is that large, it is SPLIT INTO 60 MONTHLY PIECES. That split
--   is the whole subject of the second half of this file.
--
--   This is FILE 4 OF 4, and it must run last: transactions points at folios
--   (file 03) and scheme_plans (file 01).
--
-- WHAT PROBLEM DOES THIS SOLVE:
--   Everything before this file is static. Schemes exist, people exist,
--   accounts exist — and no money has moved. This is where money moves, and
--   almost every question in the project ends up reading this table:
--
--     "which distributor has the worst rejection rate?"
--     "did redemptions spike after the downgrade?"
--     "how much did SIP collections grow?"
--     "what is this scheme's AUM?"             <- even this, indirectly
--
--   That last one matters. AUM is not stored as a typed-in number anywhere.
--   summaries.py computes it from the units in this table. So an error here
--   propagates into scheme_aum_monthly, into portfolio_holdings, and into
--   the validation that checks the two agree.
--
-- WHY THIS TABLE IS PARTITIONED:
--   PostgreSQL stores a partitioned table as one PARENT (transactions) plus
--   one CHILD table per range — transactions_2021_09, transactions_2021_10,
--   and so on to transactions_2026_08. Sixty children.
--
--   Queries and inserts go to the parent. PostgreSQL routes each row to the
--   right child by txn_date, and reads only the children a query can
--   possibly need.
--
--     without partitioning:   "SIP inflows in March 2024"
--                             -> read all 5 years of rows, keep 1 month
--
--     with partitioning:      "SIP inflows in March 2024"
--                             -> read transactions_2024_03 only
--                                the other 59 children are never touched
--
--   That is called PARTITION PRUNING, and on a 2 GiB burstable Azure server
--   it is the difference between a fast query and a timeout.
--
--   AND IT IS WHY THE AGENT HAS A GUARDRAIL THAT LOOKS PEDANTIC. Later in
--   this project, before any model-written SQL is allowed to run, one of the
--   checks in guardrails/sql_checks.py is: does any query touching
--   `transactions` compare txn_date with a value? If not, it is refused and
--   rewritten.
--
--   That is not fussiness. Without a date filter, pruning cannot happen,
--   PostgreSQL scans all 60 children, and a query that should take 200
--   milliseconds takes 20 seconds. The guardrail is the reason the
--   partitioning pays for itself.
--
--   Two further benefits worth naming: loading or deleting a whole month
--   becomes a whole-table operation rather than millions of row deletes; and
--   this is how a real fund house would store it, so the Stage 5 metadata
--   pipeline has to learn to describe a partitioned table rather than only
--   plain ones.
--
-- THE 60 MONTHS:
--   September 2021 to August 2026 inclusive. The same five-year window every
--   other dated table uses — nav_history, benchmark_values,
--   scheme_aum_monthly — and the same window set in config/generator.yaml.
--
--   THOSE THREE PLACES MUST AGREE. The dates at the bottom of this file are
--   hard-coded, and generator.yaml has its own start_date and end_date. If
--   somebody changes the window in the YAML and not here, the generator
--   produces transactions with no partition to go into, and the load fails
--   with "no partition of relation transactions found for row".
--
-- HIDDEN LINK 3 OF 3 LIVES HERE:
--   transactions.arn_code holds the distributor's ARN and matches
--   distributors.arn_no — and it is deliberately NOT declared with
--   REFERENCES.
--
--   Three things make this the hardest of the three to find:
--     - no foreign key, so the catalog says nothing
--     - the names differ: arn_code here, arn_no there
--     - about 45% of the values are NULL, meaning a direct transaction with
--       no distributor at all
--
--   So it can only be found by comparing values, AND the comparison has to
--   ignore the empty ones. metadata/links.py does both.
--
--   The expected answer is in evals/answer_key/hidden_links.yaml, which the
--   pipeline never reads. DO NOT ADD A FOREIGN KEY HERE —
--   tests/test_stage1_schema.py fails if you do, and it fails on purpose.
--
-- PLANTED PATTERNS WRITTEN THROUGH THIS TABLE:
--   PATTERN 1 — after the bond downgrade in file 02, a share of investors in
--               the affected schemes redeem heavily within about four weeks.
--   PATTERN 5 — the distributor from file 03 has ~17% of its transactions
--               rejected against a 2% background, written through
--               txn_status = 'Rejected'.
--
-- HOW TO RUN:
--   Never on its own — apply_schema.py runs all four files in one
--   transaction:
--
--       uv run python dataplane/schema/apply_schema.py
-- ############################################################################


-- ── CLEAN START ────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS transactions CASCADE;
-- ONE NAME, AND IT REMOVES 61 TABLES.
--
-- Dropping the parent drops all 60 monthly children with it. You never name
-- a child here, and you never should — the children exist only as pieces of
-- the parent, and the parent owns their lifetime.
--
-- Same IF EXISTS and CASCADE reasoning as the other three files.


-- ── THE TABLE ──────────────────────────────────────────────────────────────

CREATE TABLE transactions (
    transaction_id  bigint NOT NULL,
    -- bigint, because this table has millions of rows and keeps growing.
    --
    -- NOTE IT IS NOT DECLARED "PRIMARY KEY" HERE, unlike every other table
    -- in this schema. The primary key is declared at the bottom instead, and
    -- the reason is below on PRIMARY KEY (transaction_id, txn_date).

    txn_date        date NOT NULL,
    -- The business date the transaction was accepted.
    --
    -- THIS IS THE PARTITION KEY. Its value decides which of the 60 children
    -- the row is physically stored in, and a query that filters on it is the
    -- only kind that can prune.
    --
    -- It is NOT NULL, and for a partition key that is effectively compulsory:
    -- a row with no date would have no child to belong to.

    folio_id        bigint NOT NULL REFERENCES folios (folio_id),
    plan_id         integer NOT NULL REFERENCES scheme_plans (plan_id),
    -- Which account, and which plan inside the scheme. Both DECLARED.
    --
    -- Worth noticing: this table has two perfectly ordinary declared foreign
    -- keys and one undeclared link. The declared ones are the links somebody
    -- thought about; the undeclared one is the link that got added later.
    -- Both live in the same table.

    txn_type        text NOT NULL
        CHECK (txn_type IN ('Purchase', 'Redemption', 'SIP', 'Switch In', 'Switch Out')),
    -- FIVE KINDS, AND THE SPLIT BETWEEN THEM IS THE MOST IMPORTANT
    -- DEFINITION IN THE PROJECT:
    --
    --   money IN   ->  Purchase, SIP, Switch In
    --   money OUT  ->  Redemption, Switch Out
    --
    -- A SWITCH IS TWO ROWS, IN TWO DIFFERENT SCHEMES. 'Switch Out' is money
    -- leaving one fund and 'Switch In' is money arriving in another. Anyone
    -- who treats a switch as neutral, or forgets that a switch-out reduces
    -- units, computes the wrong AUM.
    --
    -- That definition is repeated as a constant in both
    -- dataplane/generator/transactions.py and summaries.py, and the two must
    -- never disagree — they decide the SIGN of every unit movement, and a
    -- wrong sign produces a number that is too big with nothing to indicate
    -- why.

    amount          numeric(18, 2) NOT NULL CHECK (amount > 0),
    units           numeric(18, 4) NOT NULL CHECK (units > 0),
    nav             numeric(12, 4) NOT NULL CHECK (nav > 0),
    -- The money, the units bought or sold, and the price per unit on that
    -- date. The relationship is:
    --
    --     units = amount / nav
    --
    -- NOTE THE NAV IS STORED ON THE ROW, even though it could be looked up
    -- from nav_history. That is DELIBERATE DENORMALISATION: a question like
    -- "how many units did this folio buy in March?" can then be answered
    -- without joining nav_history at all, which removes a join from the most
    -- common kind of query against the largest table in the database.
    --
    -- The cost of denormalising is that the same fact is now in two places
    -- and could disagree. tests/test_stage2_data.py therefore checks that
    -- units x nav equals amount, within a tolerance for rounding — a check
    -- that only exists because of this design choice.
    --
    -- PRECISION: amounts to 2 decimals because rupees have paise. Units to 4.
    -- NAV to 4, matching how fund houses publish it.
    --
    -- All three are CHECK > 0, strictly. A transaction of zero rupees is not
    -- a transaction, and zero units is not a trade. This is also why the
    -- generator floors redemption units rather than rounding them — rounding
    -- up could produce a negative balance, and rounding down to zero would
    -- be rejected here.

    txn_status      text NOT NULL CHECK (txn_status IN ('Completed', 'Rejected')),
    rejection_reason text,
    -- ABOUT 2% OF TRANSACTIONS ARE REJECTED — a failed bank mandate, an
    -- unregistered autopay, a KYC problem. THE ROW STAYS IN THE TABLE,
    -- exactly as it would in a real registrar system, but it moved no money
    -- and no units.
    --
    -- AND THAT IS THE MOST CONSEQUENTIAL DETAIL IN THIS FILE, because it
    -- means every single query about money has to say
    --
    --     WHERE txn_status = 'Completed'
    --
    -- Forget it and you overstate collections by about 2%, and NOTHING
    -- ERRORS to tell you. The number is simply wrong.
    --
    -- This is planted on purpose, so students meet the problem here rather
    -- than at a client. It is also why summaries.py filters on this column
    -- before computing AUM, and why forgetting that filter would make every
    -- AUM figure in the database slightly too large.
    --
    -- rejection_reason is NULL-able, and the constraint at the bottom of the
    -- table keeps the two columns consistent.

    arn_code        text,
    -- HIDDEN LINK 3. Matches distributors.arn_no by VALUE, with no
    -- REFERENCES.
    --
    -- COMPARE THIS LINE WITH file 03's sip_registrations:
    --
    --     distributor_arn  text REFERENCES distributors (arn_no)   <- declared
    --     arn_code         text                                    <- not declared
    --
    -- Same target, same meaning, different column name, and only one of them
    -- tells the database anything.
    --
    -- NULL MEANS A DIRECT TRANSACTION — the investor bought with no agent
    -- involved. About 45% of rows, because 55% of folios are Direct plans.
    --
    -- THAT NULL SHARE HAS TWO CONSEQUENCES:
    --
    --   1. Any check that every ARN here exists in distributors MUST say
    --      "WHERE arn_code IS NOT NULL", or it fails on perfectly correct
    --      data. tests/test_stage2_data.py does exactly that, and the
    --      subtlety is worth pointing out: an empty arn_code is not a broken
    --      link, it is the absence of a link.
    --
    --   2. metadata/links.py must ignore the empty values when measuring
    --      value overlap. Counting NULLs as non-matches would push the
    --      overlap below the 95% threshold and the link would never be
    --      discovered.
    --
    -- tests/test_stage1_schema.py asserts this column has NO foreign key. It
    -- asserts an ABSENCE — because if somebody "fixed" this line, link
    -- discovery would read the answer out of the catalog and score 3 out of 3
    -- without doing any work. The course would still appear to run. The
    -- lesson would be gone.

    channel         text NOT NULL
        CHECK (channel IN ('Distributor', 'Branch', 'Online', 'Mobile App')),
    -- How the transaction reached the fund house.
    --
    -- THE GENERATOR KEEPS THIS CONSISTENT WITH arn_code, and that
    -- consistency is deliberate. A row with an ARN gets 'Distributor' or
    -- occasionally 'Branch'; a row with no ARN gets 'Online', 'Mobile App' or
    -- 'Branch'.
    --
    -- Drawing the channel independently would have been far easier and would
    -- have produced 'Online' transactions carrying a distributor ARN — a row
    -- that contradicts itself. Students who cross-check two columns against
    -- each other should find they agree.

    created_at      timestamptz NOT NULL,
    -- The SYSTEM timestamp: when the registrar's system recorded it. Office
    -- hours on txn_date, in Indian time.
    --
    -- CONTRAST WITH txn_date ABOVE. txn_date is a business date with no
    -- clock; created_at is a moment in time with a zone. Both refer to the
    -- same transaction, and they answer different questions: "which month
    -- does this belong to" versus "when exactly did this happen".
    --
    -- NOTE THERE IS NO "DEFAULT now()" HERE, unlike schemes.created_at in
    -- file 01. The generator supplies the value, because these rows are
    -- backdated across five years — now() would stamp every one of a million
    -- historical transactions with today's date.
    --
    -- The generator writes it as a string ending '+05:30'. Without that
    -- offset, PostgreSQL would interpret the time in the SERVER's timezone,
    -- which on Azure is UTC, and every transaction would be recorded five
    -- and a half hours late. Nothing would error; a query grouping by hour of
    -- day would just show the business opening at 03:30.

    PRIMARY KEY (transaction_id, txn_date),
    -- THE PRIMARY KEY HAS TWO COLUMNS, AND THE SECOND ONE IS THE PARTITION
    -- KEY. This catches everybody, so here is the rule.
    --
    -- PostgreSQL REQUIRES that a unique constraint on a partitioned table
    -- includes every partition column. The reason is physical: uniqueness is
    -- enforced by an index, each child has its own index, and PostgreSQL can
    -- only guarantee uniqueness WITHIN a child. To be sure a value is unique
    -- across all 60, it has to know which child a row belongs to — which
    -- means the partition key must be part of the key.
    --
    -- Try to declare "PRIMARY KEY (transaction_id)" alone and it refuses
    -- outright:
    --
    --     ERROR: unique constraint on partitioned table must include all
    --            partitioning columns
    --
    -- That message is clear once you have seen it and impossible to guess
    -- beforehand.
    --
    -- THE PRACTICAL CONSEQUENCE: transaction_id alone is NOT guaranteed
    -- unique by the database. In our data it is, because the generator
    -- assigns 1..n in one pass — but nothing in the schema enforces it, and
    -- a query joining on transaction_id alone is relying on a property the
    -- database does not promise.

    CHECK ((txn_status = 'Rejected') = (rejection_reason IS NOT NULL))
    -- The same both-directions trick as file 03's SIP status.
    --
    --   Rejected with no reason    -> REFUSED
    --   Completed with a reason    -> REFUSED
    --
    -- A completed transaction carrying a rejection reason would be a
    -- self-contradicting row. One line makes it impossible.

) PARTITION BY RANGE (txn_date);
-- THE LINE THAT MAKES THIS A PARTITIONED TABLE.
--
-- "BY RANGE" means each child owns a continuous range of values. The other
-- options PostgreSQL offers are BY LIST (each child owns a set of specific
-- values, good for a country code) and BY HASH (rows spread evenly, good for
-- balancing writes with no natural ranges).
--
-- RANGE is right here because the data is time series and the questions are
-- about time periods. A question about March 2024 maps to exactly one range.
--
-- ONE THING TO KNOW: this parent table stores NO ROWS OF ITS OWN. It is a
-- routing layer. Until the children exist at the bottom of this file, an
-- insert into transactions FAILS — there is nowhere for the row to go. That
-- is why the loop below is part of the schema and not an afterthought.


-- ── INDEXES ────────────────────────────────────────────────────────────────

CREATE INDEX transactions_folio_idx ON transactions (folio_id);
CREATE INDEX transactions_plan_idx  ON transactions (plan_id);
CREATE INDEX transactions_arn_idx   ON transactions (arn_code);
-- THREE INDEXES, DECLARED ONCE ON THE PARENT, CREATED AUTOMATICALLY ON ALL
-- 60 CHILDREN.
--
-- That automatic propagation is worth knowing: you never create an index on
-- a child by hand, and a child created later — by the loop below — inherits
-- these as it is made.
--
-- Why each one:
--
--   folio_id   "all transactions in this account" — the most common
--              drill-down, and the generator's own unit-balance calculation
--              groups by it.
--
--   plan_id    joins to scheme_plans, which is how a question gets from a
--              transaction back to a scheme.
--
--   arn_code   THE INTERESTING ONE. This is the undeclared hidden link, and
--              it is indexed. So the join works, and works fast, even though
--              the database does not know it is a join at all.
--
--              That gap is the whole lesson: somebody who built this table
--              knew the column was used to join to distributors — they
--              indexed it for exactly that — and still did not declare the
--              foreign key. Which is precisely how real databases end up
--              this way.
--
-- Note there is NO index on txn_date. It does not need one: the partition
-- key is how rows are physically organised, so filtering on it skips whole
-- children rather than consulting an index.


-- ── THE 60 MONTHLY PIECES ──────────────────────────────────────────────────
--
-- A loop, so nobody types 60 nearly-identical CREATE TABLE lines — and more
-- importantly, so nobody MISTYPES one of them. A single wrong month boundary
-- would silently break every query about that month, and finding out which
-- one would take an afternoon.

DO $$
-- "DO" runs a block of PL/pgSQL — PostgreSQL's own procedural language —
-- without defining a stored function first. It is the way to put a loop
-- inside a plain .sql file.
--
-- The two-dollar-sign markers around this block are "dollar quoting".
-- Everything between them is passed through untouched, so the block can
-- contain single quotes without any escaping. Writing it as an ordinary
-- quoted string would mean doubling every quote inside, and the result
-- would be unreadable. (Never type the marker itself in a comment in here:
-- PostgreSQL does not see comments inside the block, so it would end the
-- block early.)
DECLARE
    month_start date := DATE '2021-09-01';
    -- The first month of the window. := is PL/pgSQL's assignment operator,
    -- not =, which is comparison.
    month_end   date;
    -- Declared without a value; set inside the loop.
BEGIN
    WHILE month_start < DATE '2026-09-01' LOOP
        -- Note the condition uses the first of SEPTEMBER 2026, which is one
        -- day after the window ends on 31 August 2026. So the last child
        -- created covers August 2026, and the loop runs exactly 60 times.
        --
        -- An off-by-one here would create 59 or 61 children, and
        -- tests/test_stage1_schema.py asserts exactly 60 for that reason.

        month_end := month_start + INTERVAL '1 month';
        -- The first of the NEXT month. PostgreSQL's interval arithmetic
        -- handles the varying month lengths and leap years itself — adding
        -- 30 days instead would drift immediately.

        EXECUTE format(
            'CREATE TABLE %I PARTITION OF transactions FOR VALUES FROM (%L) TO (%L)',
            'transactions_' || to_char(month_start, 'YYYY_MM'),
            month_start,
            month_end
        );
        -- Build the CREATE TABLE statement as text, then run it.
        --
        -- EXECUTE is needed because a table NAME cannot be a variable in
        -- ordinary SQL. The name has to be assembled into a string and then
        -- executed.
        --
        -- %I AND %L ARE NOT INTERCHANGEABLE, and using the wrong one is the
        -- classic mistake here:
        --
        --   %I  quotes as an IDENTIFIER — a table or column name.
        --       'transactions_2024_03' comes out bare, and would be
        --       double-quoted if it needed to be.
        --
        --   %L  quotes as a LITERAL — a value.
        --       2024-03-01 comes out as '2024-03-01', with the quotes.
        --
        -- Swap them and the statement is malformed. Use %s for either and
        -- you have built a statement by raw string concatenation, which is
        -- how SQL injection happens — here the inputs are our own dates so
        -- there is no attacker, but the habit is the point.
        --
        -- 'transactions_' || to_char(month_start, 'YYYY_MM')
        --   || is SQL's string concatenation. to_char formats the date as
        --   '2024_03'. So the children are named transactions_2021_09
        --   through transactions_2026_08 — sortable, and obvious at a glance
        --   in a table listing.
        --
        -- FOR VALUES FROM (x) TO (y)
        --   The range is INCLUSIVE of x and EXCLUSIVE of y. So the March
        --   child covers 2024-03-01 up to but not including 2024-04-01.
        --
        --   That is what makes the ranges fit together with no gaps and no
        --   overlaps: each month's end is the next month's start. If the
        --   upper bound were inclusive, 31 March would belong to two
        --   children and PostgreSQL would refuse to create the second one.

        month_start := month_end;
        -- Step forward. The next child starts where this one ended, which is
        -- what keeps the ranges contiguous.
    END LOOP;
END $$;
-- AFTER THIS BLOCK: one parent and 60 children. An insert into transactions
-- is now routed by txn_date to the right child, and a query filtering on
-- txn_date reads only the children it needs.
--
-- WHAT HAPPENS AT THE EDGES: a transaction dated outside the five-year
-- window has no child to go into, and the insert fails with
--
--     ERROR: no partition of relation "transactions" found for row
--
-- Which is a good failure — it is the database refusing data that does not
-- belong to the window this schema was built for, rather than silently
-- accepting it somewhere wrong. If you widen the window in
-- config/generator.yaml, widen the two dates in this loop to match.