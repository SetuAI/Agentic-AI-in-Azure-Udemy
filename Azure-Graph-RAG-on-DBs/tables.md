# Tables: what is stored where

## The short version

| | |
|---|---|
| **Server** | One Azure Database for PostgreSQL server: `fundgraphdbexample1` (Central India, PostgreSQL 16) |
| **Databases** | 2: `mf_data` and `agent_meta` |
| **Business tables** | 20, all in `mf_data`, holding 1,132,667 rows in total |
| **Agent tables** | 4 metadata tables in `agent_meta`, plus the AGE graph `schema_graph` |

The two databases have different jobs:

- **`mf_data` holds the business data**: schemes, investors, transactions and so on. This is the data users ask questions about. The agent only ever *reads* it.
- **`agent_meta` holds what the agent knows *about* that data**: a description of every table and column, the links between tables, the embeddings used for search, and the graph used to find joins. Without it, the agent would not know which tables to use or how to join them.

Row counts are from the current load (generator scale 0.2, data from 1 Sep 2021 to 31 Aug 2026). They change if the data is generated again.

---

## Database 1: `mf_data` (the business data)

The data describes an imaginary Indian mutual fund company, **Sahyadri Mutual Fund**. It is synthetic, so it contains no real people. It was created by `dataplane/schema/` (the tables) and `dataplane/generator/` (the rows).

The 20 tables fall into four groups.

### Group A: Fund house (the company's own products and people)

Created by `01_fund_house.sql`.

| Table | Rows | What it holds |
|---|---:|---|
| `scheme_categories` | 36 | The categories a scheme can belong to, such as "Banking and PSU Fund" or "Aggressive Hybrid Fund", with asset class and risk level |
| `schemes` | 40 | Every mutual fund scheme the company offers: name, code, category, benchmark, launch date, exit load, minimum SIP amount |
| `scheme_plans` | 150 | The versions of each scheme investors actually buy: Direct or Regular, Growth or IDCW. Each has its own ISIN and expense ratio. |
| `benchmarks` | 15 | The market indices that schemes compare themselves against |
| `benchmark_values` | 19,560 | The daily value of each benchmark index |
| `nav_history` | 189,482 | The daily NAV (price per unit) of each plan |
| `scheme_aum_monthly` | 2,320 | Each scheme's total assets under management (AUM) and number of folios at every month end |
| `fund_managers` | 25 | The company's fund managers: name, experience, qualification, joining date |
| `scheme_manager_assignments` | 74 | Which manager ran which scheme, from when to when, and whether as main manager or co-manager |

### Group B: Market (what the schemes invest in)

Created by `02_market.sql`.

| Table | Rows | What it holds |
|---|---:|---|
| `sectors` | 25 | Industry sectors, such as banking or housing finance |
| `issuer_groups` | 60 | Business groups that own several companies (for example, all companies of one conglomerate) |
| `issuers` | 800 | The companies, banks and government bodies whose securities the schemes hold, each with its sector and group |
| `securities` | 1,500 | The individual instruments: corporate bonds, government securities, equities, commercial paper, certificates of deposit, treasury bills |
| `credit_rating_history` | 1,973 | Every credit rating given to a security over time, with the agency and outlook. A downgrade shows up as a rating that is worse than that security's previous one. |
| `portfolio_holdings` | 111,072 | What each scheme held at each month end: the security, quantity, market value and its weight (%) in the portfolio |

### Group C: Investors and distributors (who invests, and through whom)

Created by `03_investors.sql`.

| Table | Rows | What it holds |
|---|---:|---|
| `distributors` | 600 | The agents and platforms that sell schemes, identified by their ARN number, with type, city and whether still active |
| `investors` | 40,000 | Every investor: name, contact details, PAN, city and city tier, KYC status, joining date |
| `folios` | 57,776 | An investor's account in one scheme. One investor can have folios in several schemes. |
| `sip_registrations` | 24,000 | Every SIP (monthly investment plan) set up: amount, frequency, start and end dates, status, and the date it was cancelled, if it was |

### Group D: Transactions (money moving in and out)

Created by `04_transactions.sql`.

| Table | Rows | What it holds |
|---|---:|---|
| `transactions` | 683,159 | Every purchase, redemption, SIP instalment and switch: date, amount, units, NAV used, channel, distributor, and status (completed or rejected, with the reason) |

`transactions` is by far the biggest table. It is split into **60 monthly partitions** (one per month from Sep 2021 to Aug 2026), so a question about one month reads only that month's partition. This is why every query on `transactions` must filter by `txn_date`; the agent's SQL checks enforce that.

### How the groups connect

The main chain:

```
investors → folios → transactions → scheme_plans → schemes
                  → sip_registrations ↗

schemes → portfolio_holdings → securities → issuers → sectors / issuer_groups
                                         → credit_rating_history
```

- **19 links are declared** in the database as foreign keys.
- **3 links are not declared**: they exist in the data, but nobody wrote them down. The metadata pipeline found them by comparing column values:

| Hidden link | How it was found |
|---|---|
| `credit_rating_history.sec_cd` → `securities.security_code` | Every value in `sec_cd` exists in `security_code` |
| `transactions.arn_code` → `distributors.arn_no` | Every value in `arn_code` exists in `arn_no` |
| `scheme_aum_monthly.scheme_id` → `schemes.scheme_id` | Same column name, and every value exists |

These three show as **dashed lines** in the UI's schema graph.

### Personal data

These columns hold personal data and are marked as such in the metadata:

| Table | Personal-data columns |
|---|---|
| `investors` | `full_name`, `email`, `mobile`, `pan`, `date_of_birth` |
| `folios` | `bank_account_masked` |

### Patterns planted in the data

The generator deliberately builds six stories into the data, so the demo questions have real answers to find:

1. A bond downgrade (Vardhaman Housing Finance, 12 March 2024), followed by a spike in redemptions from the two schemes holding it
2. One scheme heavily exposed to a single business group
3. A manager change, after which a scheme's performance changes
4. SIP cancellations rising in one city tier
5. One distributor with a rejection rate far above the rest
6. One scheme above the 10% single-issuer limit

---

## Database 2: `agent_meta` (what the agent knows about the data)

This database is filled by the metadata pipeline (`metadata.build`, then `metadata.review`, then `metadata.publish`), not by the data generator. The agent reads it on every question.

### The four metadata tables

| Table | Rows | What it holds | Used for |
|---|---:|---|---|
| `meta_tables` | 20 | One row per business table: a plain-English description, what one row means (the "grain"), business terms, and whether a person has approved it | Telling the SQL model what each table is for |
| `meta_columns` | 124 | One row per column of every business table: data type, description, number of distinct values, and whether it is personal data | Telling the SQL model what each column means |
| `meta_edges` | 22 | Every link between two tables: the columns to join on, whether it was declared or discovered, the evidence and the confidence, and its approval status | Building the join graph |
| `meta_embeddings` | 1,415 | Text turned into vectors (lists of 1,536 numbers) by Azure OpenAI, for pgvector search. 20 are table descriptions; 1,395 are actual values from the data, such as scheme and distributor names. | Finding the right tables for a question, and matching names like "credit risk fund" to "Sahyadri Credit Risk Fund" |

Only **approved** rows are used by the agent. Nothing the pipeline drafts is used until a person approves it with `metadata.review`.

### The graph: `schema_graph` (Apache AGE)

`metadata.publish` turns the approved links into a graph:

- **20 nodes**, one per business table (`schema_graph."Table"`)
- **22 edges**, one per approved link (`schema_graph."JOINS"`), each carrying its join columns

When a question needs several tables, the agent asks this graph for the shortest way to join them. That is the "Join path" step in the UI.

The other tables you may see in `agent_meta` belong to AGE itself and are not edited by hand:

| Where | What it is |
|---|---|
| `ag_catalog.ag_graph`, `ag_catalog.ag_label` | AGE's own list of graphs and node/edge types |
| `schema_graph._ag_label_vertex`, `schema_graph._ag_label_edge` | AGE's base tables behind the nodes and edges |

---

## Where each part shows up in the UI

| UI part | Comes from |
|---|---|
| Records per table | Row counts of the 20 tables in `mf_data` (`GET /table-counts`) |
| Schema graph | `meta_tables` and the approved `meta_edges` (`GET /schema-graph`) |
| Name matching | `meta_embeddings` (value embeddings) |
| Table search | `meta_embeddings` (table embeddings), plus the approved metrics in `config/metrics.yaml` |
| Join path | The AGE graph `schema_graph` |
| Answer table | The SQL, run read-only against `mf_data` |
