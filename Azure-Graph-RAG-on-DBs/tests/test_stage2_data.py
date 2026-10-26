"""
tests/test_stage2_data.py
=========================

WHAT THIS FILE IS FOR
---------------------
Checks the data the generator loaded into mf_data. Two kinds of checks:

    VALIDATION  -> the data is internally correct: no broken links, no
                   impossible values, and the summary tables agree with
                   the transactions they were calculated from.

    PATTERNS    -> each of the 6 planted patterns is actually visible in
                   the data, so the demo questions have something to find.

This is Gate 1 from the build plan: nothing moves to Azure until every
check here passes.

HOW TO RUN
----------
After running the generator:

    uv run pytest tests/test_stage2_data.py -v
"""

# HOW TO RUN (from the project folder, after the generator has loaded mf_data):
#     Windows (PowerShell):  uv run pytest tests/test_stage2_data.py -v
#     Mac / Linux:           uv run pytest tests/test_stage2_data.py -v
# os: reads DATA_DB_URL.
import os

# psycopg: the PostgreSQL driver.
import psycopg

# pytest: the test runner (fixtures).
import pytest

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# Read .env as soon as the test file is loaded.
load_dotenv()


# A fixture: any test with a "db" argument receives this connection. scope="module" = one for the whole file.
@pytest.fixture(scope="module")
def db():
    """One connection for all tests. autocommit keeps each check independent."""
    # Connect to mf_data; autocommit means each query stands alone.
    with psycopg.connect(os.environ["DATA_DB_URL"], autocommit=True) as conn:
        # "yield" hands the connection to the tests; it is closed after the last test.
        yield conn


# Helper used by most tests below.
def one(db, sql: str):
    """Run a query that returns one value, and return that value."""
    # First row, first column.
    return db.execute(sql).fetchone()[0]


# Money out of a scheme. Everything else (Purchase, SIP, Switch In) is money in.
OUTFLOW = "txn_type IN ('Redemption', 'Switch Out')"


# =============================================================================
# VALIDATION
# =============================================================================

# VALIDATION 1: no empty tables.
def test_every_table_has_rows(db):
    """All 20 tables were filled. An empty table means a generator step silently failed."""
    # Every ordinary table except the 60 monthly pieces, plus the transactions parent.
    tables = [r[0] for r in db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename NOT LIKE 'transactions\\_%'"
    ).fetchall()] + ["transactions"]
    # The tables with no rows.
    empty = [t for t in tables if one(db, f"SELECT count(*) FROM {t}") == 0]
    # There must be none.
    assert empty == []


# VALIDATION 2: every hidden-link value points at a real row.
def test_hidden_links_have_no_orphans(db):
    """
    The 3 hidden links have no foreign key, so Postgres cannot stop a value
    that points at nothing. We check it ourselves: every value must match a
    row in the table it points to. (Declared links are already enforced by
    Postgres during the load.)
    """
    # Hidden link 1: every AUM row's scheme_id exists in schemes.
    assert one(db, """SELECT count(*) FROM scheme_aum_monthly a
                      WHERE NOT EXISTS (SELECT 1 FROM schemes s WHERE s.scheme_id = a.scheme_id)""") == 0
    # Hidden link 2: every rating's sec_cd exists in securities.security_code.
    assert one(db, """SELECT count(*) FROM credit_rating_history c
                      WHERE NOT EXISTS (SELECT 1 FROM securities s WHERE s.security_code = c.sec_cd)""") == 0
    # Hidden link 3: every non-empty arn_code exists in distributors.arn_no.
    assert one(db, """SELECT count(*) FROM transactions t WHERE t.arn_code IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM distributors d WHERE d.arn_no = t.arn_code)""") == 0


# VALIDATION 3: units x NAV = amount.
def test_units_times_nav_equals_amount(db):
    """For every transaction, units x NAV must equal the amount to within Rs 1."""
    # The biggest difference across all transactions must be under Rs 1.
    assert one(db, "SELECT max(abs(units * nav - amount)) FROM transactions") < 1


# VALIDATION 4: no folio ever holds fewer than 0 units.
def test_no_folio_holds_negative_units(db):
    """Nobody can redeem more units than they own: every folio's net units stay at or above zero."""
    # Count folios whose completed units in minus units out is below zero.
    negative = one(db, f"""
        SELECT count(*) FROM (
            SELECT folio_id, sum(CASE WHEN {OUTFLOW} THEN -units ELSE units END) AS held
            FROM transactions WHERE txn_status = 'Completed' GROUP BY folio_id
        ) x WHERE held < -0.001""")
    # There must be none.
    assert negative == 0


# No transaction may be dated before its scheme launched.
def test_no_transaction_before_scheme_launch(db):
    # Count transactions dated before their scheme's launch date: must be 0.
    assert one(db, """SELECT count(*) FROM transactions t
                      JOIN scheme_plans p USING (plan_id) JOIN schemes s USING (scheme_id)
                      WHERE t.txn_date < s.launch_date""") == 0


# VALIDATION 6: each month's portfolio adds up to 100%.
def test_holding_weights_add_up_to_100(db):
    """For every scheme and month end, the holding weights add up to 100%."""
    # The biggest distance from 100 across every scheme and month end.
    worst = one(db, """SELECT max(abs(total - 100)) FROM (
                           SELECT sum(weight_pct) AS total FROM portfolio_holdings
                           GROUP BY scheme_id, as_of_date) x""")
    # Must be (almost) exactly 100.
    assert worst < 0.01


# VALIDATION 7: the two ways of getting AUM agree.
def test_both_routes_to_aum_agree(db):
    """
    Route 1: scheme_aum_monthly.aum_amount.
    Route 2: the sum of market_value in portfolio_holdings.
    They must agree to within Rs 1 for every scheme and month end.
    """
    # The biggest difference between the two AUM routes.
    worst = one(db, """SELECT max(abs(h.total - a.aum_amount)) FROM (
                           SELECT scheme_id, as_of_date, sum(market_value) AS total
                           FROM portfolio_holdings GROUP BY 1, 2) h
                       JOIN scheme_aum_monthly a
                         ON a.scheme_id = h.scheme_id AND a.month_end_date = h.as_of_date""")
    # Must be under Rs 1.
    assert worst < 1


# =============================================================================
# PLANTED PATTERNS
# =============================================================================

# PATTERN 1: the bond downgrade.
def test_pattern1_credit_risk_net_flows_negative_in_march_2024(db):
    """After the downgrade on 12 Mar 2024, the Credit Risk Fund's net flows turn negative that month."""
    # Money in minus money out for that fund in March 2024.
    march = one(db, f"""
        SELECT sum(CASE WHEN {OUTFLOW} THEN -t.amount ELSE t.amount END)
        FROM transactions t JOIN scheme_plans p USING (plan_id) JOIN schemes s USING (scheme_id)
        WHERE s.scheme_name = 'Sahyadri Credit Risk Fund' AND t.txn_status = 'Completed'
          AND t.txn_date BETWEEN '2024-03-01' AND '2024-03-31'""")
    # More money left than came in.
    assert march < 0


# At the last month end, the largest (scheme, group) exposure must be Mid Cap Fund + Trident Group, above 20%.
def test_pattern2_trident_group_is_the_largest_group_exposure(db):
    # The single largest scheme + group exposure at the last month end.
    top_scheme, top_group, weight = db.execute("""
        SELECT s.scheme_name, g.group_name, sum(h.weight_pct)
        FROM portfolio_holdings h JOIN securities se USING (security_id) JOIN issuers i USING (issuer_id)
        JOIN issuer_groups g USING (group_id) JOIN schemes s USING (scheme_id)
        WHERE h.as_of_date = '2026-08-31' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 1""").fetchone()
    # It must be the Mid Cap Fund in Trident Group ...
    assert (top_scheme, top_group) == ("Sahyadri Mid Cap Fund", "Trident Group")
    # ... at more than 20%.
    assert weight > 20


# PATTERN 3: the manager change.
def test_pattern3_focused_fund_beats_benchmark_only_after_manager_change(db):
    """Growth of the fund vs its benchmark, in the two years before and after 3 Oct 2023."""
    # Helper: the fund's growth minus the benchmark's growth between two dates.
    def fund_minus_benchmark(start, end):
        # Fund growth (end NAV / start NAV) minus benchmark growth, for the Direct Growth plan.
        return one(db, f"""
            WITH points AS (
                SELECT n.nav_date, n.nav, bv.index_value
                FROM nav_history n JOIN scheme_plans p USING (plan_id) JOIN schemes s USING (scheme_id)
                JOIN benchmark_values bv ON bv.benchmark_id = s.benchmark_id AND bv.value_date = n.nav_date
                WHERE s.scheme_name = 'Sahyadri Focused Fund' AND p.plan_type = 'Direct'
                  AND p.option_type = 'Growth' AND n.nav_date IN ('{start}', '{end}'))
            SELECT (max(nav) FILTER (WHERE nav_date = '{end}') / max(nav) FILTER (WHERE nav_date = '{start}'))
                 - (max(index_value) FILTER (WHERE nav_date = '{end}') / max(index_value) FILTER (WHERE nav_date = '{start}'))
            FROM points""")
    assert fund_minus_benchmark("2021-10-04", "2023-10-02") < 0     # trailed before
    assert fund_minus_benchmark("2023-10-03", "2025-10-03") > 0     # beat after


# Tier-3 SIP cancellations in the last 12 months must be more than double the 12 months before.
def test_pattern4_tier3_sip_cancellations_jump_in_last_12_months(db):
    # Tier-3 cancellations in the last 12 months, and in the 12 months before.
    last12, prev12 = db.execute("""
        SELECT count(*) FILTER (WHERE r.cancelled_date >= '2025-09-01'),
               count(*) FILTER (WHERE r.cancelled_date >= '2024-09-01' AND r.cancelled_date < '2025-09-01')
        FROM sip_registrations r JOIN folios f USING (folio_id) JOIN investors i USING (investor_id)
        WHERE i.city_tier = 3""").fetchone()
    # The last 12 months must be more than double.
    assert last12 > 2 * prev12


# Among distributors with at least 100 transactions, Konkan must have the highest rejection rate.
def test_pattern5_konkan_has_the_highest_rejection_rate(db):
    # The distributor with the highest rejection rate (at least 100 transactions).
    top = one(db, """
        SELECT d.dist_nm FROM transactions t JOIN distributors d ON d.arn_no = t.arn_code
        GROUP BY d.dist_nm HAVING count(*) >= 100
        ORDER BY avg((t.txn_status = 'Rejected')::int) DESC LIMIT 1""")
    # It must be the planted one.
    assert top == "Konkan Wealth Partners"


# PATTERN 6: the single-issuer limit.
def test_pattern6_only_one_scheme_above_10pct_in_a_company(db):
    """Excluding governments (exempt from the limit), only one scheme holds over 10% in one company."""
    # Every (scheme, company) pair above 10% at the last month end, governments excluded.
    rows = db.execute("""
        SELECT s.scheme_name, i.issuer_name, sum(h.weight_pct)
        FROM portfolio_holdings h JOIN securities se USING (security_id) JOIN issuers i USING (issuer_id)
        JOIN schemes s USING (scheme_id)
        WHERE h.as_of_date = '2026-08-31' AND i.issuer_type <> 'Government'
        GROUP BY 1, 2 HAVING sum(h.weight_pct) > 10""").fetchall()
    # There must be exactly one: the Banking fund in Malabar Commercial Bank.
    assert [(r[0], r[1]) for r in rows] == [
        ("Sahyadri Banking & Financial Services Fund", "Malabar Commercial Bank Ltd")
    ]