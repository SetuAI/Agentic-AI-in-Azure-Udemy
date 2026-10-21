# Application Insights

## What it is used for
Application Insights collects monitoring data from the running app. `api/telemetry.py` switches it on when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, and then it records:

- every HTTP request to the API (`/query`, `/schema-graph`, ...), with duration and result
- every agent step (`agent.guard_input`, `agent.write_sql`, `agent.run_sql`, ...), as a span inside the request, with its duration
- every call out to **Azure OpenAI** and **Content Safety** (recorded automatically as HTTP dependencies)
- every **PostgreSQL** query, as a dependency of type `postgresql`, for both `mf_data` and `agent_meta`. Only the SQL text is recorded, with `%s` placeholders; the values sent with it (such as embedding vectors) are not.
- errors, with the stack trace

**Why PostgreSQL needs one extra line.** The Azure package (`azure-monitor-opentelemetry`) only hooks into the older `psycopg2` library. This project uses psycopg 3, so `api/telemetry.py` also switches on `PsycopgInstrumentor` from the `opentelemetry-instrumentation-psycopg` package. Without it, the database is invisible in Application Insights.

It is optional. Without the connection string the app runs exactly the same, and nothing is sent.

**Log Analytics workspace.** Application Insights stores its data in a Log Analytics workspace. The portal creates one for you if you do not have one.

> **Watch out: you may already have a second Application Insights resource.** Creating an AI Foundry project (for Azure OpenAI) can create one automatically, named like `<resource>-appinsights`. The app does **not** send data there. Only the resource whose connection string is in `.env` gets data. In this project's own setup, that is `fundgraph-insights`, and the Foundry one, `graph-rag-001-resource-appinsights`, stays empty. Opening Application Map on the empty one shows **"No data available"**.

---

## Part 1 — Create the Application Insights resource

1. Go to https://portal.azure.com.
2. In the top search bar type **Application Insights** and open it.
3. Click **+ Create**.

### Basics tab
| Field | What to enter |
|---|---|
| Subscription | Your subscription |
| Resource group | `rg-fundgraph` |
| Name | e.g. `fundgraph-insights` |
| Region | `East US` |
| Resource mode | **Workspace-based** (the only option on new resources) |
| Log Analytics Workspace | Leave the default (a new one is created), or choose an existing one in `rg-fundgraph` |

Click **Review + create** → **Create**. Then **Go to resource**.

---

## Part 2 — Copy the connection string

1. On the resource's **Overview** page, find **Connection String** (top right of the Essentials section).
2. Click the copy icon. It looks like:
   `InstrumentationKey=1234...;IngestionEndpoint=https://eastus-8.in.applicationinsights.azure.com/;LiveEndpoint=https://eastus.livediagnostics.monitor.azure.com/;ApplicationId=...`

Copy the whole string. Use the connection string, not just the Instrumentation Key.

---

## Part 3 — Fill in `.env`

```dotenv
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...;IngestionEndpoint=...;LiveEndpoint=...;ApplicationId=...
```
The value contains `=` and `;` characters. That is fine in `.env`; do not add quotes.

---

## Part 4 — Check data is arriving

1. Start the app:
   ```bash
   uv run uvicorn api.main:app --port 8000
   ```
   The terminal should print `Application Insights: sending traces`. If it prints `not configured, skipping`, the variable is missing or empty.
2. Open http://localhost:8000 and ask two or three questions.
3. Wait 1 to 3 minutes (data is sent in batches).
4. In the portal, on the Application Insights resource:

| Page | What you see |
|---|---|
| **Investigate → Transaction search** | Each request. Click a `POST /query` to see a timeline of its agent steps and how long each took. |
| **Investigate → Performance** | Average and slowest durations per endpoint. Click **Dependencies** for calls to Azure OpenAI and Postgres. |
| **Investigate → Failures** | Requests that returned errors, with the error message and stack trace |
| **Investigate → Live metrics** | Requests as they happen, with no delay |
| **Investigate → Application map** | The app and everything it calls, as a diagram (see below) |
| **Monitoring → Logs** | Query the raw data (see below) |

### Application map
**Investigate → Application map** draws the app as a node, with an arrow to everything it calls:

```
                       ┌──▶ graph-rag-001-resource.openai.azure.com          (Azure OpenAI)
  fundgraph-api ───────┼──▶ <content-safety>.cognitiveservices.azure.com     (Content Safety)
                       └──▶ <server>.postgres.database.azure.com | mf_data / agent_meta   (PostgreSQL)
```

- Each arrow shows the number of calls, the average duration and the failure rate. Click one for details, then **Investigate performance** or **Investigate failures**.
- `fundgraph-api` is the name the app reports itself under (`OTEL_SERVICE_NAME` in `api/telemetry.py`). Your laptop and the App Service instances both appear under this name; to tell them apart, see `cloud_RoleInstance` in **Transaction search**.
- Set the time range at the top (e.g. **Last 24 hours**) and click **Refresh**. The map only shows calls inside that range.

### A useful query
In **Monitoring → Logs**, close the welcome window, switch to **KQL mode**, paste this and click **Run**. It shows the average time of each agent step over the last day:
```kusto
dependencies
| where timestamp > ago(1d) and name startswith "agent."
| summarize avg_ms = avg(duration), calls = count() by name
| order by avg_ms desc
```

The slowest database queries, and which database they ran on:
```kusto
dependencies
| where timestamp > ago(1d) and type == "postgresql"
| summarize calls = count(), avg_ms = avg(duration), max_ms = max(duration) by target, sql = substring(data, 0, 120)
| order by avg_ms desc
```

### The same queries from the terminal
```bash
APPID=$(az resource show -g fundgraph-rg-1 -n fundgraph-insights \
    --resource-type microsoft.insights/components --query properties.AppId -o tsv)

# --offset sets how far back to look; without it, only the last hour is searched
az monitor app-insights query --apps $APPID --offset 24h \
    --analytics-query "dependencies | summarize calls=count(), avg_ms=avg(duration) by type, target | order by calls desc"
```
The first time, you may need `az extension add --name application-insights`.

---

## When the app runs on App Service

`infra/azure/create-app-service.sh` copies `APPLICATIONINSIGHTS_CONNECTION_STRING` from `.env` into the web app's settings, so the hosted app reports to the same resource. (If you set up the web app in the portal, add the same value under **Environment variables**.) The PostgreSQL tracing package is in `requirements.txt`, so it is installed on App Service too. You do not need to switch on anything in the App Service's own **Application Insights** page; doing so as well can record each request twice.

To tell laptop data and hosted data apart, look at **cloud_RoleInstance** in Transaction search (your Mac's name vs the App Service instance).

---

## Day-to-day

| Task | Where |
|---|---|
| Email alert when errors appear | **Monitoring → Alerts → + Create → Alert rule**, signal **Failed requests**, threshold e.g. greater than 5 in 5 minutes, action group with your email |
| Limit data volume and cost | Log Analytics workspace → **Settings → Usage and estimated costs → Daily cap** |
| Keep data for longer or shorter | Log Analytics workspace → **Settings → Usage and estimated costs → Data Retention** |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Terminal says `not configured, skipping` | Variable missing or empty | Part 3 |
| Nothing in Transaction search after 5 minutes | Wrong or partial connection string | Copy the full **Connection String** again |
| Requests appear but no `agent.*` steps | Old server process still running | Stop and restart uvicorn |
| **Application map says "No data available"** | You opened a different Application Insights resource (often the one AI Foundry created), or the time range is too short | Open the resource whose connection string is in `.env` (compare the `InstrumentationKey`), set **Last 24 hours**, click **Refresh** |
| Application map shows OpenAI and Content Safety, but no PostgreSQL | The running app was started before PostgreSQL tracing was added, or the package isn't installed | `uv sync` and restart locally; on Azure, run `infra/azure/deploy-app.sh` again |
| `az monitor app-insights query` finds nothing, although the portal shows data | Without `--offset`, only the last hour is searched | Add `--offset 24h` (or `72h`) |
