# Host it on Azure: every command, end to end

This guide puts the web UI on the internet as an **Azure App Service** web app.
Only people in your Microsoft Entra ID (your organisation's sign-in) can open it.
**Application Insights** records how long each request and each pipeline step took,
and any errors.

You run every command below in a terminal, **from the project folder**.
Each one has a line saying what it does.

```
 your laptop                                    Azure (resource group rg-annual-report-rag)
 ───────────                                    ───────────────────────────────────────────
 ingestion (once) ───────────────────────────►  AI Search index    Blob Storage    Azure OpenAI
                                                       ▲                                ▲
 deploy-app.sh ── zip of the code ──►  App Service web app  (UI/server.py via startup.sh)
                                          │   sign-in in front of it (Microsoft Entra ID)
                                          └── timings + errors ──► Application Insights
```

The web app only **asks questions**. You still build the index on your laptop,
with `run_ingestion.py`, exactly as in the README.

---

## 0. Before you start

You need all of these done first (see the README):

- [ ] `bash azure_setup/provision.sh` has created the Azure resources
- [ ] `.env` is filled in
- [ ] `python run_ingestion.py` has built the index
- [ ] `python UI/server.py` works on your laptop

Then sign in to Azure:

```bash
az login                                         # opens a browser; sign in with your Azure account
az account show --query name -o tsv              # shows which subscription you are using
```

---

## 1. Pick names

These names are only kept in this terminal window. **If you open a new terminal,
run this block again with the same SUFFIX**. Write the number down.

```bash
RG=rg-annual-report-rag                          # the resource group provision.sh created
LOCATION=eastus2                                 # same region as provision.sh
SUFFIX=$RANDOM                                   # a random number; web app names must be unique worldwide
echo "SUFFIX=$SUFFIX  <- write this down"
APP=annualreport-web-$SUFFIX                     # the web app; its address becomes https://$APP.azurewebsites.net
PLAN=plan-annualreport                           # the App Service plan: the machine the app runs on
LOGS=log-annualreport                            # the Log Analytics workspace: where monitoring data is stored
INSIGHTS=appi-annualreport                       # Application Insights: the monitoring screens
```

---

## 2. Create Application Insights (monitoring)

```bash
az extension add --name application-insights --upgrade      # adds the "az monitor app-insights" commands

WORKSPACE_ID=$(az monitor log-analytics workspace create \
  --resource-group $RG --workspace-name $LOGS --location $LOCATION \
  --query id -o tsv)                             # creates the store for monitoring data; keeps its id

AI_CONN=$(az monitor app-insights component create \
  --resource-group $RG --app $INSIGHTS --location $LOCATION \
  --application-type web --workspace "$WORKSPACE_ID" \
  --query connectionString -o tsv)               # creates Application Insights; keeps its connection string

echo "$AI_CONN"                                  # should start with InstrumentationKey=...
```

To get monitoring on your laptop too, put the printed value in `.env` as
`APPLICATIONINSIGHTS_CONNECTION_STRING=...`. This is optional.

---

## 3. Create the web app

```bash
az appservice plan create \
  --resource-group $RG --name $PLAN --location $LOCATION \
  --is-linux --sku B1                            # a small Linux machine (Basic B1), billed per hour

az webapp create \
  --resource-group $RG --plan $PLAN --name $APP \
  --runtime "PYTHON:3.12"                        # an empty Python 3.12 web app
```

---

## 4. Tell the web app how to start, and give it its settings

```bash
az webapp config set \
  --resource-group $RG --name $APP \
  --startup-file "bash startup.sh" \
  --always-on true                               # start with startup.sh; never fall asleep when idle

az webapp config set \
  --resource-group $RG --name $APP \
  --generic-configurations '{"healthCheckPath": "/health"}'   # Azure pings /health and restarts the app if it stops answering
```

Copy every setting from your `.env` into the web app. `.env` itself is never uploaded:

```bash
SETTINGS=()                                      # an empty list
while IFS= read -r line || [ -n "$line" ]; do    # read .env one line at a time
  case "$line" in ''|\#*) continue ;; esac       # skip blank lines and comments
  SETTINGS+=("$line")                            # keep KEY=value lines
done < .env
az webapp config appsettings set \
  --resource-group $RG --name $APP \
  --settings "${SETTINGS[@]}" --output none      # every KEY=value becomes an app setting
```

Then add the settings that only matter on Azure. Run this **after** the block above,
so an empty value in `.env` cannot overwrite the connection string:

```bash
az webapp config appsettings set \
  --resource-group $RG --name $APP --output none \
  --settings \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true \
    WEBSITES_PORT=8000 \
    APPLICATIONINSIGHTS_CONNECTION_STRING="$AI_CONN"
# SCM_DO_BUILD_DURING_DEPLOYMENT  App Service runs "pip install -r requirements.txt" on every deploy
# WEBSITES_PORT                   the port UI/server.py listens on
# APPLICATIONINSIGHTS_...         switches monitoring on (telemetry.py)
```

Check them (the values are hidden, only the names are shown):

```bash
az webapp config appsettings list --resource-group $RG --name $APP --query "[].name" -o tsv
```

---

## 5. Upload the code

```bash
APP=$APP bash azure_setup/deploy-app.sh          # zip the code, upload it, App Service installs and starts it
```

The first deploy takes **5–10 minutes**, because App Service installs every
package. Watch it start:

```bash
az webapp log config --resource-group $RG --name $APP --docker-container-logging filesystem  # keep the app's output
az webapp log tail --resource-group $RG --name $APP     # live output; Ctrl+C to stop watching
```

When it is up, the output shows `Annual-report RAG UI on App Service (listening on 0.0.0.0:8000 ...)`.

Check it from your terminal **before** you switch on sign-in (step 6). After
step 6, this check is sent to the sign-in page instead:

```bash
curl https://$APP.azurewebsites.net/health      # expect: {"status": "ok"}
```

Open `https://$APP.azurewebsites.net` and ask a question. It should behave
exactly like it does on your laptop.

**Changed the code?** Run step 5 again. It is the only step you repeat.

---

## 6. Make people sign in

This step is done in the portal, because it also creates the app registration
in Entra ID for you.

1. Azure portal → your web app (`annualreport-web-<SUFFIX>`) → **Settings → Authentication**
2. **Add identity provider** → **Microsoft**
3. Keep the defaults:
   - App registration type: **Create new app registration**
   - Supported account types: **Current tenant – Single tenant** (only your organisation)
   - Restrict access: **Require authentication**
   - Unauthenticated requests: **HTTP 302 Found redirect**
4. **Add**

Open the site in a private window. Microsoft's sign-in page appears first. After
you sign in, the top right of the page shows **Signed in as <your name>** and a
**Sign out** link.

How the name gets there: App Service signs the user in **before** the request
reaches `UI/server.py`, and passes the user to it in request headers
(`X-MS-CLIENT-PRINCIPAL-NAME`, `X-MS-CLIENT-PRINCIPAL`). The page reads them
through `GET /api/me`. **Sign out** goes to App Service's own
`/.auth/logout`. On your laptop there is no sign-in, so the name and the link
are hidden.

App Service's own health check still reaches `/health` with sign-in switched on.

---

## 7. Look at the monitoring

Azure portal → **Application Insights** (`appi-annualreport`). Data appears
**1–3 minutes** after a question is asked.

| Where in the portal | What you see |
| --- | --- |
| **Performance** | each `POST /api/query` and `/api/review`: count, average and slowest times |
| **Transaction search** → click a request | the request, and under it each graph step (`graph.retrieve`, `graph.generate`, ...) with its duration |
| **Failures** | requests that failed, with the error and traceback |
| **Live metrics** | requests as they happen, for checking a deploy |

Which step is slowest? **Logs**, then run:

```kusto
dependencies
| where name startswith "graph."
| summarize runs = count(), avg_ms = avg(duration), p95_ms = percentile(duration, 95) by name
| order by avg_ms desc
```

Requests that needed a human review:

```kusto
requests
| where tostring(customDimensions.paused) == "true"
| project timestamp, name, duration, company = tostring(customDimensions.company)
```

Questions, answers and user names are **not** sent to Application Insights.
Only step names, timings, the company, the verdict and errors are sent.

---

## 8. Good to know

- **Keep it at one instance.** A paused review lives in the app's memory. With
  two instances, the reviewer's **Approve** could land on the other instance,
  which knows nothing about that review. A restart or a redeploy also forgets
  any unfinished review. (B1 runs one instance unless you scale it out.)
- **Keys** live in the web app's settings (portal → web app → **Settings →
  Environment variables**), never in the zip. After changing `.env`, run the
  settings block in step 4 again.
- **Something wrong?**

  | Symptom | Fix |
  | --- | --- |
  | Site shows *Application Error* | `az webapp log tail ...` shows why; usually a missing setting from step 4 |
  | `curl .../health` hangs, first deploy | still installing packages; wait and watch `log tail` |
  | 401 / 404 from Azure OpenAI or Search | a setting is wrong or missing; compare with your working `.env` |
  | Nothing in Application Insights | `APPLICATIONINSIGHTS_CONNECTION_STRING` missing from the app settings; wait 3 minutes after a question |
  | No name in the top right | sign-in (step 6) not switched on, or you are on `localhost` |

---

## 9. Cost and clean-up

| What | Roughly |
| --- | --- |
| App Service plan B1 | billed per hour while it exists, even when nobody uses it |
| Application Insights | the first 5 GB of data a month is free; this app sends very little |
| Everything from `provision.sh` | as in the README (AI Search Basic is billed per hour) |

Everything above sits in the same resource group, so one command deletes all of it:

```bash
bash azure_setup/teardown.sh                     # deletes the resource group: web app, plan, monitoring, and the rest
```

To keep the search index and delete **only** the hosting:

```bash
az webapp delete --resource-group $RG --name $APP                      # the web app
az appservice plan delete --resource-group $RG --name $PLAN --yes      # the machine it ran on (this is what costs money)
az monitor app-insights component delete --resource-group $RG --app $INSIGHTS     # monitoring screens
az monitor log-analytics workspace delete --resource-group $RG --workspace-name $LOGS --yes   # monitoring data
```

To remove the sign-in app registration as well: portal → **Microsoft Entra ID →
App registrations** → the one named after your web app → **Delete**.
`teardown.sh` does not delete it, because app registrations are not stored in resource groups.
