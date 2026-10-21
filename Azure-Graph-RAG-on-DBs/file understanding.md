# azure-fundgraph-rag-on-db — File Guide

This document explains every file in the project: what it contains, what it does, and where it fits.

---

## 1. What the project does

A business user types a question in plain English, for example:

> "Why did net flows in Sahyadri Credit Risk Fund drop in March 2024?"

The system then:

1. Checks the question is safe.
2. Works out what is being asked.
3. Finds the right tables and the right way to join them.
4. Writes a SQL query, checks it, and runs it on Azure PostgreSQL.
5. Returns a result table plus a short written explanation.

The data is a synthetic (generated) mutual fund database: schemes, NAVs, investors, transactions, holdings and so on.

## 2. The two databases

Both live on one Azure Database for PostgreSQL Flexible Server.

| Database | What it holds | Who writes to it |
|---|---|---|
| `mf_data` | The fund data: 20 tables. Stands in for a client's real database. | The data generator only |
| `agent_meta` | What the system has learned about `mf_data`: table notes, links between tables, embeddings (pgvector) and the join graph (Apache AGE). | The metadata pipeline |

## 3. The order everything runs in

```
1. scripts/pg_setup.py                creates the databases and extensions
2. dataplane/schema/apply_schema.py   creates the 20 empty tables in mf_data
3. dataplane/generator/run.py         generates the data and loads it into mf_data
4. metadata/build.py                  reads mf_data, finds links, drafts table notes
5. metadata/review.py                 a person checks and approves the notes and links
6. metadata/publish.py                approved notes -> pgvector, approved links -> AGE graph
7. tests/                             checks each stage worked
8. api/main.py                        the web server: users ask questions here
```

---

## 4. Files at the top level

### `commands.md`
- Every command, in the order you run them: installing tools and libraries, creating each Azure resource from the terminal, filling `.env`, building the database, the metadata pipeline, tests, linting, running the app and calling the API, hosting on App Service, looking after the hosted app, what to re-run after each kind of change, fixes for common problems, and cleaning up.

### `case study.md`
- The project told as a case study: the mutual fund company's problem, why ordinary RAG and plain text-to-SQL fall short, the idea of tables as graph nodes and joins as hops, the databases and tables, the full build (data plane, metadata pipeline, 11-step agent, UI), one real question traced step by step, the safety layers, why each Azure service is used, lessons learned, and next steps.

### `tables.md`
- Every database and table in plain language: what `mf_data` and `agent_meta` hold, the rows in each table, how the tables connect, the hidden links, the personal-data columns, and where each part shows up in the UI.

### `README.md`
- The project's front page: a screenshot of the UI, the architecture diagram, the problem and the idea (tables as nodes, joins as hops), the data, the pipeline end to end, a quickstart, demo questions with what to look for, the safety layers, how to deploy, the project layout, and links to every other document.
- Uses Mermaid diagrams and click-to-expand sections, which GitHub draws on the repository page.

### `pyproject.toml`
- Lists the Python packages the project needs (FastAPI, LangGraph, psycopg, sqlglot, polars, openai, the Azure Monitor OpenTelemetry package, the psycopg tracing package, and others).
- Tells pytest where the tests are and lets every file import project folders (`agent`, `metadata`, ...) by name.
- `uv sync` reads this file and installs everything.

### `uv.lock`
- The exact version of every installed package, written by `uv`.
- Makes sure every machine installs the same versions. Not edited by hand.

### `.env`
- Your real secrets: database URLs, Azure OpenAI key, Content Safety key, Application Insights and LangSmith settings.
- Never committed to git (blocked by `.gitignore`).

### `.env.example`
- A template of `.env` with placeholder values.
- Copy it to `.env` and fill in the real values.
- Two database URLs:
  - `DATA_DB_URL` — `mf_data` (builds tables, loads data, and the agent's questions)
  - `AGENT_DB_URL` — `agent_meta` (metadata pipeline)

### `requirements.txt`
- The exact package versions, written from `uv.lock` by `infra/azure/deploy-app.sh`.
- App Service installs packages from this file. Do not edit by hand; it is rewritten on every deploy.

### `startup.sh`
- The command App Service runs to start the app: uvicorn with one worker.
- One worker (and one instance) because conversation history is held in memory.

### `.gitignore`
- Lists files git must ignore: `.env`, the virtual environment, caches, data files.

---

## 5. `docs/azure/`, `scripts/` and `infra/azure/` — Azure setup and hosting

### `docs/azure/`
Step-by-step portal guides, one per Azure service: what to click, what to select, what goes in `.env`, how to test it, and how to fix common errors.

| File | Service |
|---|---|
| `README.md` | Start here: account, subscription, resource group, order of setup, cost control |
| `postgres.md` | Azure Database for PostgreSQL Flexible Server, with pgvector and AGE |
| `azure-openai.md` | Azure OpenAI: the resource and the two model deployments |
| `content-safety.md` | Azure AI Content Safety (Prompt Shields) |
| `application-insights.md` | Application Insights monitoring |
| `app-service.md` | Hosting the app on App Service |
| `entra-id.md` | Entra ID sign-in in front of the app |

### `docs/architecture/`
The end-to-end architecture diagram.

| File | What it is |
|---|---|
| `architecture.excalidraw` | The diagram, editable at excalidraw.com (☰ → Open) |
| `architecture-preview.png`, `architecture-preview.svg` | Static images of it; the README shows the PNG |
| `build_architecture.py` | Draws the diagram from code. After changing it, run `python3 docs/architecture/build_architecture.py docs/architecture/architecture.excalidraw docs/architecture/architecture-preview.svg`, then make a new PNG from the SVG. |

The diagram has three parts: build time (the data plane and the metadata pipeline), run time (Entra ID, App Service, the 11 agent steps coloured by what each uses, Content Safety, Azure OpenAI, Application Insights), and the PostgreSQL server with `mf_data` and `agent_meta`.

### `docs/images/`
- `ui-answer.png`: a screenshot of the demo page answering the bond-downgrade question, shown at the top of the README.

### `scripts/pg_setup.py`
Run once after creating the Azure server. Safe to run again.

1. Checks the server parameters: `azure.extensions` must allow `VECTOR` and `AGE`, and `shared_preload_libraries` must include `AGE`. If not, it stops and tells you what to change.
2. Creates the two databases, `mf_data` and `agent_meta`, if they do not exist.
3. Turns on pgvector and AGE inside `agent_meta`, then checks AGE really works.

### `infra/azure/create-app-service.sh`
Run once to host the app on Azure. Safe to run again.
1. Creates an App Service plan (Linux, B1) and a Python 3.12 web app, with HTTPS only, Always On and a health check on `/health`.
2. Copies every value from `.env` into the web app's settings.
3. Adds a Postgres firewall rule for each address the web app calls out from.
4. Creates an Entra ID app registration and turns on App Service sign-in. Only accounts in your tenant can sign in; `/health` stays open.

### `infra/azure/deploy-app.sh`
Run after every code change.
1. Writes `requirements.txt` from `uv.lock`.
2. Zips the app code. `.env`, `.venv`, caches, tests and evals are never included.
3. Uploads it; App Service installs the packages and runs `startup.sh`.
4. Waits until `/health` answers.

---

## 6. `config/` — the rules, in YAML

These files hold settings only. No secrets and no code. They can be reviewed by non-developers.

### `config/generator.yaml`
- `seed` — the random seed. The same seed always produces the same data.
- `scale` — how big the investor side is. `0.2` means 40,000 investors and about 0.8 million transactions; `1.0` means 200,000 investors and about 4.6 million.
- `start_date` / `end_date` — the 5-year window the data covers (Sep 2021 to Aug 2026).

### `config/pii.yaml`
- Lists columns that hold personal data: investor PAN, name, email, mobile, date of birth, and the folio bank account.
- The values of these columns are never sent to the AI model and never embedded.

### `config/models.yaml`
- Which Azure OpenAI deployment plays each role:
  - `small` — quick jobs such as understanding the question
  - `strong` — quality jobs: table notes, SQL, commentary
  - `embedding` — turns text into 1,536 numbers for pgvector
- `reasoning_effort` — how much the model thinks before answering.
- Code never names a model directly; it asks for a role and reads the name from here.

### `config/metadata.yaml`
Settings for the metadata pipeline:
- `link_discovery` — how strict link finding is (e.g. 95% of values must match).
- `notes` — how many example values to show the model, how many tables to draft at once.
- `embedding` — which column values get embedded, and which columns are skipped (codes, IDs).
- `graph` — the AGE graph's name and the longest join route considered (6 joins).

### `config/metrics.yaml`
- Approved definitions of business numbers: net flows, redemptions, AUM, rejection rate, SIP cancellations, issuer exposure, group exposure, fund return.
- When a question needs one of these, the SQL model is given the exact definition, so the number is always calculated the same way.

### `config/agent.yaml`
Settings for the question-answering agent:
- `max_sql_retries` — how many times it may rewrite failed SQL (2).
- `run` — query timeout (15 seconds), row cap (5,000), maximum estimated query cost.
- `date_filter_required` — `transactions` queries must filter on `txn_date`.
- `search_top_k`, `summary_rows`, `entity_min_score` — search and summary limits.
- `entity_columns` — which table and column each kind of name (scheme, issuer, city, ...) is looked up in.
- `prompt_shields.fail_open` — what to do if Content Safety cannot be reached. Must be `false` in production.

### `config/access.yaml`, `config/memory.yaml`
- Empty placeholders for later stages (access control and long-term memory).

---

## 7. `dataplane/` — building the fund database

### `dataplane/schema/` — the table definitions

| File | Creates |
|---|---|
| `01_fund_house.sql` | 9 tables: `scheme_categories`, `benchmarks`, `benchmark_values`, `schemes`, `scheme_plans`, `fund_managers`, `scheme_manager_assignments`, `nav_history`, `scheme_aum_monthly` |
| `02_market.sql` | 6 tables: `sectors`, `issuer_groups`, `issuers`, `securities`, `credit_rating_history`, `portfolio_holdings` |
| `03_investors.sql` | 4 tables: `distributors`, `investors`, `folios`, `sip_registrations` |
| `04_transactions.sql` | 1 table: `transactions`, split into 60 monthly partitions |

Points to know:
- Each file first drops its own tables, so running it again rebuilds from empty (and deletes the data).
- Some tables use old-style column names on purpose (`dist_nm`, `sec_cd`, `arn_no`), as real databases often do.
- Three links between tables are deliberately not declared as foreign keys. The metadata pipeline has to discover them:
  - `scheme_aum_monthly.scheme_id` → `schemes.scheme_id`
  - `credit_rating_history.sec_cd` → `securities.security_code`
  - `transactions.arn_code` → `distributors.arn_no`
- Comments are kept in the SQL files only, not stored in the database, so the pipeline must work out table meanings by itself.

### `dataplane/schema/apply_schema.py`
- Runs the four SQL files in order (01 → 04) against `mf_data`.
- Uses one transaction: if any file fails, nothing is changed.
- Run: `uv run python dataplane/schema/apply_schema.py`

### `dataplane/generator/` — making the synthetic data

Each file builds some tables as in-memory data frames (polars). Nothing is written until the end.

| File | What it does |
|---|---|
| `common.py` | Shared helpers: reads `generator.yaml`, business-day and month-end date functions, fixed lists of names and cities. |
| `patterns.py` | The 6 "planted patterns": fixed names, dates and sizes that give demo questions something real to find. Kept outside `config/` so the metadata pipeline cannot read the answers. |
| `fund_house.py` | Builds 8 fund-house tables: categories, benchmarks and their daily values, 40 schemes, plans, managers, and daily NAVs (prices that follow the benchmark). |
| `market.py` | Builds sectors, business groups, 800 issuers, 1,500 securities (shares, bonds, government securities, ...) and credit rating history. |
| `investors.py` | Builds distributors, investors, folios (accounts) and SIP registrations. Personal data is generated already masked (e.g. PAN `ABCPX****F`). |
| `transactions.py` | Builds every purchase, SIP payment, redemption and switch over 5 years. Makes sure no folio ever sells more units than it holds. About 2% of transactions are rejected. |
| `summaries.py` | Calculates monthly AUM (units held × NAV) and monthly portfolio holdings, so both ways of getting AUM agree. |
| `run.py` | Runs the five files above in the right order, then loads all 20 tables. Run: `uv run python -m dataplane.generator.run` |

The 6 planted patterns:
1. A bond downgrade followed by a spike in redemptions (March 2024).
2. One scheme heavily invested in one business group.
3. A manager change followed by a change in performance.
4. SIP cancellations rising in one city tier.
5. One distributor with a much higher rejection rate than the rest.
6. One scheme above the 10% single-issuer limit.

### `dataplane/load/copy_loader.py`
- Loads the generated tables into `mf_data` using Postgres `COPY` (50 to 100 times faster than `INSERT`).
- Empties all 20 tables first, then loads them in an order where every table comes after the tables it points to.
- Runs `ANALYZE` at the end so Postgres has fresh statistics.
- One transaction: if any table fails, the database keeps its previous contents.

### `dataplane/validation/`
- Empty placeholder package. The data checks currently live in `tests/test_stage2_data.py`.

---

## 8. `adapters/` — talking to the fund database

### `adapters/postgres.py`
The only code that queries `mf_data` directly. Four main functions:

| Function | What it does |
|---|---|
| `read_catalog()` | Lists tables, columns, types, primary keys, unique columns, declared foreign keys and estimated row counts, from Postgres system catalogs. |
| `read_column_stats()` | For each column: share of empty values, number of distinct values, most common values (from `pg_stats`). |
| `plan_estimate(sql)` | Runs `EXPLAIN` to get Postgres's cost estimate for a query without running it. |
| `run_readonly(sql)` | Runs a query in a READ ONLY transaction, with a timeout and a row cap. |

Two helpers for the metadata pipeline:
- `sample_values()` — a few real values from a column.
- `value_overlap()` — what share of one column's values exist in another column (used to find undeclared links).

Keeping all database-specific code here means supporting another database engine later only needs a new adapter file.

---

## 9. `metadata/` — teaching the system about the database

This is a three-step process: **build → review → publish**.

### `metadata/llm.py`
The only code that calls Azure OpenAI.
- `chat_json(role, system, user)` — sends a prompt to the `small` or `strong` model and returns its JSON reply as a Python dict.
- `embed(texts)` — turns a list of texts into embeddings (1,536 numbers each).
- Retries automatically when Azure rate-limits; optionally traces calls to LangSmith.

### `metadata/store.py`
- Defines the four tables in `agent_meta`:
  - `meta_tables` — one note per fund table (description, grain, business terms, status)
  - `meta_columns` — one note per column, with a PII flag
  - `meta_edges` — every link between tables, how it was found, how confident, status
  - `meta_embeddings` — the pgvector store, with an HNSW index for fast search
- `connect()` opens a connection to `agent_meta` with AGE's functions available.
- Everything starts as `draft` / `proposed`; only human review makes it `approved`.

### `metadata/links.py`
Finds links between tables in three ways:
1. **declared** — foreign keys in the database. Approved automatically.
2. **same_name** — a column with the same name as another table's key, with values checked.
3. **value_overlap** — names differ, but at least 95% of the values match.

Links found by methods 2 and 3 are saved as `proposed` and wait for review.

### `metadata/notes.py`
- For each table, sends the model: column names, types, empty-value rates, distinct counts, example values (never for PII columns) and links.
- The model returns a description, the grain ("One row per ..."), business terms and a description of every column.
- Keeps a fingerprint (hash of column names and types) so unchanged tables are not redrafted.

### `metadata/build.py` — step 1 of 3
- Reads the catalog and statistics from `mf_data`.
- Finds links (`links.py`) and saves them to `meta_edges`.
- Drafts notes (`notes.py`) for new or changed tables and saves them as `draft`.
- Run: `uv run python -m metadata.build` (add `--redraft` to redo every table).

### `metadata/review.py` — step 2 of 3
- Prints every note and link so a person can check them.
- `--reject-link 7` rejects link number 7.
- `--approve-all` approves everything still pending (rejected items stay rejected).
- Run: `uv run python -m metadata.review`

### `metadata/publish.py` — step 3 of 3
Uses approved items only:
1. Embeds each approved table note into `meta_embeddings` (used to find relevant tables).
2. Embeds the values of name-like text columns (scheme names, issuer names, cities, ...) so loosely typed names can be matched to exact values. PII and code columns are skipped.
3. Rebuilds the AGE graph `schema_graph`: one node per table, one `JOINS` edge per approved link.

Run: `uv run python -m metadata.publish`

### `metadata/retrieval.py`
The three lookups the agent uses:

| Function | What it does | Uses |
|---|---|---|
| `search_tables(question)` | The tables whose notes are closest in meaning to the question | pgvector |
| `match_values(text)` | The exact database value closest to a typed name ("sahyadri credit fund" → "Sahyadri Credit Risk Fund") | pgvector |
| `find_join_path(tables)` | The shortest set of joins that connects the given tables | AGE graph |

---

## 10. `agent/` — answering a question

The agent is a LangGraph workflow of 11 steps in a fixed order.

### `agent/context.py`
Things every step needs, loaded once and reused:
- `agent_config()` — `config/agent.yaml`
- `metrics()` — `config/metrics.yaml`
- `adapter()` — connection to `mf_data`; questions run in a read-only transaction
- `agent_conn()` — a new connection to `agent_meta`
- `table_notes()` — the approved table and column notes

Restart the server after re-running `metadata.publish` so new notes are picked up.

### `agent/graph.py`
- Connects the 11 steps into one workflow and decides where to go after each step.
- Wraps every step to record its duration and send a trace span to Application Insights.
- Keeps each conversation's history in memory (keyed by conversation ID) so follow-up questions work. This memory is lost on restart.
- `ask(question, conversation_id)` runs one question and returns the final state.

The route:
```
guard_input → understand → match_entities → select_tables → find_join_path
→ write_sql → check_sql → run_sql → summarise → write_commentary → remember
```
- If `check_sql` or `run_sql` fails, it goes back to `write_sql` with the error (up to 2 retries), then gives up.
- It stops early if the question is an attack, a request to change data, off topic, or needs clarification.

### `agent/steps/understand.py` — steps 1 to 3
1. **guard_input** — sends the question to Azure Prompt Shields. A detected attack stops here. Also clears the previous turn's results.
2. **understand** — the small model returns: request type, a standalone version of the question (combining follow-ups with the previous question), named things, time range, which approved metrics apply, and whether clarification is needed. Requests to change data, or off-topic ones, stop here.
3. **match_entities** — matches each name to an exact database value using pgvector.

### `agent/steps/build_query.py` — steps 4 to 8
4. **select_tables** — tables needed by matched names and metrics, plus the best matches from vector search.
5. **find_join_path** — asks the AGE graph how to join those tables.
6. **write_sql** — the strong model writes one SELECT, given only the chosen tables, join conditions, exact names and metric definitions. On a retry it also gets the previous SQL and its error.
7. **check_sql** — runs the checks in `guardrails/sql_checks.py`, then `EXPLAIN` to reject queries that are too expensive.
8. **run_sql** — runs the query read-only, with a timeout and row cap.

### `agent/steps/answer.py` — steps 9 to 11
9. **summarise** — plain Python, no model: row count, column totals and the first 20 rows. This is all the model sees of the result.
10. **write_commentary** — the strong model writes 3 to 5 plain sentences, using only numbers from the summary and never claiming one thing caused another.
11. **remember** — saves this question and its SQL (last 6 turns) for follow-up questions.

---

## 11. `guardrails/` — safety checks

### `guardrails/prompt_shields.py`
- Calls Azure AI Content Safety's Prompt Shields with the user's question.
- Returns whether an attack (an attempt to override the AI's instructions) was detected.
- If the service cannot be reached, the result follows `fail_open` in `config/agent.yaml`.

### `guardrails/sql_checks.py`
Checks model-written SQL before it runs, by parsing it with sqlglot (not by searching for words):
1. It parses and is exactly one statement.
2. It only reads (SELECT, or WITH ... SELECT). Nothing that inserts, updates, deletes, creates or drops.
3. It only uses known (approved) tables.
4. Queries on `transactions` compare `txn_date` with a value, so Postgres reads only the needed months.

`tables_in(sql)` returns the real tables a query reads; it is shown in the UI.

---

## 12. `api/` — the web server and pages

### `api/main.py`
FastAPI application. Endpoints:

| Endpoint | What it does |
|---|---|
| `GET /` | The demo page (`ui/index.html`) |
| `POST /query` | Ask a question. Returns status, result table, commentary and every detail (SQL, checks, steps, timings). |
| `GET /schema-graph` | The approved tables and links as JSON, for drawing the graph |
| `GET /graph` | Full-screen clickable schema graph (`ui/graph.html`) |
| `GET /table-counts` | Exact rows in each of the 20 tables, for the page's "Records per table" box (counted once, then reused until restart) |
| `GET /me` | The signed-in user (from Entra ID on App Service; "local" on a laptop) |
| `GET /health` | Returns "ok" if the server is up. Open without sign-in, for health checks. |
| `GET /docs` | FastAPI's automatic page for trying the API |

Run: `uv run uvicorn api.main:app --port 8000`, then open http://localhost:8000

Sign-in:
- On App Service, sign-in is handled by App Service before a request reaches the code. It passes the user's id and email in request headers.
- If those headers are missing on App Service, `/query` refuses the request (401) rather than answering anonymously.
- Each conversation is stored under the user's id, so one user cannot continue or read another user's conversation.
- On a laptop there is no sign-in; every request counts as the user "local".

### `api/telemetry.py`
- Turns on Azure Application Insights if `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, labelling the app `fundgraph-api`.
- Records every HTTP request, every agent step span, and every call out to Azure OpenAI and Content Safety.
- Also switches on PostgreSQL tracing (`PsycopgInstrumentor`), so every database query is recorded with its duration, and PostgreSQL appears in Application Map. This is needed because the Azure package only traces the older `psycopg2` library. Only the SQL text is recorded, never the values sent with it.
- Does nothing if the setting is empty.

### `ui/index.html`
- The demo page. It moved here from `api/static/`; `api/main.py` serves it from this folder.
- For each question it shows:
  1. **Numbers**: tables used, joins, rows returned, SQL attempts, total time.
  2. **GraphRAG trace**: each stage in order, with its time. Safety check, understanding (question, intent, period, metrics), name matching (with match scores), table search (why each table was picked: a matched name, a metric, a kind of data the question needs, or vector search with its similarity score; a ✓ marks tables the SQL used), the AGE join path (discovered links marked), SQL writing and checking (including failed attempts), the run, and the answer.
  3. **Schema graph**: all tables; filled = used by the SQL, outlined = picked but unused, orange = joins used, dashed = discovered links.
  4. **Records per table**: rows in each table, largest first. Hover or click a table to highlight it and its neighbours in the graph and see its description; "This answer" shows only the tables the SQL used.
  5. **Answer**: the commentary and the result table, with **Download CSV**.
  6. **SQL**: a one-line explanation, the highlighted query with **Copy SQL**, every safety check, and any earlier attempts that failed with their error.
- On App Service, shows the signed-in user's name and a "Sign out" link in the header.

### `ui/graph.html`
- Draws the 20 tables and their approved links as a clickable graph, from `/schema-graph`.
- Clicking a table shows its description, grain and row count.

---

## 13. `evals/` — scoring

### `evals/questions/questions.yaml`
- The 18 questions the evaluation asks, each with the correct outcome written beside it.
- Four groups: 6 planted-pattern questions, 6 simple questions, 3 questions that need a hidden link, 3 questions that must be refused.
- Each question lists: the expected status (`completed` or `blocked`), the tables a correct SQL must read, the joins it must use, and words the explanation should contain.
- Read only by the evaluation (`evals/run_eval.py`, not yet written), never by the agent.

### `evals/answer_key/hidden_links.yaml`
- The 3 undeclared links the pipeline should discover, and how each can be found.
- Read only by tests that score the pipeline, never by the pipeline itself.

### `evals/answer_key/planted_patterns.yaml`
- The 6 planted patterns and what each should look like in the data.
- Used for scoring only.

---

## 14. `tests/` — checks for each stage

Run all: `uv run pytest -v`. Most tests need the Azure database and keys from `.env`.

| File | What it checks |
|---|---|
| `test_stage0_setup.py` | Both databases are reachable; pgvector and AGE are installed and AGE is preloaded. |
| `test_azure_openai.py` | The chat model returns an answer and the embedding model returns 1,536 numbers. |
| `test_content_safety.py` | Prompt Shields allows a normal question and blocks an instruction-override attempt. |
| `test_stage1_schema.py` | All 20 tables exist; `transactions` has 60 monthly partitions; the 3 hidden links are not declared; the expected declared links exist. |
| `test_stage2_data.py` | Every table has rows; no broken links; units × NAV = amount; no negative holdings; holding weights add to 100%; both AUM calculations agree; each of the 6 patterns is visible. |
| `test_stage5_metadata.py` | Every table and column has an approved note; PII is flagged; at least 2 of 3 hidden links found; no wrong links approved; no personal data or codes embedded; the graph matches approved links and finds correct join routes; search finds the right tables and values. |
| `test_stage6_agent.py` | Unsafe SQL is refused and safe SQL allowed (offline); each demo question runs end to end with the right result; prompt attacks and delete requests never run. |

---

## 15. Empty placeholder folders

| Folder | Intended for |
|---|---|
| `jobs/` | Scheduled background jobs (e.g. re-running the metadata pipeline) |
| `memory/` | Long-term conversation memory (Stage 8) |
| `dataplane/validation/` | Data validation checks outside the tests |

Each contains only an empty `__init__.py` for now.

---

## 16. Caches (generated, safe to ignore)

- `.pytest_cache/` — pytest remembers which tests failed last time.
- `.ruff_cache/` — the ruff linter's cache.
- `__pycache__/` — compiled Python files.

All are ignored by git and can be deleted at any time.
