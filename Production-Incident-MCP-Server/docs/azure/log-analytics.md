
# Log Analytics workspace — where the logs live

## What it does in this project

- It **stores** every log line from the 4 apps (table `AppLogs_CL`) and every release (table `Deployments_CL`).
- It **searches** them with KQL when a tool asks.
- Every tool on our MCP server reads from here. Without it, there is nothing to search.

How data gets in and out:

```
data/app_logs.json ──► loader/send_logs.py ──► data collection rule (DCR) ──► AppLogs_CL
data/deployments.json ─┘                                                └──► Deployments_CL
                                                                                    │
AI app ──► MCP server tool ──► KQL search ─────────────────────────────────────────┘
```

The **data collection rule (DCR)** is the Azure rule that accepts uploaded rows, checks they have the right columns, and puts them in the right table. The portal creates it for you in step 3.

> Portal screens change from time to time. If a button has a slightly different name, look for the closest match.

---

## Step 1 — Create the workspace

1. In the Azure portal, search for **Log Analytics workspaces** → **Create**.
2. **Resource group:** create one, for example `rg-incident-logs`.
3. **Name:** for example `law-incident-logs`.
4. **Region:** pick one close to you, for example **Central India**. Use the **same region** for everything in this project.
5. **Review + Create** → **Create**.
6. Open the workspace → **Overview** → copy **Workspace ID** into `.env` as `LOG_ANALYTICS_WORKSPACE_ID`.

## Step 2 — Generate the logs first

The portal needs a sample file to learn the table's columns. So run the generator now, from the project folder:

```
uv run python -m log_generator.generate
```

This creates `data/sample_app_logs.json` and `data/sample_deployments.json`.

The samples are only used to tell Azure the columns. If more than 6 hours pass before you upload in step 6, run the generator again just before uploading. Azure changes the time of any log line more than 2 days older than the upload, and the loader refuses files that are too old.

## Step 3 — Create the `AppLogs` table (and the DCR)

1. In the workspace, open **Settings → Tables** → **Create** → **New custom log (Direct ingest)**. It may be called "DCR-based" instead.
2. **Table name:** `AppLogs`. Azure adds `_CL` at the end by itself, so the table becomes `AppLogs_CL`.
3. **Data collection rule:** click **Create a new data collection rule**, and name it, for example, `dcr-incident-logs`.
4. **Data collection endpoint:** leave it empty if the page allows it. Only if the page will not continue without one: create one first (search **Data collection endpoints** → **Create**, same region), then pick it here.
5. **Next** → **Browse for files** → upload `data/sample_app_logs.json`.
6. Check the preview shows 8 columns: `TimeGenerated`, `Service`, `Level`, `Message`, `RequestId`, `Endpoint`, `StatusCode`, `DurationMs`. `TimeGenerated` must be there; our generator already adds it, so no extra "transformation" is needed.
7. **Next** → **Create**.

## Step 4 — Create the `Deployments` table (same DCR)

1. **Tables** → **Create** → **New custom log (Direct ingest)** again.
2. **Table name:** `Deployments` (becomes `Deployments_CL`).
3. **Data collection rule:** pick the **same** rule, `dcr-incident-logs`.
4. Upload `data/sample_deployments.json`. Check the 5 columns: `TimeGenerated`, `Service`, `Version`, `DeployedBy`, `ChangeNote`.
5. **Next** → **Create**.

If the page did not let you pick the existing rule and made a second one, that's fine: copy that second rule's Immutable Id into `DEPLOYMENTS_DCR_IMMUTABLE_ID` in step 5.

## Step 5 — Copy the DCR values into `.env`

1. Search for **Data collection rules** → open `dcr-incident-logs`.
2. **Overview** → copy **Immutable Id** (starts with `dcr-`) into `DCR_IMMUTABLE_ID`.
3. **JSON View** (top right of the Overview page). Find:
   - `"logsIngestion"` → its `"endpoint"` value goes into `LOGS_INGESTION_ENDPOINT`. If you used a data collection endpoint in step 3, use that endpoint's **Logs Ingestion** URL instead (on the endpoint's Overview page).
   - `"streamDeclarations"` → the two names, usually `Custom-AppLogs_CL` and `Custom-Deployments_CL`. If yours differ, change `APP_LOGS_STREAM` and `DEPLOYMENTS_STREAM`.

## Step 6 — Upload, then check

1. Give the Entra ID app permission to upload: see [entra-id.md](entra-id.md).
2. Upload: `uv run python -m loader.send_logs`
3. Wait 5 to 15 minutes. The first upload into a new table can take a little longer.
4. In the workspace, open **Logs** and run each search:

```kusto
AppLogs_CL | count
```

```kusto
Deployments_CL | order by TimeGenerated asc
```

The first should be about 20,000 rows; the second, 6 rows.

## Cost

Log Analytics charges for the data you upload. Our upload is under 10 MB, a tiny amount. Searching is free on the standard plan.
