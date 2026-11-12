Azure Application Insights: setup for the Annual Report RAG project

## What Application Insights does in this project

A question to our app takes a few seconds. It passes through 7 pipeline steps, and calls Azure OpenAI and Azure AI Search several times.

On the laptop, we can watch the terminal. But once the app runs on Azure, **we can't see inside it**. If a question is slow, or fails, we wouldn't know where, or why.

**Application Insights is Azure's monitoring service. It records every request our app handles, every pipeline step, and every call to another service, with how long each one took.**

In simple words: it lets us see inside our running app.

```
our app (laptop or Azure)
      │  telemetry.py switches monitoring on
      │  sends: requests, pipeline steps, calls to Azure, errors
      ▼
Application Insights  (annual-report-insights-1)
      │  stores the data in a Log Analytics workspace
      ▼
Azure portal: Application map, Transaction search, Performance, Failures, Logs
```

**What we need from it:** one Application Insights resource, and its **connection string** in `.env` (and later, in the web app's settings).

---

## What it records, and what it doesn't

| Recorded                                                                                                                                                                  | Where it comes from                                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Every request: `POST /api/query`, `POST /api/review`, `GET /api/me` ... with its time and status code                                                               | `UI/server.py`                                    |
| Every pipeline step: `node.input_guardrail`, `node.retrieve`, `node.rerank`, `node.generate`, `node.output_guardrail`, `node.human_review`, `node.finalise` | `graph/build_graph.py`                            |
| Every call to **Azure OpenAI** (embeddings and gpt-5.1)                                                                                                             | `telemetry.py`, using `HTTPXClientInstrumentor` |
| Every call to **Azure AI Search**                                                                                                                                   | recorded automatically by Azure's own SDK           |
| Errors, with the line that failed                                                                                                                                         | automatically                                       |

| **Not** recorded             | Why                                                                                             |
| ---------------------------------- | ----------------------------------------------------------------------------------------------- |
| The user's question and the answer | our code never adds them to the records                                                         |
| Who is signed in                   | our code never adds it either                                                                   |
| Azure's `/health` visits         | `UI/server.py` skips them on purpose: Azure calls it every minute, so it would only add noise |

---

## Before you start

| Item           | Value                                                                          |
| -------------- | ------------------------------------------------------------------------------ |
| Resource group | `annual-rg-1`                                                                |
| Name           | `annual-report-insights-1`                                                   |
| Region         | **East US** (any region works: the app sends its data over the internet) |
| Portal path    | Azure portal → search **Application Insights** → **+ Create**    |

---

## Tab 1 · Basics

| Setting        | Value                              | Why                                                                  |
| -------------- | ---------------------------------- | -------------------------------------------------------------------- |
| Subscription   | your subscription                  |                                                                      |
| Resource group | **annual-rg-1**              | Keeps everything for this project in one place                       |
| Name           | **annual-report-insights-1** | Must be unique inside the resource group                             |
| Region         | **East US**                  | Where the monitoring data is stored                                  |
| Resource mode  | **Workspace-based**          | The only option now. The data is stored in a Log Analytics workspace |

**Workspace details:**

| Setting                 | Value                                                              | Why                                                                                                                                                                                            |
| ----------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Subscription            | your subscription                                                  |                                                                                                                                                                                                |
| Log Analytics Workspace | **DefaultWorkspace-...-EUS** (the default the portal offers) | A **Log Analytics workspace** is where the monitoring data is actually stored. Application Insights shows the screens; the workspace holds the data behind them. The default one is fine |

---

## Tab 2 · Tags (optional)

| Name    | Value             |
| ------- | ----------------- |
| project | annual-report-rag |

Tags let you filter costs by project later.

## Tab 3 · Review + create

Check the summary, then click **Create**. It takes under a minute.

---

## After it is created

### Step 1 · Copy the connection string into `.env`

Application Insights → **Overview** → **Connection String** (top right) → **Copy**

```dotenv
# --- Azure Application Insights (optional: monitoring is off without it) ---
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...;IngestionEndpoint=https://...;LiveEndpoint=https://...;ApplicationId=...
```

**A connection string is the address our app sends its monitoring data to.** It has four parts:

| Part                              | What it is                                                  |
| --------------------------------- | ----------------------------------------------------------- |
| `InstrumentationKey=...`        | the ID of our Application Insights resource                 |
| `IngestionEndpoint=https://...` | the address that **receives** our data                |
| `LiveEndpoint=https://...`      | the address for **Live metrics** (data as it happens) |
| `ApplicationId=...`             | another ID for the same resource                            |

It isn't as sensitive as a password: it can only **send** data in, not read data out. But it's still a good habit to keep it only in `.env`, and off the screen.

### Step 2 · Check the two packages are installed

Our `requirements.txt` already lists them:

| Package                                 | What it does                             |
| --------------------------------------- | ---------------------------------------- |
| `azure-monitor-opentelemetry`         | connects our app to Application Insights |
| `opentelemetry-instrumentation-httpx` | records the calls to Azure OpenAI        |

If you installed `requirements.txt` earlier, there's nothing to do.

### Step 3 · Test it on the laptop

From the project folder:

```bash
python UI/server.py
```

**Expected:** the first line printed is

```
Application Insights: sending traces as "annual-report-rag"
```

That line comes from `telemetry.py`. It means monitoring is **on**. (Without the connection string, it says `Application Insights: not configured, skipping`.)

Now open http://localhost:8000, and ask a question or two.

### Step 4 · See the data in the portal

Wait **2 to 3 minutes**. Monitoring data is sent in small batches, so it never appears instantly.

Then open Application Insights → **Application map**. You should see:

- a circle called **annual-report-rag**: our app
- an arrow to **annual-report-\***: Azure OpenAI and Azure AI Search
- an arrow to **settings...azure.com**: the monitoring library checking its own settings (ignore it)

That's the proof that our app is connected.

---

## Connecting the web app on Azure

On Azure, the app has no `.env`. So we give it the same connection string as an **app setting**, with one command. It's in the web app deployment guide, Step 16 (Switch monitoring on):

```bash
az webapp config appsettings set -g $RG -n $APP \
    --settings APPLICATIONINSIGHTS_CONNECTION_STRING="$AI_CONN" -o none
```

When the web app starts, `telemetry.py` reads this setting, and monitoring switches on. The web app log (`az webapp log tail`) then shows the same line:

```
Application Insights: sending traces as "annual-report-rag"
```

Here, we can notice: the laptop and the web app both send to the **same** Application Insights. So both appear under the same name, `annual-report-rag`.

---

## Where to look in the portal

| Screen                                            | What it shows                                                                                                   |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Application map**                         | our app, every service it calls, how many calls, and how long each took                                         |
| **Transaction search** → click one request | one question, step by step: the request, then each `node.*` step and each Azure call inside it, with its time |
| **Performance**                             | each request type (`POST /api/query`, ...): how many, the average time, and the slowest                       |
| **Failures**                                | requests and calls that failed, with the error                                                                  |
| **Live metrics**                            | requests as they happen. Handy right after starting the app                                                     |
| **Logs**                                    | write your own queries over all the data (see below)                                                            |

To see **which pipeline step is slowest**, open **Logs**, and run:

```kusto
dependencies
| where name startswith "node."
| summarize runs = count(), avg_ms = avg(duration), p95_ms = percentile(duration, 95) by name
| order by avg_ms desc
```

| Line                                                                                            | What it does                                                                                      |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `dependencies`                                                                                | the table where our pipeline steps and our calls to Azure are kept                                |
| `where name startswith "node."`                                                               | keep only our 7 pipeline steps                                                                    |
| `summarize runs = count(), avg_ms = avg(duration), p95_ms = percentile(duration, 95) by name` | for each step: how many times it ran, its average time, and the time 95% of runs were faster than |
| `order by avg_ms desc`                                                                        | slowest step first. It's usually `node.generate`: the gpt-5.1 call                              |

---

## Two things on the map that look wrong, but aren't

| You see                                             | What it is                                                                                                                                                                                                                                                  |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A red circle **169.254.169.254**, 100% failed | an address that only exists inside Azure virtual machines. The monitoring library checks it once at startup, to ask "am I on an Azure VM?" On the laptop or App Service, nothing answers, so the check fails. That's expected, and it isn't our app's error |
| **More than 1 instance** on our app's circle  | the number of different machines that sent data in the time range: the laptop, plus the Azure machine (a new one after each restart or redeploy). It's not the number of copies running at once                                                             |

---

## If something goes wrong

| What you see                                             | Cause                                                                  | Fix                                                                            |
| -------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `Application Insights: not configured, skipping`       | the connection string isn't in `.env` (or in the web app's settings) | Step 1, or the web app guide's Step 16                                         |
| `ModuleNotFoundError: No module named 'azure.monitor'` | the packages aren't installed                                          | `pip install -r requirements.txt`                                            |
| Nothing in the portal                                    | it's too soon                                                          | wait 3 minutes after asking a question, then click **Refresh**           |
| Still nothing after 5 minutes                            | the connection string was copied wrongly                               | copy it again from **Overview**, and restart the server                  |
| Requests appear, but no Azure OpenAI calls               | `HTTPXClientInstrumentor` ran after the openai client was created    | make sure `telemetry.setup()` is still the first thing `UI/server.py` runs |

---

## Cost

Application Insights charges for the **amount of data** stored in the Log Analytics workspace. The first **5 GB each month** is free.

Our app sends a few kilobytes per question, so a course project stays well inside the free amount.

---

## Who uses Application Insights

| Who                        | Uses it?     | How                                                                                        |
| -------------------------- | ------------ | ------------------------------------------------------------------------------------------ |
| `telemetry.py`           | Yes          | switches monitoring on, and creates the shared tracer                                      |
| `UI/server.py`           | Yes          | calls `telemetry.setup()` first, and records each request                                |
| `graph/build_graph.py`   | Yes          | records each pipeline step (`node.*`)                                                    |
| `run_query.py`           | **No** | it never calls `telemetry.setup()`, so questions asked from the terminal aren't recorded |
| The web app on App Service | Yes          | through the `APPLICATIONINSIGHTS_CONNECTION_STRING` app setting                          |
