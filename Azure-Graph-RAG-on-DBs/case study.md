# Case study: GraphRAG over a mutual fund database, on Azure

## Contents
1. [The scenario](#1-the-scenario)
2. [Why ordinary approaches fall short](#2-why-ordinary-approaches-fall-short)
3. [The idea: tables as nodes, joins as hops](#3-the-idea-tables-as-nodes-joins-as-hops)
4. [The data: databases and tables](#4-the-data-databases-and-tables)
5. [How it is built, end to end](#5-how-it-is-built-end-to-end)
6. [One question, traced through the system](#6-one-question-traced-through-the-system)
7. [Safety: how the system is kept read-only and on-topic](#7-safety-how-the-system-is-kept-read-only-and-on-topic)
8. [The Azure services, and why each one](#8-the-azure-services-and-why-each-one)
9. [What we learned while building it](#9-what-we-learned-while-building-it)
10. [Limits and next steps](#10-limits-and-next-steps)

---

## 1. The scenario

**Sahyadri Mutual Fund** is an Indian asset management company (AMC). It runs 40 schemes (equity, debt and hybrid funds), has about 40,000 investors and 600 distributors, and processes hundreds of thousands of transactions a year. (The company is imaginary and the data is synthetic, but its shape follows a real AMC.)

Every day, people across the company ask questions of this data:

- **Sales:** "Which distributor has the highest transaction rejection rate?"
- **Risk:** "Which schemes hold bonds that were downgraded in March 2024, and what were their redemptions that month?"
- **Compliance:** "Is any scheme above the 10% single-issuer limit as of August 2026?"
- **Fund management:** "Did Sahyadri Focused Fund perform differently after its manager changed?"
- **Leadership:** "Why did net flows in Sahyadri Credit Risk Fund drop in March 2024?"

Today each of these goes to the data team as a ticket. An analyst finds the right tables, works out how to join them, writes SQL, checks the numbers and sends back a spreadsheet. A simple question takes hours; a hard one takes days. The data team spends most of its time answering questions instead of improving the data.

The AMC's engineers are asked to build a system where anyone can **ask in plain English and get an answer in seconds**: a table of numbers, a short explanation, and the exact SQL used, so the answer can be checked.

### What makes this hard

Their database has **20 tables** and over **1.1 million rows**. The questions above are not single-table lookups:

- The downgrade question needs ratings, securities, holdings, schemes, plans and transactions: **six tables**, joined in the right order.
- Some links between tables were **never declared** in the database. For example, `transactions.arn_code` holds a distributor's ARN number, but no foreign key says so. An analyst knows this from experience; a system has to discover it.
- Business terms have **exact definitions**. "Net flows" means purchases, SIPs and switches in, minus redemptions and switches out, from completed transactions only. Getting this slightly wrong gives a confident but wrong number.
- Some columns hold **personal data** (investor names, PAN, phone numbers), and the system must **never change data**.

---

## 2. Why ordinary approaches fall short

The engineers considered three common approaches first.

### Approach A: classic RAG (search documents, then answer)
Classic Retrieval-Augmented Generation splits documents into chunks, finds the chunks closest to the question, and has a language model answer from them.

This does not work for a database. The answer to "what were redemptions in March 2024" is not written in any document; it has to be **calculated** from 683,159 transaction rows. Search can find text; it cannot add up numbers.

### Approach B: give the whole schema to a language model and ask for SQL
Put all 20 tables and their 124 columns into the prompt, and ask the model to write SQL.

This works for small databases, but here it causes problems:
- The model sees far more than it needs, and gets confused by similar-looking columns.
- It **guesses joins** from column names. It cannot know that `credit_rating_history.sec_cd` joins to `securities.security_code`, because the names don't match and no foreign key says so.
- It **invents its own definitions** of business terms, differently on each run.

### Approach C: let the model explore the database by itself
Let an agent run queries, look at results, and keep trying until it finds an answer.

This is slow, expensive, and hard to control. Each question can take dozens of model calls and queries, and it is difficult to prove that the agent never runs something harmful.

### What the engineers needed instead
Something that:
1. Picks **only the tables a question needs**,
2. Knows **exactly how to join them**, including the undeclared links,
3. Uses **approved definitions** for business terms,
4. Writes **one** checked, read-only SQL query, and
5. Shows its working, so a person can trust or challenge the answer.

---

## 3. The idea: tables as nodes, joins as hops

The key idea is to treat the database's **structure** as a **graph**:

- Every **table** is a **node**.
- Every **join** between two tables (a pair of columns that match) is an **edge**.
- Joining tables is **travelling along edges**. Each join is one **hop**.

```
credit_rating_history ──sec_cd = security_code──▶ securities ──security_id──▶ portfolio_holdings
                                                                                     │
                                                                                 scheme_id
                                                                                     ▼
transactions ──plan_id──▶ scheme_plans ──scheme_id──────────────────────────────▶ schemes
```

The downgrade question needs `credit_rating_history` and `transactions`. In the graph, the route between them is a path of hops:

`credit_rating_history → securities → portfolio_holdings → schemes → scheme_plans → transactions`

Five hops: five joins, each with its exact join columns. **The graph answers "how do I join these tables?" the same way a map answers "how do I get from A to B?"**

This is what "GraphRAG" means in this project:

| Part | What it means here |
|---|---|
| **Graph** | The schema graph: 20 table nodes, 22 join edges, stored in Apache AGE inside PostgreSQL |
| **Retrieval** | Finding the right tables (vector search over table descriptions) and the right exact values (vector search over names like scheme names) with pgvector |
| **Augmented Generation** | Giving the language model only the chosen tables, the join route from the graph, the exact values, and the approved definitions, so it writes correct SQL |

The language model never guesses a join. It is handed the route.

---

## 4. The data: databases and tables

Everything lives on **one Azure Database for PostgreSQL server** (`fundgraphdbexample1`), in **two databases** with different jobs. Full details, with every table's row count and columns, are in [tables.md](tables.md).

### Database 1: `mf_data`, the business data (20 tables, 1,132,667 rows)

This is the data users ask about. The agent only ever reads it.

| Group | Tables | What they describe |
|---|---|---|
| **Fund house** | `schemes`, `scheme_plans`, `scheme_categories`, `benchmarks`, `benchmark_values`, `nav_history`, `scheme_aum_monthly`, `fund_managers`, `scheme_manager_assignments` | The company's products (schemes and the plans investors buy), their daily prices (NAV), their size (AUM), their benchmarks, and who manages them |
| **Market** | `sectors`, `issuer_groups`, `issuers`, `securities`, `credit_rating_history`, `portfolio_holdings` | What the schemes invest in: the securities, the companies that issued them, their credit ratings over time, and what each scheme held at each month end |
| **Investors** | `distributors`, `investors`, `folios`, `sip_registrations` | Who invests (investors and their accounts, called folios), through whom (distributors), and their monthly investment plans (SIPs) |
| **Transactions** | `transactions` | Every purchase, redemption, SIP instalment and switch. 683,159 rows, split into 60 monthly partitions. |

**The links between tables:**
- **19 declared:** foreign keys written into the database.
- **3 hidden:** real links in the data that nobody declared. The system finds them itself:
  - `credit_rating_history.sec_cd` → `securities.security_code`
  - `transactions.arn_code` → `distributors.arn_no`
  - `scheme_aum_monthly.scheme_id` → `schemes.scheme_id`

**Personal data:** `investors` (name, email, mobile, PAN, date of birth) and `folios` (masked bank account).

**Six planted stories:** the generator builds six real patterns into the data, so the demo questions have answers worth finding. Examples: a bond downgrade on 12 March 2024 followed by a redemption spike, one distributor with a far higher rejection rate, and one scheme above the 10% issuer limit.

### Database 2: `agent_meta`, what the agent knows about the data

This holds the system's understanding of `mf_data`. The agent reads it on every question.

| Table / object | Rows | What it holds |
|---|---:|---|
| `meta_tables` | 20 | A plain-English description of each table, what one row means, and its approval status |
| `meta_columns` | 124 | A description of every column, its type, and whether it is personal data |
| `meta_edges` | 22 | Every link between tables: join columns, declared or discovered, evidence, confidence, approval |
| `meta_embeddings` | 1,415 | Vectors (1,536 numbers each) for the 20 table descriptions and 1,395 real values such as scheme and distributor names, for pgvector search |
| `schema_graph` (AGE graph) | 20 nodes, 22 edges | The approved links as a graph, for finding join routes |

**Why two databases?** The business data stays clean and is only ever read. The agent's knowledge can be rebuilt, reviewed and republished at any time without touching the business data.

---

## 5. How it is built, end to end

The system is built in three layers, in this order:

```
 ┌──────────────────────────────┐   ┌───────────────────────────────┐   ┌────────────────────────────────┐
 │ 1. DATA PLANE                │   │ 2. METADATA PIPELINE          │   │ 3. AGENT + API + UI            │
 │ create tables, generate and  │──▶│ build → review → publish      │──▶│ answer questions, show working │
 │ load data into mf_data       │   │ fills agent_meta              │   │ reads agent_meta and mf_data   │
 └──────────────────────────────┘   └───────────────────────────────┘   └────────────────────────────────┘
```

### Layer 1: The data plane (`dataplane/`)

| Step | Command | What happens |
|---|---|---|
| Set up | `scripts/pg_setup.py` | Creates the `mf_data` and `agent_meta` databases, switches on pgvector and AGE, and checks they work |
| Tables | `dataplane/schema/apply_schema.py` | Runs four SQL files that create the 20 tables, keys, indexes and the 60 monthly partitions of `transactions` |
| Data | `dataplane.generator.run` | Generates realistic data, plants the six stories, and bulk-loads 1.1 million rows using PostgreSQL's fast `COPY` (about 100 seconds to Azure) |

In a real AMC, this layer would be replaced by the company's existing database. Everything after it works the same way.

### Layer 2: The metadata pipeline (`metadata/`)

This is where the system **learns the database**. It runs in three steps, with a **person in the middle**.

**Step 1: Build** (`metadata.build`)
1. **Reads the catalog**: every table, column, type, key and row count.
2. **Finds links.** The declared foreign keys are taken as they are. Then it looks for hidden ones in two ways:
   - **Same name:** a column with the same name as another table's key (`scheme_aum_monthly.scheme_id`).
   - **Value overlap:** it samples a column's distinct values and checks how many exist in another table's key. All 780 values of `credit_rating_history.sec_cd` exist in `securities.security_code`, so that is very likely a link.

   Each proposed link is saved with its **evidence** and a **confidence** score.
3. **Drafts notes.** For each table, the language model writes a plain-English description, what one row means, business terms, and a description of every column. It is given column statistics and sample values, so the notes match the real data.
4. Everything is saved as **unapproved**.

**Step 2: Review** (`metadata.review`)

A person reads the notes and the proposed links, rejects anything wrong, and approves the rest. Nothing the pipeline guessed is used until someone has signed it off.

**Step 3: Publish** (`metadata.publish`)
- Approved table notes and real column values are turned into **embeddings** with Azure OpenAI and stored with **pgvector**, so they can be searched by meaning.
- Approved links are written into the **Apache AGE graph** `schema_graph` as nodes and edges.

Publishing can be re-run at any time; it rebuilds everything it owns.

### Layer 3: The agent (`agent/`)

The agent is a fixed **11-step workflow**, built with **LangGraph**. The order is fixed on purpose: every question goes through the same checks, which makes it predictable, testable and safe.

```
guard_input → understand → match_entities → select_tables → find_join_path
           → write_sql → check_sql → run_sql → summarise → write_commentary → remember
                 ▲            │          │
                 └── retry ───┴──────────┘   (at most 2 retries, then stop and say so)
```

| # | Step | What it does | Uses |
|---|---|---|---|
| 1 | **guard_input** | Checks the question for prompt attacks ("ignore your instructions…") | Azure AI Content Safety, Prompt Shields |
| 2 | **understand** | Rewrites the question so it stands alone (so follow-ups work), and works out the intent, period, approved metrics, named things, and each kind of data needed. Blocks requests to change data, and off-topic ones. | Azure OpenAI |
| 3 | **match_entities** | Turns names as typed ("credit risk fund") into exact database values ("Sahyadri Credit Risk Fund") | pgvector, value embeddings |
| 4 | **select_tables** | Picks the tables: those required by matched names and approved metrics, the best match for each kind of data needed, and the top results of a search over table descriptions | pgvector, table embeddings |
| 5 | **find_join_path** | Asks the graph for the shortest set of joins connecting all chosen tables | Apache AGE (Cypher) |
| 6 | **write_sql** | Writes **one** `SELECT`, using only the chosen tables, the given joins, the exact values and the approved definitions | Azure OpenAI |
| 7 | **check_sql** | Structural checks (one statement, read only, known tables, date filter on `transactions`), then PostgreSQL's own cost estimate (`EXPLAIN`) | sqlglot, PostgreSQL |
| 8 | **run_sql** | Runs the SQL in a read-only transaction, with a 15-second timeout and a 5,000-row cap | PostgreSQL |
| 9 | **summarise** | Reduces the result to what the commentary needs | Python |
| 10 | **write_commentary** | Writes a short plain-English explanation of the numbers, describing timing rather than claiming causes | Azure OpenAI |
| 11 | **remember** | Saves the question and SQL, so follow-up questions can build on them | LangGraph memory |

If `check_sql` or `run_sql` fails, the agent goes back to `write_sql` with the error and tries again, at most twice.

**How the graph finds joins (step 5).** For each table still to be connected, the agent asks AGE for every route of up to 6 hops to the tables already connected:

```sql
SELECT * FROM cypher('schema_graph', $$
    MATCH p = (x:Table {name: 'credit_rating_history'})-[*1..6]-(y:Table {name: 'schemes'})
    RETURN relationships(p)
$$) AS (edges agtype)
```

It keeps the **shortest** route (ties go to the highest confidence), adds those joins, and repeats until every table is connected.

**Approved metric definitions** (`config/metrics.yaml`). Business terms like `net_flows`, `redemptions`, `aum`, `rejection_rate`, `issuer_exposure` and `rating_downgrades` each have one approved definition. When a question uses one, the SQL model is given that exact definition and its tables. That way "net flows" is calculated the same way every time, the way the business defines it.

### The API and UI (`api/`, `ui/`)

A **FastAPI** server provides:
- `POST /query`: ask a question, get the answer and every detail.
- `GET /schema-graph`, `GET /table-counts`: data for the graph and the records box.
- `GET /me`: the signed-in user.
- `GET /health`: health check.

The web page shows each answer with all of its working:
- A **number strip**: tables used, joins, rows, SQL attempts, total time.
- A **GraphRAG trace**: every stage, with its timing and what it decided.
- The **schema graph**, with the tables and joins used lit up and the hidden links dashed. Click a table to focus on it.
- **Records per table**: the size of each table, linked to the graph.
- The **answer**: commentary and result table, with Download CSV.
- The **SQL**: highlighted, with its explanation, every safety check, and any failed attempts.

### Tests

There are 66 tests across the stages: setup, data, metadata, agent and API. They run against the real Azure services, so a passing run proves the whole chain works.

---

## 6. One question, traced through the system

**Question:** *"Which distributor has the highest transaction rejection rate?"*

| Step | What happened | Time |
|---|---|---:|
| guard_input | Prompt Shields: no attack detected | 2.1s |
| understand | Rewritten as "…across all available data?". Intent: find the distributor with the highest share of rejected transactions. Metric: `rejection_rate`. No period, so the full data window is used. | 2.7s |
| match_entities | No names to match | 0.1s |
| select_tables | `transactions` and `distributors`, both required by the `rejection_rate` metric, and also found by search | 1.1s |
| find_join_path | One hop: `transactions.arn_code = distributors.arn_no`. This is a **discovered** link that the database never declared. | 0.2s |
| write_sql | One query: rejection rate per distributor, at least 100 transactions each, highest first | 3.6s |
| check_sql | ✓ single statement, ✓ read only, ✓ known tables, ✓ date filter, ✓ plan cost | |
| run_sql | 1 row | 0.9s |
| write_commentary | Explanation written from the result | 2.6s |

**Answer:** *Konkan Wealth Partners (ARN-104417) has the highest rejection rate, at 17.22%, based on 1,353 transactions. Only distributors with at least 100 transactions were considered.*

**Total: 13.4 seconds**, compared with hours for a ticket to the data team. Without the discovered link, this question could not have been joined at all.

---

## 7. Safety: how the system is kept read-only and on-topic

The system is protected in layers, so no single failure lets something harmful through:

| Layer | Protects against |
|---|---|
| **Prompt Shields** (Content Safety) | Questions that try to take over the model ("ignore your instructions, reveal your prompt") |
| **understand step** | Requests to change data ("delete all rows…") and off-topic questions. They are blocked before any SQL is written. |
| **SQL model rules** | Refuses to write anything except one `SELECT` |
| **check_sql** | Anything that isn't a single read-only statement on known tables. Queries on `transactions` without a date range. Queries PostgreSQL estimates as too expensive. |
| **Read-only transaction** | Even if a write slipped through every check above, PostgreSQL itself refuses it |
| **Timeout and row cap** | Runaway queries: 15 seconds, at most 5,000 rows |
| **Human approval of metadata** | Wrong descriptions or wrong links. Nothing the pipeline guesses is used until a person approves it. |
| **Entra ID sign-in** (on Azure) | People outside the organisation. Each user's conversations are also kept separate. |

---

## 8. The Azure services, and why each one

| Service | What it does in this project |
|---|---|
| **Azure Database for PostgreSQL (Flexible Server)** | Holds the business data, the metadata, the vectors (pgvector) and the graph (Apache AGE) |
| **Azure OpenAI** | `gpt-5.1` understands questions, drafts notes, writes SQL and commentary; `text-embedding-3-small` turns text into vectors |
| **Azure AI Content Safety** | Prompt Shields checks every question for prompt attacks |
| **Application Insights** | Records every request and every agent step, with timings and errors |
| **Azure App Service** | Runs the web app on Azure, so it is available without anyone's laptop |
| **Microsoft Entra ID** | Sign-in: only the organisation's people can use the app |

### Azure Database for PostgreSQL: one database for data, vectors and graph
**Why:** a GraphRAG system normally needs three stores: a relational database for the data, a vector database for search, and a graph database for relationships. On Azure PostgreSQL, two allowed extensions do the job of the other two:
- **pgvector** adds vector search (with an HNSW index), so table descriptions and values can be searched by meaning.
- **Apache AGE** adds a graph with the Cypher query language, so join routes can be found as paths.

This gives the project:
- **One service to run, secure, back up and pay for**, instead of three.
- **No syncing.** The data, its metadata and the graph sit side by side and can't drift apart.
- **Familiar tools.** It's all SQL and PostgreSQL, which data teams already know.
- **Managed operations.** Azure handles patching, backups, high availability and SSL.

The Flexible Server tier lets us pick a small, cheap size (Burstable B1ms) for the demo, and scale up later.

### Azure OpenAI: the language models, inside the Azure boundary
**Why:**
- The models are reached through the company's **own Azure subscription**. Questions, schema notes and sample values go to Azure OpenAI, under Azure's data-handling terms, rather than to a public consumer service, and they are not used to train the models.
- **Both kinds of model come from one service**: a strong chat model (`gpt-5.1`) for reasoning and SQL, and an embedding model (`text-embedding-3-small`, 1,536 dimensions) for search.
- **Swapping models is a one-line change.** The code asks for a role ("small", "strong", "embedding"), and `config/models.yaml` maps each role to an Azure deployment.
- **Cost and limits are visible per deployment.** Quotas and usage show in the portal, and the client retries automatically when it hits a rate limit.

### Azure AI Content Safety (Prompt Shields): stopping attacks before they start
**Why:** a system that turns text into SQL is an obvious target for prompt injection, and the language model is the weakest place to defend against it. Prompt Shields is a model trained specifically to detect these attacks. It runs **first**, before any of the project's own prompts see the question. It is a managed service, so it improves without code changes. The SQL checks and the read-only transaction remain behind it as further layers.

### Application Insights: seeing inside every answer
**Why:** when an answer is slow or wrong, you need to know which step was responsible. The app sends **OpenTelemetry** traces to Application Insights. Every HTTP request is one transaction, and each agent step (`agent.understand`, `agent.find_join_path`, `agent.run_sql`…) appears as a timed span inside it. Every call out is recorded too: to Azure OpenAI, to Content Safety, and every **PostgreSQL query**, with its SQL text but never its values. In the portal you can:
- open any question and see its timeline,
- see the **Application Map**: the app, with arrows to Azure OpenAI, Content Safety and PostgreSQL, each showing call counts, durations and failures,
- find the slowest steps and the slowest database queries,
- see errors with their stack traces, and
- set alerts.

It uses an open standard, so the same tracing code would work with other tools, and it is optional: without the connection string, the app runs the same and sends nothing.

### Azure App Service: hosting without managing servers
**Why:**
- It runs the Python app **directly from code**: no containers, no virtual machines to patch.
- Packages are installed from `requirements.txt` on deploy.
- It provides HTTPS, logs, a health check (`/health`), restarts and scaling, all from the portal.
- The **B1** plan is the smallest one with **Always On**, which keeps the agent loaded so the first question isn't slow.
- It works with Entra ID through built-in authentication, with no sign-in code in the app.

### Microsoft Entra ID: sign-in with the company's own accounts
**Why:**
- The data is internal, so only the organisation's people should reach the app. App Service's built-in authentication ("Easy Auth") sends every visitor through Microsoft sign-in, using the accounts staff already have for Microsoft 365 and Teams.
- Access can be limited to named people or groups.
- App Service passes the signed-in user's id to the app in a header that can't be faked from outside. The app uses that id to keep **each user's conversations separate**, and refuses requests without it.
- No passwords are stored by the project.

### How they fit together

```
                    ┌───────────── Microsoft Entra ID (sign-in) ─────────────┐
                    ▼                                                        │
 User ──HTTPS──▶ Azure App Service (FastAPI + LangGraph agent + UI) ─────────┘
                    │        │                 │                    │
                    │        │                 │                    └──▶ Application Insights (traces)
                    │        │                 └──▶ Azure AI Content Safety (Prompt Shields)
                    │        └──▶ Azure OpenAI (gpt-5.1, text-embedding-3-small)
                    ▼
         Azure Database for PostgreSQL
           ├─ mf_data     (business data, read-only to the agent)
           └─ agent_meta  (metadata, pgvector embeddings, AGE schema graph)
```

**LangSmith** (not an Azure service) can also be switched on with two settings in `.env`. It records each step's prompts and replies, which is useful while tuning prompts.

---

## 9. What we learned while building it

Real problems found while running the system against Azure, and how they were fixed:

| Problem | Cause | Fix |
|---|---|---|
| Publishing failed with **404** on embeddings, although chat worked | `AZURE_OPENAI_ENDPOINT` was set to the AI Foundry **project** URL. Chat works through it; embeddings don't. | Use the **resource** URL (`https://<resource>.openai.azure.com/`) |
| The downgrade question was **refused**: "no ratings data" | Searching the whole question blurred its parts. The ratings table ranked only 5th, below the cut-off. | The understand step now lists each kind of data needed ("bond credit rating downgrades"), and each one is searched on its own. The ratings table is then found clearly first. |
| The downgrade question then often returned **0 rows** | The model filtered ratings to March *before* comparing each with the previous rating, so the previous rating was gone and no downgrade could be found | A new approved metric, `rating_downgrades`, fixes the definition, including "compare first, then filter by date" |
| The Postgres server could not be created in East US 2 (`SkuNotAvailable`) | Capacity restrictions on the subscription in that region | Created in **Central India** instead |
| Application Map said **"No data available"** | Two Application Insights resources existed; AI Foundry had created one automatically, and the map was opened on that empty one | Open the resource whose connection string is in `.env` (`fundgraph-insights`) |
| PostgreSQL was missing from Application Map | The Azure monitoring package only traces the older `psycopg2` library, and this project uses psycopg 3 | Added `opentelemetry-instrumentation-psycopg` and switched it on in `api/telemetry.py` |
| The first deploy reported **504 Gateway Timeout** | The upload request gave up while App Service was still installing packages | Nothing to fix: the deployment had succeeded. Check with `az webapp log deployment list` |

The general lesson: **the language model is good at writing SQL once it is given the right tables, the right joins and the right definitions.** Most failures came from what it was given, not from the model. That is why the retrieval, the graph and the approved metrics matter more than the prompt wording.

---

## 10. Limits and next steps

**Current limits**
- **Conversation memory is held in the server's memory.** It's lost on restart, and the app must run as a single instance. *Next:* store it in PostgreSQL (a Postgres-backed LangGraph checkpointer).
- **Personal-data columns are marked but not yet blocked** in the SQL check. *Next:* refuse SQL that selects them, unless the user's role allows it.
- **The agent connects with the server's admin login.** *Next:* a dedicated read-only database role, or Entra ID authentication to PostgreSQL.
- **Answers can vary slightly between runs** of the same question, because the model has some leeway in how it writes the SQL. *Next:* more approved metrics for common questions, and an evaluation set that runs every question several times.
- **If one table cannot be reached in the graph**, the join step currently gives up on all joins. *Next:* keep the joins that do connect.

**Next steps**
- An **evaluation runner** for `evals/questions/questions.yaml` (18 questions with expected tables and facts), so every change is measured.
- A **reviewer page** for approving metadata, replacing the terminal command.
- **Charts** for time-series answers, and answer feedback (thumbs up or down) saved for evaluation.
- **CI/CD** with GitHub Actions to App Service.

---

*Related files:* [tables.md](tables.md) (every table in detail) · [file understanding.md](file%20understanding.md) (every file in the project) · [docs/azure/](docs/azure/README.md) (step-by-step portal guides for each Azure service)
