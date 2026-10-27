# SQL Commands on Azure CLI

20 scenarios against `mf_data`, from absolute basics to advanced. Run them in **Azure Cloud Shell.**

---

# BASIC

## 1 · How many funds does the company run?

```sql
SELECT count(*) AS total_schemes
FROM schemes;
```

**Explanation:** `count(*)` counts rows. The simplest possible query — one table, no filter. Answer: 40.

---

## 2 · Show me the ten most recently launched funds.

```sql
SELECT scheme_name, scheme_code, launch_date, min_sip_amount
FROM schemes
ORDER BY launch_date DESC
LIMIT 10;
```

**Explanation:** `ORDER BY ... DESC` sorts newest first. `LIMIT 10` stops after ten rows — always use a limit while exploring, or a large table will print thousands of lines.

---

## 3 · How many investors do we have in each city tier?

```sql
SELECT city_tier, count(*) AS investors
FROM investors
GROUP BY city_tier
ORDER BY city_tier;
```

**Explanation:** `GROUP BY` collapses many rows into one per distinct value. Roughly 50% tier 1, 30% tier 2, 20% tier 3.

---

## 4 · Which ten cities have the most investors?

```sql
SELECT city, state, count(*) AS investors
FROM investors
GROUP BY city, state
ORDER BY investors DESC
LIMIT 10;
```

**Explanation:** Group by two columns to treat city and state as one unit, then sort by the computed count. You can `ORDER BY` an alias you created in the `SELECT`.

---

## 5 · How many SIPs are active, cancelled and completed?

```sql
SELECT sip_status, count(*) AS sips, round(sum(sip_amount)) AS monthly_rupees
FROM sip_registrations
GROUP BY sip_status
ORDER BY sips DESC;
```

**Explanation:** Two aggregates in one query — a count and a sum. The schema also allows `'Paused'`, but the generator never produces it, so it will not appear.

---

## 6 · Which investors have not completed their KYC?

```sql
SELECT kyc_status, count(*) AS investors,
       round(100.0 * count(*) / (SELECT count(*) FROM investors), 2) AS pct
FROM investors
GROUP BY kyc_status
ORDER BY investors DESC;
```

**Explanation:** The subquery in the `SELECT` gets the overall total so each group can be shown as a percentage. `100.0` rather than `100` forces decimal division instead of integer division.

---

# INTERMEDIATE

## 7 · List all debt funds with their minimum SIP and exit load.

```sql
SELECT s.scheme_name, c.category_name, c.risk_level,
       s.min_sip_amount, s.exit_load_pct
FROM schemes s
JOIN scheme_categories c ON c.category_id = s.category_id
WHERE c.asset_class = 'Debt'
ORDER BY c.category_name;
```

**Explanation:** The first join. `asset_class` lives in `scheme_categories`, so the only way to filter schemes by it is through the join. Note the `WHERE` is on the joined table, not the main one.

---

## 8 · Why does one fund have four different prices on the same day?

```sql
SELECT p.plan_type, p.option_type, p.expense_ratio_pct, n.nav
FROM nav_history n
JOIN scheme_plans p ON p.plan_id = n.plan_id
JOIN schemes s      ON s.scheme_id = p.scheme_id
WHERE s.scheme_name = 'Sahyadri Flexi Cap Fund'
  AND n.nav_date = '2026-08-31'
ORDER BY p.plan_type, p.option_type;
```

**Explanation:** Three tables. An investor buys a *plan*, not a scheme — Direct or Regular (who sold it) crossed with Growth or IDCW (what happens to gains). Regular carries the distributor's commission in its fee, so its NAV is permanently lower.

---

## 9 · Show me everything one investor has ever done.

```sql
SELECT t.txn_date, s.scheme_name, t.txn_type,
       t.amount, t.units, t.nav, t.txn_status, t.channel
FROM transactions t
JOIN folios f  ON f.folio_id  = t.folio_id
JOIN schemes s ON s.scheme_id = f.scheme_id
WHERE f.investor_id = 1234
  AND t.txn_date >= '2021-09-01'
ORDER BY t.txn_date;
```

**Explanation:** Four tables chained — investor to folio to transaction, plus the scheme name. The `txn_date` filter is not optional: `transactions` is split into 60 monthly partitions, and without a date filter PostgreSQL reads all of them.

---

## 10 · How much did we collect through SIPs each month last year?

```sql
SELECT date_trunc('month', txn_date)::date AS month,
       count(*) AS instalments,
       round(sum(amount)) AS rupees
FROM transactions
WHERE txn_type = 'SIP'
  AND txn_status = 'Completed'
  AND txn_date >= '2025-09-01'
GROUP BY 1
ORDER BY 1;
```

**Explanation:** `date_trunc('month', ...)` flattens every date in a month to the 1st so they group together. `GROUP BY 1` means "group by the first column in the SELECT". **`txn_status = 'Completed'` is essential** — about 2% of transactions are rejected and moved no money.

---

## 11 · What are the most common reasons transactions fail?

```sql
SELECT rejection_reason,
       count(*) AS rejections,
       round(sum(amount)) AS rupees_affected
FROM transactions
WHERE txn_status = 'Rejected'
  AND txn_date >= '2021-09-01'
GROUP BY rejection_reason
ORDER BY rejections DESC;
```

**Explanation:** `rejection_reason` is only filled on rejected rows — a schema constraint enforces that the status and the reason always agree.

---

## 12 · Which are our ten largest funds by assets?

```sql
SELECT s.scheme_name, c.asset_class,
       round(a.aum_amount) AS aum, a.folio_count
FROM scheme_aum_monthly a
JOIN schemes s            ON s.scheme_id = a.scheme_id
JOIN scheme_categories c  ON c.category_id = s.category_id
WHERE a.month_end_date = '2026-08-31'
ORDER BY a.aum_amount DESC
LIMIT 10;
```

**Explanation:** `scheme_aum_monthly.scheme_id` has **no foreign key** to `schemes` — the link is real but undeclared. The join works anyway; nothing in the schema tells you it exists.

---

## 13 · Who manages each fund right now?

```sql
SELECT s.scheme_name, m.manager_name, a.manager_role,
       a.start_date, m.experience_years
FROM scheme_manager_assignments a
JOIN schemes s       ON s.scheme_id  = a.scheme_id
JOIN fund_managers m ON m.manager_id = a.manager_id
WHERE a.end_date IS NULL
  AND a.manager_role = 'Primary'
ORDER BY s.scheme_name;
```

**Explanation:** `end_date IS NULL` means "still managing". You must write `IS NULL`, not `= NULL` — in SQL, anything compared to NULL yields NULL, which is never true.

---

# ADVANCED

## 14 · How many units does each folio of one investor currently hold?

```sql
SELECT f.folio_number, s.scheme_name,
       round(sum(CASE WHEN t.txn_type IN ('Redemption','Switch Out')
                      THEN -t.units ELSE t.units END), 4) AS units_held
FROM transactions t
JOIN folios f  ON f.folio_id  = t.folio_id
JOIN schemes s ON s.scheme_id = f.scheme_id
WHERE f.investor_id = 1234
  AND t.txn_status = 'Completed'
  AND t.txn_date >= '2021-09-01'
GROUP BY f.folio_number, s.scheme_name
ORDER BY f.folio_number;
```

**Explanation:** `CASE` flips the sign on money going out before summing. A switch is two rows in two different funds — Switch Out reduces units here, Switch In adds them elsewhere. The result is never negative, by design.

---

## 15 · Rank the funds by size within each asset class.

```sql
SELECT c.asset_class, s.scheme_name,
       round(a.aum_amount) AS aum,
       rank() OVER (PARTITION BY c.asset_class ORDER BY a.aum_amount DESC) AS rank_in_class
FROM scheme_aum_monthly a
JOIN schemes s           ON s.scheme_id = a.scheme_id
JOIN scheme_categories c ON c.category_id = s.category_id
WHERE a.month_end_date = '2026-08-31'
ORDER BY c.asset_class, rank_in_class;
```

**Explanation:** A window function. `PARTITION BY` restarts the ranking for each asset class, so you get a number-one equity fund and a number-one debt fund rather than one overall list.

---

## 16 · How fast is each fund growing month on month?

```sql
SELECT s.scheme_name, a.month_end_date,
       round(a.aum_amount) AS aum,
       round(a.aum_amount - lag(a.aum_amount) OVER (PARTITION BY a.scheme_id ORDER BY a.month_end_date)) AS change,
       round(100.0 * (a.aum_amount / nullif(lag(a.aum_amount) OVER (PARTITION BY a.scheme_id ORDER BY a.month_end_date), 0) - 1), 2) AS pct_change
FROM scheme_aum_monthly a
JOIN schemes s ON s.scheme_id = a.scheme_id
WHERE s.scheme_name = 'Sahyadri Credit Risk Fund'
  AND a.month_end_date >= '2024-01-31'
ORDER BY a.month_end_date;
```

**Explanation:** `lag()` reads the previous row's value within the partition. `nullif(x, 0)` turns a zero into NULL so the division cannot fail — the first month has no previous row, so its change is NULL.

---

## 17 · What is each security's *current* credit rating?

```sql
SELECT DISTINCT ON (c.sec_cd)
       c.sec_cd, i.issuer_name, c.rtg, c.rtg_dt, c.outlk
FROM credit_rating_history c
JOIN securities s ON s.security_code = c.sec_cd
JOIN issuers i    ON i.issuer_id     = s.issuer_id
ORDER BY c.sec_cd, c.rtg_dt DESC
LIMIT 20;
```

**Explanation:** `DISTINCT ON` is PostgreSQL-specific: it keeps the first row per group, and the `ORDER BY` decides which one is first. Sorting by date descending gives the latest rating. This is a history table — a bond has several ratings, so "the current rating" always needs this pattern.

---

## 18 · Which distributor has the worst transaction failure rate?

```sql
SELECT d.dist_nm, d.dist_typ, d.city_nm,
       count(*) AS txns,
       count(*) FILTER (WHERE t.txn_status = 'Rejected') AS rejected,
       round(100.0 * count(*) FILTER (WHERE t.txn_status = 'Rejected') / count(*), 2) AS reject_pct
FROM transactions t
JOIN distributors d ON d.arn_no = t.arn_code
WHERE t.txn_date >= '2021-09-01'
GROUP BY d.dist_nm, d.dist_typ, d.city_nm
HAVING count(*) > 500
ORDER BY reject_pct DESC
LIMIT 5;
```

**Explanation:** `FILTER (WHERE ...)` counts only matching rows inside an aggregate. `HAVING` filters *after* grouping — without it, a distributor with 3 transactions and 1 failure shows 33% and tops the list. Note the join: `arn_no` to `arn_code`, two different names with **no foreign key** between them.

---

## 19 · Is any fund over-exposed to a single business group?

```sql
SELECT sc.scheme_name, g.group_name,
       count(*) AS holdings,
       round(sum(h.weight_pct), 2) AS group_pct
FROM portfolio_holdings h
JOIN securities s    ON s.security_id = h.security_id
JOIN issuers i       ON i.issuer_id   = s.issuer_id
JOIN issuer_groups g ON g.group_id    = i.group_id
JOIN schemes sc      ON sc.scheme_id  = h.scheme_id
WHERE h.as_of_date = '2026-08-31'
GROUP BY sc.scheme_name, g.group_name
HAVING sum(h.weight_pct) > 15
ORDER BY group_pct DESC;
```

**Explanation:** Five tables. Every individual holding is under 8%, so a per-company report shows nothing wrong — the breach only exists once you group by business group and sum. Change `g.group_name` to `i.issuer_name` and the finding disappears.

---

## 20 · Did redemptions spike after the bond downgrade, and in which funds?

```sql
WITH affected AS (
    SELECT DISTINCT h.scheme_id
    FROM portfolio_holdings h
    JOIN securities s ON s.security_id = h.security_id
    JOIN issuers i    ON i.issuer_id   = s.issuer_id
    WHERE i.issuer_name = 'Vardhaman Housing Finance Ltd'
      AND s.security_type = 'Corporate Bond'
)
SELECT s.scheme_name,
       date_trunc('month', t.txn_date)::date AS month,
       count(*) FILTER (WHERE t.txn_type = 'Redemption') AS redemptions,
       round(sum(t.amount) FILTER (WHERE t.txn_type = 'Redemption')) AS redeemed_rupees
FROM transactions t
JOIN folios f   ON f.folio_id  = t.folio_id
JOIN schemes s  ON s.scheme_id = f.scheme_id
JOIN affected a ON a.scheme_id = s.scheme_id
WHERE t.txn_status = 'Completed'
  AND t.txn_date BETWEEN '2024-01-01' AND '2024-05-31'
GROUP BY s.scheme_name, 2
ORDER BY s.scheme_name, 2;
```

**Explanation:** A CTE (`WITH ... AS`) computes the list of affected funds first, then the main query uses it as if it were a table. Six tables in total, and it crosses an undeclared join. Redemptions rise sharply in March and April, then settle.

---

## BONUS · Do our two AUM numbers reconcile?

```sql
SELECT s.scheme_name,
       a.aum_amount                                  AS from_aum_table,
       round(sum(h.market_value), 2)                 AS from_holdings,
       round(a.aum_amount - sum(h.market_value), 2)  AS difference
FROM scheme_aum_monthly a
JOIN portfolio_holdings h
  ON h.scheme_id = a.scheme_id AND h.as_of_date = a.month_end_date
JOIN schemes s ON s.scheme_id = a.scheme_id
WHERE a.month_end_date = '2026-08-31'
GROUP BY s.scheme_name, a.aum_amount
ORDER BY s.scheme_name
LIMIT 10;
```

**Explanation:** Two tables computed two different ways, agreeing to the paisa. Note the join condition — `as_of_date` and `month_end_date` are the same concept under two different column names, and nothing in the schema says so.

---

## BONUS · Why is this query slow?

```sql
EXPLAIN (COSTS OFF)
SELECT sum(amount) FROM transactions
WHERE txn_date BETWEEN '2024-03-01' AND '2024-03-31';
```

```sql
EXPLAIN (COSTS OFF)
SELECT sum(amount) FROM transactions;
```

**Explanation:** The first plan names one partition; the second names 60. `transactions` is physically split by month, so a date filter lets PostgreSQL skip the other 59 pieces entirely. That is partition pruning, and it is why every query against this table should carry a date filter.

---

## Quick reference

| Command               | What it does                                     |
| --------------------- | ------------------------------------------------ |
| `\dt`               | list tables                                      |
| `\d tablename`      | columns, types, constraints, indexes, partitions |
| `\dt transactions*` | the parent table plus its 60 partitions          |
| `\l`                | list databases                                   |
| `\dx`               | list extensions                                  |
| `\timing on`        | show query duration                              |
| `\x auto`           | one field per line when a row is too wide        |
| `\q`                | quit                                             |

**Three habits:** always `LIMIT` while exploring, always put a date filter on `transactions`, and run `\d tablename` before querying something unfamiliar.
