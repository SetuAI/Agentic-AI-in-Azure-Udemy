# Commands, end to end

Every command for this project, in the order you run them, from installing the tools to running the app on Azure. Each section says when you need it. Section 13 lists what to re-run after each kind of change.

## Contents
0. [Before you start](#0-before-you-start)
1. [Install the tools (once per computer)](#1-install-the-tools-once-per-computer)
2. [Get the project and install its libraries](#2-get-the-project-and-install-its-libraries)
3. [Create the Azure resources](#3-create-the-azure-resources)
4. [Fill in `.env`](#4-fill-in-env)
5. [Build the fund database](#5-build-the-fund-database)
6. [Build, review and publish the metadata](#6-build-review-and-publish-the-metadata)
7. [Run the tests](#7-run-the-tests)
8. [Check code style](#8-check-code-style)
9. [Run the app on your laptop](#9-run-the-app-on-your-laptop)
10. [Use the API from the terminal](#10-use-the-api-from-the-terminal)
11. [Host the app on Azure App Service](#11-host-the-app-on-azure-app-service)
12. [Run and look after the hosted app](#12-run-and-look-after-the-hosted-app)
13. [What to re-run after a change](#13-what-to-re-run-after-a-change)
14. [Fixing common problems](#14-fixing-common-problems)
15. [Saving money and cleaning up](#15-saving-money-and-cleaning-up)
16. [Git](#16-git)
17. [The whole run, in one block](#17-the-whole-run-in-one-block)

---

## 0. Before you start

**Where to run commands.** Run everything in a terminal **from the project folder**, unless a section says otherwise:
```bash
cd Agentic-AI-in-Azure-Udemy/Azure-Graph-RAG-on-DBs    # the project folder
```

**Names used in this file.** These are the names from this project's own setup. Replace them with yours.

| Placeholder in this file | This project's value |
|---|---|
| Resource group | `fundgraph-rg-1` |
| Postgres server | `fundgraphdbexample1` (Central India) |
| Azure OpenAI resource | `graph-rag-001-resource` |
| Content Safety resource | `fundgraphprojexample` |
| Web app | `fundgraph-app` |
| App Service plan | `fundgraph-plan` |

**How to read the commands:**
- `uv run <command>` runs the command inside the project's own Python environment. You don't need to activate anything first.
- Lines starting with `#` are comments. You don't type them.
- A `\` at the end of a line means the command continues on the next line.
- In this file, `<...>` means "put your own value here".

**The order matters.** Each numbered section needs the ones before it to have succeeded.

---

## 1. Install the tools (once per computer)

These are for a Mac, using Homebrew.

```bash
# Homebrew (the Mac package manager), if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# uv: installs Python and the project's libraries
brew install uv

# Azure CLI: creates and manages Azure resources from the terminal
brew install azure-cli

# git: version control (often already installed)
brew install git
```

Check they work:
```bash
uv --version
az --version
git --version
```

You **do not** need PostgreSQL, Docker or a separate Python install. `uv` brings its own Python, and the `psycopg[binary]` library brings its own Postgres client.

---

## 2. Get the project and install its libraries

```bash
cd Agentic-AI-in-Azure-Udemy/Azure-Graph-RAG-on-DBs    # the project folder

# Create .venv and install every library at the exact versions in uv.lock,
# including the dev tools (pytest, ruff)
uv sync

# Optional: activate the environment, so you can type "python" and "pytest" without "uv run"
source .venv/bin/activate
deactivate              # to leave it again
```

**Libraries, later on:**
```bash
uv add <package>             # add a library (updates pyproject.toml and uv.lock)
uv add --dev <package>       # add a development-only library
uv remove <package>          # remove a library
uv lock --upgrade            # move every library to its newest allowed version
uv sync                      # install whatever uv.lock now says
uv pip list                  # see what is installed
uv run python --version      # the Python version the project uses
```

---

## 3. Create the Azure resources

You can do this **in the portal**, following `docs/azure/` (one guide per service), or **from the terminal** with the commands below. Both give the same result.

### 3.1 Sign in to Azure
```bash
az login                                         # opens a browser to sign in
az account list -o table                         # your subscriptions
az account set --subscription "<subscription name or id>"
az account show -o table                         # check which one is active
```

### 3.2 Resource group
```bash
az group create --name fundgraph-rg-1 --location centralindia
```

### 3.3 Azure Database for PostgreSQL
```bash
# Your public IP, for the firewall
MYIP=$(curl -s https://api.ipify.org); echo $MYIP

# Create the server (takes 5 to 10 minutes). You are asked for nothing;
# type your own strong password in place of <password>.
az postgres flexible-server create \
    --resource-group fundgraph-rg-1 --name fundgraphdbexample1 --location centralindia \
    --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
    --admin-user fundgraph_admin --admin-password '<password>' \
    --public-access $MYIP

# Allow the two extensions, and preload AGE
az postgres flexible-server parameter set -g fundgraph-rg-1 -s fundgraphdbexample1 \
    --name azure.extensions --value VECTOR,AGE
az postgres flexible-server parameter set -g fundgraph-rg-1 -s fundgraphdbexample1 \
    --name shared_preload_libraries --value pg_cron,pg_stat_statements,age

# Preloading needs a restart
az postgres flexible-server restart -g fundgraph-rg-1 -n fundgraphdbexample1

# Check
az postgres flexible-server parameter show -g fundgraph-rg-1 -s fundgraphdbexample1 --name azure.extensions --query value -o tsv
az postgres flexible-server parameter show -g fundgraph-rg-1 -s fundgraphdbexample1 --name shared_preload_libraries --query value -o tsv
az postgres flexible-server show -g fundgraph-rg-1 -n fundgraphdbexample1 --query "{state:state, host:fullyQualifiedDomainName}" -o table
```
If creation fails with `SkuNotAvailable`, pick another region (e.g. `southindia`, `centralus`), or check what's offered: `az postgres flexible-server list-skus --location <region> -o table`.

**Firewall** (allow your laptop; repeat whenever your IP changes):
```bash
az postgres flexible-server firewall-rule create -g fundgraph-rg-1 --server-name fundgraphdbexample1 \
    --name my-laptop --start-ip-address $(curl -s https://api.ipify.org) --end-ip-address $(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule list -g fundgraph-rg-1 --server-name fundgraphdbexample1 -o table
az postgres flexible-server firewall-rule delete -g fundgraph-rg-1 --server-name fundgraphdbexample1 --name my-laptop --yes
```

**Forgotten password:**
```bash
az postgres flexible-server update -g fundgraph-rg-1 -n fundgraphdbexample1 --admin-password '<new password>'
```

### 3.4 Azure OpenAI
```bash
az cognitiveservices account create -g fundgraph-rg-1 -n graph-rag-001-resource \
    --location northcentralus --kind OpenAI --sku S0 --custom-domain graph-rag-001-resource

# The two model deployments. Their names must match config/models.yaml.
az cognitiveservices account deployment create -g fundgraph-rg-1 -n graph-rag-001-resource \
    --deployment-name gpt-5.1 --model-name gpt-5.1 --model-version 2025-11-13 \
    --model-format OpenAI --sku-name GlobalStandard --sku-capacity 50
az cognitiveservices account deployment create -g fundgraph-rg-1 -n graph-rag-001-resource \
    --deployment-name text-embedding-3-small --model-name text-embedding-3-small --model-version 1 \
    --model-format OpenAI --sku-name GlobalStandard --sku-capacity 250

# Check
az cognitiveservices account deployment list -g fundgraph-rg-1 -n graph-rag-001-resource \
    --query "[].{deployment:name, model:properties.model.name, state:properties.provisioningState}" -o table

# Endpoint and key, for .env. Use the RESOURCE endpoint
# (https://<resource>.openai.azure.com/), not an AI Foundry project URL.
az cognitiveservices account show -g fundgraph-rg-1 -n graph-rag-001-resource --query properties.endpoint -o tsv
az cognitiveservices account keys list -g fundgraph-rg-1 -n graph-rag-001-resource --query key1 -o tsv
```
Model names, versions and regions change over time. If a deployment command fails, see what is available: `az cognitiveservices account list-models -g fundgraph-rg-1 -n graph-rag-001-resource -o table`.

### 3.5 Azure AI Content Safety
```bash
az cognitiveservices account create -g fundgraph-rg-1 -n fundgraphprojexample \
    --location eastus --kind ContentSafety --sku S0 --custom-domain fundgraphprojexample

az cognitiveservices account show -g fundgraph-rg-1 -n fundgraphprojexample --query properties.endpoint -o tsv
az cognitiveservices account keys list -g fundgraph-rg-1 -n fundgraphprojexample --query key1 -o tsv
```

### 3.6 Application Insights (optional)
```bash
az extension add --name application-insights      # once
az monitor app-insights component create -g fundgraph-rg-1 --app fundgraph-insights \
    --location centralindia --kind web --application-type web
az monitor app-insights component show -g fundgraph-rg-1 --app fundgraph-insights --query connectionString -o tsv
```

### 3.7 List everything you created
```bash
az resource list -g fundgraph-rg-1 -o table
```

---

## 4. Fill in `.env`

```bash
cp .env.example .env        # first time only, then open .env in your editor
```

| Variable | Where the value comes from |
|---|---|
| `DATA_DB_URL` | `postgresql://<admin>:<password>@<server>.postgres.database.azure.com:5432/mf_data?sslmode=require` |
| `AGENT_DB_URL` | The same, ending `/agent_meta?sslmode=require` |
| `AZURE_OPENAI_ENDPOINT` | Section 3.4 (the resource URL) |
| `AZURE_OPENAI_API_KEY` | Section 3.4 |
| `CHAT_DEPLOYMENT` | `gpt-5.1` |
| `EMBED_DEPLOYMENT` | `text-embedding-3-small` |
| `CONTENT_SAFETY_ENDPOINT`, `CONTENT_SAFETY_KEY` | Section 3.5 |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Section 3.6 (leave empty to switch tracing off) |
| `LANGSMITH_*` | Optional; leave empty |

**If the Postgres password has special characters** (`@ : / ? # % &`), encode it, then paste the result into both URLs:
```bash
uv run python -c "from urllib.parse import quote; print(quote('<password>', safe=''))"
```

**Check the services before going further:**
```bash
uv run pytest tests/test_azure_openai.py tests/test_content_safety.py -v
```

Never commit `.env` to git. It holds your secrets.

---

## 5. Build the fund database

Run these in order. Each one is safe to run again.

```bash
# 5.1 Create the mf_data and agent_meta databases; switch on pgvector and AGE
uv run python scripts/pg_setup.py

# 5.2 Create the 20 tables (drops and recreates them, so any old data is cleared)
uv run python dataplane/schema/apply_schema.py

# 5.3 Generate the data and load it (about 100 seconds; empties the tables first)
uv run python -m dataplane.generator.run
```

**Expected output:**

| Step | Expected |
|---|---|
| 5.1 | `database mf_data: created` (or `exists`), then `agent_meta: pgvector and AGE ready` |
| 5.2 | `Schema applied: all 20 tables created in mf_data.` |
| 5.3 | One line per table, ending `Done: 1,132,667 rows across 20 tables` |

**More or less data:** set `scale` in `config/generator.yaml`, then re-run 5.3 and everything after it:
- `0.2` (the default): 40,000 investors, about 0.7 million transactions.
- `1.0`: 200,000 investors, about 4.6 million transactions. It takes much longer to load.

**Check the database:**
```bash
uv run pytest tests/test_stage0_setup.py tests/test_stage1_schema.py tests/test_stage2_data.py -v
```

---

## 6. Build, review and publish the metadata

This step teaches the agent about the database. It uses Azure OpenAI.

```bash
# 6.1 Build: read the tables, find links (including hidden ones), draft notes.
#     Everything it writes is unapproved. About 2 minutes.
uv run python -m metadata.build
uv run python -m metadata.build --redraft        # redraft notes for every table, not just new or changed ones

# 6.2 Review: read what was drafted
uv run python -m metadata.review                 # notes and links
uv run python -m metadata.review --links         # links only, numbered

#     Reject any wrong link by its number (repeat --reject-link for several)
uv run python -m metadata.review --reject-link 7
uv run python -m metadata.review --reject-link 7 --reject-link 9

#     Approve everything still pending
uv run python -m metadata.review --approve-all

# 6.3 Publish: approved notes -> pgvector embeddings; approved links -> AGE graph.
#     Safe to re-run; it rebuilds everything it owns. About 75 seconds.
uv run python -m metadata.publish
```

**Expected:**

| Step | Expected |
|---|---|
| 6.1 | `19 declared, 3 undeclared links proposed`, then one `drafted …` line per table |
| 6.2 | The 3 hidden links are listed: `credit_rating_history.sec_cd → securities.security_code`, `scheme_aum_monthly.scheme_id → schemes.scheme_id` and `transactions.arn_code → distributors.arn_no` |
| 6.3 | `Graph 'schema_graph': 20 table nodes, 22 join edges.` |

**Check the metadata:**
```bash
uv run pytest tests/test_stage5_metadata.py -v
```

---

## 7. Run the tests

The tests run against your real Azure services, so `.env` must be filled in and Sections 5 and 6 done.

```bash
uv run pytest                                    # all 66 tests (about 2 minutes)
uv run pytest -v                                 # with each test's name
uv run pytest -q                                 # short output
uv run pytest -x                                 # stop at the first failure
uv run pytest --lf                               # re-run only the tests that failed last time
uv run pytest -k "join"                          # only tests whose name contains "join"
uv run pytest tests/test_stage6_agent.py         # one file
uv run pytest tests/test_stage6_agent.py::test_demo_rejection_rate   # one test
uv run pytest -v -s                              # also show print() output
```

**The test files, in stage order:**

| File | Checks | Needs |
|---|---|---|
| `tests/test_azure_openai.py` | Chat and embedding deployments answer | `.env` (OpenAI) |
| `tests/test_content_safety.py` | Prompt Shields lets a normal question through, and blocks an instruction-override attack | `.env` (Content Safety) |
| `tests/test_stage0_setup.py` | Database reachable; vector and AGE installed, AGE preloaded | 5.1 |
| `tests/test_stage1_schema.py` | The 20 tables exist; `transactions` has 60 monthly partitions; the hidden links really have no foreign key, and the declared ones do | 5.2 |
| `tests/test_stage2_data.py` | Every table has rows; the data is consistent (units × NAV = amount, no negative holdings, weights add up to 100%…); the six planted patterns are present | 5.3 |
| `tests/test_stage5_metadata.py` | Notes, links, embeddings and graph are published correctly | 6 |
| `tests/test_stage6_agent.py` | The SQL checks refuse unsafe SQL and allow safe SQL; the demo questions answer correctly (including a follow-up); a prompt attack and a delete request are blocked | 6 |

**Evaluations:** the questions and answer keys are in `evals/` (`questions/questions.yaml`, `answer_key/`). The runner that scores them, `evals/run_eval.py`, has not been written yet, so there is no command for it today.

---

## 8. Check code style

```bash
uv run ruff check .                  # report problems
uv run ruff check . --fix            # fix the ones that can be fixed automatically
uv run ruff check agent/ api/        # only some folders
uv run ruff format --check .         # see which files would be reformatted (nothing is changed)
```

---

## 9. Run the app on your laptop

```bash
uv run uvicorn api.main:app --port 8000
```
Then open **http://localhost:8000**:
- **http://localhost:8000/graph**: the full-screen schema graph
- **http://localhost:8000/docs**: try the API by hand

**Variations:**
```bash
uv run uvicorn api.main:app --port 8000 --reload     # restart automatically when Python files change
uv run uvicorn api.main:app --port 8080              # another port
uv run uvicorn api.main:app --host 0.0.0.0 --port 8000   # reachable from other devices on your network
open http://localhost:8000                           # open the page from the terminal (Mac)
```

**Stop it:** press `Ctrl+C` in its terminal. If it's running in the background, or the port is stuck:
```bash
lsof -i :8000                    # what is using port 8000
kill <PID>                       # stop it, using the PID from the line above
pkill -f "uvicorn api.main:app"  # or stop every copy of the app
```

**When a restart is needed:**

| You changed | Restart needed? |
|---|---|
| `ui/index.html` or `ui/graph.html` | No. Refresh the browser. |
| Python code | Yes, unless you started with `--reload` |
| `config/*.yaml` | Yes. Config is read once per run (this includes `--reload`, which only watches Python files). |
| `.env` | Yes |
| The data (Section 5), or the metadata (Section 6) | Yes. The row counts and table notes are read once per run. |

---

## 10. Use the API from the terminal

With the app running (Section 9):
```bash
# Is it up?
curl http://localhost:8000/health

# Ask a question (starts a new conversation)
curl -s http://localhost:8000/query -H 'Content-Type: application/json' \
    -d '{"question": "Which distributor has the highest transaction rejection rate?"}' | python3 -m json.tool

# Follow-up in the same conversation: copy conversation_id from the previous answer
curl -s http://localhost:8000/query -H 'Content-Type: application/json' \
    -d '{"question": "Now show the top 5", "conversation_id": "<conversation_id>"}' | python3 -m json.tool

# Just the SQL and the result rows
curl -s http://localhost:8000/query -H 'Content-Type: application/json' \
    -d '{"question": "Which distributor has the highest transaction rejection rate?"}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status']); print(d['details']['sql']); print(d['tables'][0]['rows'] if d['tables'] else 'no rows')"

# The schema graph (tables and approved links)
curl -s http://localhost:8000/schema-graph | python3 -m json.tool

# Rows in each table
curl -s http://localhost:8000/table-counts | python3 -m json.tool

# Who you are signed in as ("local" on a laptop)
curl -s http://localhost:8000/me
```

---

## 11. Host the app on Azure App Service

Do Sections 1 to 9 first, and check the app works on your laptop. Full explanations are in `docs/azure/app-service.md` and `docs/azure/entra-id.md`.

### 11.1 Choose a way to set up the web app

**Option A, one script:** it creates the plan and web app, copies `.env` into the app settings, opens the Postgres firewall for the app, and switches on Entra ID sign-in.
```bash
az login
RESOURCE_GROUP=fundgraph-rg-1 APP_NAME=fundgraph-app PG_SERVER=fundgraphdbexample1 LOCATION=centralindia \
    bash infra/azure/create-app-service.sh
```
| Variable | Meaning |
|---|---|
| `RESOURCE_GROUP` | Required. The resource group to use. |
| `APP_NAME` | Required. The web app's name. |
| `PG_SERVER` | Required. The Postgres server name. |
| `LOCATION` | Optional. Default `eastus`; use your Postgres region. |
| `PLAN_SKU` | Optional. Default `B1`. |
| `PG_RESOURCE_GROUP` | Optional. Use it if Postgres is in another resource group. |

Then go to 11.3.

**Option B, the portal:** follow `docs/azure/app-service.md` Part B, then `docs/azure/entra-id.md` Part A. Then run the commands in 11.2 for anything you didn't set in the portal.

### 11.2 Setting up the web app by hand, command by command
```bash
# Plan and web app
az appservice plan create -g fundgraph-rg-1 -n fundgraph-plan --is-linux --sku B1 --location centralindia
az webapp create -g fundgraph-rg-1 -p fundgraph-plan -n fundgraph-app --runtime "PYTHON:3.13"

# Startup command, Always On, HTTPS only, health check
az webapp config set -g fundgraph-rg-1 -n fundgraph-app --startup-file "bash startup.sh" --always-on true
az webapp update -g fundgraph-rg-1 -n fundgraph-app --https-only true
az webapp config set -g fundgraph-rg-1 -n fundgraph-app --generic-configurations '{"healthCheckPath": "/health"}'

# App settings: one per .env line, plus the build switch. (Values with special
# characters must be quoted. Never add POSTGRES_USER, POSTGRES_PASSWORD or WEBSITE_SITE_NAME.)
az webapp config appsettings set -g fundgraph-rg-1 -n fundgraph-app --settings \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true \
    CHAT_DEPLOYMENT=gpt-5.1 EMBED_DEPLOYMENT=text-embedding-3-small \
    AZURE_OPENAI_ENDPOINT=https://graph-rag-001-resource.openai.azure.com/ \
    AZURE_OPENAI_API_KEY='<key>' \
    DATA_DB_URL='<from .env>' AGENT_DB_URL='<from .env>' \
    CONTENT_SAFETY_ENDPOINT='<from .env>' CONTENT_SAFETY_KEY='<key>' \
    APPLICATIONINSIGHTS_CONNECTION_STRING='<from .env>'

# Check the settings by name (this does not print the values)
az webapp config appsettings list -g fundgraph-rg-1 -n fundgraph-app --query "[].name" -o tsv | sort

# Postgres firewall: one rule per outbound address of the app (run in bash; about 10 minutes)
bash -c '
IPS=$(az webapp show -g fundgraph-rg-1 -n fundgraph-app --query possibleOutboundIpAddresses -o tsv | tr "," " ")
i=0
for ip in $IPS; do
  i=$((i+1))
  az postgres flexible-server firewall-rule create -g fundgraph-rg-1 --server-name fundgraphdbexample1 \
      --name "app-$i" --start-ip-address $ip --end-ip-address $ip -o none
done
echo "added $i rules"'

# Sign-in: add the Microsoft identity provider in the portal (entra-id.md Part A),
# then keep /health open for App Service's health check:
az webapp auth update -g fundgraph-rg-1 -n fundgraph-app --excluded-paths '["/health"]'
az webapp auth show -g fundgraph-rg-1 -n fundgraph-app \
    --query "{enabled:properties.platform.enabled, excluded:properties.globalValidation.excludedPaths}" -o json
```

### 11.3 Deploy the code
```bash
az login
RESOURCE_GROUP=fundgraph-rg-1 APP_NAME=fundgraph-app bash infra/azure/deploy-app.sh
```
The script:
1. Writes `requirements.txt` from `uv.lock`.
2. Zips the code, leaving out `.env`, `.venv`, caches, tests, evals, infra and `.md` files.
3. Uploads the zip. App Service installs the packages (3 to 5 minutes the first time) and runs `startup.sh`.
4. Waits for `/health`.

To write `requirements.txt` yourself, without deploying:
```bash
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
```

**If it ends with `504 GatewayTimeout`,** the deploy has usually still worked. Check:
```bash
az webapp log deployment list -g fundgraph-rg-1 -n fundgraph-app \
    --query "[0].{status:status, complete:complete, active:active, end:end_time}" -o table
```
`status` 4 with `active` True means it succeeded; 3 means it failed.

### 11.4 Check the live site
```bash
# The address
az webapp show -g fundgraph-rg-1 -n fundgraph-app --query defaultHostName -o tsv

# Health: no sign-in needed. Expected: {"status":"ok"}
curl https://$(az webapp show -g fundgraph-rg-1 -n fundgraph-app --query defaultHostName -o tsv)/health

# Open it in the browser (you will be asked to sign in)
open https://$(az webapp show -g fundgraph-rg-1 -n fundgraph-app --query defaultHostName -o tsv)
```
After signing in, check three things:
- your name appears at the top right,
- **Records per table** shows the row total,
- a question gets an answer.

---

## 12. Run and look after the hosted app

```bash
# Live logs (prints, errors). Switch on file logging once, then tail.
az webapp log config -g fundgraph-rg-1 -n fundgraph-app --application-logging filesystem --level information
az webapp log tail -g fundgraph-rg-1 -n fundgraph-app          # Ctrl+C to stop
az webapp log download -g fundgraph-rg-1 -n fundgraph-app --log-file webapp-logs.zip

# Deployment history
az webapp log deployment list -g fundgraph-rg-1 -n fundgraph-app -o table
az webapp log deployment show -g fundgraph-rg-1 -n fundgraph-app

# Restart, stop, start
az webapp restart -g fundgraph-rg-1 -n fundgraph-app
az webapp stop -g fundgraph-rg-1 -n fundgraph-app      # site goes offline (the plan is still billed)
az webapp start -g fundgraph-rg-1 -n fundgraph-app

# State and settings
az webapp show -g fundgraph-rg-1 -n fundgraph-app --query "{state:state, host:defaultHostName}" -o table
az webapp config show -g fundgraph-rg-1 -n fundgraph-app \
    --query "{runtime:linuxFxVersion, startup:appCommandLine, alwaysOn:alwaysOn, health:healthCheckPath}" -o json

# Change one setting (the app restarts)
az webapp config appsettings set -g fundgraph-rg-1 -n fundgraph-app --settings NAME='value'
az webapp config appsettings delete -g fundgraph-rg-1 -n fundgraph-app --setting-names NAME

# Open a terminal inside the running app (for checking files, not for editing code)
az webapp ssh -g fundgraph-rg-1 -n fundgraph-app
```

**Check monitoring data (Application Insights):**
```bash
az extension add --name application-insights      # once

# Which Application Insights resources exist (AI Foundry may have created a second one)
az resource list --resource-type microsoft.insights/components --query "[].{name:name, rg:resourceGroup}" -o table

# The app id of the one the app sends to
APPID=$(az resource show -g fundgraph-rg-1 -n fundgraph-insights \
    --resource-type microsoft.insights/components --query properties.AppId -o tsv)

# Requests in the last 24 hours (--offset sets how far back; the default is only 1 hour)
az monitor app-insights query --apps $APPID --offset 24h --analytics-query "requests | summarize n=count() by name"

# What the app calls: Azure OpenAI and Content Safety (HTTP), PostgreSQL (postgresql), agent steps (InProc)
az monitor app-insights query --apps $APPID --offset 24h \
    --analytics-query "dependencies | summarize calls=count(), avg_ms=avg(duration) by type, target | order by calls desc"
```
In the portal: **fundgraph-insights → Investigate → Application map** (set **Last 24 hours**, then **Refresh**).

Keep the app at **one instance**, because conversation history is held in memory. Don't scale out.

---

## 13. What to re-run after a change

Find your change in the left column, then run the steps on the right, in order.

| What changed | Re-run |
|---|---|
| **Table definitions** (`dataplane/schema/*.sql`) | 5.2 → 5.3 → 6.1 → 6.2 → 6.3 → restart the app |
| **Generated data, or `scale`/`seed`** in `config/generator.yaml` | 5.3 → 6.1 → 6.2 → 6.3 → restart the app |
| **Table notes need redrafting** | `metadata.build --redraft` → 6.2 → 6.3 → restart the app |
| **A wrong link** | `metadata.review --reject-link <id>` → 6.3 → restart the app |
| **`config/metrics.yaml` or `config/agent.yaml`** | Restart the app (locally); redeploy (Azure) |
| **`config/models.yaml`** (a different model deployment) | Restart the app. If the *embedding* model changed: 6.3 first, because all vectors must come from the same model. |
| **Python code** | Restart the app (or use `--reload`); for Azure, 11.3 |
| **The UI** (`ui/*.html`) | Refresh the browser locally; for Azure, 11.3 |
| **A new library** | `uv add <pkg>` → restart; for Azure, 11.3 (it regenerates `requirements.txt`) |
| **`.env`** | Restart the app. On Azure, change the same value in the web app's app settings. |
| **Your laptop's IP address** (you get connection timeouts) | Add a firewall rule for the new IP (Section 3.3) |
| **The Postgres password** | Update both URLs in `.env` (encode special characters) and in the web app's app settings |
| **An Azure OpenAI or Content Safety key was regenerated** | Update `.env` and the app settings |
| **The Entra ID client secret is about to expire** | `docs/azure/entra-id.md` Part D |
| **The web app's outbound IPs changed** (e.g. after changing plan) | Run the firewall loop in 11.2 again |
| **Everything, from scratch** | Section 17 |

---

## 14. Fixing common problems

| Problem | Command or fix |
|---|---|
| `failed to resolve host ...postgres.database.azure.com` | The server name in `.env` is wrong. List your servers: `az postgres flexible-server list --query "[].fullyQualifiedDomainName" -o tsv` |
| `connection timeout` to Postgres | Firewall. Your IP: `curl -s https://api.ipify.org`. Rules: `az postgres flexible-server firewall-rule list -g fundgraph-rg-1 --server-name fundgraphdbexample1 -o table` |
| `password authentication failed` | Wrong login or password, or special characters not encoded (Section 4). Admin login: `az postgres flexible-server show -g fundgraph-rg-1 -n fundgraphdbexample1 --query administratorLogin -o tsv` |
| `pg_setup.py` says an extension is not allowed or not preloaded | Section 3.3 parameters, then `az postgres flexible-server restart -g fundgraph-rg-1 -n fundgraphdbexample1` |
| Server is stopped | `az postgres flexible-server start -g fundgraph-rg-1 -n fundgraphdbexample1` |
| OpenAI `404` on embeddings while chat works | `AZURE_OPENAI_ENDPOINT` is an AI Foundry project URL. Use `https://<resource>.openai.azure.com/` |
| OpenAI `404` on everything, or `DeploymentNotFound` | Deployment names don't match `config/models.yaml`: `az cognitiveservices account deployment list -g fundgraph-rg-1 -n graph-rag-001-resource -o table` |
| OpenAI `401` | Wrong key: `az cognitiveservices account keys list -g fundgraph-rg-1 -n graph-rag-001-resource` |
| OpenAI `429` (too many requests) | The client already retries up to 6 times. If it keeps happening, raise the deployment's capacity in the portal. |
| `Address already in use` when starting the app | `lsof -i :8000`, then `kill <PID>`, or use another `--port` |
| `ModuleNotFoundError` locally | `uv sync`, and use `uv run` |
| A question is refused or returns 0 rows | Look at the GraphRAG trace on the page: which tables were picked, and the SQL. A business term with no approved definition can be added to `config/metrics.yaml`. |
| Azure site shows `Application Error` | `az webapp log tail -g fundgraph-rg-1 -n fundgraph-app`. It's often a missing app setting (`KeyError`). |
| Azure: `ModuleNotFoundError` | `SCM_DO_BUILD_DURING_DEPLOYMENT` must be `true`, then 11.3 again |
| Azure: every question returns `401` | Sign-in is off, or not set to "Require authentication" (`docs/azure/entra-id.md`) |
| Azure: health check failing | `/health` must be excluded from sign-in: the `az webapp auth update ... --excluded-paths` command in 11.2 |
| Azure: questions time out | The app's outbound IPs aren't in the Postgres firewall: the loop in 11.2 |
| Application Map says "No data available" | Wrong resource: open the one whose connection string is in `.env` (the commands in Section 12 list them). Then set **Last 24 hours** and **Refresh**. |
| No PostgreSQL node in Application Map | The app was started before tracing was added: `uv sync` and restart; on Azure, 11.3 again |

---

## 15. Saving money and cleaning up

```bash
# Postgres: stop when not in use (Azure restarts it automatically after 7 days)
az postgres flexible-server stop -g fundgraph-rg-1 -n fundgraphdbexample1
az postgres flexible-server start -g fundgraph-rg-1 -n fundgraphdbexample1

# App Service: the plan is billed every hour it exists, even if the app is stopped.
# Deleting the plan is the only way to stop the charge.
az webapp delete -g fundgraph-rg-1 -n fundgraph-app
az appservice plan delete -g fundgraph-rg-1 -n fundgraph-plan --yes

# Delete EVERYTHING in the resource group (cannot be undone)
az group delete --name fundgraph-rg-1 --yes --no-wait
```

The app registration that Entra ID sign-in created is not inside the resource group. To remove it: **Microsoft Entra ID → App registrations → All applications →** your app **→ Delete**.

---

## 16. Git

```bash
git status                       # what has changed
git diff                         # the changes themselves
git add <files>                  # stage files (never .env)
git commit -m "Describe the change"
git log --oneline -10            # recent commits
git push                         # send to the remote, if one is set up
```
Check that `.env` is listed in `.gitignore` before your first commit.

---

## 17. The whole run, in one block

Everything after the Azure resources exist and `.env` is filled in (Sections 3 and 4), from an empty database to a deployed app:

```bash
cd Agentic-AI-in-Azure-Udemy/Azure-Graph-RAG-on-DBs    # the project folder

# Libraries
uv sync

# Database
uv run python scripts/pg_setup.py
uv run python dataplane/schema/apply_schema.py
uv run python -m dataplane.generator.run

# Metadata (read the review output before approving)
uv run python -m metadata.build
uv run python -m metadata.review --links
uv run python -m metadata.review --approve-all
uv run python -m metadata.publish

# Tests
uv run pytest -q

# Local app: open http://localhost:8000, Ctrl+C to stop
uv run uvicorn api.main:app --port 8000

# Azure (after the web app is set up, Section 11.1 or 11.2)
az login
RESOURCE_GROUP=fundgraph-rg-1 APP_NAME=fundgraph-app bash infra/azure/deploy-app.sh
curl https://$(az webapp show -g fundgraph-rg-1 -n fundgraph-app --query defaultHostName -o tsv)/health
```
